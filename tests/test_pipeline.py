"""Pipeline tests. Every FPL rule the optimiser must respect has an assertion here.

If you add a constraint to `optimiser.py`, add the test that proves it holds.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fpl_manager.data import (
    FORMATIONS,
    MAX_PER_CLUB,
    OUTFIELD,
    SQUAD_LIMITS,
    XI_MAX,
    XI_MIN,
    Season,
    format_formation,
    is_legal_xi,
    parse_formation,
)
from fpl_manager.optimiser import (
    build_squad,
    pick_xi,
    plan_transfers,
    planning_pool,
    suggest_transfers,
)
from fpl_manager.projections import project

from .conftest import N_TEAMS, SQUAD_SIZE_PER_TEAM, FakeApi


class _FakeResponse:
    """What `requests` would have returned, for the cache tests below."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


# ----------------------------------------------------------------------
# data layer
# ----------------------------------------------------------------------
def test_player_frame_is_complete(season: Season):
    assert len(season.players) == N_TEAMS * SQUAD_SIZE_PER_TEAM
    assert season.players["position"].isin(SQUAD_LIMITS).all()
    assert season.players["club"].notna().all()


def test_next_gameweek_follows_finished_events(season: Season):
    assert season.next_gameweek == season.gameweeks_played + 1


def test_fixture_ticker_covers_every_club(season: Season):
    ticker = season.fixture_ticker(horizon=6)
    assert len(ticker) == N_TEAMS
    assert ticker["avg_difficulty"].between(1, 5).all()


def test_gameweek_shape_only_reports_the_odd_ones(season: Season):
    """A normal gameweek produces no rows, so an empty frame reads as nothing
    coming rather than as a failure to look."""
    shape = season.gameweek_shape(horizon=8)
    assert set(shape.columns) == {"event", "club", "fixtures", "shape"}
    assert set(shape["shape"]) <= {"double", "blank"}
    assert (shape.loc[shape["shape"] == "double", "fixtures"] >= 2).all()
    assert (shape.loc[shape["shape"] == "blank", "fixtures"] == 0).all()


def test_a_double_is_found_and_the_rest_of_the_week_is_left_alone(season: Season):
    """Twenty clubs and nineteen fixtures in a week means one club doubles and
    one blanks, and the other eighteen are unremarkable."""
    gw = season.next_gameweek
    extra = {
        "id": 9999,
        "event": gw,
        "team_h": 1,
        "team_a": 2,
        "team_h_difficulty": 3,
        "team_a_difficulty": 3,
        "finished": False,
        "kickoff_time": pd.Timestamp("2026-08-22T14:00:00Z"),
    }
    season.fixtures = pd.concat([season.fixtures, pd.DataFrame([extra])], ignore_index=True)

    week = season.gameweek_shape(horizon=1)
    doubles = set(week.loc[week["shape"] == "double", "club"])
    assert doubles == {season.teams["short_name"][1], season.teams["short_name"][2]}


def test_a_played_fixture_still_counts_towards_the_shape(season: Season):
    """Every other reader here drops finished fixtures. This one must not: half
    way through a gameweek that would report each club that had already kicked
    off as blanking, which is the opposite of the truth."""
    gw = season.next_gameweek
    played = season.fixtures["event"] == gw
    if not played.any():
        pytest.skip("no fixtures in the next gameweek to mark as played")
    season.fixtures.loc[played, "finished"] = True

    blanks = season.gameweek_shape(horizon=1)
    assert blanks.empty, "clubs that have already played have not blanked"


def test_an_unpublished_gameweek_is_not_twenty_blanks(season: Season):
    """Past the end of the published schedule every club has no fixture. That
    is missing data, not a blank gameweek for the whole league."""
    shape = season.gameweek_shape(horizon=60)
    per_event = shape[shape["shape"] == "blank"].groupby("event").size()
    assert (per_event < len(season.teams)).all()


def test_fixture_swings_compare_one_block_against_the_next(season: Season):
    swings = season.fixture_swings(window=3)
    assert not swings.empty
    assert swings["swing"].is_monotonic_decreasing, "easiest to come should sort first"
    reconstructed = swings["now"] - swings["later"]
    assert np.allclose(reconstructed, swings["swing"])


def test_a_positive_swing_means_it_gets_easier(season: Season):
    """The sign is the whole signal, and getting it backwards would tell you to
    sell exactly the players you should be buying."""
    swings = season.fixture_swings(window=3)
    best = swings.iloc[0]
    assert best["swing"] > 0
    assert best["now"] > best["later"], "a positive swing is a hard run now, an easier one after"


def test_next_deadline_belongs_to_the_next_gameweek(season: Season):
    """The status bar puts the two side by side, so a deadline lifted from a
    different event than the gameweek shown next would be worse than none."""
    deadline = season.next_deadline
    assert deadline is not None
    assert deadline == season.events.loc[season.next_gameweek, "deadline_time"]


def test_fetched_at_reports_nothing_before_a_fetch(tmp_path):
    """A cold cache has no age to report, and inventing one would tell the user
    the numbers on screen are fresher than they are."""
    from fpl_manager.api import FplApi

    api = FplApi(cache_dir=tmp_path)
    assert api.fetched_at("bootstrap") is None


def test_fetched_at_reads_the_cache_file(tmp_path):
    from datetime import UTC, datetime

    from fpl_manager.api import FplApi

    api = FplApi(cache_dir=tmp_path)
    (tmp_path / "bootstrap.json").write_text("{}")
    fetched = api.fetched_at("bootstrap")
    assert fetched is not None
    assert abs((datetime.now(UTC) - fetched).total_seconds()) < 60


def test_a_half_written_cache_file_is_refetched(tmp_path, monkeypatch):
    """A truncated file used to take the whole page down with a JSON error.

    Refetching repairs it, which matters once anything polls a live endpoint
    often enough for a reader to catch a writer mid-write.
    """
    from fpl_manager.api import FplApi

    api = FplApi(cache_dir=tmp_path)
    (tmp_path / "bootstrap.json").write_text('{"events": [')

    monkeypatch.setattr(api.session, "get", lambda *a, **kw: _FakeResponse({"events": []}))
    assert api._get("bootstrap-static/", key="bootstrap") == {"events": []}
    assert (tmp_path / "bootstrap.json").read_text() == '{"events": []}'


def test_a_fetch_leaves_no_temporary_files(tmp_path, monkeypatch):
    """The cache is written through a temporary file so readers never see a
    partial one. Leaving those behind would fill a deploy's disk instead."""
    from fpl_manager.api import FplApi

    api = FplApi(cache_dir=tmp_path)
    monkeypatch.setattr(api.session, "get", lambda *a, **kw: _FakeResponse({"ok": True}))
    api._get("bootstrap-static/", key="bootstrap")

    assert [p.name for p in tmp_path.iterdir()] == ["bootstrap.json"]


def test_data_stamp_changes_when_the_cache_is_rewritten(tmp_path, monkeypatch):
    """Cached functions take the stamp because Streamlit will not hash a Season.
    If it did not move, a rebuilt season would keep serving stale projections."""
    import time

    from fpl_manager.api import FplApi

    api = FplApi(cache_dir=tmp_path)
    monkeypatch.setattr(api.session, "get", lambda *a, **kw: _FakeResponse({"ok": True}))

    api._get("bootstrap-static/", key="bootstrap")
    first = api.fetched_at("bootstrap")
    time.sleep(0.01)
    api._get("bootstrap-static/", key="bootstrap", ttl=0)

    assert api.fetched_at("bootstrap") != first


def test_current_gameweek_is_the_one_under_way(season: Season):
    """Between a deadline and the last final whistle, `gameweeks_played` is one
    behind. Live views want the gameweek being played, not the last finished."""
    played = season.gameweeks_played
    under_way = played + 1
    season.events["is_current"] = season.events.index == under_way

    assert season.current_gameweek == under_way
    assert season.gameweeks_played == played


