"""Tests for the haul distribution.

Two kinds of test here. The arithmetic ones check the machinery against a
brute force sum or a closed form, because a distribution that is subtly wrong
still comes back as a number between nought and one and nothing else notices.
The rest check the gate, which is the part that goes wrong in August rather
than at the keyboard.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from fpl_manager.captaincy import (
    COLUMNS,
    HAUL_POINTS,
    _distribution,
    _gameweek_pmf,
    _poisson_head,
    _return_chance,
    haul_frame,
    points_pmf,
)
from fpl_manager.data import Season
from fpl_manager.projections import (
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    GOAL_POINTS,
    SHORT_APPEARANCE_POINTS,
    SUB_SHARE,
    project,
)

from .test_pipeline import _poisson_pmf


def _one_player(fixtures: list[float], xg: float = 0.55, xa: float = 0.3, **kwargs):
    """One midfielder, one gameweek, with the role weights spelled out."""
    weights = {"start_chance": 0.7, "sub_chance": 0.2, "starter_minutes": 0.85}
    weights.update(kwargs)
    return _gameweek_pmf(
        np.array([fixtures]),
        np.array([xg]),
        np.array([xa]),
        GOAL_POINTS["MID"],
        np.array([weights["start_chance"]]),
        np.array([weights["sub_chance"]]),
        np.array([weights["starter_minutes"]]),
    )


# ----------------------------------------------------------------------
# the arithmetic
# ----------------------------------------------------------------------
def test_the_poisson_head_agrees_with_the_tail_in_projections():
    """Two walks down the same distribution live in this package now. If they
    ever disagree, one of the two screens reading them is wrong and there is
    nothing on either to say which."""
    from fpl_manager.projections import _poisson_at_least

    for rate in (0.0, 0.4, 1.0, 3.0, 9.0):
        head = _poisson_head(np.array([rate]))[0]
        for threshold in (1, 2, 5):
            tail = _poisson_at_least(pd.Series([rate]), pd.Series([float(threshold)])).iloc[0]
            assert 1 - head[:threshold].sum() == pytest.approx(tail, abs=1e-9)


def test_the_distribution_sums_to_one():
    """Truncating at ten goals in a fixture loses something. This says how
    much, so that nobody is tempted to renormalise and hide it."""
    total = _one_player([1.2]).sum()
    assert total == pytest.approx(1.0, abs=1e-6)
    assert total <= 1.0, "truncation can only lose mass, never invent it"


def test_the_haul_chance_matches_the_distribution_it_claims():
    """The brute force, over a rectangle wide enough that the corner is nothing.
    Walking the head of two Poissons is only worth doing if it lands on the
    number a sum over the terms does."""
    xg, xa, share = 0.6, 0.35, 0.9
    pmf = _one_player([1.0], xg=xg, xa=xa, start_chance=1.0, sub_chance=0.0, starter_minutes=share)

    goal_points = GOAL_POINTS["MID"]
    brute = sum(
        _poisson_pmf(xg * share, g) * _poisson_pmf(xa * share, a)
        for g in range(40)
        for a in range(40)
        if APPEARANCE_POINTS + goal_points * g + ASSIST_POINTS * a >= HAUL_POINTS
    )
    assert pmf[0, HAUL_POINTS:].sum() == pytest.approx(brute, abs=1e-9)


def test_the_distribution_has_the_mean_it_should():
    """The one reconciliation worth claiming. The scoring half of the mean is
    the rate times the fixture term times `minutes_share`, which is the
    projection's own minutes term rather than a second opinion on it."""
    xg, xa = 0.55, 0.3
    start, sub, lasts = 0.7, 0.2, 0.85
    pmf = _one_player([1.1, 0.9], xg=xg, xa=xa)

    scoring = GOAL_POINTS["MID"] * xg + ASSIST_POINTS * xa
    minutes_share = start * lasts + sub * SUB_SHARE
    appearance = start * 2 * APPEARANCE_POINTS + sub * 2 * SHORT_APPEARANCE_POINTS

    got = (pmf[0] * np.arange(pmf.shape[1])).sum()
    assert got == pytest.approx(appearance + scoring * 2.0 * minutes_share, abs=1e-9)


def test_the_return_chance_is_the_poisson_zero():
    """Analytic, so it is a sharper check than a brute force. It is read off
    the rates rather than off the points grid because a starter who does
    nothing still banks his appearance, so no return and no points differ."""
    players = pd.DataFrame(
        {
            "xg90": [0.5],
            "xa90": [0.3],
            "start_chance": [0.7],
            "sub_chance": [0.2],
            "starter_minutes": [0.85],
        },
        index=[1],
    )
    fixtures = pd.DataFrame({"id": [1, 1], "multiplier": [1.1, 0.9]})

    involvement = 0.8 * 2.0
    quiet = 0.7 * math.exp(-involvement * 0.85) + 0.2 * math.exp(-involvement * SUB_SHARE) + 0.1
    assert _return_chance(players, fixtures).iloc[0] == pytest.approx(1 - quiet, abs=1e-12)


def test_more_expected_goals_is_never_a_worse_haul_chance():
    """The property the whole thing exists to have."""
    chances = [_one_player([1.0], xg=xg)[0, HAUL_POINTS:].sum() for xg in (0.1, 0.4, 0.8, 1.2)]
    assert chances == sorted(chances)
    assert chances[0] < chances[-1]


