"""Tests for chip timing.

A chip gain is a difference between two solves, which makes it easy to get
subtly wrong in a way that still looks plausible. These assert the properties
that must hold whatever the projection says: no chip can be worth less than
nothing, and each one is worth exactly the thing it adds.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_manager import chips
from fpl_manager.data import Season
from fpl_manager.optimiser import build_squad, pick_xi
from fpl_manager.projections import project


@pytest.fixture
def horizon_data(season: Season, prior: pd.DataFrame):
    """Projections and the per-gameweek frame chips are scored against."""
    return project(season, horizon=6, prior=prior)


@pytest.fixture
def owned(horizon_data):
    projections = horizon_data[0]
    return [int(i) for i in build_squad(projections, budget_tenths=1000).squad.index]


@pytest.fixture
def evaluated(horizon_data, owned):
    projections, by_gw = horizon_data
    return chips.evaluate(projections, by_gw, owned, budget_tenths=1000)


# ----------------------------------------------------------------------
# shape
# ----------------------------------------------------------------------
def test_every_chip_is_priced_in_every_gameweek(horizon_data, evaluated):
    _, by_gw = horizon_data
    events = sorted(int(e) for e in by_gw["event"].unique())
    assert set(evaluated["event"]) == set(events)
    for chip in ("Bench Boost", "Triple Captain", "Free Hit", "Wildcard"):
        assert (evaluated["chip"] == chip).sum() == len(events)


def test_best_per_chip_gives_one_row_each(evaluated):
    best = chips.best_per_chip(evaluated)
    assert len(best) == len(chips.CHIPS)
    assert best["chip"].is_unique


def test_best_per_chip_picks_the_highest_gain(evaluated):
    best = chips.best_per_chip(evaluated)
    for _, row in best.iterrows():
        same = evaluated[evaluated["chip"] == row["chip"]]
        assert row["gain"] == pytest.approx(same["gain"].max())


def test_a_short_squad_is_not_priced(horizon_data, owned):
    """Eleven players have no bench, so there is nothing to compare against."""
    projections, by_gw = horizon_data
    assert chips.evaluate(projections, by_gw, owned[:11], budget_tenths=1000).empty


# ----------------------------------------------------------------------
# the gains themselves
# ----------------------------------------------------------------------
def test_no_chip_is_worth_less_than_nothing(evaluated):
    """Playing a chip is optional, so a negative gain means the maths is wrong."""
    assert (evaluated["gain"] >= -1e-6).all()


def test_bench_boost_is_worth_exactly_the_bench(horizon_data, owned, evaluated):
    projections, by_gw = horizon_data
    for _, row in evaluated[evaluated["chip"] == "Bench Boost"].iterrows():
        held = chips.gameweek_frame(projections, by_gw, int(row["event"]), owned)
        bench = pick_xi(held, points_col="xpts_gw")[1]
        assert row["gain"] == pytest.approx(bench["xpts_gw"].sum())


def test_triple_captain_is_worth_one_more_captain(horizon_data, owned, evaluated):
    projections, by_gw = horizon_data
    for _, row in evaluated[evaluated["chip"] == "Triple Captain"].iterrows():
        held = chips.gameweek_frame(projections, by_gw, int(row["event"]), owned)
        captain = pick_xi(held, points_col="xpts_gw")[2]
        assert row["gain"] == pytest.approx(captain["xpts_gw"])


def test_bench_boost_never_exceeds_the_whole_squad(horizon_data, owned, evaluated):
    projections, by_gw = horizon_data
    for _, row in evaluated[evaluated["chip"] == "Bench Boost"].iterrows():
        held = chips.gameweek_frame(projections, by_gw, int(row["event"]), owned)
        assert row["gain"] <= held["xpts_gw"].sum() + 1e-6


def test_free_hit_at_least_matches_the_squad_you_have(horizon_data, owned):
    """Your own squad is affordable at your own team value, so it is a floor."""
    projections, by_gw = horizon_data
    cost = int(projections.loc[owned, "now_cost"].sum())
    table = chips.evaluate(projections, by_gw, owned, budget_tenths=cost)
    assert (table[table["chip"] == "Free Hit"]["gain"] >= -1e-6).all()


def test_a_bigger_free_hit_budget_never_scores_less(horizon_data, owned):
    projections, by_gw = horizon_data
    cost = int(projections.loc[owned, "now_cost"].sum())
    lean = chips.evaluate(projections, by_gw, owned, budget_tenths=cost, chips=("free_hit",))
    rich = chips.evaluate(projections, by_gw, owned, budget_tenths=cost + 50, chips=("free_hit",))
    merged = lean.merge(rich, on="event", suffixes=("_lean", "_rich"))
    assert (merged["gain_rich"] >= merged["gain_lean"] - 1e-6).all()


# ----------------------------------------------------------------------
# the free hit squad itself
# ----------------------------------------------------------------------
def test_free_hit_squad_is_legal(horizon_data):
    from fpl_manager.data import MAX_PER_CLUB, SQUAD_LIMITS

    projections, by_gw = horizon_data
    event = int(by_gw["event"].min())
    result = chips.free_hit_squad(projections, by_gw, event, budget_tenths=1000)
    assert len(result.squad) == 15
    assert result.cost_tenths <= 1000
    assert result.squad["position"].value_counts().to_dict() == SQUAD_LIMITS
    assert result.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_gameweek_frame_scores_a_blank_as_zero(horizon_data):
    """A club with no fixture contributes nothing, with no special casing."""
    projections, by_gw = horizon_data
    event = int(by_gw["event"].min())
    frame = chips.gameweek_frame(projections, by_gw, event)
    playing = set(by_gw[by_gw["event"] == event]["id"])
    blanks = [i for i in frame.index if i not in playing]
    assert (frame.loc[blanks, "xpts_gw"] == 0).all()


def test_a_wildcard_is_priced_over_the_rest_of_the_horizon(horizon_data, evaluated):
    """The other three buy one gameweek. A wildcard is kept, so leaving it later
    buys fewer weeks and has to be worth less, which is the whole reason it is
    not priced the same way."""
    _, by_gw = horizon_data
    events = sorted(int(e) for e in by_gw["event"].unique())
    wildcard = evaluated[evaluated["chip"] == "Wildcard"].set_index("event")

    baselines = wildcard["baseline"].reindex(events)
    assert baselines.is_monotonic_decreasing, "fewer weeks left means fewer points to compare"
    assert f"over {len(events)} weeks" in wildcard.loc[events[0], "detail"]


def test_a_wildcard_never_makes_a_squad_worse(horizon_data, evaluated):
    """It re-solves from the whole game, so it cannot do worse than what you
    already own. A negative gain means the pool or the budget is wrong."""
    wildcard = evaluated[evaluated["chip"] == "Wildcard"]
    assert (wildcard["gain"] >= -1e-9).all()


def test_window_frame_sums_the_gameweeks_it_is_given(horizon_data):
    from fpl_manager.optimiser import gameweek_frame, window_frame

    projections, by_gw = horizon_data
    events = sorted(int(e) for e in by_gw["event"].unique())

    weekly = sum(gameweek_frame(projections, by_gw, e)["xpts_gw"] for e in events[:2])
    windowed = window_frame(projections, by_gw, events[0], events[1])["xpts_gw"]
    assert (weekly - windowed).abs().max() < 1e-9
