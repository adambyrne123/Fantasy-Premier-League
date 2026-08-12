"""Manager and league parsing.

The pre-season cases here are not hypothetical. Every one of them is what the
live API actually returns before a ball is kicked, which was checked against it
while this was written: profiles and past seasons are there, ranks and league
tables are not.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_manager.data import Season
from fpl_manager.leagues import leagues_of, load_manager, past_seasons, standings


def test_manager_profile_is_read(season: Season):
    manager = load_manager(season, 1)
    assert manager.entry_id == 1
    assert manager.name == "Manager1 Example"
    assert manager.team_name == "Team 1"
    assert manager.overall_points == 1001


def test_a_manager_with_no_rank_yet_is_not_a_zero(season: Season):
    """Pre-season the API sends a null overall rank. Coercing that to 0 would
    show every manager as the best in the world."""
    manager = load_manager(season, 1)
    if season.gameweeks_played == 0:
        assert manager.overall_rank is None
    else:
        assert manager.overall_rank and manager.overall_rank > 0


def test_past_seasons_come_back_oldest_first(season: Season):
    past = past_seasons(season, 1)
    assert list(past["season_name"]) == ["2023/24", "2024/25", "2025/26"]
    assert past["total_points"].is_monotonic_increasing


def test_a_first_season_manager_has_no_past(season: Season):
    """Empty is the honest answer, not an error."""
    past = past_seasons(season, 2)
    assert past.empty
    assert list(past.columns) == ["season_name", "total_points", "rank"]


def test_leagues_put_the_ones_you_joined_first(season: Season):
    """FPL puts everyone in leagues by club and country. The one someone
    actually set up is the one they came to look at."""
    frame = leagues_of(season, 1)
    assert not frame["system"].iloc[0], "a joined league should sort above a system one"
    assert frame["system"].iloc[-1]
    assert set(frame["id"]) == {901, 314}


def test_an_unscored_league_is_empty_but_still_shaped(season: Season):
    """The real endpoint returns no rows until a gameweek is scored, and the
    front end still has to render a table."""
    if season.gameweeks_played:
        pytest.skip("only the pre-season case is empty")
    table, info = standings(season, 901)
    assert table.empty
    assert "movement" in table.columns
    assert info["name"] == "League 901"


def test_a_scored_league_ranks_and_measures_movement(season: Season):
    if not season.gameweeks_played:
        pytest.skip("nothing is scored pre-season")
    table, _ = standings(season, 901)
    assert list(table["rank"]) == [1, 2, 3, 4, 5]
    assert table["manager"].notna().all()
    # row two went from 4th to 2nd, so it climbed two
    assert table.loc[1, "movement"] == 2


def test_a_new_entry_has_no_movement_rather_than_a_huge_rise(season: Season):
    """FPL marks a new entry with a last rank of zero. Subtracting that would
    read as a climb of hundreds of places."""
    if not season.gameweeks_played:
        pytest.skip("nothing is scored pre-season")
    table, _ = standings(season, 901)
    assert pd.isna(table.loc[0, "movement"])


def test_a_bad_id_is_a_message_not_a_traceback(season: Season, monkeypatch):
    """Anyone can type an id into the front end."""

    def boom(*args, **kwargs):
        raise ValueError("404 Not Found")

    monkeypatch.setattr(season.api, "entry", boom)
    with pytest.raises(RuntimeError, match="Check the id is right"):
        load_manager(season, 99999)
