"""What the best hundred managers actually own, and who they captain.

FPL puts everyone in one classic league, id 314, ranked by total points. Its
first pages are the top of the game, and their picks are public once a gameweek
has been scored. Aggregating them gives three things the bootstrap does not:
how owned a player is among managers who are doing well rather than among
everyone, how many of them captain him, and effective ownership.

The captaincy share is the point. `ROADMAP.md` records the useful captaincy
question as being about rank rather than points, which needs a model of what the
field captains, and notes that the API publishes ownership but not captaincy so
it would have to rest on ownership raised to some power set by eye. It does not:
a hundred squads say who is captained, measured. This module is that
measurement, and nothing more.

Read it for what it is. A hundred managers is a small sample, they are the top
of a table that rewards having been right rather than being right next week, and
it describes the last scored gameweek rather than the one being planned. It is
context to argue with, not a squad to copy.

This is a leaf, like `leagues.py`. It reshapes payloads and counts them, and
nothing that projects or picks may import it. In particular `captaincy.py` may
not: it is held to importing only `data` and `projections`, and the field
belongs in it as an argument rather than as a dependency.

Dormant until a gameweek has been scored. Before the first deadline league 314
has no table and every picks endpoint is a 404, so everything here returns an
empty frame with its declared columns. That is the API's behaviour, not a fault.
"""

from __future__ import annotations

import pandas as pd

from .data import SQUAD_LIMITS, XI_MAX, XI_MIN, XI_SIZE, Season, is_legal_xi
from .leagues import standings

# FPL's own "Overall" league, which every entry is in.
ELITE_LEAGUE = 314

# How many managers to sample. Fifty a page, so a hundred is two requests for
# the table and a hundred for the picks, which is the cost driver.
DEFAULT_SAMPLE = 100

# Picks for a settled gameweek can no longer change, so they are cached for a
# week rather than for the hour that suits a gameweek in progress. Without this
# a sample of a hundred refetches a hundred immutable payloads every hour.
SETTLED_TTL = 7 * 24 * 3600

PICK_COLUMNS = [
    "entry_id",
    "id",
    "slot",
    "multiplier",
    "is_captain",
    "is_vice_captain",
    "active_chip",
]

SHARE_COLUMNS = [
    "elite_ownership",
    "elite_start_share",
    "captain_share",
    "effective_ownership",
]

__all__ = [
    "DEFAULT_SAMPLE",
    "ELITE_LEAGUE",
    "PICK_COLUMNS",
    "SETTLED_TTL",
    "SHARE_COLUMNS",
    "elite_entries",
    "elite_picks",
    "field_shares",
    "template_xi",
]


def elite_entries(season: Season, sample: int = DEFAULT_SAMPLE) -> list[int]:
    """The entry ids of the top `sample` managers overall, best first.

    Reads whole pages through `leagues.standings`, which already knows that the
    table is empty before a gameweek has been scored and that a row need not
    carry every key. Takes however many rows come back rather than assuming
    fifty a page, so a short page ends the walk instead of skipping managers.
    """
    ids: list[int] = []
    page = 1
    while len(ids) < sample:
        table, info = standings(season, ELITE_LEAGUE, page=page)
        if table.empty or "entry_id" not in table.columns:
            break
        found = [int(value) for value in table["entry_id"].dropna()]
        if not found:
            break
        ids.extend(found)
        if not info["has_next"]:
            break
        page += 1
    return ids[:sample]


def elite_picks(
    season: Season,
    entries: list[int],
    gameweek: int,
    ttl: int = SETTLED_TTL,
) -> pd.DataFrame:
    """Every pick made by every sampled manager, one row per player per squad.

    `slot` is the 1 to 15 position as submitted, so slots 1 to 11 are the
    lineup that was named. `multiplier` is what the player actually counted
    for, which is 0 on the bench, 1 starting, 2 captained and 3 triple
    captained, and it is the one FPL updates when an automatic substitution
    happens. So `slot` says what was intended and `multiplier` says what
    happened.

    A manager whose picks cannot be read is skipped rather than counted as
    owning nothing. Nothing records how many were skipped, because the
    denominator every share divides by is the number of entries that appear
    here, so anyone missing is out of both halves of the fraction.
    """
    rows: list[dict] = []
    for entry_id in entries:
        try:
            payload = season.api.entry_picks(int(entry_id), int(gameweek), ttl=ttl)
        except Exception:
            # a manager who joined after this gameweek has no picks for it, and
            # a mid-sample failure should cost one entry rather than the sample
            continue
        picks = payload.get("picks") or []
        if not picks:
            continue
        chip = payload.get("active_chip")
        for pick in picks:
            rows.append(
                {
                    "entry_id": int(entry_id),
                    "id": int(pick.get("element")),
                    "slot": int(pick.get("position") or 0),
                    "multiplier": int(pick.get("multiplier") or 0),
                    "is_captain": bool(pick.get("is_captain")),
                    "is_vice_captain": bool(pick.get("is_vice_captain")),
                    "active_chip": chip,
                }
            )

    if not rows:
        return pd.DataFrame(columns=PICK_COLUMNS)
    return pd.DataFrame(rows)[PICK_COLUMNS]


