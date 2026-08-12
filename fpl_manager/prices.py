"""Which players are close to a price change.

FPL moves a player's price when enough managers transfer them in or out. The
threshold is not published and neither is the algorithm, so nothing here is a
reconstruction of it. What is well established is the shape: the threshold
scales with how many people already own the player, so a hundred thousand
transfers in means something very different for a template pick than for a
differential.

That gives one number worth computing, net transfers over the current gameweek
measured against the owner base:

    pressure = (transfers_in_event - transfers_out_event) / owners

Two limits are worth stating plainly, because they bound how much this is worth
trusting.

The API publishes a running total since the gameweek opened, not a time series.
Without storing daily samples there is no rate of change, so a player who took
five days to accumulate his net transfers looks identical to one who did it this
morning, and the second is far closer to moving.

Prices update once a day, around 01:30 UK time, and the counters reset with
them. This cannot see where in that cycle it is being asked, so a reading taken
shortly after an update understates everything.

Treat the output as a shortlist of players to go and check, which is the same
thing the projection model is for.
"""

from __future__ import annotations

import pandas as pd

from .data import Season

# Net transfers as a share of the owner base, past which a move looks likely.
# Fitted by eye against how the public price change sites behave rather than
# derived, so they are a starting point to tune, not a finding.
RISE_PRESSURE = 0.10
FALL_PRESSURE = -0.06

# Below this many owners the ratio is dominated by its own denominator, and a
# few thousand transfers on a barely owned player reads as a certainty.
MIN_OWNERS = 3000

PRICE_COLUMNS = [
    "transfers_in_event",
    "transfers_out_event",
    "cost_change_event",
    "cost_change_start",
]


def price_pressure(season: Season) -> pd.DataFrame:
    """Net transfer pressure per player, and which way it points.

    Returns one row per player with `owners`, `net_transfers`, `pressure`,
    `direction` and the price moves already made this gameweek and this season.
    `direction` is rise, fall or hold.

    Pre-season every counter is zero, so everything holds. That is correct
    rather than a failure, and `is_dormant` is how a caller tells the two apart.
    """
    players = season.players
    missing = [c for c in PRICE_COLUMNS if c not in players.columns]
    if missing:
        raise KeyError(f"Season is missing price fields: {missing}. Refresh the bootstrap.")

    owners = players["selected_by_percent"].fillna(0.0) / 100 * season.total_players
    net = players["transfers_in_event"].fillna(0) - players["transfers_out_event"].fillna(0)

    # a player nobody owns cannot be measured against his owner base, so hold
    # the denominator at a floor rather than dividing by something near zero
    pressure = net / owners.clip(lower=MIN_OWNERS)

    frame = pd.DataFrame(
        {
            "name": players["name"],
            "position": players["position"],
            "club": players["club"],
            "price": players["price"],
            "owners": owners.round().astype(int),
            "net_transfers": net.astype(int),
            "pressure": pressure,
            "cost_change_event": players["cost_change_event"].fillna(0).astype(int),
            "cost_change_start": players["cost_change_start"].fillna(0).astype(int),
        }
    )
    frame["direction"] = "hold"
    frame.loc[frame["pressure"] >= RISE_PRESSURE, "direction"] = "rise"
    frame.loc[frame["pressure"] <= FALL_PRESSURE, "direction"] = "fall"
    return frame


def is_dormant(pressure_frame: pd.DataFrame) -> bool:
    """Whether there is any transfer activity to read yet.

    True before the season opens, when every counter is zero and every player
    holds. Front ends use it to say there is nothing to show rather than
    render a table of zeroes that looks like a prediction.
    """
    return bool((pressure_frame["net_transfers"] == 0).all())


def movers(pressure_frame: pd.DataFrame, direction: str = "rise", top: int = 15) -> pd.DataFrame:
    """The players under the most pressure in one direction."""
    if direction not in {"rise", "fall"}:
        raise ValueError("direction must be rise or fall")
    picked = pressure_frame[pressure_frame["direction"] == direction]
    ascending = direction == "fall"
    return picked.sort_values("pressure", ascending=ascending).head(top)