def test_current_gameweek_is_zero_before_the_season(season: Season):
    season.events["is_current"] = False
    season.events["finished"] = False
    assert season.current_gameweek == 0


def test_the_fixture_term_is_unchanged_without_strength_ratings(season: Season):
    """The strength columns are optional on an undocumented API. With none of
    them the multiplier has to be exactly what it always was, or a trimmed
    payload silently reshapes every projection."""
    from fpl_manager.projections import fixture_multiplier

    fixtures = season.team_fixtures(horizon=4)
    plain = fixture_multiplier(fixtures["difficulty"], fixtures["is_home"])
    expected = 1.0 + (3 - fixtures["difficulty"]) * 0.09
    expected = expected + np.where(fixtures["is_home"], 0.03, -0.03)

    assert np.allclose(plain, expected)


def test_a_team_frame_without_strength_falls_back_rather_than_raising():
    from fpl_manager.projections import strength_multiplier

    season = Season(FakeApi(played=12, strengths=False))
    fixtures = season.team_fixtures(horizon=4)

    assert strength_multiplier(fixtures).isna().all()


def test_zero_ratings_are_treated_as_absent(season: Season):
    """FPL publishes all six ratings as zero until the season is under way, so
    this is what the term actually meets in August. A zero is a club that has
    not been rated, not one with no attack, and dividing by it is worse than
    falling back."""
    from fpl_manager.projections import fixture_multiplier, strength_multiplier

    fixtures = season.team_fixtures(horizon=4)
    for column in ("attack_for", "defence_for", "attack_against", "defence_against"):
        fixtures[column] = 0.0

    strength = strength_multiplier(fixtures)
    assert strength.isna().all()

    blended = fixture_multiplier(fixtures["difficulty"], fixtures["is_home"], strength)
    plain = fixture_multiplier(fixtures["difficulty"], fixtures["is_home"])
    assert np.allclose(blended, plain)


def test_one_unrated_club_does_not_poison_the_rest(season: Season):
    """A promoted club can be rated later than the others, and that must not
    take the whole term down with it."""
    from fpl_manager.projections import strength_multiplier

    fixtures = season.team_fixtures(horizon=4).copy()
    fixtures.loc[fixtures.index[:2], "defence_against"] = 0.0

    strength = strength_multiplier(fixtures)
    assert strength.iloc[:2].isna().all()
    assert strength.iloc[2:].notna().any()


def test_the_average_fixture_scores_one(season: Season):
    """The normalisation is what keeps the headline number in points. Drop it
    and every projection shifts by a constant, which moves the chip comparison
    and the budget the greedy baseline is measured against."""
    from fpl_manager.projections import strength_multiplier

    fixtures = season.team_fixtures(horizon=6)
    strength = strength_multiplier(fixtures)

    assert strength.notna().all()
    assert 0.9 < strength.mean() < 1.1


def test_a_strong_attack_against_a_weak_defence_beats_a_hostile_one(season: Season):
    """The multiplier rises with the strength ratio, every other input equal.

    Deliberately not asserted against the difficulty-only value. The two
    signals have different spreads, so on a fixture FPL already rates 5 the
    rating alone is lower than anything the strength term produces, and
    blending the two correctly moves it up. Monotonicity in strength is the
    property that has to hold; agreement with the rating it is there to
    correct is not.
    """
    from fpl_manager.projections import fixture_multiplier

    fixtures = season.team_fixtures(horizon=4).head(1).copy()
    difficulty, is_home = fixtures["difficulty"], fixtures["is_home"]

    def blended(ratio: float) -> float:
        strength = pd.Series([ratio], index=fixtures.index)
        return float(fixture_multiplier(difficulty, is_home, strength).iloc[0])

    assert blended(1.4) > blended(1.0) > blended(0.7)


def test_a_neutral_strength_pulls_the_fixture_towards_average(season: Season):
    """Two clubs of exactly average rating say the fixture is unremarkable, so
    the blend dilutes whatever the difficulty rating thought of it.

    This is what `STRENGTH_WEIGHT` buys and the reason it is not 1.0: the term
    can disagree with the rating, but only by its share. Asserted because the
    alternative reading, that a neutral strength is a no-op, is the intuitive
    one and is wrong."""
    from fpl_manager.projections import HOME_BONUS, fixture_multiplier

    # a fixture the difficulty rating has an opinion about, since softening a
    # rating that already said "unremarkable" is not a thing that can be
    # observed and picking whichever fixture came first left this to luck
    fixtures = season.team_fixtures(horizon=4)
    fixtures = fixtures[fixtures["difficulty"] != 3].head(1).copy()
    difficulty, is_home = fixtures["difficulty"], fixtures["is_home"]
    home_adjust = HOME_BONUS if bool(is_home.iloc[0]) else -HOME_BONUS

    plain = float(fixture_multiplier(difficulty, is_home).iloc[0]) - home_adjust
    neutral = (
        float(
            fixture_multiplier(difficulty, is_home, pd.Series([1.0], index=fixtures.index)).iloc[0]
        )
        - home_adjust
    )

    assert abs(neutral - 1.0) < abs(plain - 1.0), "a neutral rating should soften the rating"


def test_the_component_rate_is_inert_pre_season(season: Season):
    """FPL publishes the expected goals fields as zero until the season starts,
    so in August this term has nothing to say and the projection has to rest on
    last season exactly as it did before."""
    from fpl_manager.projections import component_rate, team_defence_rate

    if season.gameweeks_played:
        pytest.skip("this is the pre-season half of the fixture")

    rate = component_rate(season.players, team_defence_rate(season.players))
    assert rate.isna().all(), "nothing played means nothing to rebuild a rate from"


def test_a_penalty_taker_outprojects_an_identical_team_mate(season: Season):
    """Spot kicks are a claim on chances still to come, so they cannot be read
    off the expected goals a player has already accumulated."""
    from fpl_manager.projections import component_rate, team_defence_rate

    players = season.players.copy()
    forwards = players[(players["position"] == "FWD") & (players["minutes"] >= 900)]
    if len(forwards) < 2:
        pytest.skip("needs two forwards with minutes")

    taker, other = forwards.index[0], forwards.index[1]
    # identical in every respect the model reads, then one takes the penalties
    for column in ("minutes", "expected_goals", "expected_assists", "team"):
        players.loc[other, column] = players.loc[taker, column]
    players["penalties_order"] = np.nan
    players.loc[taker, "penalties_order"] = 1

    rate = component_rate(players, team_defence_rate(players))
    assert rate[taker] > rate[other]


def test_a_mean_defence_beats_a_leaky_one_for_a_defender(season: Season):
    """The clean sheet half of the rate is the whole reason a defender and a
    forward cannot share one scalar.

    Passes for two reasons now rather than one: the tight club's defender
    collects the clean sheet more often and is charged for conceded goals less
    often. `test_a_leaky_defence_costs_more_than_the_clean_sheet_alone` is the
    one that pins the second of those down."""
    from fpl_manager.projections import component_rate

    players = season.players.copy()
    defenders = players[(players["position"] == "DEF") & (players["minutes"] >= 900)]
    # one per club, since the clean sheet term is keyed on the club and two
    # defenders from the same one would be handed the same number
    defenders = defenders.groupby("team").head(1)
    if len(defenders) < 2:
        pytest.skip("needs two defenders at different clubs")

    tight, leaky = defenders.index[0], defenders.index[1]
    for column in ("minutes", "expected_goals", "expected_assists"):
        players.loc[leaky, column] = players.loc[tight, column]

    defence = pd.Series(
        {int(players.loc[tight, "team"]): 0.8, int(players.loc[leaky, "team"]): 2.4}
    )
    rate = component_rate(players, defence)
    assert rate[tight] > rate[leaky]


