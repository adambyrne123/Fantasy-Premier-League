"""Score the projection against what was actually scored.

The projection was built and tuned before a ball was kicked, and every constant
in `projections.py` was set by eye. This is what turns each of them into a
measured quantity: rebuild the bootstrap as it stood after any gameweek that
has been played, project the next one, and compare with what FPL then paid.

The rebuild is possible because `event/{gw}/live/` reports every player's
counting stats per gameweek, and the bootstrap's season-to-date figures are
exactly the sum of those. One request per played gameweek, cached for a month
because a settled gameweek cannot change. `COUNTING_STATS` in `data.py` is the
list of what gets rewound.

What cannot be rewound, and reads as it stands today: availability, the
published chance of playing, prices, set piece duty and ownership. The API
keeps no history of any of them. Availability is the one that matters, since a
player who was injured for GW2 projects here as though he had been fit, so
every figure is reported twice, over everyone and over those who played.
Prices drift by a few tenths and the rest are second order.

This module reads realised points, so it is a consumer of the projection and
never an input to it. `projections.py`, `optimiser.py` and `chips.py` may not
import it, for the reason `live.py` is a leaf, and there is a test.
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pandas as pd

from . import projections
from .api import FplApi
from .data import COUNTING_STATS, Season
from .projections import GAMES_IN_SEASON, project

# The size of the shortlist the top-end metric reads. Fifty is roughly the
# pool a manager actually chooses from in a week.
TOP_N = 50

BASELINES = ("prior_baseline", "form_baseline")


def gameweek_stats(api: FplApi, gameweek: int) -> pd.DataFrame:
    """Every player's counting stats for one gameweek, from the live payload.

    Indexed by player id with one column per `COUNTING_STATS`, numeric and
    zero-filled. A player absent from the payload is absent here rather than
    zero, so a caller can tell "did not feature" from "did not exist".

    Read through `live_settled`, which caches under its own key for a month,
    and never through `live` with a long ttl: that key's file may have been
    written halfway through a Saturday by the live tab, and the first run of
    this served exactly that, 48 players short by a match.
    """
    payload = api.live_settled(gameweek) or {}
    rows = {}
    for entry in payload.get("elements") or []:
        element = entry.get("id")
        if element is None:
            continue
        stats = entry.get("stats") or {}
        rows[int(element)] = {
            c: pd.to_numeric(stats.get(c), errors="coerce") for c in COUNTING_STATS
        }

    frame = pd.DataFrame.from_dict(rows, orient="index", columns=list(COUNTING_STATS))
    frame.index.name = "id"
    return frame.astype("float64").fillna(0.0)


def load_stats(api: FplApi, through: int) -> dict[int, pd.DataFrame]:
    """The live payloads for gameweeks 1 to `through`, keyed by gameweek."""
    return {gw: gameweek_stats(api, gw) for gw in range(1, through + 1)}


def as_of(season: Season, gameweek: int, stats: dict[int, pd.DataFrame]) -> Season:
    """The season as it stood once `gameweek` had been played.

    A shallow copy of `season` with the events, the fixtures and the players'
    counting stats rewound. `gameweek` 0 is pre-season: every counter at
    zero, no gameweek finished, the first one next, which is exactly the
    state the August guards in `projections.py` exist to protect.

    Everything the API keeps no history of is left as it is today, and
    availability is opened up entirely rather than read from the present:
    a player injured now was not necessarily injured then, and a player fit
    now was not necessarily fit then, and of the two errors the second is
    the one a projection should be scored on making.
    """
    missing = [gw for gw in range(1, gameweek + 1) if gw not in stats]
    if missing:
        raise KeyError(f"no live stats for gameweek(s) {missing}")

    rewound = copy.copy(season)

    events = season.events.copy()
    events["finished"] = events.index <= gameweek
    events["is_current"] = events.index == gameweek
    events["is_next"] = events.index == gameweek + 1
    rewound.events = events

    fixtures = season.fixtures.copy()
    fixtures["finished"] = fixtures["event"].le(gameweek).fillna(False).astype(bool)
    rewound.fixtures = fixtures

    players = season.players.copy()
    played = [stats[gw] for gw in range(1, gameweek + 1)]
    for column in COUNTING_STATS:
        if column not in players.columns:
            continue
        if played:
            # summed with the gaps skipped rather than aligned. A player who
            # joined in September is absent from the August payloads, and a
            # plain sum of Series aligns him to NaN and a zero-fill then wipes
            # the gameweeks he did play. The oracle in
            # `test_live_against_the_api.py` found that on two players.
            stacked = pd.concat([frame[column] for frame in played], axis=1)
            total = stacked.sum(axis=1, min_count=1)
            players[column] = total.reindex(players.index).fillna(0.0).astype("float64")
        else:
            players[column] = 0.0
    players["chance_of_playing_next_round"] = np.nan
    players["available"] = True
    rewound.players = players
    return rewound


def evaluate(
    season: Season,
    prior: pd.DataFrame | None = None,
    through: int | None = None,
    stats: dict[int, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """One row per player per gameweek: what was projected and what happened.

    For each gameweek `k + 1` up to `through`, the projection is rebuilt off
    the season as of `k` at a horizon of one and read against the live
    payload for `k + 1`. Two baselines ride along so the model is always read
    against something: `prior_baseline` is last season's points over 38,
    which is the identity the model was built to escape, and `form_baseline`
    is this season's points per game so far, the sort every spreadsheet does.
    The second has nothing to say before a gameweek has been played.

    Empty before anything has been played. Players missing from a live
    payload are dropped for that gameweek rather than scored as nought.
    """
    played = season.gameweeks_played
    last = played if through is None else max(0, min(int(through), played))
    if last < 1:
        return pd.DataFrame(columns=_SCORED_COLUMNS)

    stats = stats if stats is not None else load_stats(season.api, last)
    if prior is not None and not prior.empty and "prior_points" in prior.columns:
        prior_per_gw = pd.to_numeric(prior["prior_points"], errors="coerce") / GAMES_IN_SEASON
    else:
        prior_per_gw = pd.Series(dtype="float64")

    scored = []
    for k in range(last):
        rewound = as_of(season, k, stats)
        rates, _ = project(rewound, horizon=1, prior=prior)
        actual = stats[k + 1].reindex(rates.index)
        minutes = actual["minutes"].fillna(0.0)
        frame = pd.DataFrame(
            {
                "event": k + 1,
                "name": rates["name"],
                "position": rates["position"],
                "xpts": rates["xpts_next"].astype("float64"),
                "points": actual["total_points"],
                "played": minutes > 0,
                "minutes_share": rates["minutes_share"].astype("float64"),
                "minutes": (minutes / 90).clip(0, 1),
                "prior_baseline": prior_per_gw.reindex(rates.index).fillna(0.0),
                "form_baseline": (
                    pd.to_numeric(rewound.players["total_points"], errors="coerce") / k
                    if k
                    else np.nan
                ),
            },
            index=rates.index,
        )
        scored.append(frame.dropna(subset=["points"]))

    out = pd.concat(scored)
    out.index.name = "id"
    return out[_SCORED_COLUMNS]


_SCORED_COLUMNS = [
    "event",
    "name",
    "position",
    "xpts",
    "points",
    "played",
    "minutes_share",
    "minutes",
    *BASELINES,
]


def rank_correlation(a: pd.Series, b: pd.Series) -> float:
    """Spearman's rho without scipy, which the deploy is deliberately without.

    Ranking both sides and taking Pearson on the ranks is the definition, ties
    averaged. NaN with fewer than two pairs to rank.
    """
    pair = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(pair) < 2:
        return float("nan")
    return float(pair["a"].rank().corr(pair["b"].rank()))


def _metrics(frame: pd.DataFrame) -> pd.Series:
    """The accuracy of one block of scored rows, pooled or for one gameweek."""
    error = frame["points"] - frame["xpts"]
    on = frame[frame["played"]]
    # the realised mean of the top of each gameweek's shortlist, averaged over
    # the gameweeks rather than pooled, so a week with more rows does not
    # count for more. A block already cut to one gameweek arrives without the
    # column and is one shortlist
    if "event" in frame.columns:
        tops = frame.groupby("event", group_keys=False).apply(
            lambda g: g.nlargest(TOP_N, "xpts")["points"].mean(), include_groups=False
        )
    else:
        tops = pd.Series([frame.nlargest(TOP_N, "xpts")["points"].mean()])
    return pd.Series(
        {
            "n": float(len(frame)),
            "bias": float(error.mean()),
            "mae": float(error.abs().mean()),
            "rmse": float(math.sqrt((error**2).mean())) if len(frame) else float("nan"),
            "rank": rank_correlation(frame["xpts"], frame["points"]),
            "rank_played": rank_correlation(on["xpts"], on["points"]),
            "bias_played": float((on["points"] - on["xpts"]).mean()) if len(on) else float("nan"),
            f"top{TOP_N}": float(tops.mean()) if len(tops) else float("nan"),
            "minutes_corr": float(frame["minutes_share"].corr(frame["minutes"])),
            "minutes_mae": float((frame["minutes"] - frame["minutes_share"]).abs().mean()),
            "prior_rank": rank_correlation(frame["prior_baseline"], frame["points"]),
            "prior_mae": float((frame["points"] - frame["prior_baseline"]).abs().mean()),
            "form_rank": rank_correlation(frame["form_baseline"], frame["points"]),
            "form_mae": float((frame["points"] - frame["form_baseline"]).abs().mean()),
        }
    )


def summarise(scored: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Accuracy per gameweek, and pooled over all of them.

    Rank correlation is the one to read first. Bias and error are dominated by
    the half of the game that does not play, and the top-fifty figure is what
    the shortlist a manager actually chooses from went on to score.
    """
    if scored.empty:
        return pd.DataFrame(), pd.Series(dtype="float64")
    per_gameweek = scored.groupby("event").apply(_metrics, include_groups=False)
    per_gameweek.index.name = "event"
    return per_gameweek, _metrics(scored)


