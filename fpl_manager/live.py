"""What a gameweek is scoring while it is being played.

Realised outcomes, not projections. Nothing here may be imported by
`projections.py`, `optimiser.py` or `chips.py`: those look forward on a six
hour cache, this looks at the last sixty seconds, and wiring one into the other
puts two numbers that disagree on the same screen.

The two live endpoints answer different questions and are both needed.
`event/{gw}/live/` sums a player's stats across his fixtures, which is what
"how much has he scored" means in a double gameweek. Bonus is awarded per
fixture, so it comes from `fixtures/?event=`, where the bonus points system
scores arrive already grouped by match.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .data import XI_SIZE, Season, is_legal_xi
from .squad import MySquad

# what each rank in a fixture's bonus points system is worth
BONUS_BY_RANK = {1: 3, 2: 2, 3: 1}

FIXTURE_COLS = [
    "team_h",
    "team_a",
    "kickoff_time",
    "started",
    "finished",
    "finished_provisional",
    "bonus_added",
]

ELEMENT_STATS = [
    "minutes",
    "total_points",
    "bonus",
    "bps",
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "yellow_cards",
    "red_cards",
    "saves",
]


def bonus_awarded(bps: pd.Series) -> pd.Series:
    """Three, two and one to the top scorers in one fixture, ties sharing.

    Standard competition ranking reproduces all four of FPL's published tie
    cases without any of them needing to be special cased. Two players tied on
    top both rank 1 and both get 3, the next player ranks 3 and gets 1. Three
    tied on top take every place and nothing else is awarded. Two tied for
    second both get 2 and no third place is given.
    """
    if bps.empty:
        return pd.Series(dtype="int64")
    ranks = bps.rank(method="min", ascending=False)
    return ranks.map(BONUS_BY_RANK).fillna(0).astype("int64")


def _stat_values(fixture: dict, identifier: str) -> pd.Series:
    """One fixture's entry for a named stat, both sides in one series.

    The stats array is a list of named blocks whose order is not guaranteed and
    which is absent entirely before kickoff, so it is looked up by identifier
    rather than by position.
    """
    for block in fixture.get("stats") or []:
        if block.get("identifier") != identifier:
            continue
        rows = (block.get("h") or []) + (block.get("a") or [])
        pairs = {int(r["element"]): float(r["value"]) for r in rows if r.get("element")}
        return pd.Series(pairs, dtype="float64")
    return pd.Series(dtype="float64")


def _has_real_bonus(fixture: dict) -> bool:
    """Whether FPL has applied this fixture's bonus rather than us guessing it.

    Asked per fixture because a gameweek spread over two days has real bonus on
    the first day's matches while the second day is still provisional, so any
    gameweek-wide answer is wrong for half the screen. The bonus block appears
    in the stats array only once the points are applied, and `finished` covers
    the case where the block is there but empty.
    """
    identifiers = {b.get("identifier") for b in fixture.get("stats") or []}
    if "bonus" in identifiers:
        return True
    return bool(fixture.get("finished"))


def provisional_bonus(fixtures: list[dict]) -> pd.Series:
    """Bonus each player would get if his matches ended now, by element id.

    Zero for a fixture that has not started and zero once the real bonus has
    landed, so a caller can add this to the bonus the API reports without ever
    counting the same points twice. Doubles sum, which is correct: two fixtures
    can each award bonus.
    """
    totals: Counter[int] = Counter()
    for fixture in fixtures:
        if not fixture.get("started") or _has_real_bonus(fixture):
            continue
        bps = _stat_values(fixture, "bps")
        # a player with no bonus points system score has not been on the pitch
        bps = bps[bps > 0]
        for element, points in bonus_awarded(bps).items():
            if points:
                totals[int(element)] += int(points)
    return pd.Series(totals, dtype="int64").sort_index()


def _element_frame(payload: dict) -> pd.DataFrame:
    """Per-player totals for the gameweek, summed across his fixtures."""
    rows = {}
    for entry in payload.get("elements") or []:
        element = entry.get("id")
        if element is None:
            continue
        stats = entry.get("stats") or {}
        rows[int(element)] = {
            c: pd.to_numeric(stats.get(c), errors="coerce") for c in ELEMENT_STATS
        }

    frame = pd.DataFrame.from_dict(rows, orient="index", columns=ELEMENT_STATS)
    frame.index.name = "element"
    return frame.fillna(0.0)


def _fixture_frame(fixtures: list[dict]) -> pd.DataFrame:
    """Which matches have kicked off, ended, and had their bonus applied."""
    rows = []
    for fixture in fixtures:
        if fixture.get("id") is None:
            continue
        rows.append(
            {
                "id": int(fixture["id"]),
                "team_h": fixture.get("team_h"),
                "team_a": fixture.get("team_a"),
                "kickoff_time": fixture.get("kickoff_time"),
                "started": bool(fixture.get("started")),
                "finished": bool(fixture.get("finished")),
                "finished_provisional": bool(fixture.get("finished_provisional")),
                "bonus_added": _has_real_bonus(fixture),
            }
        )

    frame = pd.DataFrame(rows, columns=["id", *FIXTURE_COLS])
    frame["kickoff_time"] = pd.to_datetime(frame["kickoff_time"], utc=True, errors="coerce")
    for flag in ("started", "finished", "finished_provisional", "bonus_added"):
        frame[flag] = frame[flag].astype(bool)
    return frame.set_index("id")


@dataclass(frozen=True)
class LiveGameweek:
    """One gameweek's scoring as it stands right now."""

    gameweek: int
    elements: pd.DataFrame
    fixtures: pd.DataFrame
    provisional: pd.Series
    fetched_at: datetime | None = None

    @property
    def in_play(self) -> bool:
        """Whether a match is being played, which is what decides polling.

        Read off fixtures rather than the gameweek's own flags, because those
        come from bootstrap on a six hour cache and this is precisely the
        transition they would be stale about.
        """
        if self.fixtures.empty:
            return False
        return bool((self.fixtures["started"] & ~self.fixtures["finished"]).any())

    @property
    def all_settled(self) -> bool:
        """Every match played out, so autosubs are final rather than a guess."""
        if self.fixtures.empty:
            return False
        return bool(self.fixtures["finished"].all())

    def points(self, element: int) -> int:
        """A player's score including bonus that has not been applied yet."""
        if element not in self.elements.index:
            return 0
        scored = int(self.elements.loc[element, "total_points"])
        return scored + int(self.provisional.get(element, 0))

    def settled(self, elements: pd.Series) -> pd.Series:
        """Whether each player's matches are done, so a blank means he blanked.

        Anyone whose club has no fixture this gameweek counts as settled: he is
        not going to play, and waiting for a match that does not exist would
        leave a blank gameweek permanently provisional.
        """
        if self.fixtures.empty:
            return pd.Series(True, index=elements.index)

        unfinished = self.fixtures[~self.fixtures["finished"]]
        busy = set(unfinished["team_h"]).union(unfinished["team_a"])
        return ~elements.isin(busy)


