"""Smoke test for the Streamlit front end.

Runs the real `app.py` through Streamlit's AppTest harness with the API swapped
for synthetic data. It will not catch a bad layout, but it does catch the thing
that actually breaks a Streamlit app in practice, which is an exception thrown
somewhere down a tab the developer did not click on before shipping.
"""

from __future__ import annotations

import json
import re
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
    # the Leagues tab reads these three, and leaving them on the catch-all
    # below would hand it an empty payload and quietly test nothing
    monkeypatch.setattr(api.FplApi, "entry", lambda self, e: fake.entry(e))
    monkeypatch.setattr(api.FplApi, "entry_history", lambda self, e: fake.entry_history(e))
    monkeypatch.setattr(
        api.FplApi, "entry_picks", lambda self, e, gw, ttl=None: fake.entry_picks(e, gw, ttl)
    )
    monkeypatch.setattr(
        api.FplApi, "league_standings", lambda self, lid, page=1: fake.league_standings(lid, page)
    )
    monkeypatch.setattr(api.FplApi, "_get", lambda self, *a, **kw: {})

    from fpl_manager.data import Season

    prior_path = tmp_path / "prior_season.parquet"
    make_prior(Season(fake)).to_parquet(prior_path)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", prior_path)

    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    # Every run here solves the full squad MILP, and there are enough of these
    # now that a loaded machine can push one past a two minute budget. AppTest
    # reports that as a bare RuntimeError, which reads like a broken app rather
    # than a slow one, so the budget is generous on purpose. It costs nothing
    # when the run is healthy.
    return AppTest.from_file(str(APP), default_timeout=400)


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
    for expected in [
        "My squad",
        "Wildcard",
        "Players",
        "Captain",
        "ROI",
        "Fixtures",
        "Planner",
        "Chips",
        "Leagues",
        "Live",
    ]:
        assert expected in labels


def test_every_tab_explains_what_its_numbers_mean(app):
    """One "How to read this" per tab, since the numbers are not self evident.

    `xpts_next` and `xpts_total` are both called projected points and are not
    the same thing, and the headline on the Squad tab counts the eleven while
    the shirts under it count one gameweek. None of that is guessable.
    """
    at = app.run()
    assert not at.exception

    # an expander with an icon comes back through `status`, not `expander`
    notes = [e for e in at.status if e.label == "How to read this"]
    assert len(notes) == len(at.tabs), "every tab should carry one"


def test_the_pool_columns_say_what_they_are():
    """A column header on its own does not distinguish points per million from
    points per 90, and the table has both."""
    import re

    source = APP.read_text(encoding="utf-8")
    glossary = re.search(r"^GLOSSARY = \{.*?^\}", source, re.S | re.M)
    assert glossary, "the one place a number is defined"

    for column in (
        "xpts_next",
        "xpts_total",
        "value",
        "points_per_90",
        "minutes_share",
        "haul_chance",
        "return_chance",
        "xpts_gw",
        "start_chance",
        "credibility",
    ):
        assert f'"{column}"' in glossary.group(0), f"{column} is not self explanatory"


def _states(at):
    """Text of every empty-state card. They are markdown, not st.info."""
    return [m.value for m in at.markdown if 'class="state"' in m.value]


def _leagues_entry(at):
    return next(n for n in at.number_input if n.key == "league_entry")


def test_the_leagues_tab_asks_before_it_fetches(app):
    """Nothing should be requested for a manager nobody has named."""
    at = app.run()
    assert not at.exception
    assert any("Enter a manager id" in info.value for info in at.info)


def _field_toggle(at):
    return next(t for t in at.sidebar.toggle if "top managers" in t.label)


def test_the_field_waits_to_be_asked(app):
    """A hundred requests should never fire because somebody opened a tab."""
    at = app.run()
    assert not at.exception
    assert any("Turn on" in info.value for info in at.info)


def test_the_field_says_why_it_is_empty_before_a_gameweek_is_scored(app):
    """The state it ships in. Picks are not published until a deadline passes,
    so switching it on in August has to explain itself rather than look broken."""
    at = app.run()
    _field_toggle(at).set_value(True).run()
    assert not at.exception
    assert any("No squads to read yet" in state for state in _states(at))


def test_the_template_fills_in_once_the_field_is_read(midseason_app):
    at = midseason_app.run()
    _field_toggle(at).set_value(True).run()
    assert not at.exception

    assert any("of the top" in c.value and "as they lined up" in c.value for c in at.caption)
    assert any("Start %" in m.value for m in at.markdown), "no template pitch"
    assert any("did with the armband" in c.value for c in at.caption), "captain tab missed it"


def test_the_divergence_table_carries_both_ownerships(midseason_app):
    """The whole point of reading the top of the table is the gap between what
    they own and what everyone owns, so both have to be on the same row."""
    at = midseason_app.run()
    _field_toggle(at).set_value(True).run()
    assert not at.exception

    tables = [d.value for d in at.dataframe if "elite_ownership" in d.value.columns]
    assert tables, "no divergence table"
    assert "ownership" in tables[0].columns
    assert "elite_gap" in tables[0].columns


def test_club_form_waits_for_a_match_to_be_played(app):
    at = app.run()
    assert not at.exception
    assert any("Nothing played yet" in state for state in _states(at))