def test_a_forward_outscores_a_defender_on_the_same_expected_goals(season: Season):
    """A goal is worth six to a defender and four to a forward, so the position
    has to enter the rate rather than being averaged away."""
    from fpl_manager.projections import component_rate

    players = season.players.copy()
    playing = players[players["minutes"] >= 900]
    forward = playing[playing["position"] == "FWD"].index[:1]
    defender = playing[playing["position"] == "DEF"].index[:1]
    if not len(forward) or not len(defender):
        pytest.skip("needs one of each with minutes")

    f, d = forward[0], defender[0]
    players.loc[d, ["minutes", "expected_goals", "expected_assists"]] = players.loc[
        f, ["minutes", "expected_goals", "expected_assists"]
    ].to_numpy()

    # no clean sheet term, so the only difference left is what a goal is worth
    rate = component_rate(players, None)
    assert rate[d] > rate[f], "six points a goal beats four"


def _poisson_pmf(lam: float, k: int) -> float:
    """The Poisson probability of exactly k, written out rather than imported,
    so the tests below check the estimators against arithmetic rather than
    against the same library the estimators could have used."""
    return math.exp(-lam) * lam**k / math.factorial(k)


# The API serves these as whole counts and the frame holds them as integers, so
# a test writing a per 90 figure back into one has to widen it first. Real code
# only ever reads them.
COUNTING_STATS = [
    "saves",
    "yellow_cards",
    "red_cards",
    "defensive_contribution",
    "tackles",
    "recoveries",
    "clearances_blocks_interceptions",
]


def _editable(season: Season) -> pd.DataFrame:
    players = season.players.copy()
    for column in COUNTING_STATS:
        if column in players.columns:
            players[column] = players[column].astype("float64")
    return players


def test_the_new_scoring_terms_are_inert_before_the_first_deadline(season: Season):
    """The one that matters, because August is when this can silently go wrong.

    Before the first deadline the API is still serving last season's counts.
    Checked live while this was written: with no gameweek finished, players were
    carrying three thousand minutes and last year's expected goals. So the 270
    minute gate is wide open pre-season and is not what protects the model. What
    protects it is the blend weight being zero until a gameweek is played.

    The fixture zeroes these fields pre-season, which is kinder than the real
    payload, so this writes the stale values in itself. Trusting the fake here
    would prove nothing.
    """
    from fpl_manager.projections import build_rates, load_prior

    if season.gameweeks_played:
        pytest.skip("this is the pre-season half of the fixture")

    prior = load_prior(season)
    before = build_rates(season, prior)["points_per_90"]

    # last season's figures, the way the API actually serves them in August
    stale = season.players
    stale["minutes"] = 3000
    stale["saves"] = 90
    stale["defensive_contribution"] = 400
    stale["yellow_cards"] = 9
    stale["red_cards"] = 2
    stale["expected_goals_conceded"] = 45.0

    after = build_rates(season, prior)["points_per_90"]
    pd.testing.assert_series_equal(before, after)


def test_a_shot_stopper_outrates_a_spectator(season: Season):
    """A keeper at a club under siege makes saves a keeper at a good one never
    gets the chance to, and before this the two read identically."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    keepers = players[(players["position"] == "GKP") & (players["minutes"] >= 900)]
    if len(keepers) < 2:
        pytest.skip("needs two keepers with minutes")

    busy, idle = keepers.index[0], keepers.index[1]
    for column in ("minutes", "expected_goals", "expected_assists", "team"):
        players.loc[idle, column] = players.loc[busy, column]
    players.loc[busy, "saves"] = players.loc[busy, "minutes"] / 90 * 4
    players.loc[idle, "saves"] = 0

    rate = component_rate(players, None)
    assert rate[busy] > rate[idle]


def test_saves_are_a_keepers_points_and_nobody_elses(season: Season):
    """Only a keeper is paid for them, so the same figure on an outfielder has
    to leave his rate alone rather than quietly paying him for it."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    defenders = players[(players["position"] == "DEF") & (players["minutes"] >= 900)]
    if defenders.empty:
        pytest.skip("needs a defender with minutes")

    d = defenders.index[0]
    before = component_rate(players, None)[d]
    players.loc[d, "saves"] = players.loc[d, "minutes"] / 90 * 6
    after = component_rate(players, None)[d]

    assert after == pytest.approx(before)


