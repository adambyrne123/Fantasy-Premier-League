"""Haul and return chances for the armband.

Everything else in the model is a point estimate, which is the right shape for
deciding who to own and the wrong shape for deciding who to captain. Two
players projected at six points are not the same bet if one gets there off a
steady floor and the other off a one in six chance of a double. This puts a
distribution on the part of a gameweek that actually swings, so the two can be
told apart.

Goals and assists are independent Poissons on the same expected rates
`component_rate` uses, drawn per fixture, so a double gameweek is a convolution
rather than a special case. The minutes term is a mixture of starting, coming
off the bench and not featuring, drawn once for the gameweek rather than once
per fixture, because starting is a property of a player's week rather than of a
match.

Three things this deliberately leaves out, all of which have to stay visible
wherever it is displayed:

- **Bonus**, which the model has no term for anywhere. A real ten point week
  for a forward is a goal, an assist and three bonus. Here that is nine and
  does not clear the bar, so `haul_chance` sits below the real thing.
- **Clean sheets, saves, defensive contribution, conceded goals and cards.** So
  for a keeper or a defender this is not a haul chance at all, only an
  attacking one, and it should be labelled that way.
- **Any reconciliation with `xpts_next`.** That figure blends the component
  rate against what a player has been scoring, and this uses the component side
  at full weight. The one thing that does line up is the mean of the goals in
  here, which is `xg90 * minutes_share * multiplier` exactly.

This module is a leaf on purpose and there is a test holding it to that. Nothing
in `optimiser.py` or `chips.py` may import it. Putting a variance term in the
objective was argued out once already: it is precision theatre on a model this
rough, and it would turn a squad the user can argue with into one they cannot.
The useful version of caring about variance is this, read beside the projection.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Season
from .projections import (
    APPEARANCE_MINUTES,
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    GOAL_POINTS,
    SHORT_APPEARANCE_POINTS,
    SUB_SHARE,
    attacking_rates,
)

# What counts as a haul. Ten is a goal and an assist for a midfielder with his
# appearance points, or two goals for a forward, and it is the figure people
# already have in mind when they say the word.
HAUL_POINTS = 10

# Goals or assists in a single fixture, past which there is nothing left to
# count. The largest rate this model produces is around one a match, where the
# mass above ten is smaller than 1e-8, so the truncation is far below anything
# the inputs are accurate to. The pmf is left summing to slightly under one
# rather than renormalised, because renormalising would hide it.
MAX_EVENTS = 10

# Points a single fixture's goals and assists can be worth, appearance points
# aside. Derived from the scoring table rather than picked, so changing what a
# goal pays cannot leave this stale.
GRID = MAX_EVENTS * (max(GOAL_POINTS.values()) + ASSIST_POINTS) + 1

# Below this a bar is not a pixel tall, so `points_pmf` stops there rather
# than returning ninety columns of nothing to draw. It is a display cut and
# not part of the model: the distribution itself sums to one and is tested
# on the way out of `_distribution`, before this is applied.
CHART_FLOOR = 1e-5

COLUMNS = [
    "name",
    "position",
    "club",
    "team",
    "price",
    "ownership",
    "event",
    "fixtures",
    "opponents",
    "xpts_gw",
    "haul_chance",
    "return_chance",
]

__all__ = ["CHART_FLOOR", "COLUMNS", "HAUL_POINTS", "haul_frame", "points_pmf"]


def _poisson_head(rate: np.ndarray, highest: int = MAX_EVENTS) -> np.ndarray:
    """`P(exactly k)` for k up to `highest`, one row per player.

    The same walk down the head of the distribution that `_poisson_at_least` in
    `projections.py` does, keeping the terms rather than summing them as it
    goes. Eleven terms, which is why this is arithmetic rather than a
    dependency. There is a test asserting the two agree, so neither can drift
    away from the other.
    """
    rate = np.clip(np.nan_to_num(np.asarray(rate, dtype="float64")), 0.0, None)
    out = np.empty((len(rate), highest + 1))
    term = np.exp(-rate)
    for k in range(highest + 1):
        out[:, k] = term
        term = term * rate / (k + 1)
    return out


def _fixture_pmf(lam_goals: np.ndarray, lam_assists: np.ndarray, goal_points: int) -> np.ndarray:
    """Points from goals and assists in one fixture, appearance points aside.

    Walked as a rectangle rather than convolved, because within one position a
    goal is worth a fixed number of points and the destination is therefore a
    plain integer index rather than something to scatter into.
    """
    goals = _poisson_head(lam_goals)
    assists = _poisson_head(lam_assists)
    out = np.zeros((len(goals), GRID))
    for g in range(MAX_EVENTS + 1):
        for a in range(MAX_EVENTS + 1):
            out[:, goal_points * g + ASSIST_POINTS * a] += goals[:, g] * assists[:, a]
    return out


def _convolve(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Row by row convolution, which `np.convolve` will not do."""
    out = np.zeros((len(left), left.shape[1] + right.shape[1] - 1))
    for k in range(right.shape[1]):
        out[:, k : k + left.shape[1]] += left * right[:, [k]]
    return out


