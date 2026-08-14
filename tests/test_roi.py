"""Tests for realised points per million.

The division is trivial. What is worth guarding is that the module knows which
season's points it is holding, because FPL changes that underneath it without
the schema changing, and a silent switch would turn last season's verdict into
this season's without anything looking wrong.
"""

from __future__ import annotations

import pytest

from fpl_manager.data import Season
from fpl_manager.roi import (
    LAST_SEASON,
    NOTHING_YET,
    THIS_SEASON,
    best_by_position,
    points_source,
    roi_frame,
)


def test_roi_is_points_over_price(season: Season):
    frame = roi_frame(season)
    pid = int(frame.index[0])
    price = season.players.loc[pid, "now_cost"] / 10
    assert frame.loc[pid, "roi"] == pytest.approx(season.players.loc[pid, "total_points"] / price)


def test_every_player_gets_a_row(season: Season):
    assert set(roi_frame(season).index) == set(season.players.index)


def test_best_return_comes_first(season: Season):
    frame = roi_frame(season)
    assert frame["roi"].is_monotonic_decreasing


def test_a_cheaper_player_on_the_same_points_returns_more(season: Season):
    """The whole point of dividing by price."""
    cheap, dear = season.players["now_cost"].idxmin(), season.players["now_cost"].idxmax()
    season.players.loc[[cheap, dear], "total_points"] = 100
    frame = roi_frame(season)
    assert frame.loc[cheap, "roi"] > frame.loc[dear, "roi"]


def test_a_cameo_is_not_ranked(season: Season):
    """One substitute appearance can post a flattering rate."""
    pid = int(season.players.index[0])
    season.players.loc[pid, "minutes"] = 20
    season.players.loc[pid, "total_points"] = 12
    frame = roi_frame(season, min_minutes=180)
    assert not frame.loc[pid, "ranked"]
    assert pid in frame.index, "unranked players stay in the frame to be shown, not dropped"


def test_projected_value_is_carried_when_offered(season: Season, projections):
    frame = roi_frame(season, projections)
    assert "projected_roi" in frame.columns
    assert frame["gap"].equals(frame["projected_roi"] - frame["roi"])


def test_projections_are_optional(season: Season):
    assert "projected_roi" not in roi_frame(season).columns


# ----------------------------------------------------------------------
# which season's points these are
# ----------------------------------------------------------------------
def test_zero_totals_read_as_nothing_scored_yet(season: Season):
    season.players["total_points"] = 0
    assert points_source(season) == NOTHING_YET


def test_totals_without_a_finished_gameweek_are_last_seasons(season: Season):
    """FPL serves the old season's totals until it resets them at the rollover."""
    if season.gameweeks_played:
        pytest.skip("this is the mid-season parametrisation")
    season.players["total_points"] = 50
    assert points_source(season) == LAST_SEASON


def test_totals_after_a_finished_gameweek_are_this_seasons(season: Season):
    if not season.gameweeks_played:
        pytest.skip("this is the pre-season parametrisation")
    season.players["total_points"] = 50
    assert points_source(season) == THIS_SEASON


def test_best_by_position_covers_each_position(season: Season):
    season.players["minutes"] = 1000
    best = best_by_position(roi_frame(season), top=3)
    assert set(best["position"]) == {"GKP", "DEF", "MID", "FWD"}
    assert (best.groupby("position").size() <= 3).all()


def test_best_by_position_ignores_players_who_barely_played(season: Season):
    season.players["minutes"] = 10
    assert best_by_position(roi_frame(season)).empty