def test_saves_are_paid_in_whole_threes(season: Season):
    """FPL pays `floor(saves / 3)` within a match and the leftovers are lost, so
    a rate divided by three pays for saves nobody was paid for. Worth about a
    third of a point per 90 for a busy keeper, which is not a rounding detail.

    Pinned against the exact Poisson figure so that simplifying this back to
    `saves90 / 3` fails here and says what was lost."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    keepers = players[(players["position"] == "GKP") & (players["minutes"] >= 900)]
    if keepers.empty:
        pytest.skip("needs a keeper with minutes")

    k = keepers.index[0]
    minutes = players.loc[k, "minutes"]
    players.loc[k, "saves"] = 0
    baseline = component_rate(players, None)[k]
    players.loc[k, "saves"] = minutes / 90 * 3.0
    paid = component_rate(players, None)[k] - baseline

    exact = sum(math.floor(s / 3) * _poisson_pmf(3.0, s) for s in range(40))
    assert paid == pytest.approx(exact, abs=0.01)
    assert paid < 3.0 / 3, "dividing the rate by three would overpay"


def test_the_conceded_charge_is_nothing_at_a_clean_sheet():
    """The clean sheet term pays at nought conceded and this one charges from
    two, so they are disjoint halves of one distribution rather than the same
    goals counted twice."""
    from fpl_manager.projections import _conceded_points

    assert _conceded_points(pd.Series([0.0])).iloc[0] == pytest.approx(0.0)


@pytest.mark.parametrize("lam", [0.8, 1.35, 2.0, 3.5])
def test_the_conceded_charge_matches_the_distribution_it_claims(lam: float):
    """`E[floor(C / 2)]` in closed form, checked against summing the Poisson
    term by term. Exact rather than approximate, so the only assumption in the
    penalty is the Poisson the clean sheet already makes."""
    from fpl_manager.projections import _conceded_points

    brute = sum(math.floor(c / 2) * _poisson_pmf(lam, c) for c in range(40))
    assert _conceded_points(pd.Series([lam])).iloc[0] == pytest.approx(brute, abs=1e-9)


def test_a_leaky_defence_costs_more_than_the_clean_sheet_alone(season: Season):
    """Before this, a defender at a bad club only missed out on the upside. Now
    he is charged for the downside too, so the gap has to be strictly wider than
    the clean sheet difference on its own."""
    from fpl_manager.projections import CLEAN_SHEET_POINTS, component_rate

    players = season.players.copy()
    defenders = players[(players["position"] == "DEF") & (players["minutes"] >= 900)]
    defenders = defenders.groupby("team").head(1)
    if len(defenders) < 2:
        pytest.skip("needs two defenders at different clubs")

    tight, leaky = defenders.index[0], defenders.index[1]
    for column in ("minutes", "expected_goals", "expected_assists", "saves"):
        players.loc[leaky, column] = players.loc[tight, column]
    players.loc[leaky, "defensive_contribution"] = players.loc[tight, "defensive_contribution"]
    players.loc[leaky, "yellow_cards"] = players.loc[tight, "yellow_cards"]
    players.loc[leaky, "red_cards"] = players.loc[tight, "red_cards"]

    defence = pd.Series(
        {int(players.loc[tight, "team"]): 1.0, int(players.loc[leaky, "team"]): 3.5}
    )
    rate = component_rate(players, defence)
    clean_sheet_only = CLEAN_SHEET_POINTS["DEF"] * (math.exp(-1.0) - math.exp(-3.5))

    assert rate[tight] - rate[leaky] > clean_sheet_only


def test_a_midfielder_is_not_charged_for_goals_conceded(season: Season):
    """The strongest of these, because it pins two things at once. A midfielder
    gets the clean sheet point and none of the penalty, so his gap between the
    same two clubs has to be exactly the clean sheet difference. If the penalty
    leaked into his position, or if it had been folded into the clean sheet term
    rather than sitting beside it, this moves."""
    from fpl_manager.projections import CLEAN_SHEET_POINTS, component_rate

    players = season.players.copy()
    mids = players[(players["position"] == "MID") & (players["minutes"] >= 900)]
    mids = mids.groupby("team").head(1)
    if len(mids) < 2:
        pytest.skip("needs two midfielders at different clubs")

    tight, leaky = mids.index[0], mids.index[1]
    for column in ("minutes", "expected_goals", "expected_assists", "defensive_contribution"):
        players.loc[leaky, column] = players.loc[tight, column]
    players.loc[leaky, "yellow_cards"] = players.loc[tight, "yellow_cards"]
    players.loc[leaky, "red_cards"] = players.loc[tight, "red_cards"]
    players.loc[[tight, leaky], "penalties_order"] = np.nan
    players.loc[[tight, leaky], "direct_freekicks_order"] = np.nan

    defence = pd.Series(
        {int(players.loc[tight, "team"]): 1.0, int(players.loc[leaky, "team"]): 3.5}
    )
    rate = component_rate(players, defence)
    clean_sheet_only = CLEAN_SHEET_POINTS["MID"] * (math.exp(-1.0) - math.exp(-3.5))

    assert rate[tight] - rate[leaky] == pytest.approx(clean_sheet_only, abs=1e-9)


@pytest.mark.parametrize(("rate", "threshold"), [(6.0, 10), (8.0, 10), (10.0, 12), (12.0, 12)])
def test_the_defensive_contribution_tail_matches_the_distribution(rate: float, threshold: int):
    """Walking the head of the Poisson is only worth doing if it lands on the
    same number a sum over the terms does."""
    from fpl_manager.projections import _poisson_at_least

    brute = sum(_poisson_pmf(rate, k) for k in range(threshold, 60))
    got = _poisson_at_least(pd.Series([rate]), pd.Series([float(threshold)])).iloc[0]
    assert got == pytest.approx(brute, abs=1e-9)


def test_each_row_gets_its_own_defensive_contribution_threshold():
    """Every player carries his own bar, so the terms below it have to be taken
    off per row rather than off the frame as a whole."""
    from fpl_manager.projections import _poisson_at_least

    got = _poisson_at_least(pd.Series([8.0, 8.0]), pd.Series([10.0, 12.0]))
    assert got.iloc[0] > got.iloc[1], "the same work clears a lower bar more often"
    assert got.iloc[1] == pytest.approx(sum(_poisson_pmf(8.0, k) for k in range(12, 60)), abs=1e-9)


def test_the_defensive_contribution_tail_is_a_probability():
    """It multiplies a points value, so anything outside nought to one is points
    invented or points lost rather than a chance of clearing a bar."""
    from fpl_manager.projections import _poisson_at_least

    rates = pd.Series([0.0, 1.0, 5.0, 9.0, 14.0, 40.0])
    tail = _poisson_at_least(rates, pd.Series([10.0] * len(rates)))
    assert ((tail >= 0) & (tail <= 1)).all()
    assert tail.is_monotonic_increasing, "more work clears the bar more often"
    assert _poisson_at_least(rates, pd.Series([0.0] * len(rates))).eq(1.0).all()

    lower = _poisson_at_least(rates, pd.Series([10.0] * len(rates)))
    higher = _poisson_at_least(rates, pd.Series([12.0] * len(rates)))
    assert (lower >= higher).all(), "a higher bar is cleared no more often"


def test_a_defender_clears_the_bar_more_easily_than_a_midfielder(season: Season):
    """Ten for a defender and twelve for everyone else, so identical defensive
    work is not worth the same to both."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    playing = players[players["minutes"] >= 900]
    defender = playing[playing["position"] == "DEF"].index[:1]
    mid = playing[playing["position"] == "MID"].index[:1]
    if not len(defender) or not len(mid):
        pytest.skip("needs one of each with minutes")

    d, m = defender[0], mid[0]
    # identical in everything, including the attacking return, so the only
    # thing left between them is where their threshold sits
    for column in ("minutes", "expected_goals", "expected_assists"):
        players.loc[m, column] = players.loc[d, column]
    players.loc[[d, m], "penalties_order"] = np.nan
    players.loc[[d, m], "direct_freekicks_order"] = np.nan
    players.loc[[d, m], "yellow_cards"] = 0
    players.loc[[d, m], "red_cards"] = 0
    work = players.loc[d, "minutes"] / 90 * 10.0
    players.loc[[d, m], "defensive_contribution"] = work

    # no clean sheet and no conceded charge, so a goal being worth six to one
    # and five to the other is the only other difference, and it favours the
    # defender in the same direction rather than against it
    rate = component_rate(players, None)
    assert rate[d] > rate[m]


def test_a_keeper_gets_no_defensive_contribution(season: Season):
    """He is not eligible for the category, so the work has to be worth nothing
    to him rather than merely hard to reach."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    keepers = players[(players["position"] == "GKP") & (players["minutes"] >= 900)]
    if keepers.empty:
        pytest.skip("needs a keeper with minutes")

    k = keepers.index[0]
    before = component_rate(players, None)[k]
    players.loc[k, "defensive_contribution"] = players.loc[k, "minutes"] / 90 * 20
    after = component_rate(players, None)[k]

    assert after == pytest.approx(before)


def test_a_red_card_costs_three_times_a_yellow(season: Season):
    """Cards are the one linear term here, so the ratio between them is just the
    scoring table and is worth pinning as such."""
    from fpl_manager.projections import component_rate

    players = _editable(season)
    mids = players[(players["position"] == "MID") & (players["minutes"] >= 900)]
    if mids.empty:
        pytest.skip("needs a midfielder with minutes")

    m = mids.index[0]
    per_season = players.loc[m, "minutes"] / 90
    players.loc[m, ["yellow_cards", "red_cards"]] = 0
    clean = component_rate(players, None)[m]

    players.loc[m, "yellow_cards"] = per_season
    yellow = component_rate(players, None)[m] - clean
    players.loc[m, ["yellow_cards", "red_cards"]] = [0, per_season]
    red = component_rate(players, None)[m] - clean

    assert yellow < 0 and red < 0, "a card costs points"
    assert red == pytest.approx(3 * yellow)


def test_no_penalty_turns_a_rate_negative(season: Season):
    """Charges are subtracted from a rate, never used to make one. A player the
    inputs cannot describe has to stay NaN so the caller falls back, rather than
    arriving as a confident negative number nobody asked for."""
    from fpl_manager.projections import component_rate, team_defence_rate

    players = season.players.copy()
    rate = component_rate(players, team_defence_rate(players))
    described = rate.dropna()
    assert (described >= 0).all(), "no player is worth less than nothing per 90"

    # nothing played, everything charged
    idle = players[players["minutes"] == 0].index[:1]
    if len(idle):
        players.loc[idle, ["yellow_cards", "red_cards", "defensive_contribution"]] = [9, 3, 400]
        again = component_rate(players, team_defence_rate(players))
        assert again[idle[0]] != again[idle[0]], "no minutes still means no rate"


def test_a_missing_category_loses_one_term_not_the_rate(season: Season):
    """The API is undocumented and a payload from before 2025/26 has no
    defensive contribution in it at all. Losing that term is right, losing every
    player's rate over it is not."""
    from fpl_manager.projections import component_rate

    players = season.players.copy()
    if not players["minutes"].ge(900).any():
        pytest.skip("needs somebody with minutes")

    full = component_rate(players, None)
    without = component_rate(
        players.drop(
            columns=[
                "defensive_contribution",
                "tackles",
                "recoveries",
                "clearances_blocks_interceptions",
            ]
        ),
        None,
    )

    described = full.dropna().index
    assert len(described), "the fixture should describe somebody"
    assert without[described].notna().all(), "one missing column is not a missing rate"
    assert (without[described] <= full[described] + 1e-9).all()


