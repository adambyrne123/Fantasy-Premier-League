"""Public manager and league data, turned into frames.

Read-only and unauthenticated, the same endpoints the FPL site uses for its own
manager and league pages. Nothing here is opinionated: it reshapes three
payloads and works out a rank movement, and that is all. Anything that ranks or
scores players belongs in `projections.py`.

Three of the four shapes this parses were read off the live API. The fourth,
the rows inside a league table, could not be: no league has a single row in it
until a gameweek has been scored, so `standings` keeps only the columns it
actually finds rather than assuming a key is there.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .data import Season

# what a standings row is called in the payload, against what we call it
STANDING_COLUMNS = {
    "rank": "rank",
    "last_rank": "last_rank",
    "entry": "entry_id",
    "entry_name": "team",
    "player_name": "manager",
    "total": "total",
    "event_total": "gameweek",
}

# system leagues are the ones FPL puts everyone in, by club, country and start
# gameweek. Nobody chose them, so they are worth telling apart from the mini
# league someone actually set up.
SYSTEM_LEAGUE = "s"


@dataclass(frozen=True)
class Manager:
    """Who someone is and how they are doing, from their public entry."""

    entry_id: int
    name: str
    team_name: str
    overall_points: int
    overall_rank: int | None
    gameweek_points: int
    seasons_played: int


def _fetch(call, what: str, entry_id: int):
    """Turn a failed lookup into a message rather than a traceback.

    Anyone can type an id into the front end, and a wrong one is a 404 from
    `requests` several frames down.
    """
    try:
        return call()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read {what} for id {entry_id}. Check the id is right and public. ({exc})"
        ) from exc


def load_manager(season: Season, entry_id: int) -> Manager:
    """The public profile for one manager.

    Available all year, including before a ball is kicked, though the rank is
    None until the first gameweek has been scored.
    """
    payload = _fetch(lambda: season.api.entry(int(entry_id)), "the manager", entry_id)

    first = str(payload.get("player_first_name") or "").strip()
    last = str(payload.get("player_last_name") or "").strip()
    rank = payload.get("summary_overall_rank")

    return Manager(
        entry_id=int(entry_id),
        name=" ".join(part for part in (first, last) if part) or f"Entry {entry_id}",
        team_name=str(payload.get("name") or ""),
        overall_points=int(payload.get("summary_overall_points") or 0),
        overall_rank=int(rank) if rank else None,
        gameweek_points=int(payload.get("summary_event_points") or 0),
        seasons_played=len(payload.get("years_active") or [])
        if isinstance(payload.get("years_active"), list)
        else int(payload.get("years_active") or 0),
    )


def past_seasons(season: Season, entry_id: int) -> pd.DataFrame:
    """Every previous season this manager played, oldest first.

    Empty for anyone in their first season, which is a fact worth showing
    rather than an error.
    """
    payload = _fetch(lambda: season.api.entry_history(int(entry_id)), "the history", entry_id)
    rows = payload.get("past") or []
    if not rows:
        return pd.DataFrame(columns=["season_name", "total_points", "rank"])

    frame = pd.DataFrame(rows)
    for column in ("total_points", "rank"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    keep = [c for c in ("season_name", "total_points", "rank") if c in frame.columns]
    return frame[keep].sort_values("season_name").reset_index(drop=True)


def leagues_of(season: Season, entry_id: int) -> pd.DataFrame:
    """The classic leagues a manager is in, the ones they joined first.

    Saves anyone hunting for a league id, since the entry payload already lists
    them. Sorted with the leagues someone actually joined above the ones FPL
    put them in automatically.
    """
    payload = _fetch(lambda: season.api.entry(int(entry_id)), "the manager", entry_id)
    rows = (payload.get("leagues") or {}).get("classic") or []
    if not rows:
        return pd.DataFrame(columns=["id", "name", "league_type", "rank", "system"])

    frame = pd.DataFrame(rows)
    frame["system"] = frame.get("league_type", pd.Series(index=frame.index)).eq(SYSTEM_LEAGUE)
    if "entry_rank" in frame.columns:
        frame["rank"] = pd.to_numeric(frame["entry_rank"], errors="coerce")
    else:
        frame["rank"] = pd.NA

    keep = [c for c in ("id", "name", "league_type", "rank", "system") if c in frame.columns]
    return frame[keep].sort_values(["system", "name"]).reset_index(drop=True)


def standings(season: Season, league_id: int, page: int = 1) -> tuple[pd.DataFrame, dict]:
    """One page of a league table, plus what the league is called.

    The returned frame carries `movement`, being how many places a manager has
    climbed since the last gameweek. A new entry has a last rank of zero rather
    than a null, which would otherwise read as an enormous rise, so it is left
    empty instead.
    """
    payload = _fetch(
        lambda: season.api.league_standings(int(league_id), page=page), "the league", league_id
    )
    meta = payload.get("league") or {}
    block = payload.get("standings") or {}
    info = {
        "id": meta.get("id", league_id),
        "name": meta.get("name") or f"League {league_id}",
        "page": block.get("page", page),
        "has_next": bool(block.get("has_next")),
    }

    rows = block.get("results") or []
    if not rows:
        return pd.DataFrame(columns=[*STANDING_COLUMNS.values(), "movement"]), info

    frame = pd.DataFrame(rows)
    present = {raw: tidy for raw, tidy in STANDING_COLUMNS.items() if raw in frame.columns}
    frame = frame[list(present)].rename(columns=present)

    for column in ("rank", "last_rank", "total", "gameweek", "entry_id"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if {"rank", "last_rank"} <= set(frame.columns):
        moved = frame["last_rank"] - frame["rank"]
        frame["movement"] = moved.where(frame["last_rank"] > 0)
    else:
        frame["movement"] = pd.NA

    return frame.reset_index(drop=True), info
