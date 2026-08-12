"""The squad you actually own: fifteen ids, money in the bank, selling prices.

This module knows nothing about projections or optimisation. It answers one
question, which is what you currently hold and what you could raise by selling
it, and hands that to the optimiser as plain numbers.

Two sources, because neither is sufficient on its own. The public API gives
picks only after a deadline has passed and never gives selling prices, since
those depend on what you paid. A local squad file covers both gaps but has to
be kept up to date by hand. Reading an entry and falling back to the file is the
usual arrangement.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .data import Season

SQUAD_SIZE = 15


def selling_price_tenths(purchase_tenths: int, now_tenths: int) -> int:
    """What FPL pays you for a player, in tenths.

    You get back what you paid plus half of any rise, rounded down to the
    nearest 0.1. A fall is absorbed in full, so a player who has dropped is
    worth their current price. Working in tenths keeps the rounding exact,
    which matters because the difference between 4.4 and 4.5 decides whether a
    transfer is affordable.
    """
    if now_tenths <= purchase_tenths:
        return int(now_tenths)
    return int(purchase_tenths + (now_tenths - purchase_tenths) // 2)


@dataclass
class MySquad:
    """Fifteen owned players plus the money and transfers available.

    `purchase_prices` is what you paid, in tenths. It is the only part that
    cannot be recovered from the public API, so it lives in the squad file and
    is the reason for keeping one.
    """

    player_ids: list[int]
    bank_tenths: int = 0
    free_transfers: int = 1
    purchase_prices: dict[int, int] = field(default_factory=dict)
    explicit_selling: dict[int, int] = field(default_factory=dict)
    entry_id: int | None = None
    gameweek: int | None = None
    _selling_prices: dict[int, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        # a squad file may carry selling prices and no season to derive from
        if self.explicit_selling and not self._selling_prices:
            self._selling_prices = dict(self.explicit_selling)

    @property
    def bank(self) -> float:
        return self.bank_tenths / 10

    @property
    def selling_prices(self) -> dict[int, int]:
        """Selling price per owned player, in tenths.

        Empty for any player whose purchase price is unknown. The optimiser
        treats a missing entry as current price, which overstates your spending
        power slightly, so it is better to record what you paid than to guess.
        """
        return dict(self._selling_prices)

    def resolve_selling_prices(self, season: Season) -> dict[int, int]:
        """Work out selling prices from purchases and today's prices.

        A selling price written down explicitly wins over a derived one, since
        the site shows you the real figure and that is worth more than any
        reconstruction of it.
        """
        now = season.players["now_cost"]
        resolved = dict(self.explicit_selling)
        for pid in self.player_ids:
            if pid in resolved:
                continue
            paid = self.purchase_prices.get(pid)
            if paid is None or pid not in now.index:
                continue
            resolved[pid] = selling_price_tenths(int(paid), int(now.loc[pid]))
        self._selling_prices = resolved
        return resolved

    def value_tenths(self, season: Season) -> int:
        """Team value, which is what the squad would raise plus the bank."""
        selling = self.resolve_selling_prices(season)
        now = season.players["now_cost"]
        total = 0
        for pid in self.player_ids:
            if pid in selling:
                total += selling[pid]
            elif pid in now.index:
                total += int(now.loc[pid])
        return total + self.bank_tenths

    def frame(self, projections: pd.DataFrame) -> pd.DataFrame:
        """The owned players as rows of a projections frame.

        Ids that are not in the projections are dropped rather than raising,
        because a player can leave the game mid-season and a stale squad file
        should still be usable.
        """
        known = [i for i in self.player_ids if i in projections.index]
        return projections.loc[known].copy()

    def missing_from(self, projections: pd.DataFrame) -> list[int]:
        """Owned ids the projections do not know about, for warning on."""
        return [i for i in self.player_ids if i not in projections.index]

    def unpriced(self) -> list[int]:
        """Owned ids with no selling price, so valued at today's price.

        The optimiser assumes current price for these, which overstates what
        you could raise by selling them. Front ends use this to say so rather
        than quietly planning with money that is not there.
        """
        known = self._selling_prices
        return [i for i in self.player_ids if i not in known]

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> MySquad:
        """A squad from a built 15, priced at what it would cost today.

        Today's price is the right purchase price at the moment you make the
        transfer and drifts from then on, which is the same assumption
        `write_squad_file` documents. It exists so a freshly built squad can be
        written out with its money intact.
        """
        return cls(
            player_ids=[int(i) for i in frame.index],
            purchase_prices={int(i): int(cost) for i, cost in frame["now_cost"].items()},
        )


# ----------------------------------------------------------------------
# squad files
# ----------------------------------------------------------------------
def _to_tenths(value) -> int:
    """Read a price written either as 14.2 or as 142.

    No player has ever cost 30.0m and none has ever cost 0.3m, so the gap
    between the two scales is wide enough to tell them apart. This exists so a
    hand-written squad file can use the numbers shown on the site.
    """
    number = float(value)
    return round(number * 10) if number < 30 else round(number)


def _parse_players(raw) -> tuple[list[int], dict[int, int], dict[int, int]]:
    """Accept either a list of ids or a list of objects carrying prices.

    The bare list is the least tedious thing to type by hand, and files the app
    downloaded before it started writing prices are still in that form. The
    object form is the one worth keeping, since it carries the money.
    """
    ids: list[int] = []
    purchases: dict[int, int] = {}
    selling: dict[int, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            ids.append(int(item))
            continue
        pid = int(item["id"])
        ids.append(pid)
        paid = item.get("purchase_price", item.get("bought_for"))
        if paid is not None:
            purchases[pid] = _to_tenths(paid)
        sells = item.get("selling_price", item.get("sell_price"))
        if sells is not None:
            selling[pid] = _to_tenths(sells)
    return ids, purchases, selling


def parse_squad(payload, season: Season | None = None) -> MySquad:
    """Build a squad from already-read JSON.

    Separate from `load_squad_file` because an upload arrives as bytes, and a
    web front end serving several people at once must not stage it through a
    file on the server. Passing a season resolves selling prices immediately.
    Without one the squad still loads and the optimiser falls back to current
    prices.
    """
    if isinstance(payload, list):
        payload = {"players": payload}

    raw_players = payload.get("players") or payload.get("player_ids") or []
    ids, purchases, selling = _parse_players(raw_players)
    if not ids:
        raise ValueError("That squad lists no players.")

    bank = payload.get("bank_tenths")
    if bank is None and payload.get("bank") is not None:
        bank = round(float(payload["bank"]) * 10)

    squad = MySquad(
        player_ids=ids,
        bank_tenths=int(bank or 0),
        free_transfers=int(payload.get("free_transfers", 1)),
        purchase_prices=purchases,
        explicit_selling=selling,
        entry_id=payload.get("entry_id", payload.get("entry")),
        gameweek=payload.get("gameweek"),
    )
    if season is not None:
        squad.resolve_selling_prices(season)
    return squad


def load_squad_file(path: Path | str, season: Season | None = None) -> MySquad:
    """Read a squad from disk."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"No squad file at {path}. Create one with `fpl-manager build --save {path.name}`."
        )
    try:
        return parse_squad(json.loads(path.read_text()), season)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def squad_payload(squad: MySquad, season: Season | None = None) -> dict:
    """A squad as the JSON `parse_squad` reads back.

    Purchase prices default to today's price, which is correct at the moment
    you make the transfer and drifts from then on. That is the intended use:
    save when you buy, not weeks later.
    """
    players_frame = season.players if season is not None else None

    players = []
    for pid in squad.player_ids:
        entry: dict = {"id": int(pid)}
        if players_frame is not None and pid in players_frame.index:
            row = players_frame.loc[pid]
            # a label for whoever edits this file, ignored when reading it back
            entry["name"] = f"{row['web_name']} ({row['club']})"
        paid = squad.purchase_prices.get(pid)
        if paid is None and players_frame is not None and pid in players_frame.index:
            paid = int(players_frame.loc[pid, "now_cost"])
        if paid is not None:
            entry["purchase_price"] = round(int(paid) / 10, 1)
        if pid in squad.explicit_selling:
            entry["selling_price"] = round(squad.explicit_selling[pid] / 10, 1)
        players.append(entry)

    return {
        "entry_id": squad.entry_id,
        "gameweek": squad.gameweek or (season.next_gameweek if season is not None else None),
        "bank": round(squad.bank_tenths / 10, 1),
        "free_transfers": int(squad.free_transfers),
        "players": players,
    }