def test_club_form_lists_every_club_once_there_are_results(midseason_app):
    at = midseason_app.run()
    assert not at.exception
    tables = [d.value for d in at.dataframe if "scored_per_game" in d.value.columns]
    assert tables, "no club form table"
    assert len(tables[0]) == 20
    assert tables[0]["scored_per_game"].is_monotonic_decreasing


def test_where_they_sit_comes_before_the_league_table(midseason_app):
    """Their standing across every league is the first thing wanted, and last
    season's history is the last. Order asserted because it is the whole point
    of the layout and nothing else would catch it moving."""
    at = midseason_app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception

    captions = [c.value for c in at.caption]
    sits = next(i for i, c in enumerate(captions) if "Where they sit" in c)
    table = next(i for i, c in enumerate(captions) if "Their classic leagues" in c)
    previous = next(i for i, c in enumerate(captions) if "Previous seasons" in c)
    assert sits < table < previous


def test_every_league_they_are_in_carries_their_rank(midseason_app):
    at = midseason_app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    tables = [d.value for d in at.dataframe if "kind" in d.value.columns]
    assert tables, "no standing-per-league table"
    assert {"name", "rank"} <= set(tables[0].columns)


def _month_box(at):
    return next(c for c in at.checkbox if c.key == "league_month")


def test_the_month_waits_to_be_asked(midseason_app):
    """One request per manager in the league, so it must not fire because
    somebody opened a tab."""
    at = midseason_app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    tables = [d.value for d in at.dataframe if "month_points" in d.value.columns]
    assert not tables, "the month should be off until asked for"


def test_the_month_adds_points_and_a_rank_within_the_league(midseason_app, monkeypatch):
    """The month is pinned rather than read off the clock. Which gameweeks fall
    in a month is `Season.gameweeks_in_month`'s job and is tested there; this is
    about the table getting the columns."""
    from fpl_manager.data import Season

    monkeypatch.setattr(Season, "gameweeks_in_month", lambda self, when=None: [1, 2])

    at = midseason_app.run()
    _leagues_entry(at).set_value(1).run()
    _month_box(at).set_value(True).run()
    assert not at.exception

    table = next(d.value for d in at.dataframe if "month_points" in d.value.columns)
    assert table["month_points"].notna().any(), "somebody should have scored"
    assert table["month_rank"].min() == 1, "a rank inside the league starts at one"
    # the month is its own ordering, not a copy of the season one
    by_month = table.sort_values("month_rank")["entry_id"].tolist()
    assert by_month == table.sort_values("month_points", ascending=False)["entry_id"].tolist()


def test_a_manager_id_shows_their_record(midseason_app):
    at = midseason_app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Overall points" in labels
    assert "Overall rank" in labels


