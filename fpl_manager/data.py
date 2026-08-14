"""Turns raw FPL payloads into tidy pandas frames.

Prices are held in the API's native tenths of a million (55 means 5.5m). They
are only converted to millions at display time, which keeps the optimiser on
integers and avoids floating point drift against the 100.0m budget.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import pandas as pd

from .api import FplApi

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_LIMITS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET_TENTHS = 1000
MAX_PER_CLUB = 3
SQUAD_SIZE = sum(SQUAD_LIMITS.values())
XI_SIZE = 11


def is_legal_xi(positions: Iterable[str]) -> bool:
    """Whether eleven positions make a formation FPL would accept.

    Lives here beside the limits it reads rather than with either caller, since
    the optimiser has to respect it when choosing a lineup and the live view has
    to respect it when working out which substitutions actually happened.
    """
    counts = Counter(positions)
    if sum(counts.values()) != XI_SIZE:
        return False
    return all(XI_MIN[pos] <= counts.get(pos, 0) <= XI_MAX[pos] for pos in XI_MIN)


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
            # who takes what. 1 is first choice, and the field is absent for
            # everyone who takes nothing, so it stays NaN rather than becoming 0
            "penalties_order",
            "direct_freekicks_order",
            "corners_and_indirect_freekicks_order",
            # what price moves are worked out from, see prices.py
            "transfers_in_event",
            "transfers_out_event",
            "cost_change_event",
            "cost_change_start",
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
    def total_players(self) -> int:
        """How many people are playing FPL.

        Needed to turn an ownership percentage into a number of owners, which
        is what price change thresholds scale with. Zero if the payload does
        not carry it, and callers treat that as unknown rather than as nobody.
        """
        return int(self._boot.get("total_players") or 0)

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

    @property
    def current_gameweek(self) -> int:
        """The gameweek under way, or 0 before the season starts.

        `gameweeks_played` counts finished gameweeks, so between a deadline and
        the last final whistle it is one behind. Anything reading a live squad
        wants this one, since picks are published as soon as the deadline
        passes.
        """
        current = self.events.index[self.events["is_current"]]
        if len(current):
            return int(current[0])
        return self.gameweeks_played

    @property
    def data_stamp(self) -> str:
        """A token that changes when the underlying bootstrap was refetched.

        Callers that cache on a Season have to pass this alongside it. Streamlit
        is told to skip hashing the Season itself, since it holds an HTTP
        session, which means a rebuilt Season would otherwise keep being served
        results computed from the old one.
        """
        fetched = self.api.fetched_at("bootstrap")
        return "unknown" if fetched is None else fetched.isoformat()

    @property
    def next_deadline(self) -> pd.Timestamp | None:
        """When transfers for `next_gameweek` lock, in UTC.

        Follows `next_gameweek` rather than looking for `is_next` itself, so the
        two can never disagree about which gameweek is being talked about. Once
        a gameweek is under way its deadline is in the past, and this still
        returns it, because whether that reads as a countdown or as a gameweek
        already running is a question for whoever is displaying it.
        """
        deadline = self.events["deadline_time"].get(self.next_gameweek)
        return None if deadline is None or pd.isna(deadline) else deadline

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
        return self._attach_strength(out).sort_values(["team", "event"]).reset_index(drop=True)

    def _attach_strength(self, fixtures: pd.DataFrame) -> pd.DataFrame:
        """Both sides' attack and defence ratings for each fixture.

        FPL rates a club's attack and defence separately, and differently at
        home and away, on a continuous scale. That is four numbers per fixture
        where the difficulty rating is one integer from five.

        Every column is NaN when the payload does not carry the ratings, which
        is what the projection falls back to the difficulty rating on. They are
        optional fields on an undocumented API, so their absence is a case to
        handle rather than an error.
        """
        wanted = [
            "strength_attack_home",
            "strength_attack_away",
            "strength_defence_home",
            "strength_defence_away",
        ]
        columns = ("attack_for", "defence_for", "attack_against", "defence_against")
        if not all(c in self.teams.columns for c in wanted):
            for column in columns:
                fixtures[column] = float("nan")
            return fixtures

        # a club is rated by its home numbers when it is the home side, so the
        # away club in the same fixture is rated by its away numbers
        for side in ("for", "against"):
            team = fixtures["team"] if side == "for" else fixtures["opponent"]
            playing_home = fixtures["is_home"] if side == "for" else ~fixtures["is_home"]
            for what in ("attack", "defence"):
                at_home = team.map(self.teams[f"strength_{what}_home"])
                at_away = team.map(self.teams[f"strength_{what}_away"])
                fixtures[f"{what}_{side}"] = at_home.where(playing_home, at_away).astype("float64")

        return fixtures

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

    def gameweek_shape(self, horizon: int = 12, start_gw: int | None = None) -> pd.DataFrame:
        """Clubs that play twice in a gameweek, and clubs that do not play.

        One row per club per affected gameweek, with `shape` reading "double"
        or "blank". Gameweeks where everything is normal produce no rows at
        all, so an empty frame means there is nothing coming.

        Reads every fixture in the window rather than only the unfinished ones,
        unlike everything else here. A club that has already played the first
        leg of a double still has a double, and filtering to unfinished
        fixtures halfway through a gameweek would report every club that had
        already kicked off as blanking.

        A gameweek with no fixtures published at all is skipped rather than
        reported as twenty clubs blanking at once, which is what the far end of
        the horizon looks like before the schedule is complete.
        """
        start_gw = start_gw or self.next_gameweek
        window = self.fixtures[
            self.fixtures["event"].notna()
            & (self.fixtures["event"] >= start_gw)
            & (self.fixtures["event"] < start_gw + horizon)
        ]
        if window.empty:
            return pd.DataFrame(columns=["event", "club", "fixtures", "shape"])

        playing = pd.concat(
            [
                window[["event", "team_h"]].rename(columns={"team_h": "team"}),
                window[["event", "team_a"]].rename(columns={"team_a": "team"}),
            ]
        )
        counts = playing.groupby(["event", "team"]).size().rename("fixtures")

        rows = []
        for event in sorted(counts.index.get_level_values("event").unique()):
            in_week = counts.xs(event, level="event").reindex(self.teams.index).fillna(0)
            for team, played in in_week.items():
                shape = "double" if played >= 2 else "blank" if played == 0 else None
                if shape:
                    rows.append(
                        {
                            "event": int(event),
                            "club": self.teams["short_name"].get(team),
                            "fixtures": int(played),
                            "shape": shape,
                        }
                    )

        if not rows:
            return pd.DataFrame(columns=["event", "club", "fixtures", "shape"])
        return pd.DataFrame(rows).sort_values(["event", "shape", "club"]).reset_index(drop=True)

    def fixture_swings(self, window: int = 3, start_gw: int | None = None) -> pd.DataFrame:
        """How much each club's run changes between one block of gameweeks and the next.

        `swing` is the mean difficulty of the next `window` gameweeks minus the
        mean of the `window` after. Positive means it gets easier later, so the
        club's players are worth waiting for. Negative means the good run is
        happening now and is the signal to plan an exit.

        Sorted easiest-to-come first. A club with no fixture at all in either
        block has no comparison to make and is left out, which is why the game
        counts come back alongside: a swing resting on one fixture is a much
        weaker signal than one resting on three.
        """
        start_gw = start_gw or self.next_gameweek
        tf = self.team_fixtures(window * 2, start_gw=start_gw)
        if tf.empty:
            return pd.DataFrame(
                columns=["club", "now", "later", "swing", "now_games", "later_games"]
            )

        tf = tf.copy()
        tf["block"] = (tf["event"] >= start_gw + window).map({False: "now", True: "later"})
        means = tf.pivot_table(index="team", columns="block", values="difficulty", aggfunc="mean")
        games = tf.pivot_table(index="team", columns="block", values="difficulty", aggfunc="size")

        out = pd.DataFrame(index=means.index)
        out.insert(0, "club", self.teams["short_name"])
        for block in ("now", "later"):
            out[block] = means[block] if block in means.columns else float("nan")
            out[f"{block}_games"] = (
                games[block].fillna(0).astype(int) if block in games.columns else 0
            )

        out["swing"] = out["now"] - out["later"]
        out = out.dropna(subset=["swing"])
        return out.sort_values("swing", ascending=False).reset_index(drop=True)[
            ["club", "now", "later", "swing", "now_games", "later_games"]
        ]
