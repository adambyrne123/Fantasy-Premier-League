"""What the top of the table owns, and who it captains.

Two things are worth holding onto here. The denominator is the managers whose
picks actually came back, so anyone unreadable is out of both halves of every
fraction rather than counted as owning nothing. And the whole module is dormant
before a gameweek has been scored, which is not a hypothetical: league 314 has
no table and every picks endpoint is a 404 until the first one settles, so the
pre-season case is the one that will be live on the day this ships.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fpl_manager.data import SQUAD_LIMITS, XI_SIZE, Season, is_legal_xi
from fpl_manager.elite import (
    PICK_COLUMNS,
    SHARE_COLUMNS,
    elite_entries,
    elite_picks,
    field_shares,
    template_xi,
)

from .conftest import FakeApi


def sampled(season: Season) -> tuple[list[int], pd.DataFrame]:
    """The entries and their picks for the last gameweek that was scored."""
    entries = elite_entries(season)
    return entries, elite_picks(season, entries, season.gameweeks_played)


def test_entries_come_back_in_league_order(season: Season):
    entries = elite_entries(season)
    if season.gameweeks_played == 0:
        assert entries == []
    else:
        assert entries == [1001, 1002, 1003, 1004, 1005]


def test_the_sample_is_capped_at_what_was_asked_for(season: Season):
    assert len(elite_entries(season, sample=3)) <= 3


def test_every_manager_contributes_fifteen_picks(season: Season):
    entries, picks = sampled(season)
    if not entries:
        pytest.skip("no gameweek has been scored, so there are no picks to read")
    assert list(picks.columns) == PICK_COLUMNS
    assert picks["entry_id"].nunique() == len(entries)
    assert (picks.groupby("entry_id").size() == 15).all()


def test_a_manager_who_cannot_be_read_leaves_the_denominator(season: Season):
    """He is one fewer manager, not one more who owns nobody.

    Counting him as a non-owner would drag every share towards zero, which is
    the wrong direction and the easy mistake: the loop that skips him is also
    the loop that would have added him to the count.
    """
    entries = elite_entries(season)
    if not entries:
        pytest.skip("no gameweek has been scored, so there are no picks to read")

    class OneManagerMissing(FakeApi):
        def entry_picks(self, entry_id, gameweek, ttl=None):
            if entry_id == entries[0]:
                raise RuntimeError("404")
            return super().entry_picks(entry_id, gameweek, ttl=ttl)

    broken = Season(OneManagerMissing(played=season.gameweeks_played))
    picks = elite_picks(broken, entries, broken.gameweeks_played)

    assert entries[0] not in set(picks["entry_id"])
    assert picks["entry_id"].nunique() == len(entries) - 1
    # fifteen picks a manager, over the managers who resolved
    assert field_shares(picks)["elite_ownership"].sum() == pytest.approx(15.0)


def test_shares_are_shares_of_the_sample(season: Season):
    _, picks = sampled(season)
    if picks.empty:
        pytest.skip("no gameweek has been scored, so there are no picks to read")

    shares = field_shares(picks)
    assert list(shares.columns) == SHARE_COLUMNS
    for column in ("elite_ownership", "elite_start_share", "captain_share"):
        assert shares[column].between(0.0, 1.0).all()

    # every manager captains exactly one player, so the captain shares add to one
    assert shares["captain_share"].sum() == pytest.approx(1.0)
    assert shares["elite_ownership"].sum() == pytest.approx(15.0)
    assert (shares["elite_start_share"] <= shares["elite_ownership"]).all()


def test_effective_ownership_counts_the_armband():
    """The one piece of arithmetic worth pinning down exactly.

    Effective ownership is the sum of the multipliers over the sample, so a
    captain counts twice and a triple captain three times. Read against a hand
    written frame rather than a generated season, because the point is the
    number and not the plumbing.
    """
    picks = pd.DataFrame(
        [
            {"entry_id": 1, "id": 10, "slot": 1, "multiplier": 2, "is_captain": True},
            {"entry_id": 2, "id": 10, "slot": 1, "multiplier": 3, "is_captain": True},
            {"entry_id": 3, "id": 10, "slot": 12, "multiplier": 0, "is_captain": False},
            {"entry_id": 4, "id": 11, "slot": 2, "multiplier": 1, "is_captain": False},
        ]
    )
    shares = field_shares(picks)

    assert shares.loc[10, "elite_ownership"] == pytest.approx(0.75)
    # benched, so owned by three and started by two
    assert shares.loc[10, "elite_start_share"] == pytest.approx(0.50)
    assert shares.loc[10, "captain_share"] == pytest.approx(0.50)
    assert shares.loc[10, "effective_ownership"] == pytest.approx(1.25)
    assert shares.loc[11, "effective_ownership"] == pytest.approx(0.25)


def test_starting_is_read_off_the_multiplier_rather_than_the_slot():
    """After an automatic substitution the slot is a plan and the multiplier is
    what happened, so the share that matters comes off the multiplier."""
    picks = pd.DataFrame(
        [
            {"entry_id": 1, "id": 10, "slot": 14, "multiplier": 1, "is_captain": False},
            {"entry_id": 1, "id": 11, "slot": 5, "multiplier": 0, "is_captain": False},
        ]
    )
    shares = field_shares(picks)
    assert shares.loc[10, "elite_start_share"] == 1.0
    assert shares.loc[11, "elite_start_share"] == 0.0


def test_the_template_is_a_shape_fpl_would_accept(season: Season):
    _, picks = sampled(season)
    xi, bench = template_xi(field_shares(picks), season)
    if picks.empty:
        pytest.skip("no gameweek has been scored, so there is no template")

    assert len(xi) == XI_SIZE
    assert is_legal_xi(xi["position"])
    assert len(bench) == sum(SQUAD_LIMITS.values()) - XI_SIZE
    assert not set(xi.index) & set(bench.index)

    squad = pd.concat([xi, bench])
    assert squad["position"].value_counts().to_dict() == SQUAD_LIMITS
    assert xi["elite_start_share"].is_monotonic_decreasing


def test_nothing_here_raises_before_the_first_gameweek_is_scored():
    """The state this ships in. League 314 has no table and every picks
    endpoint is a 404, so each step has to return its columns and stop."""
    season = Season(FakeApi(played=0))

    entries = elite_entries(season)
    assert entries == []

    picks = elite_picks(season, [1001, 1002], gameweek=1)
    assert picks.empty
    assert list(picks.columns) == PICK_COLUMNS

    shares = field_shares(picks)
    assert shares.empty
    assert list(shares.columns) == SHARE_COLUMNS

    xi, bench = template_xi(shares, season)
    assert xi.empty and bench.empty
    assert "elite_ownership" in xi.columns