def test_the_leagues_tab_follows_the_loaded_squad(app_with_squad):
    """One id, entered once. A widget's default only applies the first time it
    renders, so this box used to keep whoever it saw first and go on reporting
    on them after a different squad was loaded."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert _leagues_entry(at).value == 123, "should be the id loaded in the sidebar"


def test_the_leagues_tab_follows_a_change_of_squad(app_with_squad):
    """The regression this exists for. Loading a second id used to leave this
    tab reporting on the first, because a widget's `value` is only its default
    on the render that creates it."""
    at = _load_the_squad(app_with_squad.run())
    assert _leagues_entry(at).value == 123

    next(t for t in at.sidebar.text_input if t.key == "sidebar_entry").set_value("456").run()
    assert not at.exception
    assert _leagues_entry(at).value == 456, "should follow the squad now loaded"


def test_looking_up_another_manager_survives_a_rerun(app_with_squad):
    """The tab is for any public manager, not only for you, so an id typed in
    here has to stick rather than snap back to the loaded squad on the next
    rerun."""
    at = _load_the_squad(app_with_squad.run())
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    assert _leagues_entry(at).value == 1

    # a rerun that does not change the loaded squad must leave the override be
    next(s for s in at.sidebar.slider if s.label == "Gameweeks to project over").set_value(4).run()
    assert not at.exception
    assert _leagues_entry(at).value == 1


def test_an_unranked_manager_reads_as_unranked_not_as_first(app):
    """Pre-season the API sends a null rank, and formatting that as a number
    would put every manager top of the world."""
    at = app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    rank = next(m for m in at.metric if m.label == "Overall rank")
    assert rank.value == "Not ranked yet"


def test_an_empty_league_table_says_so(app):
    """The real endpoint returns no rows until a gameweek is scored."""
    at = app.run()
    _leagues_entry(at).set_value(1).run()
    assert not at.exception
    assert any("has no table yet" in card for card in _states(at))


def test_a_bad_manager_id_is_an_error_not_a_traceback(app, monkeypatch):
    at = app.run()
    from fpl_manager import leagues as leagues_module

    def boom(*args, **kwargs):
        raise RuntimeError("Could not read the manager for id 42. Check the id is right.")

    monkeypatch.setattr(leagues_module, "load_manager", boom)
    _leagues_entry(at).set_value(42).run()
    assert not at.exception
    assert any("Check the id is right" in e.value for e in at.error)
    assert any("Squad cost" in m.label for m in at.metric), "other tabs should survive"


def test_squad_summary_is_shown(app):
    at = app.run()
    assert not at.exception
    assert any("Squad cost" in m.label for m in at.metric)


def _status_bar(at):
    """The rendered strip, not the stylesheet that also mentions the class."""
    return next(m.value for m in at.markdown if '<div class="statusbar">' in m.value)


def _pool(at):
    """The player pool table on the Players tab.

    Keyed rather than guessed at. It used to be found by the fixture run only
    it carried, which quietly depended on no earlier tab rendering one. The
    Captain tab renders a run too, and sits after Players so the old scan
    still lands, but a tab reorder should not silently change which table
    every assertion below is talking about. The scan stays as a fallback for
    a Streamlit version that does not expose the key.
    """
    for frame in at.dataframe:
        if getattr(frame, "key", None) == "pool":
            return frame.value
    return next(
        d.value for d in at.dataframe if any(str(c).startswith("GW") for c in d.value.columns)
    )


def _pitch(at):
    return next(m.value for m in at.markdown if 'class="pitch' in m.value)


def test_every_card_carries_both_projections_labelled(app):
    """One unlabelled number meant the shirts never added up to the headline
    above them, and nothing on screen said why. Both spans, both named."""
    at = app.run()
    assert not at.exception
    pitch = _pitch(at)

    assert pitch.count('<span class="box">') == 30, "two spans on each of fifteen cards"
    assert pitch.count('class="v next"') == 15
    assert pitch.count('class="v span"') == 15
    assert pitch.count('<span class="k">GW1</span>') == 15, "the next gameweek names itself"
    assert pitch.count('<span class="k">6 GW</span>') == 15, "so does the horizon"


def test_the_card_labels_follow_the_horizon(app):
    """The right hand figure is whatever the slider says, so a label naming a
    fixed span would go quietly wrong the moment anyone moved it."""
    at = app.run()
    assert '<span class="k">6 GW</span>' in _pitch(at)

    # by label, not by index: this page has six sliders and adding a seventh
    # should not silently point this test at the wrong one
    horizon = next(s for s in at.slider if s.label == "Gameweeks to project over")
    horizon.set_value(3).run()
    assert not at.exception
    pitch = _pitch(at)
    assert '<span class="k">3 GW</span>' in pitch
    assert "6 GW" not in pitch


def test_the_squad_headline_is_the_cards_added_up(app):
    """The one number a reader will try to reconcile by hand. It is the eleven
    horizon figures plus the captain's next gameweek, not the captain doubled,
    and the caption under the pitch says exactly that."""
    import re

    at = app.run()
    pitch = _pitch(at)
    xi = pitch.split('class="bench-strip')[0]

    spans = [float(v) for v in re.findall(r'class="v span">([\d.]+)<', xi)]
    captain_card = next(c for c in xi.split('<div class="shirt') if 'class="badge c"' in c)
    captain_next = float(re.search(r'class="v next">([\d.]+)<', captain_card).group(1))

    assert len(spans) == 11
    headline = next(m for m in at.metric if "Projected over" in m.label)
    assert abs(sum(spans) + captain_next - float(headline.value.split()[0])) < 1.0


def test_every_card_in_the_squad_carries_a_face(app):
    """Fifteen identical white rectangles tell you nothing about who is in the
    side, which is the one thing a pitch view exists to say. The bench is part
    of that: it renders in the same block and is where the players you least
    recognise sit."""
    at = app.run()
    assert not at.exception
    pitch = _pitch(at)
    assert pitch.count('<span class="mug"') == 15, "eleven starters and four on the bench"
    assert "photos/players/110x140/p500" in pitch, "built from the player's code, not his id"


def test_the_kit_is_the_fallback_and_not_a_backdrop(app):
    """Roughly half the cheapest players have no photograph and the CDN answers
    403, and cheap players are exactly what the optimiser puts on a bench.

    The kit has to be `object` fallback content, which renders only when the
    photograph fails. Putting it behind the face instead looks right until you
    notice every FPL photograph is a cut-out with a transparent background, so
    the kit shows through around every player rather than only the missing ones.
    """
    at = app.run()
    pitch = _pitch(at)
    assert pitch.count("<object ") == 15, "one per card, starters and bench"
    assert pitch.count("dist/img/shirts/standard/shirt_") == 15, "one kit per card"
    for card in pitch.split('<span class="mug"')[1:]:
        mug = card.split("</span>")[0]
        assert "background-image" not in mug, "a kit behind the face bleeds through it"
        assert mug.index("photos/players") < mug.index("shirts/standard"), (
            "the photograph is the object, the kit is what it falls back to"
        )


def test_every_card_carries_its_club_crest(app):
    """FPL's photographs go stale after a transfer, so a player can appear in
    the kit of the club he has just left. The crest comes off the live team
    code, so it stays right when the photograph does not."""
    at = app.run()
    pitch = _pitch(at)
    assert pitch.count('class="crest"') == 15
    assert "badges/70/t" in pitch


def test_no_pitch_image_relies_on_a_script_handler(app):
    """Streamlit strips every on* attribute from the HTML it renders, so an
    onerror fallback here is removed before it can run and fails silently.
    That is exactly how this shipped broken once."""
    at = app.run()
    assert "onerror" not in _pitch(at)


def test_the_keeper_wears_a_different_kit_from_the_outfielders(app):
    """Keepers have their own kit image, the `_1` variant. Getting this wrong
    puts an outfield shirt on the one player guaranteed to be on the pitch."""
    at = app.run()
    pitch = _pitch(at)
    assert "_1-66.png" in pitch, "the keeper should have the keeper kit"
    assert re.search(r"shirt_\d+-66\.png", pitch), "outfielders should not"


def test_the_drill_down_shows_the_player_s_face(midseason_app):
    at = _select_row(midseason_app.run())
    assert not at.exception
    head = next(m.value for m in at.markdown if 'class="pd-head"' in m.value)
    assert "photos/players" in head
    assert "shirts/standard" in head, "the kit has to back the face here too"


def test_the_pool_carries_a_club_badge(app):
    at = app.run()
    assert not at.exception
    pool = _pool(at)
    assert "badge" in pool.columns
    assert pool["badge"].str.contains("badges/70/t").all()


def test_club_tables_carry_badges_too(app):
    """The fixture ticker and the swing tables are club-keyed, so the badge
    reads faster than the three letter short name does."""
    at = app.run()
    assert not at.exception
    badged = [d.value for d in at.dataframe if "badge" in d.value.columns]
    assert len(badged) >= 3, "the pool, the ticker and at least one swing table"
    for frame in badged:
        assert frame["badge"].dropna().str.contains("badges/70/t").all()


def test_the_roi_tables_are_badged_once_there_are_points_to_rank(midseason_app):
    """Both ROI tables only fill in once a gameweek has been scored, and the
    projected-against-returned one needs a club column to map a badge from,
    which it did not originally select."""
    at = midseason_app.run()
    assert not at.exception
    badged = [d.value for d in at.dataframe if "badge" in d.value.columns]
    projected = [f for f in badged if "projected_roi" in f.columns]
    assert projected, "the projected against returned table should carry badges"
    assert projected[0]["badge"].str.contains("badges/70/t").all()


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
    assert any("No players match" in card for card in _states(at))
    assert any("Squad cost" in m.label for m in at.metric), "the Squad tab should still render"


def test_the_pool_says_how_much_it_is_hiding(app):
    """It used to cut silently to forty rows with nothing on screen saying so."""
    at = app.run()
    assert any("Showing" in c.value and "players" in c.value for c in at.caption)


def _select_rows(at, rows):
    """Tick rows in the pool table.

    A dataframe selection is a click on a canvas, which AppTest cannot do, so
    this writes the widget state Streamlit would have written. It has to be a
    fresh dict rather than a mutation, since widget state is read only.
    """
    at.session_state["pool"] = {"selection": {"rows": list(rows), "columns": []}}
    return at.run()


def _select_row(at, row=0):
    return _select_rows(at, [row])


def _rows_for_clubs(at, *clubs):
    """A row position in the pool for one player from each club named.

    Picking by club rather than by position so a test can say who it wants
    without depending on which synthetic player happens to sort to the top.
    """
    listed = list(_pool(at)["club"])
    return [listed.index(club) for club in clubs]


def test_ticking_a_player_shows_the_working_behind_his_projection(midseason_app):
    """A total says nothing about which of the three terms produced it, which
    is the whole reason the drill-down exists."""
    at = _select_row(midseason_app.run())
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Points per 90" in labels
    assert "Expected minutes" in labels
    assert any(label.startswith("Projected") for label in labels)


def test_the_working_renders_in_the_page_rather_than_a_dialog(midseason_app):
    """Ticks are how a comparison is built, so a modal on the first tick would
    have to be dismissed before the second could be reached."""
    at = _select_row(midseason_app.run())
    assert not at.exception
    assert not at.get("dialog"), "the drill-down should be inline"


def test_the_drill_down_says_so_when_there_is_no_current_sample(app):
    """`current_p90` is NaN before a ball is kicked, and formatting that into a
    rate would print nan at the reader instead of saying there is no sample."""
    at = _select_row(app.run())
    assert not at.exception
    assert any("no sample yet" in c.value for c in at.caption)


def test_streamlit_drops_the_ticks_when_the_table_changes(midseason_app):
    """The reason the picks are held by id rather than read off the table.

    A selection is row positions, not players, and Streamlit drops it whenever
    the data underneath changes. Pinned because everything else here depends on
    it: if this ever stopped being true, position 35 could resolve to a
    different player, or index past the end of a table that now has twenty
    rows.
    """
    at = _select_rows(midseason_app.run(), [35])
    assert "Points per 90" in [m.label for m in at.metric], "row 35 should tick"

    at = at.text_input[0].set_value("C01").run()
    assert not at.exception
    assert len(_pool(at)) < 35, "the filter has to leave fewer rows than the tick"
    assert at.session_state["pool"]["selection"]["rows"] == [], "Streamlit clears it"


def test_a_comparison_survives_the_horizon_moving(midseason_app):
    """Reading two players over three gameweeks and then over six is the whole
    point of the slider, and the slider changes every number in the table, which
    is enough for Streamlit to drop the ticks. Losing the comparison there would
    wipe it halfway through the thought it exists to support."""
    at = _compare(midseason_app.run(), "C01", "C02")
    assert len(_compare_table(at)) == 2

    at = next(s for s in at.slider if s.label == "Gameweeks to project over").set_value(3).run()
    assert not at.exception
    assert at.session_state["pool"]["selection"]["rows"] == [], "the ticks are gone"
    assert len(_compare_table(at)) == 2, "the comparison is not"
    assert any("Still reading" in c.value for c in at.caption), "and it says why the boxes emptied"


def test_a_filtered_out_player_leaves_the_comparison(midseason_app):
    """Held picks are still held by a filter, so a player the pool can no longer
    reach has to drop rather than linger as a row nobody can untick."""
    at = _compare(midseason_app.run(), "C01", "C02")
    at = at.text_input[0].set_value("C01").run()
    assert not at.exception
    assert not [d for d in at.dataframe if "ep_next" in d.value.columns], "one left is no pair"


def test_clearing_held_picks_empties_the_comparison(midseason_app):
    """The ticks are gone, so the button is the only way back out."""
    at = _compare(midseason_app.run(), "C01", "C02")
    at = next(s for s in at.slider if s.label == "Gameweeks to project over").set_value(3).run()
    next(b for b in at.button if b.key == "clear_compared").click().run()
    assert not at.exception
    assert not [d for d in at.dataframe if "ep_next" in d.value.columns]


def test_clearing_the_ticks_leaves_the_tab_clean(midseason_app):
    """Unticking has to take the working and the comparison away with it."""
    at = _select_row(midseason_app.run())
    assert "Points per 90" in [m.label for m in at.metric]

    at = _select_rows(at, [])
    assert not at.exception
    assert "Points per 90" not in [m.label for m in at.metric]
    assert any("Nothing ticked" in c.value for c in at.caption)


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


def _compare(at, *clubs):
    """Tick one player from each club named, which is how a comparison starts."""
    return _select_rows(at, _rows_for_clubs(at, *clubs))


def _compare_table(at):
    """The comparison table, found by FPL's own projection only it carries."""
    return next(d.value for d in at.dataframe if "ep_next" in d.value.columns)