def calibration(scored: pd.DataFrame, bins: int = 10) -> pd.DataFrame:
    """Mean projected against mean realised, by decile of the projection.

    A well calibrated model lands the two columns close in every row. One that
    is right about order and wrong about scale reads as a steady gap, which is
    a different fix from a model that is wrong about order.
    """
    if scored.empty:
        return pd.DataFrame(columns=["xpts", "points", "n"])
    ranked = scored["xpts"].rank(method="first")
    decile = pd.qcut(ranked, min(bins, ranked.nunique()), labels=False)
    out = scored.groupby(decile).agg(
        xpts=("xpts", "mean"), points=("points", "mean"), n=("xpts", "size")
    )
    out.index.name = "decile"
    return out


def position_bias(scored: pd.DataFrame) -> pd.DataFrame:
    """Bias and error by position, over those who played.

    Conditioning on having played is deliberate. Over everyone the number is
    mostly about who was left out, which the rewind cannot know. Over those
    who featured it is about whether the scoring model pays each position
    what FPL does.
    """
    if scored.empty:
        return pd.DataFrame(columns=["bias", "mae", "n"])
    on = scored[scored["played"]]
    error = on["points"] - on["xpts"]
    return pd.DataFrame(
        {
            "bias": error.groupby(on["position"]).mean(),
            "mae": error.abs().groupby(on["position"]).mean(),
        }
    ).join(on.groupby("position").size().rename("n"))


def sweep(
    season: Season,
    prior: pd.DataFrame | None,
    constant: str,
    values: list[float],
    through: int | None = None,
    stats: dict[int, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """The pooled accuracy at each value of one `projections.py` constant.

    Sets the module constant, re-evaluates, and puts it back however the
    evaluation ends. Only a numeric constant that already exists can be swept,
    so a typo is an error rather than a run that quietly changed nothing.
    """
    current = getattr(projections, constant, None)
    if current is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        raise ValueError(f"{constant} is not a numeric constant in projections.py")

    played = season.gameweeks_played
    last = played if through is None else max(0, min(int(through), played))
    stats = stats if stats is not None else load_stats(season.api, last)

    rows = {}
    try:
        for value in values:
            setattr(projections, constant, type(current)(value))
            _, pooled = summarise(evaluate(season, prior, through=last, stats=stats))
            rows[float(value)] = pooled
    finally:
        setattr(projections, constant, current)

    out = pd.DataFrame(rows).T
    out.index.name = constant
    return out
