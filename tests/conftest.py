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

    def __init__(
        self, n_teams: int = N_TEAMS, played: int = 0, seed: int = 7, strengths: bool = True
    ):
        self.rng = random.Random(seed)
        self.n_teams = n_teams
        self.played = played
        self._teams = [self._make_team(t, strengths) for t in range(1, n_teams + 1)]
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

    def _make_team(self, t: int, strengths: bool) -> dict:
        """One club, optionally without the detailed strength ratings.

        `strengths=False` is the payload an older or a trimmed API response
        looks like, and is what the projection has to fall back to the 1-5
        difficulty rating on. Worth being able to generate, since that fallback
        is otherwise never exercised.
        """
        team = {
            "id": t,
            "name": f"Club {t}",
            "short_name": f"C{t:02d}",
            "strength": self.rng.randint(2, 5),
        }
        if not strengths:
            return team

        # spread across a realistic band, with home ratings above away ones
        attack = self.rng.randint(1000, 1400)
        defence = self.rng.randint(1000, 1400)
        team.update(
            {
                "strength_overall_home": attack + 40,
                "strength_overall_away": attack - 40,
                "strength_attack_home": attack + 50,
                "strength_attack_away": attack - 50,
                "strength_defence_home": defence + 50,
                "strength_defence_away": defence - 50,
            }
        )
        return team

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
                    # per 90 attacking output by position, so the component
                    # model has something to separate a forward from a keeper
                    xg90 = {1: 0.0, 2: 0.04, 3: 0.16, 4: 0.38}[etype] * self.rng.uniform(0.4, 1.8)
                    xa90 = {1: 0.0, 2: 0.05, 3: 0.15, 4: 0.11}[etype] * self.rng.uniform(0.4, 1.8)
                    # Defensive work per 90, spread wide enough on purpose that
                    # generated players land on both sides of their threshold.
                    # If every defender sat above 10 the tests could not tell a
                    # working estimator from a constant.
                    defcon90 = {1: 0.0, 2: 9.0, 3: 6.0, 4: 2.0}[etype] * self.rng.uniform(0.4, 1.8)
                    saves90 = 3.0 * self.rng.uniform(0.4, 1.8) if etype == 1 else 0.0
                    # defenders and holding midfielders get booked more than
                    # forwards do, which is part of what separates the positions
                    yellow90 = {1: 0.02, 2: 0.12, 3: 0.11, 4: 0.06}[etype]
                    elements.append(
                        {
                            "id": pid,
                            # `code` is the photograph id and `team_code` the
                            # kit id, both distinct from the ids beside them.
                            # The front end builds image URLs off these, so the
                            # fake has to keep them apart the way the real
                            # payload does or a swapped pair would pass here.
                            "code": 500_000 + pid,
                            "team_code": 100 + team,
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
                            # FPL's own forecast for the next gameweek. It is
                            # forward looking, so unlike the counting stats
                            # around it there is nothing stale about it before
                            # the first deadline and it is published either
                            # way. Loosely tied to the player's scoring so it
                            # is neither a copy of our projection nor noise,
                            # and spread off the id rather than off `rng`
                            # because drawing here would shift every random
                            # number after it and rewrite the whole season.
                            "ep_next": f"{ppg * (0.6 + pid % 9 / 10):.1f}",
                            # Attackers out-shoot defenders and keepers do not
                            # shoot at all, because a component model that saw
                            # one flat rate could not be told apart from no
                            # model. Zero before a ball is kicked, the way the
                            # real payload is: FPL publishes these as zero
                            # until the season is under way.
                            "expected_goals": f"{minutes / 90 * xg90:.2f}"
                            if self.played
                            else "0.0",
                            "expected_assists": f"{minutes / 90 * xa90:.2f}"
                            if self.played
                            else "0.0",
                            # charged only while the player was on the pitch, so
                            # a keeper's figure is his club's and an outfielder's
                            # is a fraction of it
                            "expected_goals_conceded": f"{minutes / 90 * 1.35:.2f}"
                            if self.played
                            else "0.0",
                            # The rest of what FPL pays for. Zero pre-season
                            # like their neighbours above, and split so that
                            # `defensive_contribution` is exactly the sum its
                            # position counts, the way the real payload is:
                            # recoveries are in it for everyone but defenders.
                            "saves": round(minutes / 90 * saves90) if self.played else 0,
                            "yellow_cards": round(minutes / 90 * yellow90) if self.played else 0,
                            "red_cards": 1 if self.played and pid % 47 == 0 else 0,
                            "defensive_contribution": round(minutes / 90 * defcon90)
                            if self.played
                            else 0,
                            "tackles": round(minutes / 90 * defcon90 * 0.25) if self.played else 0,
                            "clearances_blocks_interceptions": (
                                round(minutes / 90 * defcon90)
                                - round(minutes / 90 * defcon90 * 0.25)
                                if etype == 2
                                else round(minutes / 90 * defcon90 * 0.3)
                            )
                            if self.played
                            else 0,
                            "recoveries": (
                                0
                                if etype == 2
                                else round(minutes / 90 * defcon90)
                                - round(minutes / 90 * defcon90 * 0.25)
                                - round(minutes / 90 * defcon90 * 0.3)
                            )
                            if self.played
                            else 0,
                            # one taker per club per duty, the way the real
                            # payload is, and absent for everyone else
                            **(
                                {"penalties_order": 1}
                                if etype == 4 and pid % SQUAD_SIZE_PER_TEAM == 19
                                else {}
                            ),
                            **(
                                {"direct_freekicks_order": 1}
                                if etype == 3 and pid % SQUAD_SIZE_PER_TEAM == 11
                                else {}
                            ),
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

    def fixtures_for_event(self, gameweek: int) -> list[dict]:
        """One gameweek's fixtures, with in-play stats on the live ones.

        The first fixture of the gameweek is finished and has had its bonus
        applied, the second is under way with bonus still to be decided, and
        the rest have not kicked off. That is the mix a live view has to render
        correctly on a Saturday afternoon.
        """
        rows = [dict(f) for f in self._fixtures if f["event"] == gameweek]
        for i, fixture in enumerate(rows):
            home = [e["id"] for e in self._elements if e["team"] == fixture["team_h"]][:4]
            away = [e["id"] for e in self._elements if e["team"] == fixture["team_a"]][:4]
            bps_h = [{"element": e, "value": 30 - n * 5} for n, e in enumerate(home)]
            bps_a = [{"element": e, "value": 28 - n * 5} for n, e in enumerate(away)]

            if i == 0:
                fixture["started"] = True
                fixture["finished"] = True
                fixture["finished_provisional"] = True
                fixture["stats"] = [
                    {"identifier": "bps", "h": bps_h, "a": bps_a},
                    {
                        "identifier": "bonus",
                        "h": [{"element": home[0], "value": 3}],
                        "a": [{"element": away[0], "value": 2}],
                    },
                ]
            elif i == 1:
                fixture["started"] = True
                fixture["finished"] = False
                fixture["finished_provisional"] = False
                fixture["stats"] = [{"identifier": "bps", "h": bps_h, "a": bps_a}]
            else:
                fixture["started"] = False
                fixture["finished"] = False
                fixture["finished_provisional"] = False
                fixture["stats"] = []
        return rows

    def live(self, gameweek: int) -> dict:
        """Per-player totals for the gameweek, summed across his fixtures."""
        playing = set()
        for fixture in self.fixtures_for_event(gameweek):
            if fixture["started"]:
                playing.update([fixture["team_h"], fixture["team_a"]])

        elements = []
        for element in self._elements:
            on = element["team"] in playing and element["id"] % 3 != 0
            elements.append(
                {
                    "id": element["id"],
                    "stats": {
                        "minutes": 90 if on else 0,
                        "total_points": 2 + element["id"] % 9 if on else 0,
                        "bonus": 0,
                        "bps": 20 + element["id"] % 15 if on else 0,
                        "goals_scored": 1 if on and element["id"] % 11 == 0 else 0,
                        "assists": 0,
                        "clean_sheets": 0,
                        "goals_conceded": 0,
                        "yellow_cards": 0,
                        "red_cards": 0,
                        "saves": 0,
                    },
                }
            )
        return {"elements": elements}

    def event_status(self) -> dict:
        return {"status": [], "leagues": "Updated"}

    def fetched_at(self, key: str):
        return None

    def entry(self, entry_id: int) -> dict:
        """A manager's public profile, with the leagues they are in.

        Entry 1 is in a private league and a system one, so a test can tell the
        two apart. Entry 2 is in their first season and has no past to show.
        """
        return {
            "id": entry_id,
            "player_first_name": f"Manager{entry_id}",
            "player_last_name": "Example",
            "name": f"Team {entry_id}",
            "summary_overall_points": 1000 + entry_id,
            "summary_overall_rank": 500_000 - entry_id if self.played else None,
            "summary_event_points": 50 + entry_id,
            "years_active": list(range(3)),
            "leagues": {
                "classic": [
                    {
                        "id": 900 + entry_id,
                        "name": f"Private {entry_id}",
                        "league_type": "x",
                        "entry_rank": 4,
                    },
                    {"id": 314, "name": "Overall", "league_type": "s", "entry_rank": 123456},
                ]
            },
        }

    def entry_history(self, entry_id: int) -> dict:
        """Past seasons, deliberately out of order so a sort is exercised."""
        if entry_id == 2:
            return {"current": [], "past": [], "chips": []}
        return {
            "current": [],
            "chips": [],
            "past": [
                {"season_name": "2024/25", "total_points": 2100, "rank": 400_000},
                {"season_name": "2023/24", "total_points": 2000, "rank": 900_000},
                {"season_name": "2025/26", "total_points": 2300, "rank": 120_000},
            ],
        }

    def league_standings(self, league_id: int, page: int = 1) -> dict:
        """A league table, empty before a gameweek has been scored.

        That empty case is the real pre-season behaviour of this endpoint and
        the reason the parser cannot assume any row key exists.
        """
        info = {"id": league_id, "name": f"League {league_id}"}
        if not self.played:
            return {"league": info, "standings": {"has_next": False, "page": page, "results": []}}

        results = [
            {
                "id": i,
                "entry": 1000 + i,
                "entry_name": f"Team {i}",
                "player_name": f"Manager {i}",
                "rank": i,
                # the first row is a new entry, which FPL marks with a last
                # rank of zero rather than a null
                "last_rank": 0 if i == 1 else i + 2,
                "total": 500 - i * 10,
                "event_total": 60 - i,
            }
            for i in range(1, 6)
        ]
        return {"league": info, "standings": {"has_next": False, "page": page, "results": results}}

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