def load_live(season: Season, gameweek: int) -> LiveGameweek:
    """Fetch one gameweek's live state.

    Every endpoint is allowed to come back empty. Out of season they do, and a
    live view that raises rather than saying "nothing is being played" is worse
    than no live view.
    """
    api = season.api
    try:
        payload = api.live(gameweek) or {}
    except Exception:
        payload = {}
    try:
        raw_fixtures = api.fixtures_for_event(gameweek) or []
    except Exception:
        raw_fixtures = []

    if not isinstance(raw_fixtures, list):
        raw_fixtures = []

    return LiveGameweek(
        gameweek=gameweek,
        elements=_element_frame(payload),
        fixtures=_fixture_frame(raw_fixtures),
        provisional=provisional_bonus(raw_fixtures),
        fetched_at=api.fetched_at(f"live_{gameweek}"),
    )


@dataclass(frozen=True)
class Lineup:
    """Who actually counts, once FPL's substitution rules have been applied."""

    starters: list[int]
    bench: list[int]
    subs: list[tuple[int, int]]
    captain: int | None
    captain_multiplier: int
    settled: bool


def resolve_autosubs(
    picks: pd.DataFrame,
    minutes: pd.Series,
    positions: pd.Series,
    settled: pd.Series,
    captain: int | None = None,
    vice_captain: int | None = None,
    captain_multiplier: int = 2,
) -> Lineup:
    """Apply FPL's automatic substitutions to a set of picks.

    A rule, not an optimisation. `optimiser.pick_xi` would field a better XI
    than FPL will, which is the wrong answer stated confidently, so the bench
    is walked in the order the manager set it and the first legal replacement
    comes on.

    A starter is only replaced once his matches are over. Nobody has failed to
    play while his game is still going, and treating an unplayed first half as
    a blank would sub players off mid-afternoon and then undo it.

    `picks` is indexed by pick position, 1 to 15, and carries an `element`
    column. Positions 1 to 11 start.
    """
    order = list(picks.sort_index()["element"].astype(int))
    starters = order[:XI_SIZE]
    bench = order[XI_SIZE:]

    def played(element: int) -> bool:
        return float(minutes.get(element, 0)) > 0

    def done(element: int) -> bool:
        return bool(settled.get(element, False))

    subs: list[tuple[int, int]] = []
    used: set[int] = set()

    for out in list(starters):
        if played(out) or not done(out):
            continue
        for coming_in in bench:
            if coming_in in used or not done(coming_in) or not played(coming_in):
                continue
            # the reserve keeper covers the keeper and nobody else, in either
            # direction, so the swap is checked rather than the formation
            keeper_out = positions.get(out) == "GKP"
            keeper_in = positions.get(coming_in) == "GKP"
            if keeper_out != keeper_in:
                continue

            trial = [coming_in if p == out else p for p in starters]
            if not keeper_out and not is_legal_xi([positions.get(p) for p in trial]):
                continue

            starters = trial
            used.add(coming_in)
            subs.append((out, coming_in))
            break

    armband, multiplier = captain, captain_multiplier
    if armband is not None and done(armband) and not played(armband):
        if vice_captain is not None and played(vice_captain):
            armband = vice_captain
        elif vice_captain is not None and not done(vice_captain):
            pass  # the vice may still play, so the armband is not settled yet
        else:
            multiplier = 1

    return Lineup(
        starters=starters,
        bench=[p for p in bench if p not in used] + [o for o, _ in subs],
        subs=subs,
        captain=armband,
        captain_multiplier=multiplier,
        settled=bool(settled.reindex(order).fillna(False).all()),
    )