def _compare_runs(at):
    """The rows behind the grouped run chart.

    Streamlit sends the chart's data as a named Arrow dataset rather than
    inline in the spec, so reading it back means unpacking the payload the
    browser would get.
    """
    import io
    import json

    import pyarrow as pa

    chart = next(
        c
        for c in at.get("vega_lite_chart")
        if "xOffset" in json.loads(c.proto.spec).get("encoding", {})
    )
    dataset = chart.proto.datasets[0]
    return pa.ipc.open_stream(io.BytesIO(dataset.data.data)).read_pandas()


def test_the_comparison_waits_to_be_asked(midseason_app):
    """Four charts and a table for nobody would push the price pressure
    section off the bottom of the tab for the whole of a normal visit."""
    at = midseason_app.run()
    assert not at.exception
    assert not [d for d in at.dataframe if "ep_next" in d.value.columns]


def test_every_compared_player_gets_a_row(midseason_app):
    """The point of the section: several players read against each other,
    rather than the same list read several times over."""
    at = midseason_app.run()
    at = _compare(at, "C01", "C02", "C03")
    assert not at.exception

    table = _compare_table(at)
    assert len(table) == 3
    assert list(table["club"]) == ["C01", "C02", "C03"], "in the order the table lists them"


def test_the_comparison_carries_fpls_own_projection_beside_ours(midseason_app):
    """`ep_next` is FPL's number and is deliberately not an input to ours, so
    the one honest place for it is next to ours where it can disagree."""
    at = midseason_app.run()
    at = _compare(at, "C01", "C02")
    assert not at.exception

    table = _compare_table(at)
    assert table["ep_next"].gt(0).all(), "their projection should be populated"
    assert not table["ep_next"].equals(table["xpts_next"]), "it is theirs, not a copy of ours"


