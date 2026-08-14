"""In-play scoring: bonus, autosubs and what a squad is worth right now.

The bonus tie cases below are FPL's published rules restated as assertions. If
one of them fails, `bonus_awarded` has stopped matching the game.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_manager.data import Season, is_legal_xi
from fpl_manager.live import (
    LiveGameweek,
    bonus_awarded,
    load_live,
    provisional_bonus,
    resolve_autosubs,
    score_entry,
)

from .conftest import FakeApi


# ----------------------------------------------------------------------
# bonus
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("bps", "expected", "rule"),
    [
        ([30, 30, 25], [3, 3, 1], "two tie for top, both get 3 and the next gets 1"),
        ([30, 30, 30, 25], [3, 3, 3, 0], "three tie for top and take everything"),
        ([30, 25, 25, 20], [3, 2, 2, 0], "two tie for second, both get 2 and no third"),
        ([30, 25, 20, 20], [3, 2, 1, 1], "two tie for third and both get 1"),
    ],
)
def test_bonus_follows_the_published_tie_rules(bps, expected, rule):
    awarded = bonus_awarded(pd.Series(bps, index=range(len(bps))))
    assert list(awarded) == expected, rule


def test_bonus_of_an_empty_fixture_is_empty():
    assert bonus_awarded(pd.Series(dtype="float64")).empty


def _fixture(fid: int, *, started: bool, bonus: bool, bps: dict[int, int]) -> dict:
    stats = [{"identifier": "bps", "h": [{"element": e, "value": v} for e, v in bps.items()]}]
    if bonus:
        stats.append({"identifier": "bonus", "h": [{"element": next(iter(bps)), "value": 3}]})
    return {
        "id": fid,
        "team_h": 1,
        "team_a": 2,
        "started": started,
        "finished": bonus,
        "finished_provisional": bonus,
        "stats": stats,
    }


def test_a_fixture_that_has_not_started_awards_no_provisional_bonus():
    fixture = _fixture(1, started=False, bonus=False, bps={10: 30, 11: 20})
    assert provisional_bonus([fixture]).empty


def test_provisional_bonus_stops_once_the_real_bonus_lands():
    """The view adds provisional to the bonus the API reports, so leaving it on
    after the points are applied would count the same bonus twice."""
    fixture = _fixture(1, started=True, bonus=True, bps={10: 30, 11: 20})
    assert provisional_bonus([fixture]).empty


def test_provisional_bonus_is_awarded_while_a_fixture_is_in_play():
    fixture = _fixture(1, started=True, bonus=False, bps={10: 30, 11: 25, 12: 20})
    awarded = provisional_bonus([fixture])
    assert awarded.to_dict() == {10: 3, 11: 2, 12: 1}


def test_a_player_with_no_bonus_points_score_gets_nothing():
    """A zero means he has not been on the pitch, not that he is third best."""
    fixture = _fixture(1, started=True, bonus=False, bps={10: 30, 11: 0, 12: 0})
    assert provisional_bonus([fixture]).to_dict() == {10: 3}


def test_a_double_gameweek_ranks_each_fixture_separately():
    """Bonus is awarded per match. Ranking a player's summed score across two
    fixtures would award one set of bonus for two games, and would rank him
    against players he never shared a pitch with."""
    first = _fixture(1, started=True, bonus=False, bps={10: 30, 11: 25, 12: 20})
    second = _fixture(2, started=True, bonus=False, bps={10: 30, 20: 25, 21: 20})

    awarded = provisional_bonus([first, second])
    assert awarded[10] == 6, "top of both fixtures is two lots of three"
    assert awarded[11] == 2
    assert awarded[20] == 2


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------
def test_load_live_survives_every_endpoint_being_empty():
    """Out of season the live endpoints return nothing. A live view that raises
    rather than saying so takes the whole page down with it."""

    class Empty(FakeApi):
        def live(self, gameweek: int) -> dict:
            return {}

        def fixtures_for_event(self, gameweek: int) -> list[dict]:
            return []

    season = Season(Empty(played=0))
    live = load_live(season, 1)

    assert live.elements.empty
    assert live.fixtures.empty
    assert not live.in_play
    assert not live.all_settled
    assert live.points(1) == 0


def test_load_live_reads_a_gameweek_in_progress(season: Season):
    live = load_live(season, season.next_gameweek)

    assert live.in_play, "the fake has one fixture started and unfinished"
    assert not live.all_settled
    assert (live.elements["minutes"] >= 0).all()


def test_a_blank_club_counts_as_settled(season: Season):
    """Waiting for a match that does not exist would leave a blank gameweek
    provisional forever."""
    live = load_live(season, season.next_gameweek)
    unfinished = live.fixtures[~live.fixtures["finished"]]
    busy = set(unfinished["team_h"]).union(unfinished["team_a"])
    idle = [t for t in season.teams.index if t not in busy]

    assert idle, "the fake should leave some clubs without an unfinished fixture"
    assert live.settled(pd.Series(idle, index=idle)).all()


# ----------------------------------------------------------------------
# formations
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("shape", "legal"),
    [
        (["GKP"] + ["DEF"] * 3 + ["MID"] * 4 + ["FWD"] * 3, True),
        (["GKP"] + ["DEF"] * 5 + ["MID"] * 4 + ["FWD"], True),
        (["GKP"] + ["DEF"] * 4 + ["MID"] * 5 + ["FWD"], True),
        (["GKP"] + ["DEF"] * 2 + ["MID"] * 5 + ["FWD"] * 3, False),
        (["GKP"] * 2 + ["DEF"] * 3 + ["MID"] * 4 + ["FWD"] * 2, False),
        (["GKP"] + ["DEF"] * 3 + ["MID"] * 4 + ["FWD"] * 2, False),
    ],
)
def test_formation_legality(shape, legal):
    assert is_legal_xi(shape) is legal


# ----------------------------------------------------------------------
# autosubs
# ----------------------------------------------------------------------
def _squad(shape: list[str]) -> tuple[pd.DataFrame, pd.Series]:
    """Fifteen picks in order, with positions. Ids are the pick positions."""
    picks = pd.DataFrame({"element": range(1, 16)}, index=range(1, 16))
    return picks, pd.Series(shape, index=range(1, 16))


BASE_SHAPE = (
    ["GKP"]
    + ["DEF"] * 4
    + ["MID"] * 4
    + ["FWD"] * 2  # the XI, a 4-4-2
    + ["GKP", "DEF", "MID", "FWD"]  # the bench
)


def _all_settled() -> pd.Series:
    return pd.Series(True, index=range(1, 16))


def test_a_starter_who_played_is_never_replaced():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())
    assert lineup.subs == []
    assert lineup.starters == list(range(1, 12))


def test_the_first_eligible_bench_player_comes_on_not_the_best():
    """FPL walks the bench in the order the manager set it. Picking the highest
    scorer instead would field a better XI than the one that actually stands."""
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[10] = 0  # a starting forward blanked
    minutes[12] = 0  # the reserve keeper did not play either

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())
    assert lineup.subs == [(10, 13)], "13 is the first outfield bench player who played"


def test_a_substitution_that_would_break_the_formation_is_skipped():
    """Coming on for the third defender in a 4-4-2 with another forward would
    leave three at the back minus one, which FPL will not field."""
    shape = (
        ["GKP"]
        + ["DEF"] * 3
        + ["MID"] * 4
        + ["FWD"] * 3  # a 3-4-3, so the defence is already at its minimum
        + ["GKP", "FWD", "DEF", "MID"]
    )
    picks, positions = _squad(shape)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[4] = 0  # the third defender blanked

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())
    assert lineup.subs == [(4, 14)], "the forward at 13 is skipped for the defender at 14"


def test_the_reserve_keeper_only_ever_replaces_the_keeper():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[1] = 0  # the keeper blanked

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())
    assert lineup.subs == [(1, 12)]


def test_an_outfield_blank_never_brings_the_keeper_on():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[11] = 0
    minutes[13] = 0
    minutes[14] = 0
    minutes[15] = 0

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())
    assert lineup.subs == [], "only the reserve keeper is left and he cannot come on"


def test_nothing_is_substituted_while_a_match_is_still_being_played():
    """A player on zero minutes at half past three has not blanked, he has not
    started yet. Subbing him now and undoing it later is worse than waiting."""
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[10] = 0
    settled = _all_settled()
    settled[10] = False

    lineup = resolve_autosubs(picks, minutes, positions, settled)
    assert lineup.subs == []
    assert not lineup.settled


def test_the_armband_moves_to_the_vice_when_the_captain_blanks():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[2] = 0

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled(), captain=2, vice_captain=3)
    assert lineup.captain == 3
    assert lineup.captain_multiplier == 2


def test_both_the_captain_and_vice_blanking_drops_the_multiplier():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[2] = 0
    minutes[3] = 0

    lineup = resolve_autosubs(picks, minutes, positions, _all_settled(), captain=2, vice_captain=3)
    assert lineup.captain_multiplier == 1


def test_the_armband_waits_while_the_vice_still_has_a_match_to_play():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    minutes[2] = 0
    minutes[3] = 0
    settled = _all_settled()
    settled[3] = False

    lineup = resolve_autosubs(picks, minutes, positions, settled, captain=2, vice_captain=3)
    assert lineup.captain_multiplier == 2, "the vice may still score, so nothing is decided"


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------
def _live(points: dict[int, int], provisional: dict[int, int] | None = None) -> LiveGameweek:
    elements = pd.DataFrame(
        {
            "minutes": {e: 90 for e in points},
            "total_points": points,
            "bonus": {e: 0 for e in points},
            "bps": {e: 0 for e in points},
        }
    )
    return LiveGameweek(
        gameweek=1,
        elements=elements,
        fixtures=pd.DataFrame(),
        provisional=pd.Series(provisional or {}, dtype="int64"),
    )


def test_the_captain_is_doubled_exactly_once():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    lineup = resolve_autosubs(picks, minutes, positions, _all_settled(), captain=2)

    live = _live({e: 2 for e in range(1, 16)})
    assert score_entry(live, lineup).total == 11 * 2 + 2


def test_provisional_bonus_reaches_the_total_and_is_reported():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    lineup = resolve_autosubs(picks, minutes, positions, _all_settled(), captain=2)

    live = _live({e: 2 for e in range(1, 16)}, provisional={2: 3})
    score = score_entry(live, lineup)

    assert score.provisional_bonus == 6, "the captain's provisional bonus doubles too"
    assert score.total == 11 * 2 + 2 + 3 * 2


def test_a_bench_player_does_not_score():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())

    live = _live({e: 100 if e > 11 else 2 for e in range(1, 16)})
    assert score_entry(live, lineup).total == 22


def test_scoring_a_loaded_squad_end_to_end(season: Season):
    """The bridge both front ends call, so neither has to know that pick order
    is the bench order or that substitutions wait on the final whistle."""
    from fpl_manager.live import score_squad
    from fpl_manager.squad import MySquad

    players = season.players
    owned: list[int] = []
    for position, count in (("GKP", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)):
        owned += list(players.index[players["position"] == position][:count])
    # a legal XI has to come first, since pick order is what starts
    order = [p for p in owned if players.loc[p, "position"] != "GKP"]
    squad_ids = [owned[0], *order[:10], owned[1], *order[10:]]

    live = load_live(season, season.next_gameweek)
    squad = MySquad(player_ids=squad_ids, captain_id=squad_ids[1], vice_captain_id=squad_ids[2])
    score = score_squad(live, season, squad)

    assert len(score.lineup.starters) == 11
    assert score.playing + score.to_play == 11
    assert score.total >= 0


def test_players_still_to_play_are_counted():
    picks, positions = _squad(BASE_SHAPE)
    minutes = pd.Series(90, index=range(1, 16))
    lineup = resolve_autosubs(picks, minutes, positions, _all_settled())

    live = _live({e: 2 for e in range(1, 12)})
    live.elements.loc[5, "minutes"] = 0
    score = score_entry(live, lineup)

    assert score.playing == 10
    assert score.to_play == 1
