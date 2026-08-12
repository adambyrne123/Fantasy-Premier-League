"""Squad selection as a mixed integer programme.

Picking 15 players under a budget, a club cap and position quotas is a knapsack
problem, so it gets a solver rather than a greedy loop. Greedy picks by value
per million look sensible and are reliably a few points short, because the
binding constraint is usually the interaction between the club cap and the
cheap enabler slots rather than raw value.

Bench players are weighted down instead of ignored, since a bench that never
plays is still four slots of budget you are choosing not to spend on the XI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pulp

from .data import BUDGET_TENTHS, MAX_PER_CLUB, SQUAD_LIMITS, XI_MAX, XI_MIN

TRANSFER_COST = 4


@lru_cache(maxsize=1)
def solver() -> pulp.LpSolver:
    """A CBC solver that actually has a binary to run.

    PuLP bundles CBC for x86 only, so on Windows for ARM it looks for a build
    that was never shipped and every solve dies before it starts. Windows runs
    x64 executables under emulation, so falling back to the bundled x64 binary
    keeps this to one solver dependency rather than two. On any platform PuLP
    ships a binary for, this is the default and the fallback never runs.

    The fallback goes through COIN_CMD because PULP_CBC_CMD refuses an explicit
    path. That is the only part of the PuLP 4.0 migration done here, and it is
    deliberately confined to this function.
    """
    default = pulp.PULP_CBC_CMD(msg=False)
    if default.available():
        return default

    solverdir = Path(pulp.__file__).parent / "solverdir"
    # 64 bit first, since the 32 bit build runs out of memory on big models
    candidates = sorted(solverdir.rglob("cbc*"), key=lambda p: ("64" not in p.parent.name, str(p)))
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix not in {".exe", ""}:
            continue
        fallback = pulp.COIN_CMD(path=str(candidate), msg=False)
        if fallback.available():
            return fallback

    raise RuntimeError(
        "No CBC binary available for this platform. Install one with "
        "`pip install pulp[cbc]` or point PuLP at an existing solver."
    )


@dataclass
class SquadResult:
    squad: pd.DataFrame
    xi: pd.DataFrame
    bench: pd.DataFrame
    captain: pd.Series
    vice_captain: pd.Series
    cost_tenths: int
    projected: float
    transfers_in: pd.DataFrame = field(default_factory=pd.DataFrame)
    transfers_out: pd.DataFrame = field(default_factory=pd.DataFrame)
    hits: int = 0

    @property
    def cost(self) -> float:
        return self.cost_tenths / 10


def _base_problem(
    pool: pd.DataFrame,
    points_col: str,
    bench_weight: float,
    captain_col: str | None,
):
    """Shared variables and constraints for any 15-man squad selection."""
    ids = list(pool.index)
    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    x = pulp.LpVariable.dicts("pick", ids, cat="Binary")
    y = pulp.LpVariable.dicts("start", ids, cat="Binary")
    c = pulp.LpVariable.dicts("captain", ids, cat="Binary")

    prob += pulp.lpSum(x.values()) == 15

    for pos, count in SQUAD_LIMITS.items():
        members = pool.index[pool["position"] == pos]
        prob += pulp.lpSum(x[i] for i in members) == count

    for team_id in pool["team"].unique():
        members = pool.index[pool["team"] == team_id]
        prob += pulp.lpSum(x[i] for i in members) <= MAX_PER_CLUB

    prob += pulp.lpSum(y.values()) == 11
    for i in ids:
        prob += y[i] <= x[i]
        prob += c[i] <= y[i]
    prob += pulp.lpSum(c.values()) == 1

    for pos in SQUAD_LIMITS:
        members = pool.index[pool["position"] == pos]
        prob += pulp.lpSum(y[i] for i in members) >= XI_MIN[pos]
        prob += pulp.lpSum(y[i] for i in members) <= XI_MAX[pos]

    points = pool[points_col].to_dict()
    cap_points = pool[captain_col].to_dict() if captain_col else points

    objective = pulp.lpSum(
        bench_weight * points[i] * x[i]
        + (1 - bench_weight) * points[i] * y[i]
        + cap_points[i] * c[i]
        for i in ids
    )
    return prob, x, y, c, objective


def _assemble(
    pool: pd.DataFrame, x, y, c, points_col: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    picked = [i for i in pool.index if x[i].value() > 0.5]
    starting = {i for i in picked if y[i].value() > 0.5}
    captain_id = next(i for i in picked if c[i].value() > 0.5)

    squad = pool.loc[picked].copy()
    squad["starting"] = squad.index.isin(starting)
    squad["is_captain"] = squad.index == captain_id

    order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
    squad = squad.sort_values(
        by=["starting", "position", points_col],
        key=lambda s: s.map(order) if s.name == "position" else s,
        ascending=[False, True, False],
    )

    xi = squad[squad["starting"]]
    bench = squad[~squad["starting"]].sort_values(points_col, ascending=False)
    captain = squad.loc[captain_id]
    vice_pool = xi[xi.index != captain_id]
    vice = vice_pool.iloc[vice_pool[points_col].argmax()]
    return squad, xi, bench, captain, vice


def build_squad(
    projections: pd.DataFrame,
    budget_tenths: int = BUDGET_TENTHS,
    bench_weight: float = 0.12,
    points_col: str = "xpts_total",
    captain_col: str = "xpts_next",
    min_minutes_share: float = 0.05,
    include: list[int] | None = None,
    exclude: list[int] | None = None,
) -> SquadResult:
    """Pick the best legal 15 from scratch under the budget.

    `bench_weight` is the fraction of a bench player's projection that counts
    towards the objective. Push it towards 0 for an aggressive build with two
    playing-time punts on the bench, towards 0.3 for a squad you can rotate.
    """
    pool = projections[projections["minutes_share"] >= min_minutes_share].copy()
    if exclude:
        pool = pool.drop(index=[i for i in exclude if i in pool.index])
    if include:
        forced = projections.loc[[i for i in include if i in projections.index]]
        pool = pd.concat([pool, forced[~forced.index.isin(pool.index)]])

    prob, x, y, c, objective = _base_problem(pool, points_col, bench_weight, captain_col)
    prob += objective
    prob += pulp.lpSum(pool.loc[i, "now_cost"] * x[i] for i in pool.index) <= budget_tenths

    for i in include or []:
        if i in pool.index:
            prob += x[i] == 1

    status = prob.solve(solver())
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"No feasible squad found (solver status: {pulp.LpStatus[status]})")

    squad, xi, bench, captain, vice = _assemble(pool, x, y, c, points_col)
    return SquadResult(
        squad=squad,
        xi=xi,
        bench=bench,
        captain=captain,
        vice_captain=vice,
        cost_tenths=int(squad["now_cost"].sum()),
        projected=float(xi[points_col].sum() + captain[captain_col]),
    )


def suggest_transfers(
    projections: pd.DataFrame,
    current_ids: list[int],
    selling_prices: dict[int, int] | None = None,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    max_transfers: int = 3,
    bench_weight: float = 0.12,
    points_col: str = "xpts_total",
    captain_col: str = "xpts_next",
    min_minutes_share: float = 0.05,
) -> SquadResult:
    """Find the transfer plan with the best projection net of point hits.

    Selling prices matter: FPL gives you back the purchase price plus half of
    any rise, rounded down, so a player who has gone up 0.4 is worth 0.2 more
    than you paid, not 0.4. Pass the real selling prices where you know them,
    otherwise current price is assumed and the plan will be slightly optimistic
    about your spending power.
    """
    selling_prices = selling_prices or {}
    pool = projections[projections["minutes_share"] >= min_minutes_share].copy()

    held = projections.loc[[i for i in current_ids if i in projections.index]]
    pool = pd.concat([pool, held[~held.index.isin(pool.index)]])
    owned = [i for i in current_ids if i in pool.index]

    prob, x, y, c, objective = _base_problem(pool, points_col, bench_weight, captain_col)

    transfers = 15 - pulp.lpSum(x[i] for i in owned)
    hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
    prob += hits >= transfers - free_transfers
    prob += transfers <= max_transfers

    incoming_cost = pulp.lpSum(
        pool.loc[i, "now_cost"] * x[i] for i in pool.index if i not in set(owned)
    )
    proceeds = pulp.lpSum(
        selling_prices.get(i, int(pool.loc[i, "now_cost"])) * (1 - x[i]) for i in owned
    )
    prob += incoming_cost <= bank_tenths + proceeds

    prob += objective - TRANSFER_COST * hits

    status = prob.solve(solver())
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"No feasible plan found (solver status: {pulp.LpStatus[status]})")

    squad, xi, bench, captain, vice = _assemble(pool, x, y, c, points_col)
    kept = set(squad.index)
    out_ids = [i for i in owned if i not in kept]
    in_ids = [i for i in squad.index if i not in set(owned)]

    return SquadResult(
        squad=squad,
        xi=xi,
        bench=bench,
        captain=captain,
        vice_captain=vice,
        cost_tenths=int(squad["now_cost"].sum()),
        projected=float(xi[points_col].sum() + captain[captain_col]),
        transfers_in=pool.loc[in_ids],
        transfers_out=pool.loc[out_ids],
        hits=round(hits.value() or 0),
    )


def pick_xi(
    squad_projections: pd.DataFrame, points_col: str = "xpts_next"
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Choose the best legal XI, bench order and captain from a fixed 15."""
    pool = squad_projections
    prob = pulp.LpProblem("fpl_xi", pulp.LpMaximize)
    ids = list(pool.index)
    y = pulp.LpVariable.dicts("start", ids, cat="Binary")
    c = pulp.LpVariable.dicts("captain", ids, cat="Binary")

    prob += pulp.lpSum(y.values()) == 11
    prob += pulp.lpSum(c.values()) == 1
    for i in ids:
        prob += c[i] <= y[i]
    for pos in SQUAD_LIMITS:
        members = pool.index[pool["position"] == pos]
        prob += pulp.lpSum(y[i] for i in members) >= XI_MIN[pos]
        prob += pulp.lpSum(y[i] for i in members) <= XI_MAX[pos]

    pts = pool[points_col].to_dict()
    prob += pulp.lpSum(pts[i] * y[i] + pts[i] * c[i] for i in ids)
    prob.solve(solver())

    starting = [i for i in ids if y[i].value() > 0.5]
    captain_id = next(i for i in ids if c[i].value() > 0.5)
    xi = pool.loc[starting].sort_values(points_col, ascending=False)
    bench = pool.drop(index=starting).sort_values(points_col, ascending=False)
    return xi, bench, pool.loc[captain_id]
