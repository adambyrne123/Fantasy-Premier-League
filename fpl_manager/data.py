"""Turns raw FPL payloads into tidy pandas frames.

Prices are held in the API's native tenths of a million (55 means 5.5m). They
are only converted to millions at display time, which keeps the optimiser on
integers and avoids floating point drift against the 100.0m budget.
"""

from __future__ import annotations

import pandas as pd

from .api import FplApi

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_LIMITS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET_TENTHS = 1000
MAX_PER_CLUB = 3


class Season:
    """Everything loaded for the current season, in frame form."""

    def __init__(self, api: FplApi | None = None):
        self.api = api or FplApi()
        boot = self.api.bootstrap()
        self._boot = boot

        self.teams = self._build_teams(boot["teams"])
        self.events = self._build_events(boot["events"])
        self.players = self._build_players(boot["elements"])
        self.fixtures = self._build_fixtures(self.api.fixtures())

    # ------------------------------------------------------------------
    # builders
    # ------------------------------------------------------------------
    @staticmethod
    def _build_teams(raw: list[dict]) -> pd.DataFrame:
        cols = [
            "id",
            "name",
            "short_name",
            "strength",
            "strength_overall_home",
            "strength_overall_away",
            "strength_attack_home",
            "strength_attack_away",
            "strength_defence_home",
            "strength_defence_away",
        ]
        df = pd.DataFrame(raw)
        return df[[c for c in cols if c in df.columns]].set_index("id")

    @staticmethod
    def _build_events(raw: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(raw)
        keep = [
            "id",
            "name",
            "deadline_time",
            "finished",
            "is_current",
            "is_next",
            "average_entry_score",
        ]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["deadline_time"] = pd.to_datetime(df["deadline_time"], utc=True)
        return df.set_index("id")

    def _build_players(self, raw: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(raw)
        numeric = [
            "now_cost",
            "total_points",
            "minutes",
            "starts",
            "goals_scored",
            "assists",
            "clean_sheets",
            "bonus",
            "bps",
            "form",
            "points_per_game",
            "selected_by_percent",
            "expected_goals",
            "expected_assists",
            "expected_goal_involvements",
            "expected_goals_conceded",
            "ep_next",
            "chance_of_playing_next_round",
        ]
        for col in numeric:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["position"] = df["element_type"].map(POSITIONS)
        df["club"] = df["team"].map(self.teams["short_name"])
        df["price"] = df["now_cost"] / 10
        df["name"] = df["web_name"]
        df["full_name"] = df["first_name"].str.cat(df["second_name"], sep=" ")

        # status codes: a=available, d=doubtful, i=injured, s=suspended,
        # u=unavailable, n=not in squad
        df["available"] = df["status"].isin(["a", "d"])
        return df.set_index("id")

    @staticmethod
    def _build_fixtures(raw: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(raw)
        keep = [
            "id",
            "event",
            "team_h",
            "team_a",
            "team_h_difficulty",
            "team_a_difficulty",
            "finished",
            "kickoff_time",
        ]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["kickoff_time"] = pd.to_datetime(df["kickoff_time"], utc=True)
        return df

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def next_gameweek(self) -> int:
        """The gameweek that is open for transfers, or 1 before the season."""
        nxt = self.events.index[self.events["is_next"]]
        if len(nxt):
            return int(nxt[0])
        current = self.events.index[self.events["is_current"]]
        if len(current):
            return int(current[0])
        unfinished = self.events.index[~self.events["finished"]]
        return int(unfinished[0]) if len(unfinished) else 38

    @property
    def gameweeks_played(self) -> int:
        return int(self.events["finished"].sum())

    def team_fixtures(self, horizon: int, start_gw: int | None = None) -> pd.DataFrame:
        """One row per club per fixture across the horizon.

        Handles doubles and blanks naturally: a club with two fixtures in a
        gameweek gets two rows, a club with none gets zero rows.
        """
        start_gw = start_gw or self.next_gameweek
        end_gw = start_gw + horizon - 1
        fx = self.fixtures
        window = fx[
            fx["event"].notna()
            & (fx["event"] >= start_gw)
            & (fx["event"] <= end_gw)
            & (~fx["finished"])
        ]

        home = window.rename(
            columns={
                "team_h": "team",
                "team_a": "opponent",
                "team_h_difficulty": "difficulty",
            }
        )[["event", "team", "opponent", "difficulty"]].copy()
        home["is_home"] = True

        away = window.rename(
            columns={
                "team_a": "team",
                "team_h": "opponent",
                "team_a_difficulty": "difficulty",
            }
        )[["event", "team", "opponent", "difficulty"]].copy()
        away["is_home"] = False

        out = pd.concat([home, away], ignore_index=True)
        out["event"] = out["event"].astype(int)
        out["opponent_short"] = out["opponent"].map(self.teams["short_name"])
        return out.sort_values(["team", "event"]).reset_index(drop=True)

    def fixture_grid(self, horizon: int = 6) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Club by gameweek grids of difficulty and opponent.

        Returns a numeric frame for colouring and a label frame for display.
        A club with two fixtures in a gameweek gets the mean difficulty and
        both opponents in the label. A blank gets NaN and an empty label.
        """
        tf = self.team_fixtures(horizon)
        tf = tf.copy()
        tf["club"] = tf["team"].map(self.teams["short_name"])
        tf["label"] = tf["opponent_short"] + tf["is_home"].map({True: " (H)", False: " (A)"})

        difficulty = tf.pivot_table(
            index="club", columns="event", values="difficulty", aggfunc="mean"
        )
        labels = (
            tf.groupby(["club", "event"])["label"]
            .apply(lambda s: ", ".join(s))
            .unstack()
            .reindex(index=difficulty.index, columns=difficulty.columns)
            .fillna("")
        )
        return difficulty, labels

    def fixture_ticker(self, horizon: int = 6) -> pd.DataFrame:
        """Average difficulty and fixture count per club over the horizon."""
        tf = self.team_fixtures(horizon)
        agg = (
            tf.groupby("team")
            .agg(fixtures=("difficulty", "size"), avg_difficulty=("difficulty", "mean"))
            .reindex(self.teams.index)
            .fillna({"fixtures": 0, "avg_difficulty": 3.0})
        )
        agg.insert(0, "club", self.teams["short_name"])
        return agg.sort_values(["avg_difficulty", "fixtures"], ascending=[True, False])
