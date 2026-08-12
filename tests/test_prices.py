"""Tests for price change pressure.

The thresholds are heuristics and not worth pinning to exact values, so these
assert the properties that have to hold whatever the constants are set to: the
signal is relative to ownership, it is silent before the season starts, and it
never claims a direction it has no evidence for.
"""

from __future__ import annotations

import pytest

from fpl_manager.data import Season
from fpl_manager.prices import (
    FALL_PRESSURE,
    MIN_OWNERS,
    RISE_PRESSURE,
    is_dormant,
    movers,
    price_pressure,
)


def test_every_player_gets_a_row(season: Season):
    frame = price_pressure(season)
    assert list(frame.index) == list(season.players.index)


def test_direction_follows_the_thresholds(season: Season):
    frame = price_pressure(season)
    assert (frame.loc[frame["direction"] == "rise", "pressure"] >= RISE_PRESSURE).all()
    assert (frame.loc[frame["direction"] == "fall", "pressure"] <= FALL_PRESSURE).all()
    holds = frame[frame["direction"] == "hold"]["pressure"]
    assert ((holds > FALL_PRESSURE) & (holds < RISE_PRESSURE)).all()


def test_the_same_net_transfers_matter_more_to_a_differential(season: Season):
    """The point of dividing by owners. A template pick barely moves on 50k."""
    players = season.players
    widely, barely = (
        players["selected_by_percent"].idxmax(),
        players["selected_by_percent"].idxmin(),
    )
    if players.loc[widely, "selected_by_percent"] == players.loc[barely, "selected_by_percent"]:
        pytest.skip("synthetic season gave every player the same ownership")

    season.players.loc[[widely, barely], "transfers_in_event"] = 50_000
    season.players.loc[[widely, barely], "transfers_out_event"] = 0
    frame = price_pressure(season)
    assert frame.loc[barely, "pressure"] > frame.loc[widely, "pressure"]


def test_a_barely_owned_player_does_not_read_as_a_certainty(season: Season):
    """Dividing by a near zero owner base would make any transfer look decisive."""
    pid = int(season.players["selected_by_percent"].idxmin())
    season.players.loc[pid, "selected_by_percent"] = 0.0
    season.players.loc[pid, "transfers_in_event"] = 500
    season.players.loc[pid, "transfers_out_event"] = 0
    assert price_pressure(season).loc[pid, "pressure"] == pytest.approx(500 / MIN_OWNERS)


def test_nothing_moves_before_the_season_starts(season: Season):
    """Pre-season every counter is zero, and that is dormant rather than a hold."""
    if season.gameweeks_played:
        pytest.skip("this is the mid-season parametrisation")
    frame = price_pressure(season)
    assert is_dormant(frame)
    assert (frame["direction"] == "hold").all()


def test_movers_are_ordered_by_how_close_they_are(season: Season):
    season.players["transfers_in_event"] = 400_000
    season.players["transfers_out_event"] = 0
    rising = movers(price_pressure(season), "rise", top=5)
    assert len(rising) == 5
    assert rising["pressure"].is_monotonic_decreasing


def test_an_unknown_direction_is_rejected(season: Season):
    with pytest.raises(ValueError, match="rise or fall"):
        movers(price_pressure(season), "sideways")


def test_a_season_without_price_fields_says_so(season: Season):
    season.players = season.players.drop(columns=["transfers_in_event"])
    with pytest.raises(KeyError, match="transfers_in_event"):
        price_pressure(season)
