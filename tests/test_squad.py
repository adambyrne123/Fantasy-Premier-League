"""Tests for loading the squad you own.

The money is the part worth guarding. Getting selling prices wrong does not
throw, it just quietly tells you that you can afford a player you cannot, so
each rounding case has an assertion here.
"""

from __future__ import annotations

import json

import pytest

from fpl_manager.data import Season
from fpl_manager.squad import (
    MySquad,
    load_from_entry,
    load_squad,
    load_squad_file,
    selling_price_tenths,
    write_squad_file,
)


# ----------------------------------------------------------------------
# selling prices
# ----------------------------------------------------------------------
def test_a_rise_is_returned_at_half():
    """Bought at 5.0, now 5.4, so you get 5.2 back rather than 5.4."""
    assert selling_price_tenths(50, 54) == 52


def test_an_odd_rise_rounds_down():
    """Bought at 5.0, now 5.3. Half of 0.3 rounds down to 0.1, not up to 0.2."""
    assert selling_price_tenths(50, 53) == 51


def test_a_fall_is_absorbed_in_full():
    assert selling_price_tenths(60, 55) == 55


def test_an_unchanged_price_sells_for_what_it_cost():
    assert selling_price_tenths(75, 75) == 75


def test_selling_prices_derive_from_purchases(season: Season):
    pid = int(season.players.index[0])
    now = int(season.players.loc[pid, "now_cost"])
    squad = MySquad(player_ids=[pid], purchase_prices={pid: now - 4})
    assert squad.resolve_selling_prices(season)[pid] == selling_price_tenths(now - 4, now)


def test_a_written_down_selling_price_wins(season: Season):
    """The site shows you the real figure, so prefer it to any reconstruction."""
    pid = int(season.players.index[0])
    squad = MySquad(player_ids=[pid], purchase_prices={pid: 40}, explicit_selling={pid: 123})
    assert squad.resolve_selling_prices(season)[pid] == 123


def test_players_with_no_recorded_price_are_left_out(season: Season):
    """Better to say nothing than to guess, since the optimiser has a fallback."""
    ids = [int(i) for i in season.players.index[:3]]
    squad = MySquad(player_ids=ids, purchase_prices={ids[0]: 50})
    assert set(squad.resolve_selling_prices(season)) == {ids[0]}


# ----------------------------------------------------------------------
# squad files
# ----------------------------------------------------------------------
@pytest.fixture
def owned(season: Season) -> list[int]:
    return [int(i) for i in season.players.index[:15]]


def test_round_trip_preserves_the_squad(tmp_path, season: Season, owned):
    path = tmp_path / "squad.json"
    original = MySquad(player_ids=owned, bank_tenths=17, free_transfers=2, entry_id=99)
    write_squad_file(path, original, season)

    loaded = load_squad_file(path, season)
    assert loaded.player_ids == owned
    assert loaded.bank_tenths == 17
    assert loaded.free_transfers == 2
    assert loaded.entry_id == 99


def test_saved_purchase_prices_default_to_todays_price(tmp_path, season: Season, owned):
    path = tmp_path / "squad.json"
    write_squad_file(path, MySquad(player_ids=owned), season)
    loaded = load_squad_file(path, season)
    for pid in owned:
        assert loaded.purchase_prices[pid] == int(season.players.loc[pid, "now_cost"])


def test_a_bare_list_of_ids_loads(tmp_path, season: Season, owned):
    """The app's download button writes this shape, so it has to read back."""
    path = tmp_path / "squad.json"
    path.write_text(json.dumps({"players": owned}))
    assert load_squad_file(path, season).player_ids == owned


def test_prices_written_in_millions_are_understood(tmp_path, season: Season, owned):
    """A hand-written file uses the numbers shown on the site, so 5.5 not 55."""
    path = tmp_path / "squad.json"
    path.write_text(
        json.dumps(
            {
                "bank": 1.5,
                "players": [{"id": owned[0], "purchase_price": 5.5}]
                + [{"id": p} for p in owned[1:]],
            }
        )
    )
    loaded = load_squad_file(path, season)
    assert loaded.purchase_prices[owned[0]] == 55
    assert loaded.bank_tenths == 15


def test_a_missing_file_says_how_to_make_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="build --save"):
        load_squad_file(tmp_path / "absent.json")


def test_an_empty_file_is_rejected(tmp_path):
    path = tmp_path / "squad.json"
    path.write_text(json.dumps({"players": []}))
    with pytest.raises(ValueError, match="no players"):
        load_squad_file(path)


# ----------------------------------------------------------------------
# projections and live entries
# ----------------------------------------------------------------------
def test_frame_returns_only_owned_players(season: Season, projections, owned):
    frame = MySquad(player_ids=owned).frame(projections)
    assert list(frame.index) == owned


def test_a_departed_player_does_not_break_the_frame(season: Season, projections, owned):
    """A stale squad file should still be usable after someone leaves the game."""
    squad = MySquad(player_ids=[*owned, 999_999])
    assert len(squad.frame(projections)) == len(owned)
    assert squad.missing_from(projections) == [999_999]


def test_reading_an_entry_fails_clearly_without_published_picks(season: Season):
    """Picks are not public until a deadline passes, and that must not traceback."""
    with pytest.raises(RuntimeError):
        load_from_entry(season, entry_id=1234567)


def test_load_squad_falls_back_to_the_file(tmp_path, season: Season, owned):
    path = tmp_path / "squad.json"
    write_squad_file(path, MySquad(player_ids=owned, bank_tenths=5), season)
    loaded = load_squad(season, path=path, entry_id=1234567)
    assert loaded.player_ids == owned
    assert loaded.bank_tenths == 5


def test_load_squad_needs_one_source_or_the_other(season: Season):
    with pytest.raises(ValueError):
        load_squad(season)