def field_shares(picks: pd.DataFrame) -> pd.DataFrame:
    """How much of the sample owns, starts and captains each player.

    Indexed by player id, one row per player anybody owns. Every column is a
    share of the managers who resolved, so they are directly comparable with
    the `ownership` the bootstrap publishes for the whole game.

    - `elite_ownership` holds him at all, bench included
    - `elite_start_share` had him counting for something, so it reads after
      automatic substitutions rather than before them
    - `captain_share` gave him the armband
    - `effective_ownership` is the sum of the multipliers, the usual definition,
      so a captain counts twice and a triple captain three times. It is what
      decides how much a haul actually moves you against the field: everyone
      owning him means his points land in everyone's total and not only in
      yours.
    """
    if picks.empty:
        return pd.DataFrame(columns=SHARE_COLUMNS, index=pd.Index([], name="id"))

    entries = picks["entry_id"].nunique()
    grouped = picks.groupby("id")
    started = picks[picks["multiplier"] > 0]
    captained = picks[picks["is_captain"]]

    out = pd.DataFrame(index=grouped.size().index)
    out.index.name = "id"
    out["elite_ownership"] = grouped.size() / entries
    out["elite_start_share"] = started.groupby("id").size().reindex(out.index).fillna(0) / entries
    out["captain_share"] = captained.groupby("id").size().reindex(out.index).fillna(0) / entries
    out["effective_ownership"] = grouped["multiplier"].sum() / entries
    return out[SHARE_COLUMNS].sort_values("elite_ownership", ascending=False)


def template_xi(shares: pd.DataFrame, season: Season) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The fifteen the field owns most, split into the eleven it starts most.

    Description rather than selection. It reports what a hundred managers did,
    it does not solve anything, and it must not: `optimiser.py` picks squads on
    a projection and this counts squads that already exist.

    The fifteen respect the position quotas, because a template with four
    keepers in it would be describing the aggregate rather than a squad. They
    deliberately do not respect the three per club cap: every real squad in the
    sample respects it, and the most owned fifteen across a hundred of them need
    not, so imposing it here would drop a player the field genuinely owns in
    favour of one it does not.

    The eleven is filled to the position minima first and then by start share,
    so it is always a shape FPL would accept.

    Returns the eleven and the four on the bench, both best first, with the
    name, position, club and price alongside. Empty frames when there is
    nothing to describe.
    """
    columns = ["name", "position", "club", "price", *SHARE_COLUMNS]
    if shares.empty:
        empty = pd.DataFrame(columns=columns)
        return empty, empty.copy()

    players = season.players[["name", "position", "club", "price"]]
    pool = players.join(shares, how="inner").dropna(subset=["position"])
    if pool.empty:
        empty = pd.DataFrame(columns=columns)
        return empty, empty.copy()

    squad_ids: list = []
    by_ownership = pool.sort_values("elite_ownership", ascending=False)
    for position, quota in SQUAD_LIMITS.items():
        squad_ids.extend(by_ownership.index[by_ownership["position"] == position][:quota])

    squad = pool.loc[squad_ids].sort_values("elite_start_share", ascending=False)

    chosen: list = []
    counts = dict.fromkeys(XI_MAX, 0)
    for position, minimum in XI_MIN.items():
        for player_id in squad.index[squad["position"] == position][:minimum]:
            chosen.append(player_id)
            counts[position] += 1
    for player_id, position in squad["position"].items():
        if len(chosen) == XI_SIZE:
            break
        if player_id in chosen or counts[position] >= XI_MAX[position]:
            continue
        chosen.append(player_id)
        counts[position] += 1

    xi = squad.loc[chosen].sort_values("elite_start_share", ascending=False)
    bench = squad.drop(index=chosen)
    if len(xi) == XI_SIZE and not is_legal_xi(xi["position"]):  # pragma: no cover
        raise AssertionError("template_xi built a formation FPL would reject")
    return xi[columns], bench[columns]