def test_a_double_gameweek_totals_its_fixtures_rather_than_picking_one():
    """A double is worth more than a single, and by more than two chances at
    the same event would give. The bar is on the week rather than on either
    match, so a five and a five is a haul while neither half of it is, and the
    second appearance lowers what the goals have to find by two more. Anyone
    reaching for a union bound here will get a number that is too small."""
    single = _one_player([1.0])[0, HAUL_POINTS:].sum()
    double = _one_player([1.0, 1.0])[0, HAUL_POINTS:].sum()

    two_shots = 1 - (1 - single) ** 2
    assert double > single
    assert double > two_shots, "points carry across the week, they do not reset"


def test_the_role_is_drawn_once_for_the_gameweek():
    """A player who starts seven weeks in ten does not start both legs of a
    double 49% of the time. Drawing the role per fixture would say he does, and
    it would understate exactly the week a Triple Captain is weighed for."""
    certain = _one_player([1.0, 1.0], start_chance=1.0, sub_chance=0.0)
    split = _one_player([1.0, 1.0], start_chance=0.7, sub_chance=0.2)

    # the chance he never features is the leftover, and it sits at nought points
    assert split[0, 0] > 0.1
    assert certain[0, 0] == pytest.approx(0.0, abs=1e-9)
    assert split[0, HAUL_POINTS:].sum() < certain[0, HAUL_POINTS:].sum()


# ----------------------------------------------------------------------
# the gate, which is the part that goes wrong in August
# ----------------------------------------------------------------------
def test_nothing_is_offered_before_the_season_has_started(season: Season):
    """The one that matters. Before the first deadline the API serves last
    season's minutes and expected goals under the same field names, so the 270
    minute gate is wide open and a haul chance built on it would be a year old.

    `FakeApi` zeroes those fields pre-season and the real payload does not, so
    trusting the fixture here would prove nothing. The stale values go in by
    hand, the same way `test_the_new_scoring_terms_are_inert_before_the_first_deadline`
    does it.
    """
    if season.gameweeks_played:
        pytest.skip("this is the pre-season half of the fixture")

    projections, by_gw = project(season, horizon=6)
    assert haul_frame(season, projections, by_gw).empty

    stale = season.players
    stale["minutes"] = 3000
    stale["expected_goals"] = 12.0
    stale["expected_assists"] = 8.0

    frame = haul_frame(season, projections, by_gw)
    assert frame.empty, "last season's expected goals reached a haul chance"
    assert list(frame.columns) == COLUMNS, "a caller should not have to guess the shape"


def test_the_frame_is_a_table_of_probabilities(season: Season, projections: pd.DataFrame):
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    _, by_gw = project(season, horizon=6)
    frame = haul_frame(season, projections, by_gw)

    assert len(frame)
    assert list(frame.columns) == COLUMNS
    for column in ("haul_chance", "return_chance"):
        assert frame[column].notna().all()
        assert frame[column].between(0, 1).all()
    assert (frame["haul_chance"] <= frame["return_chance"] + 1e-12).all(), (
        "a haul without a return is not a thing that can happen"
    )
    assert frame["haul_chance"].is_monotonic_decreasing, "best haul chance first"


def test_a_blank_gameweek_has_no_row(season: Season, projections: pd.DataFrame):
    """A club with no fixture drops out on the join rather than through a test
    on how many matches the week holds."""
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    _, by_gw = project(season, horizon=6)
    event = int(by_gw["event"].min())
    blanked = int(projections["team"].iloc[0])
    idle = projections.index[projections["team"] == blanked]

    without = by_gw[~(by_gw["id"].isin(idle) & (by_gw["event"] == event))]
    frame = haul_frame(season, projections, without, event=event)
    assert not frame.index.isin(idle).any()
    assert len(frame)


def test_a_player_short_of_the_minutes_gate_is_left_out(season: Season, projections: pd.DataFrame):
    """Dropped rather than carried as NaN. A column of blanks sorts badly and
    reads as broken, and there is nothing honest to put in it."""
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    from fpl_manager.projections import COMPONENT_MINUTES

    _, by_gw = project(season, horizon=6)
    short = season.players.index[season.players["minutes"] < COMPONENT_MINUTES]
    frame = haul_frame(season, projections, by_gw)

    assert len(short), "the fixture should have somebody who has barely played"
    assert not frame.index.isin(short).any()


def test_the_chart_frame_carries_the_distribution(season: Season, projections: pd.DataFrame):
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    _, by_gw = project(season, horizon=6)
    wanted = list(haul_frame(season, projections, by_gw).index[:3])
    chart = points_pmf(season, projections, by_gw, wanted)

    assert list(chart.columns) == ["id", "points", "probability"]
    assert set(chart["id"]) == set(wanted)
    assert chart["probability"].between(0, 1).all()
    # cut for drawing, so it is short of one rather than over it
    totals = chart.groupby("id")["probability"].sum()
    assert (totals <= 1.0).all()
    assert (totals > 0.99).all()


def test_the_gameweek_can_be_chosen(season: Season, projections: pd.DataFrame):
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    _, by_gw = project(season, horizon=6)
    events = sorted(int(e) for e in by_gw["event"].dropna().unique())
    later = haul_frame(season, projections, by_gw, event=events[2])
    assert (later["event"] == events[2]).all()
    assert haul_frame(season, projections, by_gw)["event"].eq(events[0]).all()


def test_the_solved_distribution_is_a_distribution(season: Season, projections: pd.DataFrame):
    """Every row, not just the handful the chart asks for."""
    if not season.gameweeks_played:
        pytest.skip("this is the mid-season half of the fixture")

    _, by_gw = project(season, horizon=6)
    _, pmf, _ = _distribution(season, projections, by_gw, None)
    assert (pmf >= 0).all()
    assert pmf.sum(axis=1) == pytest.approx(1.0, abs=1e-6)