def test_the_defensive_contribution_falls_back_to_its_parts(season: Season):
    """`defensive_contribution` is the sum of the actions its position counts,
    verified against the live payload, so rebuilding it from those parts has to
    reproduce it. Recoveries are in the sum for everyone but defenders."""
    from fpl_manager.projections import component_rate

    players = season.players.copy()
    if not players["minutes"].ge(900).any():
        pytest.skip("needs somebody with minutes")

    full = component_rate(players, None)
    rebuilt = component_rate(players.drop(columns=["defensive_contribution"]), None)

    described = full.dropna().index
    pd.testing.assert_series_equal(full[described], rebuilt[described], atol=1e-9)


def test_team_defence_is_read_off_the_keepers(season: Season):
    """Expected goals conceded is charged per player per minute on the pitch,
    so summing outfielders counts the same goals once per defender."""
    from fpl_manager.projections import team_defence_rate

    rate = team_defence_rate(season.players)
    if not season.gameweeks_played:
        assert rate.empty or rate.isna().all()
        return

    assert (rate.dropna() >= 0).all()
    assert (rate.dropna() < 6).all(), "a club conceding six a game is a parsing error"


def test_strength_is_clipped_rather_than_extrapolated(season: Season):
    """A rating ratio far off 1.0 is a mismatch, not evidence for a projection
    three times the size."""
    from fpl_manager.projections import STRENGTH_CEILING, STRENGTH_FLOOR, strength_multiplier

    fixtures = season.team_fixtures(horizon=6)
    strength = strength_multiplier(fixtures)

    assert strength.between(STRENGTH_FLOOR, STRENGTH_CEILING).all()


def test_team_fixtures_carries_both_sides_strength(season: Season):
    fixtures = season.team_fixtures(horizon=4)
    for column in ("attack_for", "defence_for", "attack_against", "defence_against"):
        assert column in fixtures.columns
        assert fixtures[column].notna().all()


def test_team_fixtures_emits_one_row_per_fixture(season: Season):
    """Doubles and blanks fall out of the row count, with no special casing."""
    horizon = 4
    tf = season.team_fixtures(horizon)
    counts = tf.groupby(["team", "event"]).size()
    assert counts.max() >= 1
    assert tf["event"].nunique() <= horizon


# ----------------------------------------------------------------------
# projections
# ----------------------------------------------------------------------
def test_every_player_gets_a_projection(projections: pd.DataFrame):
    assert projections["xpts_total"].notna().all()
    assert (projections["xpts_total"] >= 0).all()


def test_minutes_share_is_a_fraction(projections: pd.DataFrame):
    assert projections["minutes_share"].between(0, 1).all()


def test_unavailable_players_project_no_minutes(season: Season, projections: pd.DataFrame):
    injured = season.players.index[season.players["status"] == "i"]
    assert (projections.loc[injured, "minutes_share"] == 0).all()


def test_shrinkage_shifts_weight_towards_current_season(prior: pd.DataFrame):
    """Pre-season leans entirely on prior data, mid-season blends it."""
    from .conftest import FakeApi

    pre = Season(FakeApi(played=0))
    mid = Season(FakeApi(played=12))
    pre_proj = project(pre, horizon=6, prior=prior.reindex(pre.players.index))[0]
    mid_proj = project(mid, horizon=6, prior=prior.reindex(mid.players.index))[0]
    assert pre_proj["current_p90"].isna().all()
    assert mid_proj["current_p90"].notna().any()


def test_projection_survives_missing_prior_data(season: Season):
    """New signings and promoted clubs still get a price-based estimate."""
    projections, _ = project(season, horizon=6, prior=None)
    assert projections["points_per_90"].notna().all()
    assert (projections["points_per_90"] >= 0).all()


def test_the_minutes_mixture_adds_back_up_to_the_minutes_share(projections: pd.DataFrame):
    """The model wants the minutes term as one number and `captaincy.py` wants
    it as the two it is made of. The moment those stop being the same number,
    one screen is quietly disagreeing with another about how much a player
    plays, and neither says which one to believe."""
    from fpl_manager.projections import SUB_SHARE

    rebuilt = (
        projections["start_chance"] * projections["starter_minutes"]
        + projections["sub_chance"] * SUB_SHARE
    )
    assert (rebuilt - projections["minutes_share"]).abs().max() < 1e-12
    assert projections["start_chance"].between(0, 1).all()
    assert projections["starter_minutes"].between(0, 1).all()
    # what is left over is the chance he does not feature at all
    assert ((projections["start_chance"] + projections["sub_chance"]) <= 1 + 1e-12).all()


def test_the_mixture_leaves_out_anyone_who_is_not_playing(
    season: Season, projections: pd.DataFrame
):
    """An injured player keeps no chance of coming off the bench either. A
    mixture that split the share without carrying the availability cut into
    both branches would hand him a cameo, and a haul chance with it."""
    injured = season.players.index[season.players["status"] == "i"]
    assert (projections.loc[injured, "start_chance"] == 0).all()
    assert (projections.loc[injured, "sub_chance"] == 0).all()


def test_the_attacking_rates_are_the_ones_the_component_rate_uses():
    """One definition of expected goals per 90. Two is how the Players tab and
    the Captain tab come to disagree about the same forward."""
    from fpl_manager.projections import (
        COMPONENT_MINUTES,
        GOAL_POINTS,
        PENALTY_XG_P90,
        attacking_rates,
        component_rate,
    )

    players = pd.DataFrame(
        {
            "minutes": [900.0, float(COMPONENT_MINUTES - 1)],
            "expected_goals": [5.0, 5.0],
            "expected_assists": [2.0, 2.0],
            "penalties_order": [1.0, 1.0],
            "direct_freekicks_order": [np.nan, np.nan],
            "position": ["MID", "MID"],
        },
        index=[1, 2],
    )
    rates = attacking_rates(players)
    assert rates.loc[1, "xg90"] == pytest.approx(5.0 / 10 + PENALTY_XG_P90, abs=1e-12)
    assert rates.loc[1, "xa90"] == pytest.approx(2.0 / 10, abs=1e-12)
    assert bool(rates.loc[1, "played_enough"])

    # below the gate what is left is the set piece duty on its own, zero rather
    # than NaN, so the term drops out instead of taking the component with it
    assert rates.loc[2, "xg90"] == pytest.approx(PENALTY_XG_P90, abs=1e-12)
    assert rates.loc[2, "xa90"] == 0.0
    assert not bool(rates.loc[2, "played_enough"])

    # and the component rate is reading these rather than keeping its own copy
    bumped = players.copy()
    bumped.loc[1, "expected_goals"] = 6.0
    moved = component_rate(bumped, None)[1] - component_rate(players, None)[1]
    assert moved == pytest.approx(GOAL_POINTS["MID"] * 0.1, abs=1e-9)


# ----------------------------------------------------------------------
# squad building: these are game rules, not preferences
# ----------------------------------------------------------------------
@pytest.fixture
def squad(projections: pd.DataFrame):
    return build_squad(projections, budget_tenths=1000)


def test_squad_has_fifteen_players(squad):
    assert len(squad.squad) == 15


def test_squad_is_within_budget(squad):
    assert squad.cost_tenths <= 1000


def test_squad_meets_position_quotas(squad):
    assert squad.squad["position"].value_counts().to_dict() == SQUAD_LIMITS


