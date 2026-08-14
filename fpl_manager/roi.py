"""What a player has actually returned per million.

    roi = total_points / price

This is the realised counterpart to `value` in `projections.py`, which is
projected points per million. Both are worth having and they answer different
questions: `value` is who is worth buying next, `roi` is who has already paid
for their place. A player can look good on one and poor on the other, and that
gap is usually the interesting part.

Two things make the number less innocent than the division suggests.

The points come from FPL's `total_points`, which the API resets around the first
deadline of a season. Before that reset it still carries the previous season's
totals, so the same expression means last season's return in early August and
this season's return a week later. `points_source` exists so a caller can say
which one it is holding rather than quietly changing what the column means.

The price is today's price, not what anyone paid. A player who has risen 0.5m
scores worse here than he did for whoever bought him early, which is correct
for deciding what to buy now and wrong as a verdict on the buy.
"""

from __future__ import annotations

import pandas as pd

from .data import Season

# A player can post a flattering rate off one substitute appearance, so the
# default asks for a token amount of football before he is ranked at all.
MIN_MINUTES = 180

LAST_SEASON = "last season"
THIS_SEASON = "this season"
NOTHING_YET = "nothing yet"


def points_source(season: Season) -> str:
    """Which season the `total_points` column is currently describing.

    FPL zeroes the counters at the season rollover, so the meaning of
    `total_points` changes underneath this module without the schema changing.
    Reading the data is more reliable than reading the calendar: totals that
    are all zero mean the reset has happened and nothing has been scored yet,
    and non-zero totals with no finished gameweek mean the old season's numbers
    are still being served.
    """
    if season.players["total_points"].fillna(0).sum() == 0:
        return NOTHING_YET
    if season.gameweeks_played == 0:
        return LAST_SEASON
    return THIS_SEASON


def roi_frame(
    season: Season,
    projections: pd.DataFrame | None = None,
    min_minutes: int = MIN_MINUTES,
) -> pd.DataFrame:
    """Points per million for every player, best first.

    Pass `projections` to carry the projected `value` alongside, which is what
    makes the comparison between what a player has returned and what he is
    expected to return possible in one table.

    `min_minutes` drops players who have barely played rather than letting a
    single cameo top the table. Anyone below it is still in the frame, marked
    `ranked` False, so a caller can show them without pretending the rate means
    anything.
    """
    players = season.players
    price = players["now_cost"] / 10

    frame = pd.DataFrame(
        {
            "name": players["name"],
            "position": players["position"],
            "club": players["club"],
            "price": price,
            "points": players["total_points"].fillna(0).astype(int),
            "minutes": players["minutes"].fillna(0).astype(int),
            "ownership": players["selected_by_percent"].fillna(0.0),
        }
    )
    frame["roi"] = frame["points"] / price
    frame["ranked"] = frame["minutes"] >= min_minutes

    if projections is not None and "value" in projections.columns:
        frame["projected_roi"] = projections["value"].reindex(frame.index)
        # positive means he is expected to return better than he has, which is
        # the shape of an improving player or one who was injured
        frame["gap"] = frame["projected_roi"] - frame["roi"]

    return frame.sort_values("roi", ascending=False)


def best_by_position(roi: pd.DataFrame, top: int = 5) -> pd.DataFrame:
    """The strongest returns in each position, ranked players only."""
    ranked = roi[roi["ranked"]]
    return (
        ranked.groupby("position", group_keys=False)
        .head(top)
        .sort_values(["position", "roi"], ascending=[True, False])
    )