def test_a_blank_gameweek_is_a_zero_rather_than_a_missing_bar(monkeypatch, tmp_path):
    """Two runs drawn only where their clubs play stop lining up under each
    other, and the week one of them sits out reads as a week nobody asked
    about. The synthetic season plays every club every week, so the blank has
    to be made."""
    from fpl_manager.data import Season

    unblanked = Season.team_fixtures

    def blanked(self, horizon, start_gw=None):
        runs = unblanked(self, horizon, start_gw)
        return runs[~((runs["team"] == 1) & (runs["event"] == int(runs["event"].min()) + 1))]

    monkeypatch.setattr(Season, "team_fixtures", blanked)

    at = _app(monkeypatch, tmp_path, played=12).run()
    at = _compare(at, "C01", "C02")
    assert not at.exception

    runs = _compare_runs(at)
    weeks = sorted(int(e) for e in runs["event"].unique())
    drawn = runs.groupby("name")["event"].apply(lambda events: sorted(int(e) for e in events))
    assert all(player == weeks for player in drawn), (
        "every player needs a row in every gameweek, or the bars stop aligning"
    )

    table = _compare_table(at)
    resting = table.loc[table["club"] == "C01", "name"].iloc[0]
    sat_out = runs[(runs["name"] == resting) & (runs["event"] == weeks[1])]
    assert len(sat_out) == 1
    assert sat_out.iloc[0]["fixture"] == "—", "the blank should name itself as one"
    assert sat_out.iloc[0]["xpts"] == 0.0, "a blank scores nothing, which is not nothing shown"


def test_the_compared_player_keeps_his_colour_across_every_chart(midseason_app):
    """Four charts about the same two people. A colour that means one player in
    the run and the other in the terms below is worse than no colour at all.

    The scan is over every chart on the page, so a new chart anywhere that
    colours by a field called `name` lands in this count and fails here rather
    than where it was written. Colour by something else and it stays out.
    """
    import json

    at = midseason_app.run()
    at = _compare(at, "C01", "C02")
    assert not at.exception

    scales = [
        spec["encoding"]["color"]["scale"]
        for spec in _chart_specs(at)
        if spec.get("encoding", {}).get("color", {}).get("field") == "name"
    ]
    assert len(scales) == 4, "the run and the three terms"
    assert len({json.dumps(scale, sort_keys=True) for scale in scales}) == 1