def test_squad_respects_club_cap(squad):
    assert squad.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_starting_eleven_is_a_legal_formation(squad):
    assert len(squad.xi) == 11
    counts = squad.xi["position"].value_counts()
    assert counts.get("GKP", 0) == 1
    for pos, minimum in XI_MIN.items():
        assert counts.get(pos, 0) >= minimum
    # the maximums went unasserted for a long time, which left a formation
    # constraint applied to the wrong variable free to pass
    for pos, maximum in XI_MAX.items():
        assert counts.get(pos, 0) <= maximum


def test_captain_starts_and_vice_differs(squad):
    assert squad.captain.name in squad.xi.index
    assert squad.vice_captain.name != squad.captain.name


def test_forced_inclusions_are_honoured(projections: pd.DataFrame):
    target = int(projections.sort_values("price", ascending=False).index[0])
    result = build_squad(projections, include=[target])
    assert target in result.squad.index


def test_exclusions_are_honoured(projections: pd.DataFrame):
    banned = int(projections.sort_values("xpts_total", ascending=False).index[0])
    result = build_squad(projections, exclude=[banned])
    assert banned not in result.squad.index


def test_the_bundled_solver_is_preferred_over_a_hand_found_one():
    """PuLP's own solver comes first wherever it has a binary, and reordering
    this to quieten a deprecation warning took the deployed app down.

    `PULP_CBC_CMD` resolves the right binary for the platform and chmods it
    executable, which a `COIN_CMD` pointed at a path found by globbing does
    not. On Linux, where the wheel ships the binary without the execute bit,
    skipping it means every solve dies in `posix_spawn`.

    The skip below is why CI runs on Linux. On Windows on ARM this test has
    never once reached its assertion, so a skip there is the fallback doing its
    job and a skip on Linux is this test quietly covering nothing.
    """
    import sys

    import pulp

    from fpl_manager.optimiser import solver

    solver.cache_clear()
    try:
        if not pulp.PULP_CBC_CMD(msg=False).available():
            if sys.platform.startswith("linux"):
                pytest.fail("PuLP ships a Linux binary, so this must never skip in CI")
            pytest.skip("no bundled binary on this platform, which is the fallback's job")
        assert isinstance(solver(), pulp.PULP_CBC_CMD)
    finally:
        solver.cache_clear()


def test_the_solver_falls_back_when_nothing_is_bundled(monkeypatch):
    """Windows on ARM, where PuLP looks for a build that was never shipped."""
    import pulp

    from fpl_manager.optimiser import solver

    monkeypatch.setattr(pulp.PULP_CBC_CMD, "available", lambda self: False)
    solver.cache_clear()
    try:
        found = solver()
        assert isinstance(found, pulp.COIN_CMD)
        assert found.available(), "a fallback that cannot run is worse than none"
    except RuntimeError as exc:
        assert "No CBC binary" in str(exc)
    finally:
        solver.cache_clear()


def test_solver_beats_greedy_value_picking(projections: pd.DataFrame, squad):
    """The reason selection is a MILP rather than a sort.

    If this starts failing, something in the formulation has broken rather than
    the greedy baseline having got cleverer.
    """
    picked, spend, by_pos, by_club = [], 0, {}, {}
    for pid, row in projections.sort_values("value", ascending=False).iterrows():
        pos, club = row["position"], row["club"]
        if by_pos.get(pos, 0) >= SQUAD_LIMITS[pos] or by_club.get(club, 0) >= MAX_PER_CLUB:
            continue
        if spend + row["now_cost"] > 1000:
            continue
        picked.append(pid)
        spend += row["now_cost"]
        by_pos[pos] = by_pos.get(pos, 0) + 1
        by_club[club] = by_club.get(club, 0) + 1

    greedy_xi = pick_xi(projections.loc[picked], points_col="xpts_total")[0]
    assert squad.xi["xpts_total"].sum() >= greedy_xi["xpts_total"].sum()


def test_tighter_budget_gives_a_cheaper_squad(projections: pd.DataFrame, squad):
    lean = build_squad(projections, budget_tenths=850)
    assert lean.cost_tenths <= 850
    assert lean.projected <= squad.projected


