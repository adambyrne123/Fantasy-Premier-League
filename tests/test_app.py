"""Smoke test for the Streamlit front end.

Runs the real `app.py` through Streamlit's AppTest harness with the API swapped
for synthetic data. It will not catch a bad layout, but it does catch the thing
that actually breaks a Streamlit app in practice, which is an exception thrown
somewhere down a tab the developer did not click on before shipping.
"""

from __future__ import annotations

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
    for expected in ["Squad", "Players", "Fixtures", "Transfers", "Chips"]:
        assert expected in labels


def test_squad_summary_is_shown(app):
    at = app.run()
    assert not at.exception
    assert any("Squad cost" in m.label for m in at.metric)


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