def test_the_comparison_follows_the_horizon(midseason_app):
    """The run and the totals beside it are both cut to the slider, so a chart
    still drawing six gameweeks after it was moved to three would be arguing
    against the numbers on the same screen."""
    at = midseason_app.run()
    at = _compare(at, "C01", "C02")
    at = next(s for s in at.slider if s.label == "Gameweeks to project over").set_value(3).run()
    assert not at.exception

    runs = _compare_runs(at)
    assert runs.groupby("name")["event"].count().eq(3).all()


def test_the_comparison_is_capped(midseason_app):
    """Past about four the grouped bars stop being readable and the table
    starts scrolling sideways on anything smaller than a laptop.

    A dataframe has no `max_selections`, so unlike the multiselect this
    replaced, the cap has to be enforced in the app and said out loud rather
    than being enforced by the widget refusing the fifth tick.
    """
    at = _select_rows(midseason_app.run(), [0, 1, 2, 3, 4])
    assert not at.exception
    assert len(_compare_table(at)) == 4
    assert any("Reading the first 4" in i.value for i in at.info)


def test_the_radar_says_nothing_is_coming_rather_than_showing_a_blank_table(app):
    """The synthetic season has every club playing exactly once a week, which
    is also what a real fixture list looks like until postponements start."""
    at = app.run()
    assert not at.exception
    assert any("Nothing irregular coming" in card for card in _states(at))


def test_the_radar_lists_clubs_once_a_double_exists(monkeypatch, tmp_path):
    """The empty case is the one the synthetic season reaches on its own, so
    the populated table needs its rows supplying."""
    import pandas as pd

    from fpl_manager.data import Season

    rows = pd.DataFrame(
        [
            {"event": 24, "club": "C01", "fixtures": 2, "shape": "double"},
            {"event": 24, "club": "C02", "fixtures": 2, "shape": "double"},
            {"event": 24, "club": "C03", "fixtures": 0, "shape": "blank"},
        ]
    )
    monkeypatch.setattr(Season, "gameweek_shape", lambda self, horizon=12: rows)

    at = _app(monkeypatch, tmp_path, played=12).run()
    assert not at.exception
    table = next(
        d.value for d in at.dataframe if {"double", "blank"} <= set(map(str, d.value.columns))
    )
    assert list(table["event"]) == [24]
    assert table.loc[0, "double"] == "C01, C02"
    assert table.loc[0, "blank"] == "C03"


def test_swings_split_into_easing_and_worsening(app):
    at = app.run()
    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("Hard now, easier later" in c for c in captions)
    assert any("Easy now, harder later" in c for c in captions)


def test_changing_the_swing_window_reruns_cleanly(app):
    at = app.run()
    next(s for s in at.slider if s.key == "swing_window").set_value(5).run()
    assert not at.exception


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


def _formation_picker(at):
    return next(s for s in at.sidebar.selectbox if s.label == "Formation")


def test_the_pitch_reads_back_the_shape_the_solver_chose(app):
    """Free by default, so the caption is a report rather than an instruction."""
    at = app.run()
    assert not at.exception
    assert _formation_picker(at).value == "Best"
    assert re.search(r"Formation \d-\d-\d<", _pitch(at))
    assert "behind the best shape" not in _pitch(at)


def test_pinning_a_formation_changes_the_pitch_and_says_what_it_cost(app):
    """The override has to reach the solve, not just the caption.

    Counting the shirt rows is what proves it did. A caption written from the
    selector rather than from the XI would pass on the string alone.
    """
    at = app.run()
    _formation_picker(at).set_value("3-4-3").run()
    assert not at.exception

    pitch = _pitch(at)
    assert "Formation 3-4-3" in pitch
    assert "pts behind the best shape" in pitch, "a shape it would not have chosen costs points"
    # the pitch holds the XI in rows plus the bench in one more
    rows = pitch.split('<div class="pitch-line">')[1:]
    assert [row.count('<div class="shirt') for row in rows] == [1, 3, 4, 3, 4]


def test_a_pinned_formation_reaches_the_transfer_pitches(app_with_squad):
    """Now and After come from two different solves, so they are the likeliest
    pair to disagree about the shape."""
    at = _load_the_squad(app_with_squad.run())
    _formation_picker(at).set_value("4-4-2").run()
    assert not at.exception

    compact = [m.value for m in at.markdown if 'class="pitch compact"' in m.value]
    if not compact:
        pytest.skip("no transfer worth making, so neither pitch is drawn")
    assert len(compact) == 2
    assert all("Formation 4-4-2" in p for p in compact)


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


def test_the_captain_tab_says_what_is_missing_before_the_season(app):
    """The gate is the point of the tab pre-season. A haul chance built on the
    stale payload the API serves in August would look current and be a year
    old, so the tab holds an explanation rather than a table."""
    at = app.run()
    assert not at.exception
    assert any("Not enough of this season yet" in card for card in _states(at))


def test_the_captain_tab_ranks_on_projected_points(midseason_app):
    """Doubling a score means the captain is whoever is expected to score most.
    The chances beside it are context for chasing a rank and for Triple
    Captain, and sorting on them would say otherwise."""
    at = midseason_app.run()
    assert not at.exception

    table = next(d.value for d in at.dataframe if getattr(d, "key", None) == "captain_pool")
    assert {"haul_chance", "return_chance", "xpts_gw"} <= set(table.columns)
    assert table["xpts_gw"].is_monotonic_decreasing, "best projection first"
    assert table["haul_chance"].between(0, 1).all()
    # a haul without a return is not a thing that can happen
    assert (table["haul_chance"] <= table["return_chance"] + 1e-12).all()


