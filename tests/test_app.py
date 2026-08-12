"""Smoke test for the Streamlit front end.

Runs the real `app.py` through Streamlit's AppTest harness with the API swapped
for synthetic data. It will not catch a bad layout, but it does catch the thing
that actually breaks a Streamlit app in practice, which is an exception thrown
somewhere down a tab the developer did not click on before shipping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fpl_manager import api
from fpl_manager import projections as proj_module

APP = Path(__file__).resolve().parent.parent / "app.py"

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from .conftest import FakeApi, make_prior  # noqa: E402


def _app(monkeypatch, tmp_path, played: int):
    """The app wired to synthetic data, with no network and no disk cache."""
    fake = FakeApi(played=played)
    monkeypatch.setattr(api.FplApi, "bootstrap", lambda self: fake.bootstrap())
    monkeypatch.setattr(api.FplApi, "fixtures", lambda self: fake.fixtures())
    monkeypatch.setattr(api.FplApi, "_get", lambda self, *a, **kw: {})

    from fpl_manager.data import Season

    prior_path = tmp_path / "prior_season.parquet"
    make_prior(Season(fake)).to_parquet(prior_path)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", prior_path)

    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    return AppTest.from_file(str(APP), default_timeout=120)


@pytest.fixture
def app(monkeypatch, tmp_path):
    return _app(monkeypatch, tmp_path, played=0)


@pytest.fixture
def midseason_app(monkeypatch, tmp_path):
    """Twelve gameweeks in, which is the only way to reach anything that needs
    transfer activity. Pre-season those counters are all zero."""
    return _app(monkeypatch, tmp_path, played=12)


def test_app_starts_without_exception(app):
    at = app.run()
    assert not at.exception


def test_every_tab_renders(app):
    at = app.run()
    assert not at.exception
    labels = [tab.label for tab in at.tabs] if at.tabs else []
    for expected in ["Squad", "Players", "ROI", "Fixtures", "Transfers", "Chips"]:
        assert expected in labels


def test_squad_summary_is_shown(app):
    at = app.run()
    assert not at.exception
    assert any("Squad cost" in m.label for m in at.metric)


def _status_bar(at):
    """The rendered strip, not the stylesheet that also mentions the class."""
    return next(m.value for m in at.markdown if '<div class="statusbar">' in m.value)


def _pool(at):
    """The player pool table, found by the fixture run only it carries."""
    return next(
        d.value for d in at.dataframe if any(str(c).startswith("GW") for c in d.value.columns)
    )


def test_the_status_bar_carries_the_deadline_and_the_data_age(midseason_app):
    """Neither was on screen anywhere before, and they are what say whether the
    projections are still worth acting on."""
    at = midseason_app.run()
    assert not at.exception
    bar = _status_bar(at)
    assert "Deadline" in bar
    assert "FPL data" in bar


def test_the_status_bar_survives_a_season_with_no_deadline_left(app):
    """`next_deadline` is None once the last gameweek is done, and the strip has
    to render anyway rather than formatting None into a date."""
    at = app.run()
    assert not at.exception
    assert "Gameweek" in _status_bar(at)


def test_the_pool_shows_each_player_s_fixture_run(app):
    """The run has to sit beside the numbers, or comparing two players means
    holding the Fixtures tab in your head."""
    at = app.run()
    assert not at.exception
    pool = _pool(at)
    horizon = at.sidebar.slider[0].value
    assert sum(str(c).startswith("GW") for c in pool.columns) == horizon


def test_home_and_away_are_distinguished_by_case(app):
    """Upper case at home, lower case away, which is what saves the table a
    legend. A run that is all one case means the encoding has been lost."""
    at = app.run()
    gw_cols = [c for c in _pool(at).columns if str(c).startswith("GW")]
    runs = _pool(at)[gw_cols].to_numpy().ravel()
    played = [str(v) for v in runs if str(v) not in {"—", "nan"}]
    assert any(v.isupper() for v in played)
    assert any(v.islower() for v in played)


@pytest.mark.parametrize("view", ["Model terms", "Form and attack", "Availability"])
def test_every_stat_view_renders(midseason_app, view):
    """Each view names a different set of columns, and one that does not exist
    on the frame is an exception rather than a missing column."""
    at = midseason_app.run()
    at.segmented_control[0].set_value(view).run()
    assert not at.exception
    assert any(str(c).startswith("GW") for c in _pool(at).columns), "the run should survive"


def test_filtering_to_nothing_leaves_the_other_tabs_alone(app):
    """An empty result is an ordinary thing to do. Reaching st.stop() here
    would blank the four tabs below it."""
    at = app.run()
    at.text_input[0].set_value("no such player anywhere").run()
    assert not at.exception
    assert any("Nothing matches" in info.value for info in at.info)
    assert any("Squad cost" in m.label for m in at.metric), "the Squad tab should still render"


def test_the_pool_says_how_much_it_is_hiding(app):
    """It used to cut silently to forty rows with nothing on screen saying so."""
    at = app.run()
    assert any("Showing" in c.value and "players" in c.value for c in at.caption)


def _select_row(at, row=0):
    """Pick a row in the pool table.

    A dataframe selection is a click on a canvas, which AppTest cannot do, so
    this writes the widget state Streamlit would have written. It has to be a
    fresh dict rather than a mutation, since widget state is read only.
    """
    at.session_state["pool"] = {"selection": {"rows": [row], "columns": []}}
    return at.run()


def test_selecting_a_player_shows_the_working_behind_his_projection(midseason_app):
    """A total says nothing about which of the three terms produced it, which
    is the whole reason the drill-down exists."""
    at = _select_row(midseason_app.run())
    assert not at.exception
    labels = [m.label for m in at.get("dialog")[0].metric]
    assert "Points per 90" in labels
    assert "Expected minutes" in labels
    assert any(label.startswith("Projected") for label in labels)


def test_the_drill_down_says_so_when_there_is_no_current_sample(app):
    """`current_p90` is NaN before a ball is kicked, and formatting that into a
    rate would print nan at the reader instead of saying there is no sample."""
    at = _select_row(app.run())
    assert not at.exception
    assert any("no sample yet" in c.value for c in at.get("dialog")[0].caption)


def test_clearing_the_selection_allows_the_drill_down_to_reopen(midseason_app):
    """The guard that stops the dialog reopening on every rerun has to reset,
    or a player can only ever be inspected once."""
    at = _select_row(midseason_app.run())
    assert at.session_state["inspected"] is not None
    at.session_state["pool"] = {"selection": {"rows": [], "columns": []}}
    at.run()
    assert at.session_state["inspected"] is None


def _chart_specs(at):
    """Every Vega-Lite spec on the page, parsed.

    `.proto.spec` rather than `.spec`, since the element's own accessor reads
    session state and a chart with no key has none.
    """
    specs = [json.loads(c.proto.spec) for c in at.get("vega_lite_chart")]
    assert specs, "no charts found, so an assertion about them proves nothing"
    return specs


def _has_scale_binding(spec) -> bool:
    """Whether a spec binds a selection to the scales, which is what claims
    the wheel. `.interactive()` puts it on the top level of a layered chart."""
    if isinstance(spec, dict):
        if any(p.get("bind") == "scales" for p in spec.get("params", []) if isinstance(p, dict)):
            return True
        return any(_has_scale_binding(v) for v in spec.values())
    if isinstance(spec, list):
        return any(_has_scale_binding(v) for v in spec)
    return False


def test_charts_do_not_claim_the_wheel_until_asked(app):
    """A chart left `.interactive()` zooms when you scroll the page past it, so
    you scroll down and arrive having quietly rescaled it."""
    at = app.run()
    assert not at.exception
    assert not any(_has_scale_binding(spec) for spec in _chart_specs(at))


def test_turning_zoom_on_gives_the_chart_back_its_wheel(app):
    at = app.run()
    next(t for t in at.toggle if t.key == "pool_zoom").set_value(True).run()
    assert not at.exception
    assert any(_has_scale_binding(spec) for spec in _chart_specs(at))


def test_changing_horizon_reruns_cleanly(app):
    at = app.run()
    at.sidebar.slider[0].set_value(10).run()
    assert not at.exception


def test_lowering_budget_is_handled(app):
    """An infeasible budget should surface as an error, not a traceback."""
    at = app.run()
    budget = at.sidebar.number_input[0]
    budget.set_value(80.0).run()
    assert not at.exception


def test_player_pool_chart_is_built(app):
    """The scatter is the whole point of the Players tab.

    A chart with a bad spec produces no element and throws nothing, so the app
    still looks healthy while the tab is empty. Assert the element exists.
    """
    at = app.run()
    assert not at.exception
    assert len(at.get("vega_lite_chart")) >= 1


def test_choosing_an_entry_id_reruns_cleanly(app):
    """The entry path is the one people will use once a deadline has passed."""
    at = app.run()
    at.sidebar.radio[0].set_value("FPL entry id").run()
    assert not at.exception


def test_roi_says_nothing_before_any_points_are_scored(app):
    at = app.run()
    assert not at.exception
    assert any("every return is zero" in info.value for info in at.info)


def test_roi_ranks_players_once_points_exist(midseason_app):
    at = midseason_app.run()
    assert not at.exception
    assert any("Best return" in m.label for m in at.metric)
    assert not any("every return is zero" in info.value for info in at.info)


def test_price_pressure_says_nothing_before_the_season(app):
    at = app.run()
    assert not at.exception
    assert any("nothing to read" in info.value for info in at.info)


def test_price_pressure_renders_once_transfers_exist(midseason_app):
    """The populated branch is unreachable pre-season, so it needs its own run."""
    at = midseason_app.run()
    assert not at.exception
    assert not any("nothing to read" in info.value for info in at.info)
    assert any("Closest to rising" in caption.value for caption in at.caption)


def test_a_download_is_offered(app):
    at = app.run()
    assert not at.exception
    assert at.get("download_button"), "the Squad tab should offer a download"


def test_the_download_goes_through_the_shared_payload_builder():
    """AppTest cannot see a download's bytes, so this checks the wiring instead.

    That the payload carries purchase prices is asserted properly against
    `squad_payload` in test_squad.py. What can still silently regress here is
    the app going back to assembling its own JSON, which is how it came to be
    writing ids and nothing else.
    """
    assert "squad_payload(" in APP.read_text()


def test_the_app_stages_no_uploads_on_the_server():
    """Several people use the deploy at once, so a shared upload path collides."""
    assert "uploaded_squad.json" not in APP.read_text()


@pytest.fixture
def app_with_squad(monkeypatch, tmp_path):
    """Mid-season, with a squad loaded through the entry id path.

    Nothing else in this file ever loads a squad, so without this the whole
    populated half of the Transfers and Chips tabs never runs.
    """
    at = _app(monkeypatch, tmp_path, played=12)

    from fpl_manager import squad as squad_module
    from fpl_manager.data import Season
    from fpl_manager.optimiser import build_squad
    from fpl_manager.projections import project

    from .conftest import FakeApi, make_prior

    # a real 15, because the first fifteen ids are all one club and there is no
    # legal transfer plan out of a squad that breaks the club cap
    fake_season = Season(FakeApi(played=12))
    built, _ = project(fake_season, horizon=6, prior=make_prior(fake_season))
    owned = [int(i) for i in build_squad(built).squad.index]
    monkeypatch.setattr(
        squad_module,
        "load_from_entry",
        lambda season, entry_id, gameweek=None: squad_module.MySquad(
            player_ids=owned, bank_tenths=10, free_transfers=2, entry_id=entry_id
        ),
    )
    return at


def _load_the_squad(at):
    at.sidebar.radio[0].set_value("FPL entry id").run()
    at.sidebar.number_input[1].set_value(123).run()
    return at


def test_a_loaded_squad_fills_the_transfer_tab(app_with_squad):
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("Plan across several gameweeks" in t.label for t in at.toggle)


def test_missing_selling_prices_are_warned_about(app_with_squad):
    """An entry publishes no purchase prices, so the money has to be flagged."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("valued at today's price" in w.value for w in at.warning)


