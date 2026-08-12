"""Thin, cached client for the public Fantasy Premier League API.

The API is read-only, unauthenticated and undocumented. Endpoints used here are
the ones the official site consumes internally, so treat schema changes as
possible at any time.
"""

from __future__ import annotations

import json
import os
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

    def _get(self, path: str, key: str | None = None, ttl: int | None = None) -> Any:
        key = key or path.strip("/")
        ttl = self.ttl if ttl is None else ttl
        cached = self._cache_path(key)

        if ttl and cached.exists() and (time.time() - cached.stat().st_mtime) < ttl:
            return json.loads(cached.read_text())

        resp = self.session.get(f"{BASE}/{path.lstrip('/')}", timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        cached.write_text(json.dumps(payload))
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

    def entry_picks(self, entry_id: int, gameweek: int) -> dict:
        """A manager's picks for a finished gameweek.

        Only available once the gameweek deadline has passed, so this returns
        404 before the first deadline of the season.
        """
        return self._get(
            f"entry/{entry_id}/event/{gameweek}/picks/",
            key=f"picks_{entry_id}_{gameweek}",
            ttl=3600,
        )

    def entry_history(self, entry_id: int) -> dict:
        """Per-gameweek results, chips used and past seasons for a manager."""
        return self._get(f"entry/{entry_id}/history/", key=f"history_{entry_id}", ttl=3600)

    def live(self, gameweek: int) -> dict:
        """Live points for every player in a gameweek. Short TTL by design."""
        return self._get(f"event/{gameweek}/live/", key=f"live_{gameweek}", ttl=60)