def write_squad_file(path: Path | str, squad: MySquad, season: Season | None = None) -> Path:
    """Write a squad to disk in the form `load_squad_file` reads back."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(squad_payload(squad, season), indent=2))
    return path


# ----------------------------------------------------------------------
# live entries
# ----------------------------------------------------------------------
def _estimate_free_transfers(history: dict, up_to_gw: int) -> int:
    """Best guess at free transfers, since the API does not expose them.

    Transfers now roll up to five, so a run of quiet weeks is worth counting.
    This reads backwards through finished gameweeks and adds one per week with
    no transfers made. It cannot see a wildcard played, so treat it as a
    starting value to correct rather than an answer.
    """
    events = history.get("current") or []
    banked = 1
    for row in reversed(events):
        if int(row.get("event", 0)) > up_to_gw:
            continue
        if int(row.get("event_transfers", 0)) > 0:
            break
        banked += 1
        if banked >= 5:
            break
    return min(banked, 5)


def load_from_entry(season: Season, entry_id: int, gameweek: int | None = None) -> MySquad:
    """Read a squad from a public FPL entry.

    Picks are only published once a deadline has passed, so this raises before
    the first deadline of a season and the caller should fall back to a squad
    file. Selling prices are not public either, so they come back empty and the
    plan will be slightly optimistic about spending power.
    """
    if entry_id is None:
        raise ValueError("No entry id given and no squad file to fall back on.")

    gameweek = gameweek or season.gameweeks_played
    if gameweek < 1:
        raise RuntimeError(
            "No gameweek has finished yet, so the FPL API publishes no picks. "
            "Use a squad file instead."
        )

    try:
        picks_payload = season.api.entry_picks(int(entry_id), int(gameweek))
    except Exception as exc:
        raise RuntimeError(
            f"Could not read picks for entry {entry_id} in GW{gameweek}. "
            "Entries are only public after a deadline, and the id must be correct. "
            f"({exc})"
        ) from exc

    picks = picks_payload.get("picks") or []
    ids = [int(p["element"]) for p in picks if p.get("element") is not None]
    if len(ids) != SQUAD_SIZE:
        raise RuntimeError(f"Expected {SQUAD_SIZE} picks for entry {entry_id}, got {len(ids)}.")

    bank = (picks_payload.get("entry_history") or {}).get("bank")
    if bank is None:
        try:
            bank = (season.api.entry(int(entry_id)) or {}).get("last_deadline_bank")
        except Exception:
            bank = None

    free = 1
    with contextlib.suppress(Exception):
        free = _estimate_free_transfers(season.api.entry_history(int(entry_id)), gameweek)

    return MySquad(
        player_ids=ids,
        bank_tenths=int(bank or 0),
        free_transfers=free,
        entry_id=int(entry_id),
        gameweek=int(gameweek),
    )


def merge_prices(squad: MySquad, saved: MySquad, season: Season | None = None) -> MySquad:
    """Take what you paid from a squad file into a squad read from an entry.

    The entry knows who you own, the file is the only source of what you paid
    for them. Prices for players you no longer hold are dropped, so an out of
    date file contributes what it still can rather than being rejected whole.

    Both front ends go through this so they cannot disagree about how the two
    sources combine.
    """
    held = set(squad.player_ids)
    squad.purchase_prices = {
        pid: paid for pid, paid in saved.purchase_prices.items() if pid in held
    }
    squad.explicit_selling = {
        pid: sell for pid, sell in saved.explicit_selling.items() if pid in held
    }
    if season is not None:
        squad.resolve_selling_prices(season)
    return squad


def load_squad(
    season: Season, path: Path | str | None = None, entry_id: int | None = None
) -> MySquad:
    """Load from an entry if one is given and readable, otherwise from a file.

    This is the order the front ends want: live data is more current, but it is
    unavailable pre-season and carries no purchase prices, so the file wins
    whenever the entry cannot be read.
    """
    if entry_id:
        try:
            squad = load_from_entry(season, entry_id)
            if path and Path(path).exists():
                merge_prices(squad, load_squad_file(path))
            squad.resolve_selling_prices(season)
            return squad
        except (RuntimeError, ValueError):
            if not path:
                raise
    if not path:
        raise ValueError("Give either a squad file or an entry id.")
    return load_squad_file(path, season)