# ----------------------------------------------------------------------
# transfers
# ----------------------------------------------------------------------
def test_transfer_plan_respects_the_cap(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    plan = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    assert len(plan.transfers_in) <= 2
    assert len(plan.transfers_in) == len(plan.transfers_out)


def test_transfer_plan_keeps_squad_legal(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    plan = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    assert len(plan.squad) == 15
    assert plan.squad["position"].value_counts().to_dict() == SQUAD_LIMITS
    assert plan.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_transfers_stay_inside_the_bank(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    bank = 15
    plan = suggest_transfers(
        projections, owned, bank_tenths=bank, free_transfers=1, max_transfers=2
    )
    if plan.transfers_in.empty:
        pytest.skip("solver chose to roll the transfer")
    assert plan.transfers_in["now_cost"].sum() <= plan.transfers_out["now_cost"].sum() + bank


def test_selling_prices_constrain_spending(projections: pd.DataFrame, squad):
    """Undervaluing the squad should never buy a more expensive replacement."""
    owned = [int(i) for i in squad.squad.index]
    poor = {i: int(projections.loc[i, "now_cost"] * 0.7) for i in owned}
    plan = suggest_transfers(
        projections, owned, selling_prices=poor, bank_tenths=0, free_transfers=1, max_transfers=1
    )
    if plan.transfers_in.empty:
        pytest.skip("solver chose to roll the transfer")
    proceeds = sum(poor[int(i)] for i in plan.transfers_out.index)
    assert plan.transfers_in["now_cost"].sum() <= proceeds


def test_no_free_transfers_discourages_churn(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    free = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    costly = suggest_transfers(
        projections, owned, bank_tenths=15, free_transfers=0, max_transfers=2
    )
    assert len(costly.transfers_in) <= len(free.transfers_in) + 1
    assert costly.hits == max(0, len(costly.transfers_in))


# ----------------------------------------------------------------------
# multi-gameweek transfer planning
# ----------------------------------------------------------------------
@pytest.fixture
def horizon(season: Season, prior: pd.DataFrame):
    """Projections plus the per-gameweek frame the planner links weeks with."""
    return project(season, horizon=4, prior=prior)


@pytest.fixture
def multiweek(horizon, squad):
    projections, by_gameweek = horizon
    return plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        free_transfers=1,
        weeks=3,
    )


def test_a_plan_covers_every_week_asked_for(multiweek):
    assert len(multiweek.weeks) == 3
    assert [w.event for w in multiweek.weeks] == sorted(w.event for w in multiweek.weeks)


def test_every_week_is_a_legal_squad(multiweek):
    """The rules hold in each week separately, not just at the end."""
    for week in multiweek.weeks:
        assert len(week.squad) == 15
        assert week.squad["position"].value_counts().to_dict() == SQUAD_LIMITS
        assert week.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_every_week_fields_a_legal_eleven(multiweek):
    for week in multiweek.weeks:
        assert len(week.xi) == 11
        assert len(week.bench) == 4
        for pos, minimum in XI_MIN.items():
            assert (week.xi["position"] == pos).sum() >= minimum
        assert week.captain.name in set(week.xi.index)


def test_each_week_changes_by_exactly_its_transfers(multiweek, squad):
    """The link constraint is the whole point, so hold it to the letter."""
    held = {int(i) for i in squad.squad.index}
    for week in multiweek.weeks:
        expected = held - set(week.transfers_out.index) | set(week.transfers_in.index)
        assert set(week.squad.index) == expected
        assert len(week.transfers_in) == len(week.transfers_out)
        held = expected


def test_the_bank_never_goes_negative(multiweek):
    for week in multiweek.weeks:
        assert week.bank_tenths >= 0


def test_the_transfer_cap_holds_in_every_week(horizon, squad):
    projections, by_gameweek = horizon
    plan = plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        weeks=3,
        max_transfers_per_week=1,
    )
    for week in plan.weeks:
        assert len(week.transfers_in) <= 1


def test_a_quiet_week_banks_a_free_transfer(horizon, squad):
    """Rolling is the thing single-week planning cannot do."""
    projections, by_gameweek = horizon
    plan = plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        free_transfers=1,
        weeks=3,
    )
    for previous, following in zip(plan.weeks, plan.weeks[1:], strict=False):
        if not len(previous.transfers_in):
            assert following.free_transfers <= 5
            assert following.free_transfers >= previous.free_transfers


def test_unknown_selling_prices_are_declared(horizon, squad):
    """The money error compounds over weeks, so it must not be silent."""
    projections, by_gameweek = horizon
    owned = [int(i) for i in squad.squad.index]
    vague = plan_transfers(projections, by_gameweek, owned, bank_tenths=10, weeks=2)
    assert vague.approximate_money

    known = {i: int(projections.loc[i, "now_cost"]) for i in owned}
    exact = plan_transfers(
        projections, by_gameweek, owned, selling_prices=known, bank_tenths=10, weeks=2
    )
    assert not exact.approximate_money


def test_selling_prices_constrain_the_whole_plan(horizon, squad):
    """Undervaluing the squad must bind in every week, not only the first."""
    projections, by_gameweek = horizon
    owned = [int(i) for i in squad.squad.index]
    poor = {i: int(projections.loc[i, "now_cost"] * 0.6) for i in owned}
    plan = plan_transfers(
        projections, by_gameweek, owned, selling_prices=poor, bank_tenths=0, weeks=3
    )
    bank = 0
    for week in plan.weeks:
        bank += sum(
            poor.get(int(i), int(projections.loc[i, "now_cost"])) for i in week.transfers_out.index
        )
        bank -= sum(int(projections.loc[i, "now_cost"]) for i in week.transfers_in.index)
        assert bank >= 0


def test_the_pool_always_keeps_what_you_own(horizon, squad):
    """A plan that cannot see a player it owns cannot sell him."""
    projections, _ = horizon
    owned = [int(i) for i in squad.squad.index]
    pool = planning_pool(projections, owned, pool_size=40)
    assert set(owned) <= set(pool.index)


def test_planning_needs_a_gameweek_to_plan_over(horizon, squad):
    projections, by_gameweek = horizon
    with pytest.raises(ValueError, match="No gameweeks"):
        plan_transfers(
            projections,
            by_gameweek.iloc[0:0],
            current_ids=[int(i) for i in squad.squad.index],
        )


# ----------------------------------------------------------------------
# lineup
# ----------------------------------------------------------------------
def test_lineup_splits_eleven_and_four(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    xi, bench, captain = pick_xi(projections.loc[owned])
    assert len(xi) == 11
    assert len(bench) == 4
    assert captain.name in xi.index


def test_lineup_meets_formation_minimums(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    xi, _, _ = pick_xi(projections.loc[owned])
    counts = xi["position"].value_counts()
    assert counts.get("GKP", 0) == 1
    for pos, minimum in XI_MIN.items():
        assert counts.get(pos, 0) >= minimum


# ----------------------------------------------------------------------
# formation: free by default, pinned on request
# ----------------------------------------------------------------------
def _shape(xi: pd.DataFrame) -> tuple[int, int, int]:
    return tuple(int((xi["position"] == pos).sum()) for pos in OUTFIELD)


def test_every_generated_formation_is_one_fpl_allows():
    assert len(FORMATIONS) == 8
    for shape in FORMATIONS:
        positions = ["GKP"] + [
            pos for pos, n in zip(OUTFIELD, shape, strict=True) for _ in range(n)
        ]
        assert is_legal_xi(positions)


def test_illegal_shapes_are_refused():
    for bad in ["2-5-3", "4-4-3", "3-4", "nonsense"]:
        with pytest.raises(ValueError):
            parse_formation(bad)


def test_no_formation_asked_for_means_no_formation_imposed():
    assert parse_formation(None) is None


@pytest.mark.parametrize("shape", FORMATIONS, ids=format_formation)
def test_a_pinned_shape_is_the_shape_that_gets_built(projections: pd.DataFrame, shape):
    result = build_squad(projections, formation=parse_formation(shape))
    assert _shape(result.xi) == shape


@pytest.mark.parametrize("shape", FORMATIONS, ids=format_formation)
def test_a_pinned_shape_is_the_shape_that_gets_fielded(projections: pd.DataFrame, squad, shape):
    owned = [int(i) for i in squad.squad.index]
    xi, bench, captain = pick_xi(projections.loc[owned], formation=parse_formation(shape))
    assert _shape(xi) == shape
    assert len(bench) == 4
    assert captain.name in xi.index


def test_pinning_a_shape_can_only_cost_points(projections: pd.DataFrame):
    """The free solve searches every shape, so no pinned one can beat it.

    This is the assertion that catches a formation constraint written onto the
    squad variable rather than the starting one. That would still produce the
    shape asked for, and would quietly buy a better squad than the rules allow.
    """
    best = build_squad(projections).projected
    for shape in FORMATIONS:
        assert build_squad(projections, formation=parse_formation(shape)).projected <= best + 1e-6


def test_a_free_solve_is_unchanged_by_the_formation_argument(projections: pd.DataFrame, squad):
    """`formation=None` has to reproduce the old output exactly."""
    assert build_squad(projections, formation=None).projected == build_squad(projections).projected
    owned = [int(i) for i in squad.squad.index]
    xi, _, _ = pick_xi(projections.loc[owned], formation=None)
    assert list(xi.index) == list(pick_xi(projections.loc[owned])[0].index)


def test_an_unfieldable_shape_is_an_error_not_a_traceback(projections: pd.DataFrame, squad):
    """A squad short of players has no legal eleven, pinned or otherwise.

    Without the status check this surfaced as a `StopIteration` out of the
    captain lookup, which reads like a bug in the caller rather than a squad
    that cannot field the shape asked for.
    """
    owned = [int(i) for i in squad.squad.index]
    short = projections.loc[owned]
    short = short[short["position"] != "FWD"]
    with pytest.raises(RuntimeError, match="No legal eleven"):
        pick_xi(short, formation=parse_formation("3-4-3"))


def test_a_pinned_shape_holds_for_every_week_of_a_plan(horizon, squad):
    """A formation is a choice about how you play, so it binds every week."""
    week_projections, by_gameweek = horizon
    plan = plan_transfers(
        week_projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        weeks=2,
        formation=parse_formation("4-4-2"),
    )
    for week in plan.weeks:
        assert _shape(week.xi) == (4, 4, 2)


# ----------------------------------------------------------------------
# prior season loading: the thing that decides whether a cold deploy boots
# ----------------------------------------------------------------------
def test_prior_prefers_the_cache(season: Season, prior: pd.DataFrame, tmp_path, monkeypatch):
    from fpl_manager import projections as proj_module

    cached = tmp_path / "cached.parquet"
    prior.to_parquet(cached)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", cached)
    monkeypatch.setattr(proj_module, "BUNDLED_PRIOR", tmp_path / "absent.parquet")

    assert len(proj_module.load_prior(season)) == len(prior)


def test_prior_falls_back_to_the_committed_copy(
    season: Season, prior: pd.DataFrame, tmp_path, monkeypatch
):
    """With an empty cache the repo copy is used rather than 700 requests."""
    from fpl_manager import projections as proj_module

    bundled = tmp_path / "prior_season.parquet"
    prior.to_parquet(bundled)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", tmp_path / "absent.parquet")
    monkeypatch.setattr(proj_module, "BUNDLED_PRIOR", bundled)

    def explode(*args, **kwargs):
        raise AssertionError("fetched from the network when a committed copy existed")

    monkeypatch.setattr(proj_module, "fetch_prior_season", explode)
    assert len(proj_module.load_prior(season)) == len(prior)


# ----------------------------------------------------------------------
# prior season: only the season that just finished counts
# ----------------------------------------------------------------------
def test_only_the_most_recent_season_is_kept(season: Season):
    """`history_past` holds Premier League seasons only.

    A player who spent last year abroad has an entry that is years old, and
    treating it as last season's form is worse than admitting we know nothing.
    """
    from fpl_manager.projections import fetch_prior_season

    from .conftest import LAST_SEASON

    prior = fetch_prior_season(season, delay=0.0)
    assert not prior.empty
    assert (prior["prior_season"] == LAST_SEASON).all()


def test_players_with_only_stale_history_are_dropped(season: Season):
    from fpl_manager.projections import fetch_prior_season

    prior = fetch_prior_season(season, delay=0.0)
    stale = [int(i) for i in season.players.index if i % 5 == 0 and i % 7 != 0]
    assert stale, "fixture should produce some stale-history players"
    assert not set(stale) & set(prior.index)


def test_starts_are_captured(season: Season):
    """The minutes model needs starts, so the fetch has to bring them back."""
    from fpl_manager.projections import fetch_prior_season

    prior = fetch_prior_season(season, delay=0.0)
    assert "prior_starts" in prior.columns
    assert prior["prior_starts"].notna().all()


# ----------------------------------------------------------------------
# the minutes model must not collapse back into last season's points
# ----------------------------------------------------------------------
def test_projection_is_not_just_last_seasons_points_over_38(season: Season):
    """The identity this replaced.

    points/(minutes/90) times minutes/(38*90) is exactly points/38, so the old
    formulation made the rate and the minutes terms cancel. If that identity
    ever comes back, every player's per-gameweek projection equals their prior
    total over 38 and the two terms are decorative.
    """
    from fpl_manager.projections import build_rates

    from .conftest import make_prior

    prior = make_prior(season)
    rates = build_rates(season, prior)
    joined = rates.join(prior[["prior_points", "prior_minutes"]])
    solid = joined[(joined["prior_minutes"] >= 900) & (joined["minutes_share"] > 0)]
    assert len(solid) > 20

    per_gameweek = solid["points_per_90"] * solid["minutes_share"]
    collapsed = solid["prior_points"] / 38
    assert not np.allclose(per_gameweek, collapsed, rtol=0.02)


def test_rate_and_minutes_can_move_independently(season: Season):
    """Two players with the same prior total should not be interchangeable.

    One scoring at a high rate over few starts is a different proposition from
    one grinding out the same total every week, and the model has to be able to
    tell them apart.
    """
    from fpl_manager.projections import build_rates

    from .conftest import make_prior

    prior = make_prior(season)
    rates = build_rates(season, prior).join(prior[["prior_points", "prior_minutes"]])
    rates = rates[(rates["prior_minutes"] > 0) & (rates["prior_points"] > 0)]
    rates["per_gw"] = rates["points_per_90"] * rates["minutes_share"]

    # bucket by prior total, then check the projections inside a bucket differ
    buckets = rates.groupby(rates["prior_points"].round(-1))["per_gw"].agg(["size", "std"])
    populated = buckets[buckets["size"] >= 3]
    assert len(populated) > 0
    assert (populated["std"].fillna(0) > 0).any()


def test_differential_ranks_projection_against_ownership(projections: pd.DataFrame):
    """Percentiles on both sides, so the number has no units to argue about and
    no constant to tune."""
    assert projections["differential"].between(-1, 1).all()
    assert projections["differential"].abs().sum() > 0


def test_a_differential_is_a_player_the_crowd_underrates(projections: pd.DataFrame):
    best = projections["differential"].idxmax()
    worst = projections["differential"].idxmin()

    assert projections.loc[best, "ownership"] <= projections.loc[worst, "ownership"]


def test_differential_ignores_price(projections: pd.DataFrame):
    """It is about who owns him, not what he costs. Value already covers price,
    and folding it in here would make two columns say the same thing."""
    from fpl_manager.projections import differential_score

    doubled = projections.copy()
    doubled["now_cost"] = doubled["now_cost"] * 2
    assert (differential_score(doubled) == differential_score(projections)).all()


def test_the_planning_pool_keeps_cheap_enablers(projections: pd.DataFrame):
    """Ranking on points alone drops the cheapest players first, which are the
    ones that decide whether a route is affordable at all."""
    from fpl_manager.data import SQUAD_LIMITS
    from fpl_manager.optimiser import MIN_POOL_PER_POSITION, POOL_SIZE, planning_pool

    pool = planning_pool(projections, current_ids=[])
    playing = projections[projections["minutes_share"] >= 0.05]

    cheaper = 0
    for pos, count in SQUAD_LIMITS.items():
        take = max(MIN_POOL_PER_POSITION, round(POOL_SIZE * count / 15))
        by_points = playing[playing["position"] == pos].nlargest(take, "xpts_total")
        here = pool[pool["position"] == pos]
        cheaper += int(here["price"].min() < by_points["price"].min())

    assert cheaper, "no position gained a player cheaper than a points ranking would keep"


def test_the_planning_pool_still_keeps_the_best_players(projections: pd.DataFrame):
    """Reserving places for value must not cost the pool its top scorers."""
    from fpl_manager.optimiser import planning_pool

    pool = planning_pool(projections, current_ids=[])
    playing = projections[projections["minutes_share"] >= 0.05]
    for pos in ("GKP", "DEF", "MID", "FWD"):
        best = playing[playing["position"] == pos].nlargest(5, "xpts_total").index
        assert all(i in pool.index for i in best), f"{pos} lost a top scorer"


# ----------------------------------------------------------------------
# module boundaries: which module is allowed to know about which
# ----------------------------------------------------------------------
PACKAGE = Path(__file__).resolve().parent.parent / "fpl_manager"


def _imports_of(module: str) -> set[str]:
    """Every module inside the package that this one imports.

    Parsed rather than imported, so asking the question does not itself create
    the dependency, and rather than scanned for substrings, because an import
    is a shape the grammar already knows how to find.
    """
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:  # from . import x
                found.update(alias.name for alias in node.names)
            elif node.level and node.module:  # from .x import y
                found.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("fpl_manager."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("fpl_manager."):
                    found.add(alias.name.split(".")[1])
    return found


def test_the_haul_model_is_a_leaf():
    """Nothing that chooses a squad may read the haul chances.

    This is the whole reason `captaincy.py` is its own module. A variance term
    in the objective was argued out once already and turned down: it is
    precision theatre on a model this rough, and a squad picked partly on a
    distribution nobody can see is one the user cannot argue with. The useful
    version of caring about variance is a haul chance on a captaincy view, read
    beside the projection rather than folded into it. Left as a note in a file,
    that argument would be relitigated within the year, so it is a test.
    """
    for module in ("optimiser", "chips", "projections", "data", "live", "roi", "squad"):
        assert "captaincy" not in _imports_of(module), f"{module}.py reached for the haul model"


def test_captaincy_imports_only_what_it_is_allowed_to():
    """It reads the rates and the season, and nothing else in the package."""
    assert _imports_of("captaincy") <= {"data", "projections"}


def test_live_stays_a_leaf():
    """Stated in `CLAUDE.md` and until now checked by nothing.

    The forward looking modules run on a six hour cache and `live.py` looks at
    the last sixty seconds. Joining them puts two numbers that disagree on the
    same screen, and a realised score is an outcome rather than evidence for a
    projection.
    """
    for module in ("projections", "optimiser", "chips", "captaincy"):
        assert "live" not in _imports_of(module), f"{module}.py imported live scoring"
