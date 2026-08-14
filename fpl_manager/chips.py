"""When to play a chip, and what it is worth if you do.

Modelling a chip is easy. Bench Boost adds the bench, Triple Captain adds one
more copy of the captain, Free Hit swaps the whole squad for one week. The
question worth answering is not how to score them but which gameweek to spend
them on, so everything here returns a gain per gameweek rather than a single
number.

A gain is always measured against what you would have scored anyway, with the
lineup and captain already chosen optimally for that gameweek. Playing Bench
Boost is worth your bench, not your whole squad, and quoting the larger figure
would be flattering the chip.

Wildcard is the exception to the one-gameweek framing. You keep the squad, so
what it is worth is not one week but everything from the week you play it to
the end of the horizon, and its gain therefore falls the longer you leave it.
That makes it comparable with the others without pretending it is the same
kind of thing, and it is still `build_squad` at your team value underneath.
"""

from __future__ import annotations

import pandas as pd

# lives in optimiser.py because the multi-week planner needs it too and chips
# already depends on the optimiser rather than the other way round
from .optimiser import build_squad, gameweek_frame, pick_xi, window_frame

CHIPS = ("bench_boost", "triple_captain", "free_hit", "wildcard")

__all__ = ["CHIPS", "best_per_chip", "evaluate", "gameweek_frame", "window_frame"]


def _best_lineup(frame: pd.DataFrame) -> tuple[float, pd.DataFrame, pd.Series]:
    """Points from the best legal XI and captain for a single gameweek."""
    xi, bench, captain = pick_xi(frame, points_col="xpts_gw")
    return float(xi["xpts_gw"].sum() + captain["xpts_gw"]), bench, captain


def evaluate(
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    squad_ids: list[int],
    budget_tenths: int,
    chips: tuple[str, ...] = CHIPS,
    min_minutes_share: float = 0.05,
) -> pd.DataFrame:
    """Score every chip in every gameweek of the horizon.

    `budget_tenths` is your team value, which is what a Free Hit squad has to
    fit inside. Returns one row per chip per gameweek, sorted by gain, so the
    top row is the best single play available over the horizon.

    Free Hit costs one solve per gameweek. That is a few hundred milliseconds
    each and only runs for the gameweeks in the horizon, so it stays quick
    enough for a slider to drive.
    """
    events = sorted(int(e) for e in by_gameweek["event"].unique())
    owned = [i for i in squad_ids if i in projections.index]
    rows = []

    for event in events:
        held = gameweek_frame(projections, by_gameweek, event, owned)
        if len(held) < 15:
            # a squad short of fifteen has no legal XI to compare against
            continue
        baseline, bench, captain = _best_lineup(held)

        if "bench_boost" in chips:
            rows.append(
                {
                    "chip": "Bench Boost",
                    "event": event,
                    "gain": float(bench["xpts_gw"].sum()),
                    "baseline": baseline,
                    "detail": ", ".join(bench["name"].astype(str)),
                }
            )

        if "triple_captain" in chips:
            rows.append(
                {
                    "chip": "Triple Captain",
                    "event": event,
                    "gain": float(captain["xpts_gw"]),
                    "baseline": baseline,
                    "detail": str(captain["name"]),
                }
            )

        if "free_hit" in chips:
            pool = gameweek_frame(projections, by_gameweek, event)
            try:
                fresh = build_squad(
                    pool,
                    budget_tenths=budget_tenths,
                    bench_weight=0.0,
                    points_col="xpts_gw",
                    captain_col="xpts_gw",
                    min_minutes_share=min_minutes_share,
                )
            except RuntimeError:
                continue
            rows.append(
                {
                    "chip": "Free Hit",
                    "event": event,
                    "gain": float(fresh.projected - baseline),
                    "baseline": baseline,
                    "detail": f"captain {fresh.captain['name']}",
                }
            )

        if "wildcard" in chips:
            row = _wildcard(
                projections, by_gameweek, event, owned, budget_tenths, min_minutes_share
            )
            if row is not None:
                rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["chip", "event", "gain", "baseline", "detail"])
    return pd.DataFrame(rows).sort_values("gain", ascending=False).reset_index(drop=True)


def _wildcard(
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int,
    owned: list[int],
    budget_tenths: int,
    min_minutes_share: float,
) -> dict | None:
    """What a wildcard played in `event` is worth over the rest of the horizon.

    Measured differently from the other three on purpose. They buy you one
    gameweek, so their gain is that gameweek. A wildcard is kept, so its gain
    is every remaining gameweek, and it falls the longer you leave it simply
    because there is less season left to spend it on. Comparing a one-week gain
    with a rest-of-horizon one is not like for like, which is why the detail
    column says how many weeks are being counted.

    The horizon still bounds the answer. A wildcard is usually played for
    fixtures beyond six weeks out, so a short horizon understates it.
    """
    held = window_frame(projections, by_gameweek, event, ids=owned)
    if len(held) < 15:
        return None

    xi, _, captain = pick_xi(held, points_col="xpts_gw")
    baseline = float(xi["xpts_gw"].sum() + captain["xpts_gw"])

    pool = window_frame(projections, by_gameweek, event)
    try:
        fresh = build_squad(
            pool,
            budget_tenths=budget_tenths,
            bench_weight=0.0,
            points_col="xpts_gw",
            captain_col="xpts_gw",
            min_minutes_share=min_minutes_share,
        )
    except RuntimeError:
        return None

    weeks = int(by_gameweek.loc[by_gameweek["event"] >= event, "event"].nunique())
    return {
        "chip": "Wildcard",
        "event": event,
        "gain": float(fresh.projected - baseline),
        "baseline": baseline,
        "detail": f"over {weeks} weeks, captain {fresh.captain['name']}",
    }


def best_per_chip(evaluated: pd.DataFrame) -> pd.DataFrame:
    """The single best gameweek for each chip, which is the actual decision."""
    if evaluated.empty:
        return evaluated
    return (
        evaluated.sort_values("gain", ascending=False)
        .groupby("chip", as_index=False)
        .first()
        .sort_values("gain", ascending=False)
        .reset_index(drop=True)
    )


def free_hit_squad(
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int,
    budget_tenths: int,
    min_minutes_share: float = 0.05,
):
    """The squad to field if you play a Free Hit in `event`.

    Bench weight is zero because a Free Hit lasts one gameweek and the bench
    does not play in it, so the four cheapest legal bodies are the right answer.
    """
    pool = gameweek_frame(projections, by_gameweek, event)
    return build_squad(
        pool,
        budget_tenths=budget_tenths,
        bench_weight=0.0,
        points_col="xpts_gw",
        captain_col="xpts_gw",
        min_minutes_share=min_minutes_share,
    )
