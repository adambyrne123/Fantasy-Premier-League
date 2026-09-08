"""Thin, cached client for the public Fantasy Premier League API.

The API is read-only, unauthenticated and undocumented. Endpoints used here are
the ones the official site consumes internally, so treat schema changes as
possible at any time.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

BASE = "https://fantasy.premierleague.com/api"
USER_AGENT = "fpl-manager/0.1 (personal use)"


def default_cache_dir() -> Path:
    """Where responses are cached.

    Overridable via FPL_CACHE_DIR so the same code works in a container or a
    CI runner, where a home directory may not persist between steps.
    """
    override = os.environ.get("FPL_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "fpl_manager"


class FplApi:
    """Fetches FPL endpoints, caching responses on disk.

    Parameters
    ----------
    cache_dir:
        Where JSON responses are written.
    ttl:
        Seconds a cached file stays valid. Player prices change once a day
        around 01:30 UK time, so a few hours is usually fine. Pass ttl=0 to
        force a refresh.
    """

    def __init__(self, cache_dir: Path | str | None = None, ttl: int = 6 * 3600):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key.replace('/', '_')}.json"

    def fetched_at(self, key: str) -> datetime | None:
        """When the cached response for `key` was last written, if it exists.

        The disk cache is shared by every visitor to a deployed app, so this is
        the only honest answer to how old the numbers on screen are. Anything
        derived from a single session would tell everyone who did not press
        Refresh that the data was fresher than it is.
        """
        cached = self._cache_path(key)
        if not cached.exists():
            return None
        return datetime.fromtimestamp(cached.stat().st_mtime, tz=UTC)

    def _write_cache(self, cached: Path, payload: Any) -> None:
        """Replace the cached file in one step rather than two.

        Writing in place truncates before it fills, so a reader arriving in
        between gets half a file. Rare enough to ignore while every fetch was
        driven by a page load, and no longer rare once anything polls a live
        endpoint every minute. `os.replace` is atomic within a directory, and
        the destination inherits the temporary file's mtime, so `fetched_at`
        still reports when the response arrived.
        """
        tmp = cached.with_name(f"{cached.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, cached)
        finally:
            tmp.unlink(missing_ok=True)

    def _get(self, path: str, key: str | None = None, ttl: int | None = None) -> Any:
        key = key or path.strip("/")
        ttl = self.ttl if ttl is None else ttl
        cached = self._cache_path(key)

        if ttl and cached.exists() and (time.time() - cached.stat().st_mtime) < ttl:
            try:
                return json.loads(cached.read_text())
            except (json.JSONDecodeError, OSError):
                # a file left half written by an older build, or by a process
                # killed mid-write. Refetching repairs it, so it is not worth
                # raising over.
                pass

        resp = self.session.get(f"{BASE}/{path.lstrip('/')}", timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        self._write_cache(cached, payload)
        return payload

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def bootstrap(self) -> dict:
        """Every player, club, gameweek and game setting in one call."""
        return self._get("bootstrap-static/", key="bootstrap")

    def fixtures(self) -> list[dict]:
        """All fixtures for the season, including unplayed ones with FDR."""
        return self._get("fixtures/", key="fixtures")

    def element_summary(self, player_id: int) -> dict:
        """Per-fixture history for one player, plus previous-season totals."""
        return self._get(
            f"element-summary/{player_id}/",
            key=f"element_{player_id}",
            ttl=24 * 3600,
        )

    def entry(self, entry_id: int) -> dict:
        """Public profile for an FPL manager (name, rank, squad value)."""
        return self._get(f"entry/{entry_id}/", key=f"entry_{entry_id}", ttl=3600)

    def entry_picks(self, entry_id: int, gameweek: int, ttl: int | None = None) -> dict:
        """A manager's picks for a finished gameweek.

        Only available once the gameweek deadline has passed, so this returns
        404 before the first deadline of the season.

        The default hour suits a gameweek in progress, where automatic
        substitutions are written into this payload as the matches finish. A
        caller reading a settled gameweek should pass a much longer ttl: those
        picks can no longer change, and anything sampling a hundred managers at
        once would otherwise refetch a hundred immutable payloads every hour.
        """
        return self._get(
            f"entry/{entry_id}/event/{gameweek}/picks/",
            key=f"picks_{entry_id}_{gameweek}",
            ttl=3600 if ttl is None else ttl,
        )

    def entry_history(self, entry_id: int) -> dict:
        """Per-gameweek results, chips used and past seasons for a manager."""
        return self._get(f"entry/{entry_id}/history/", key=f"history_{entry_id}", ttl=3600)

    def league_standings(self, league_id: int, page: int = 1) -> dict:
        """One page of a classic league table, fifty managers at a time.

        Returns an empty results list until a gameweek has been scored, since
        nobody has a rank before anyone has any points. A longer TTL than the
        entry endpoints because a league table only moves when a gameweek is
        published, not continuously.
        """
        return self._get(
            f"leagues-classic/{league_id}/standings/?page_standings={page}",
            key=f"league_{league_id}_{page}",
            ttl=1800,
        )

    def live(self, gameweek: int, ttl: int | None = None) -> dict:
        """Live points for every player in a gameweek. Short TTL by design.

        Stats are summed across a player's fixtures, so in a double gameweek
        this is the right source for what he has scored and the wrong one for
        anything decided per fixture. See `fixtures_for_event`.

        The default minute suits a gameweek in progress. A caller reading a
        gameweek FPL has already audited wants `live_settled` instead, and
        not merely a longer ttl on this: the file behind this key may have
        been written halfway through a Saturday, and a long ttl on it would
        serve that half-played snapshot as though it were the audited
        gameweek. Found that way, with 48 players short by a match.
        """
        return self._get(
            f"event/{gameweek}/live/", key=f"live_{gameweek}", ttl=60 if ttl is None else ttl
        )

    def live_settled(self, gameweek: int, ttl: int = 30 * 24 * 3600) -> dict:
        """The same payload for a gameweek FPL has finished processing.

        Its own cache key, so it can never read the in-play poll's file. A
        settled gameweek's figures do not change, so the first fetch under
        this key is post-audit by construction and the month-long ttl is
        safe on it. The caller is responsible for only asking about a
        gameweek that `Season.gameweeks_played` covers.
        """
        return self._get(f"event/{gameweek}/live/", key=f"live_{gameweek}_settled", ttl=ttl)

    def fixtures_for_event(self, gameweek: int) -> list[dict]:
        """One gameweek's fixtures, carrying in-play stats once they kick off.

        Cached under its own key rather than `fixtures`, or a live fetch every
        minute would keep overwriting the whole season's fixture list that the
        projection reads on a six hour ttl.
        """
        return self._get(
            f"fixtures/?event={gameweek}",
            key=f"fixtures_gw_{gameweek}",
            ttl=60,
        )

    def event_status(self) -> dict:
        """Per day, whether the games are done and whether bonus has landed.

        Keyed by date rather than by fixture, so it answers a headline question
        and never the question of whether one particular match has had its
        bonus applied.
        """
        return self._get("event-status/", key="event_status", ttl=60)