def test_the_captain_tab_starts_on_the_positions_the_number_suits(midseason_app):
    """A defender's route to ten is mostly a clean sheet and a defensive
    contribution, neither of which is in this number, so starting the filter on
    everybody would put a column of misleadingly low figures at the top."""
    at = midseason_app.run()
    assert not at.exception

    positions = next(m for m in at.multiselect if m.key == "captain_positions")
    assert set(positions.value) == {"MID", "FWD"}


def test_price_pressure_says_nothing_before_the_season(app):
    at = app.run()
    assert not at.exception
    assert any("No transfer activity yet" in card for card in _states(at))


def test_price_pressure_renders_once_transfers_exist(midseason_app):
    """The populated branch is unreachable pre-season, so it needs its own run."""
    at = midseason_app.run()
    assert not at.exception
    assert not any("No transfer activity yet" in card for card in _states(at))
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
    solved = build_squad(built)
    # an entry publishes its picks in order, the eleven first and the bench
    # after, plus the armband. Shape the fake the same way or the My squad
    # pitch has nothing true to draw.
    xi_ids = [int(i) for i in solved.xi.index]
    owned = xi_ids + [int(i) for i in solved.squad.index if int(i) not in xi_ids]
    monkeypatch.setattr(
        squad_module,
        "load_from_entry",
        lambda season, entry_id, gameweek=None: squad_module.MySquad(
            player_ids=owned,
            bank_tenths=10,
            free_transfers=2,
            entry_id=entry_id,
            captain_id=xi_ids[0],
            vice_captain_id=xi_ids[1],
        ),
    )
    return at


@pytest.fixture
def app_with_a_bad_lineup(monkeypatch, tmp_path):
    """The same fifteen, lined up the wrong way round.

    The caption saying what your own selection gives up only appears when there
    is something to give up, and `app_with_squad` deliberately lines up exactly
    as the solver would, so nothing else reaches it.
    """
    at = _app(monkeypatch, tmp_path, played=12)

    from fpl_manager import squad as squad_module
    from fpl_manager.data import Season
    from fpl_manager.optimiser import build_squad
    from fpl_manager.projections import project

    from .conftest import FakeApi, make_prior

    fake_season = Season(FakeApi(played=12))
    built, _ = project(fake_season, horizon=6, prior=make_prior(fake_season))
    solved = build_squad(built)
    xi_ids = [int(i) for i in solved.xi.index]
    bench_ids = [int(i) for i in solved.squad.index if int(i) not in xi_ids]
    started_the_bench = bench_ids + xi_ids
    monkeypatch.setattr(
        squad_module,
        "load_from_entry",
        lambda season, entry_id, gameweek=None: squad_module.MySquad(
            player_ids=started_the_bench,
            bank_tenths=10,
            free_transfers=2,
            entry_id=entry_id,
            captain_id=started_the_bench[0],
            vice_captain_id=started_the_bench[1],
        ),
    )
    return at


def _load_the_squad(at):
    """Drive the sidebar the way a user does, by key rather than by position.

    The id box is a text input, not a number: an entry id is not a quantity and
    steppers on one imply it can be nudged.
    """
    next(r for r in at.sidebar.radio if r.key == "squad_source").set_value("FPL entry id").run()
    next(t for t in at.sidebar.text_input if t.key == "sidebar_entry").set_value("123").run()
    return at


def test_a_loaded_squad_fills_my_squad(app_with_squad):
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any(m.label == "In the bank" for m in at.metric)
    assert any(n.label == "Max transfers to consider" for n in at.number_input)


def test_missing_selling_prices_are_warned_about(app_with_squad):
    """An entry publishes no purchase prices, so the money has to be flagged."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("valued at today's price" in w.value for w in at.warning)


def test_my_squad_says_so_when_nothing_is_loaded(app):
    at = app.run()
    assert not at.exception
    assert any("No squad loaded" in card for card in _states(at))


def test_my_squad_reads_the_armband_off_the_entry(app_with_squad):
    """The armband and the bench order come from the entry and nowhere else, so
    a page that shows the solver's instead is showing a team nobody picked."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("As you have it · captain " in c.value for c in at.caption)
    assert any(m.label == "In the bank" for m in at.metric)
    # the fixture lines up exactly as the solver would, so there is nothing to
    # disagree about and the second-guessing caption must stay away
    assert not any("would start a different eleven" in c.value for c in at.caption)


def test_a_lineup_the_model_disagrees_with_is_argued_with(app_with_a_bad_lineup):
    at = _load_the_squad(app_with_a_bad_lineup.run())
    assert not at.exception
    assert any("would start a different eleven" in c.value for c in at.caption)


def test_a_squad_file_has_no_lineup_to_show(app_with_squad, monkeypatch):
    """A file carries fifteen ids and nothing about how they were arranged, so
    the page has to say why it is showing the solver's eleven instead."""
    from fpl_manager import squad as squad_module

    real = squad_module.load_from_entry
    monkeypatch.setattr(
        squad_module,
        "load_from_entry",
        lambda season, entry_id, gameweek=None: squad_module.MySquad(
            player_ids=real(season, entry_id).player_ids
        ),
    )
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("nothing about how you lined them up" in i.value for i in at.info)


