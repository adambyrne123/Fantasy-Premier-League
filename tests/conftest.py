"""Synthetic payloads shaped like the real FPL API.

No test in this suite touches the network. `FakeApi` mirrors the shape of the
two endpoints `Season` consumes, which is enough to exercise the whole pipeline
from frame building through to solving.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from fpl_manager.data import Season

LAST_SEASON = "2025/26"
POS_COUNTS = {1: 3, 2: 7, 3: 8, 4: 4}
PRICE_RANGE = {1: (40, 60), 2: (40, 70), 3: (45, 130), 4: (45, 145)}
N_TEAMS = 20
SQUAD_SIZE_PER_TEAM = sum(POS_COUNTS.values())


class FakeApi:
    """Stands in for `FplApi`, generating a deterministic synthetic season."""

    def __init__(self, n_teams: int = N_TEAMS, played: int = 0, seed: int = 7):
        self.rng = random.Random(seed)
        self.n_teams = n_teams
        self.played = played
        self._teams = [
            {
                "id": t,
                "name": f"Club {t}",
                "short_name": f"C{t:02d}",
                "strength": self.rng.randint(2, 5),
            }
            for t in range(1, n_teams + 1)
        ]
        self._elements = self._make_players()
        self._events = [
            {
                "id": gw,
                "name": f"Gameweek {gw}",
                "deadline_time": f"2026-08-{min(28, 21 + gw):02d}T17:30:00Z",
                "finished": gw <= played,
                "is_current": gw == played,
                "is_next": gw == played + 1,
                "average_entry_score": 50,
            }
            for gw in range(1, 39)
        ]
        self._fixtures = self._make_fixtures()

    def _make_players(self) -> list[dict]:
        elements, pid = [], 1
        for team in range(1, self.n_teams + 1):
            for etype, count in POS_COUNTS.items():
                lo, hi = PRICE_RANGE[etype]
                for _ in range(count):
                    minutes = self.rng.choice([0, 300, 900, 1800, 2800, 3200])
                    # roughly consistent with the minutes, the way the real
                    # payload is, since the minutes model now reads starts
                    starts = min(self.played, round(minutes / 90)) if self.played else 0
                    ppg = self.rng.uniform(1.0, 6.5)
                    elements.append(
                        {
                            "id": pid,
                            "first_name": f"First{pid}",
                            "second_name": f"Last{pid}",
                            "web_name": f"P{pid}",
                            "team": team,
                            "element_type": etype,
                            "now_cost": self.rng.randint(lo, hi),
                            "total_points": int(ppg * self.played) if self.played else 0,
                            "minutes": minutes if self.played else 0,
                            "starts": starts,
                            "goals_scored": 0,
                            "assists": 0,
                            "clean_sheets": 0,
                            "bonus": 0,
                            "bps": 0,
                            "form": "0.0",
                            "points_per_game": "0.0",
                            "selected_by_percent": f"{self.rng.uniform(0.1, 45):.1f}",
                            "status": self.rng.choices(["a", "d", "i"], [0.9, 0.05, 0.05])[0],
                            "news": "",
                            "chance_of_playing_next_round": None,
                            "ep_next": "0.0",
                            "expected_goals": "0.0",
                            "expected_assists": "0.0",
                            # zero before a ball is kicked, the way the real
                            # payload is, so price pressure reads as dormant
                            "transfers_in_event": self.rng.randint(0, 90_000) if self.played else 0,
                            "transfers_out_event": self.rng.randint(0, 90_000)
                            if self.played
                            else 0,
                            "cost_change_event": 0,
                            "cost_change_start": self.rng.choice([-1, 0, 0, 0, 1])
                            if self.played
                            else 0,
                        }
                    )
                    pid += 1
        return elements

    def _make_fixtures(self) -> list[dict]:
        fixtures, fid = [], 1
        for gw in range(1, 39):
            teams = list(range(1, self.n_teams + 1))
            self.rng.shuffle(teams)
            for i in range(0, len(teams) - 1, 2):
                fixtures.append(
                    {
                        "id": fid,
                        "event": gw,
                        "team_h": teams[i],
                        "team_a": teams[i + 1],
                        "team_h_difficulty": self.rng.randint(2, 5),
                        "team_a_difficulty": self.rng.randint(2, 5),
                        "finished": gw <= self.played,
                        "kickoff_time": f"2026-08-{min(28, 21 + gw):02d}T14:00:00Z",
                    }
                )
                fid += 1
        return fixtures

    def bootstrap(self) -> dict:
        return {
            "teams": self._teams,
            "elements": self._elements,
            "events": self._events,
            "total_players": 9_000_000,
        }

    def fixtures(self) -> list[dict]:
        return self._fixtures

    def element_summary(self, player_id: int) -> dict:
        """Past seasons for one player.

        Every fifth player last appeared in an older season, which is what a
        spell abroad looks like in this payload: `history_past` holds Premier
        League seasons only, so the newest entry can be years out of date.
        """
        if player_id % 7 == 0:
            return {"history_past": []}
        stale = player_id % 5 == 0
        season_name = "2021/22" if stale else LAST_SEASON
        minutes = 900 + (player_id % 5) * 500
        return {
            "history_past": [
                {
                    "season_name": season_name,
                    "total_points": 40 + player_id % 120,
                    "minutes": minutes,
                    "starts": round(minutes / 90),
                    "end_cost": 45 + player_id % 60,
                }
            ]
        }


def make_prior(season: Season, seed: int = 11) -> pd.DataFrame:
    """Synthetic previous-season totals, with a fifth of players missing."""
    rng = random.Random(seed)
    rows = []
    for pid in season.players.index:
        if rng.random() < 0.2:
            continue
        minutes = rng.choice([0, 400, 1200, 2400, 3100])
        rows.append(
            {
                "id": int(pid),
                "prior_season": LAST_SEASON,
                "prior_points": int(minutes / 90 * rng.uniform(1.0, 6.0)),
                "prior_minutes": minutes,
                "prior_starts": round(minutes / 90),
            }
        )
    return pd.DataFrame(rows).set_index("id")


@pytest.fixture(params=[0, 12], ids=["preseason", "midseason"])
def season(request) -> Season:
    """A loaded season, run once pre-season and once twelve gameweeks in."""
    return Season(FakeApi(played=request.param))


@pytest.fixture
def prior(season: Season) -> pd.DataFrame:
    return make_prior(season)


@pytest.fixture
def projections(season: Season, prior: pd.DataFrame) -> pd.DataFrame:
    from fpl_manager.projections import project

    return project(season, horizon=6, prior=prior)[0]