def test_an_illegal_squad_is_an_error_not_a_traceback(monkeypatch, tmp_path):
    """Anyone can upload a hand-edited file, and fifteen from one club has no
    legal plan out of it. That has to read as a message, not a stack trace."""
    at = _app(monkeypatch, tmp_path, played=12)

    from fpl_manager import squad as squad_module
    from fpl_manager.data import Season

    from .conftest import FakeApi

    all_one_club = [int(i) for i in Season(FakeApi(played=12)).players.index[:15]]
    monkeypatch.setattr(
        squad_module,
        "load_from_entry",
        lambda season, entry_id, gameweek=None: squad_module.MySquad(player_ids=all_one_club),
    )

    at = _load_the_squad(at.run())
    assert not at.exception
    assert any("no more than" in e.value for e in at.error)


def test_the_multi_week_planner_runs(app_with_squad):
    at = _load_the_squad(app_with_squad.run())
    planner = next(t for t in at.toggle if "Plan across several gameweeks" in t.label)
    planner.set_value(True).run()
    assert not at.exception
    assert any("Projected over" in m.label for m in at.metric)


def test_app_holds_no_model_logic():
    """The front end is a view. If it starts deciding things, split it out."""
    text = APP.read_text()
    for banned in ["LpProblem", "points_per_90 *", "def project("]:
        assert banned not in text, f"model logic leaked into app.py: {banned}"