@dataclass(frozen=True)
class EntryScore:
    """What a squad has scored this gameweek, and how much is still a guess."""

    total: int
    provisional_bonus: int
    playing: int
    to_play: int
    lineup: Lineup


def score_entry(live: LiveGameweek, lineup: Lineup) -> EntryScore:
    """Add up a resolved lineup, doubling the captain exactly once."""
    total = 0
    for element in lineup.starters:
        points = live.points(element)
        if element == lineup.captain:
            points *= lineup.captain_multiplier
        total += points

    bonus = sum(int(live.provisional.get(e, 0)) for e in lineup.starters)
    if lineup.captain in lineup.starters:
        bonus += int(live.provisional.get(lineup.captain, 0)) * (lineup.captain_multiplier - 1)

    minutes = live.elements["minutes"] if "minutes" in live.elements else pd.Series(dtype=float)
    playing = sum(1 for e in lineup.starters if float(minutes.get(e, 0)) > 0)

    return EntryScore(
        total=total,
        provisional_bonus=bonus,
        playing=playing,
        to_play=len(lineup.starters) - playing,
        lineup=lineup,
    )


def score_squad(live: LiveGameweek, season: Season, squad: MySquad) -> EntryScore:
    """Resolve and score a loaded squad in one call.

    The bridge both front ends need, so neither has to know that pick order is
    the bench order or that substitutions wait on the final whistle.

    `player_ids` is in pick order when the squad came from an entry, which is
    what makes positions 1 to 11 the XI. A squad file carries no order, so this
    reads its first eleven as the XI and its list order as the bench, which is
    the best available answer and the reason the live view asks for an entry id.
    """
    elements = list(squad.player_ids)
    clubs = season.players["team"].reindex(elements)
    positions = season.players["position"].reindex(elements)
    minutes = live.elements["minutes"] if "minutes" in live.elements else pd.Series(dtype=float)

    picks = pd.DataFrame({"element": elements}, index=range(1, len(elements) + 1))
    lineup = resolve_autosubs(
        picks=picks,
        minutes=minutes.reindex(elements).fillna(0.0),
        positions=positions,
        settled=live.settled(clubs),
        captain=squad.captain_id,
        vice_captain=squad.vice_captain_id,
        captain_multiplier=squad.captain_multiplier,
    )
    return score_entry(live, lineup)


def player_view(live: LiveGameweek, season: Season, elements: list[int]) -> pd.DataFrame:
    """A display frame for a set of players, with names and clubs attached.

    A front end concern only in the sense of shape. It lives here because
    joining live stats onto the player frame is model layer work and `app.py`
    is not allowed to do any.
    """
    players = season.players
    frame = live.elements.reindex(elements).fillna(0.0)
    frame.insert(0, "name", players["name"].reindex(elements))
    frame.insert(1, "position", players["position"].reindex(elements))
    frame.insert(2, "club", players["club"].reindex(elements))
    frame["provisional_bonus"] = live.provisional.reindex(elements).fillna(0).astype("int64")
    frame["points"] = frame["total_points"].astype("int64") + frame["provisional_bonus"]
    return frame


__all__ = [
    "BONUS_BY_RANK",
    "EntryScore",
    "Lineup",
    "LiveGameweek",
    "bonus_awarded",
    "load_live",
    "player_view",
    "provisional_bonus",
    "resolve_autosubs",
    "score_entry",
    "score_squad",
]