def test_a_mistyped_entry_id_is_an_error_not_a_traceback(app):
    """Anyone can put a name or a URL in that box. It has to say so."""
    at = app.run()
    next(r for r in at.sidebar.radio if r.key == "squad_source").set_value("FPL entry id").run()
    next(t for t in at.sidebar.text_input if t.key == "sidebar_entry").set_value("salah").run()
    assert not at.exception
    assert any("just digits" in e.value for e in at.error)


def test_a_remembered_id_comes_back_from_the_url(app_with_squad):
    """The URL is the only place the id can be kept. The deploy is public and
    multi tenant, so there is nowhere per user to write it."""
    at = app_with_squad
    at.query_params["entry"] = "123"
    at.run()
    assert not at.exception
    box = next(t for t in at.sidebar.text_input if t.key == "sidebar_entry")
    assert box.value == "123", "a bookmarked id should not need retyping"
    assert any(m.label == "In the bank" for m in at.metric), "and should load on its own"


def test_an_impossible_build_leaves_the_other_tabs_standing(monkeypatch, tmp_path):
    """Locks and a budget can be set to something with no legal fifteen in it.

    Wildcard is the second of ten tabs, so reporting that with st.stop() would
    take the eight below it down as well.
    """
    at = _app(monkeypatch, tmp_path, played=12)

    from fpl_manager import optimiser

    def nothing_fits(*args, **kwargs):
        raise RuntimeError("No legal squad under those constraints")

    monkeypatch.setattr(optimiser, "build_squad", nothing_fits)

    at.run()
    assert not at.exception
    assert any("Try relaxing the locks" in e.value for e in at.error)
    assert any(str(c).startswith("GW") for c in _pool(at).columns), "Players still renders"


def test_an_illegal_squad_is_an_error_not_a_traceback(monkeypatch, tmp_path):
    """Anyone can upload a hand-edited file, and fifteen from one club has no
    legal plan out of it. That has to read as a message, not a stack trace.

    Every tab that touches the squad has to survive it, not just the first one
    to notice. This passed for a while because the transfer tab called
    st.stop() here, which halted the script before Chips could try to price a
    chip against a squad with no legal eleven in it.
    """
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
    """Planner is the whole tab now, so it solves as soon as a squad is loaded
    rather than waiting behind a toggle."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any("Projected over" in m.label for m in at.metric)


def test_the_single_week_answer_lives_in_one_place(app_with_squad):
    """My squad owns the one week move and Planner owns the route. Two tabs
    solving the same week is two tabs that can disagree about the best move."""
    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert sum(n.label == "Max transfers to consider" for n in at.number_input) == 1
    assert sum(s.label == "Gameweeks to plan" for s in at.slider) == 1


def test_the_live_tab_survives_empty_live_endpoints(app):
    """The harness stubs every endpoint but bootstrap and fixtures to `{}`,
    which is also what the real API gives out of season. The tab has to say so
    rather than take the page down."""
    at = app.run()
    assert not at.exception
    assert any(tab.label == "Live" for tab in at.tabs)


def test_the_live_tab_renders_mid_season(midseason_app, monkeypatch):
    from fpl_manager import api

    from .conftest import FakeApi

    fake = FakeApi(played=12)
    monkeypatch.setattr(api.FplApi, "live", lambda self, gw: fake.live(gw))
    monkeypatch.setattr(
        api.FplApi, "fixtures_for_event", lambda self, gw: fake.fixtures_for_event(gw)
    )

    at = midseason_app.run()
    assert not at.exception


def test_the_live_tab_scores_a_loaded_squad(app_with_squad, monkeypatch):
    from fpl_manager import api

    from .conftest import FakeApi

    fake = FakeApi(played=12)
    monkeypatch.setattr(api.FplApi, "live", lambda self, gw: fake.live(gw))
    monkeypatch.setattr(
        api.FplApi, "fixtures_for_event", lambda self, gw: fake.fixtures_for_event(gw)
    )

    at = _load_the_squad(app_with_squad.run())
    assert not at.exception
    assert any(m.label == "Points" for m in at.metric)


def test_the_live_gameweek_is_drawn_on_a_pitch(app_with_squad, monkeypatch):
    """A live gameweek is read as a formation, the way the points page on the
    FPL site lays it out, rather than as two tables of numbers."""
    from fpl_manager import api

    from .conftest import FakeApi

    fake = FakeApi(played=12)
    monkeypatch.setattr(api.FplApi, "live", lambda self, gw: fake.live(gw))
    monkeypatch.setattr(
        api.FplApi, "fixtures_for_event", lambda self, gw: fake.fixtures_for_event(gw)
    )

    at = _load_the_squad(app_with_squad.run())
    assert not at.exception

    pitches = [m.value for m in at.markdown if 'class="pitch"' in m.value]
    live = [p for p in pitches if ">Pts<" in p]
    assert live, "the live gameweek should be on a pitch"
    assert ">Mins<" in live[0], "points alone cannot say whether he played"
    assert 'class="badge c"' in live[0], "the armband has to be on the shirt"
    assert 'class="badge v"' in live[0], "and the vice, who is who it falls to"
    assert "bench-strip" in live[0], "and the bench in the order it would come on"

    # whole points, since a settled score printed as 6.0 reads as an estimate
    assert not re.search(r'class="v next">\d+\.\d<', live[0])


def test_app_holds_no_model_logic():
    """The front end is a view. If it starts deciding things, split it out."""
    text = APP.read_text()
    for banned in ["LpProblem", "points_per_90 *", "def project("]:
        assert banned not in text, f"model logic leaked into app.py: {banned}"