def _place(pmf: np.ndarray, base: np.ndarray, width: int) -> np.ndarray:
    """Shift each row up by its appearance points, onto a shared grid.

    Appearance points are the one part of the total that is certain given the
    role, so they move the whole distribution along rather than spreading it.
    """
    out = np.zeros((len(pmf), width))
    for shift in np.unique(base):
        rows = base == shift
        out[rows, shift : shift + pmf.shape[1]] = pmf[rows]
    return out


def _branch(
    multipliers: np.ndarray,
    xg90: np.ndarray,
    xa90: np.ndarray,
    goal_points: int,
    share: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """One role held fixed across the gameweek, and what it scores.

    Fixtures are conditionally independent given the role, so their point
    distributions convolve. Holding the role fixed across them is the whole
    reason this is not drawn per fixture: a player who starts four weeks in
    five does not start both legs of a double sixty four percent of the time,
    and getting that wrong understates exactly the case a Triple Captain is
    being weighed for.
    """
    pmf = None
    for column in range(multipliers.shape[1]):
        scaled = multipliers[:, column] * share
        fixture = _fixture_pmf(xg90 * scaled, xa90 * scaled, goal_points)
        pmf = fixture if pmf is None else _convolve(pmf, fixture)

    per_fixture = np.where(
        share >= APPEARANCE_MINUTES / 90, APPEARANCE_POINTS, SHORT_APPEARANCE_POINTS
    )
    return pmf, (per_fixture * multipliers.shape[1]).astype(int)


def _gameweek_pmf(
    multipliers: np.ndarray,
    xg90: np.ndarray,
    xa90: np.ndarray,
    goal_points: int,
    start_chance: np.ndarray,
    sub_chance: np.ndarray,
    starter_minutes: np.ndarray,
) -> np.ndarray:
    """The whole gameweek for one position and one fixture count.

    Column index is the points total, so reading a threshold off it is a slice.
    """
    fixtures = multipliers.shape[1]
    width = fixtures * (GRID - 1) + fixtures * APPEARANCE_POINTS + 1
    total = np.zeros((len(xg90), width))

    for weight, share in (
        (start_chance, starter_minutes),
        (sub_chance, np.full(len(xg90), SUB_SHARE)),
    ):
        pmf, base = _branch(multipliers, xg90, xa90, goal_points, share)
        total += weight[:, None] * _place(pmf, base, width)

    # whatever is left is the chance he does not feature, which scores nothing
    total[:, 0] += np.clip(1.0 - start_chance - sub_chance, 0.0, 1.0)
    return total


def _empty() -> pd.DataFrame:
    """The shape of the answer, with nothing in it.

    A caller that has to check `empty` anyway should not also have to guess at
    the columns, which is the same reason `Season.gameweek_shape` does this.
    """
    frame = pd.DataFrame(columns=COLUMNS)
    frame.index.name = "id"
    return frame


def _working_set(
    season: Season,
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame, int] | None:
    """Who is eligible, which fixtures they have, and for which gameweek.

    Returns None wherever there is not enough to answer with, which is both a
    finished gameweek and `COMPONENT_MINUTES` of it per player. The second
    condition alone would not do: before the first deadline the API is still
    serving last season's minutes and expected goals under the same field
    names, so in August the gate would be wide open on a year old number.
    """
    if season.gameweeks_played == 0 or by_gameweek.empty:
        return None

    fixtures = by_gameweek.dropna(subset=["event", "multiplier"])
    if fixtures.empty:
        return None

    event = int(fixtures["event"].min()) if event is None else int(event)
    fixtures = fixtures[fixtures["event"] == event]

    rates = attacking_rates(season.players)
    eligible = rates.index[rates["played_enough"].astype(bool)]
    fixtures = fixtures[fixtures["id"].isin(eligible) & fixtures["id"].isin(projections.index)]
    if fixtures.empty:
        return None

    players = projections.loc[sorted(set(fixtures["id"]))].join(rates[["xg90", "xa90"]])
    return players, fixtures, event


def _slots(fixtures: pd.DataFrame) -> pd.DataFrame:
    """One column per fixture a club has in the gameweek, widest club first.

    A double gets two columns and a blank never reaches here, which is what
    keeps the arithmetic below free of any test on how many matches a week
    holds.
    """
    ordered = fixtures.sort_values(["id", "is_home"], kind="stable")
    ordered = ordered.assign(slot=ordered.groupby("id").cumcount())
    return ordered.pivot(index="id", columns="slot", values="multiplier")


def _distribution(
    season: Season,
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int | None,
) -> tuple[pd.DataFrame, np.ndarray, int] | None:
    """Every eligible player's points distribution for one gameweek.

    Solved in groups of one position and one fixture count, since those are the
    two things that decide the shape of the grid.
    """
    working = _working_set(season, projections, by_gameweek, event)
    if working is None:
        return None
    players, fixtures, event = working

    wide = _slots(fixtures).reindex(players.index)
    counts = wide.notna().sum(axis=1)
    width = int(counts.max()) * (GRID - 1) + int(counts.max()) * APPEARANCE_POINTS + 1
    pmf = np.zeros((len(players), width))

    positions = players["position"].to_numpy()
    for position in pd.unique(positions):
        for count in sorted(set(counts[positions == position])):
            rows = np.flatnonzero((positions == position) & (counts.to_numpy() == count))
            if not len(rows):
                continue
            block = _gameweek_pmf(
                np.nan_to_num(wide.to_numpy()[rows][:, :count]),
                players["xg90"].to_numpy()[rows],
                players["xa90"].to_numpy()[rows],
                GOAL_POINTS[position],
                players["start_chance"].to_numpy()[rows],
                players["sub_chance"].to_numpy()[rows],
                players["starter_minutes"].to_numpy()[rows],
            )
            pmf[np.ix_(rows, range(block.shape[1]))] = block

    return players, pmf, event


def _return_chance(players: pd.DataFrame, fixtures: pd.DataFrame) -> pd.Series:
    """Chance of at least one goal or assist, in closed form.

    Read off the rates rather than off the points distribution, because zero
    points and no return are different events: a starter who does nothing still
    banks his appearance. The Poisson zero over the summed rate is exact, which
    also makes it the sharper of the two tests on the machinery above.
    """
    total = fixtures.groupby("id")["multiplier"].sum().reindex(players.index).fillna(0.0)
    involvement = (players["xg90"] + players["xa90"]) * total
    quiet = players["start_chance"] * np.exp(-involvement * players["starter_minutes"])
    quiet = quiet + players["sub_chance"] * np.exp(-involvement * SUB_SHARE)
    quiet = quiet + (1.0 - players["start_chance"] - players["sub_chance"]).clip(lower=0.0)
    return (1.0 - quiet).clip(0.0, 1.0)


def haul_frame(
    season: Season,
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int | None = None,
) -> pd.DataFrame:
    """Haul and return chances for one gameweek, best haul first.

    `haul_chance` is the chance of `HAUL_POINTS` or more from goals, assists and
    appearance points, over however many fixtures the club has that week.
    `return_chance` is the chance of at least one goal or assist.

    Empty until there is enough of this season to mean anything. Players who
    have not played `COMPONENT_MINUTES` are dropped rather than carried as NaN,
    since a column of blanks sorts badly and reads as broken.
    """
    solved = _distribution(season, projections, by_gameweek, event)
    if solved is None:
        return _empty()
    players, pmf, event = solved

    fixtures = by_gameweek[by_gameweek["event"] == event]
    fixtures = fixtures[fixtures["id"].isin(players.index)]
    labels = fixtures["opponent_short"].where(
        fixtures["is_home"], fixtures["opponent_short"].str.lower()
    )

    out = players.reindex(columns=["name", "position", "club", "team", "price", "ownership"])
    out["event"] = event
    out["fixtures"] = fixtures.groupby("id").size().reindex(players.index).fillna(0).astype(int)
    out["opponents"] = (
        fixtures.assign(label=labels).groupby("id")["label"].agg("+".join).reindex(players.index)
    )
    out["xpts_gw"] = fixtures.groupby("id")["xpts"].sum().reindex(players.index).fillna(0.0)
    out["haul_chance"] = pmf[:, HAUL_POINTS:].sum(axis=1)
    out["return_chance"] = _return_chance(players, fixtures)
    return out.sort_values("haul_chance", ascending=False)


def points_pmf(
    season: Season,
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    player_ids: list[int],
    event: int | None = None,
) -> pd.DataFrame:
    """The distribution behind `haul_chance`, long, for charting.

    Columns are `id`, `points` and `probability`. The tail is cut at
    `CHART_FLOOR`, so these do not quite sum to one and are not the thing to
    check the distribution against.
    """
    solved = _distribution(season, projections, by_gameweek, event)
    if solved is None:
        return pd.DataFrame(columns=["id", "points", "probability"])
    players, pmf, _ = solved

    wanted = [pid for pid in player_ids if pid in players.index]
    if not wanted:
        return pd.DataFrame(columns=["id", "points", "probability"])

    rows = players.index.get_indexer(wanted)
    block = pmf[rows]
    meaningful = np.flatnonzero(block.max(axis=0) >= CHART_FLOOR)
    block = block[:, : int(meaningful.max()) + 1]

    frame = pd.DataFrame(block, index=pd.Index(wanted, name="id"))
    frame.columns.name = "points"
    return frame.stack().rename("probability").reset_index()
