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
MAX_FREE_TRANSFERS = 5

# How many candidates a multi-week plan considers. Every week multiplies the
# binary variables, so this is the dial that decides whether the solve returns
# in the time a slider can wait. See `planning_pool` for what it costs.
POOL_SIZE = 140
MIN_POOL_PER_POSITION = 10

# Measured against the real season on a 140 player pool: three weeks solves in
# about 0.7s and four in about 1.5s, but five jumps to five or six seconds,
# which is past what a slider can re-solve on. Pool size barely moves any of
# those numbers, so the week count is the dial that matters and this is where
# it stops being interactive.
MAX_PLAN_WEEKS = 4


def gameweek_frame(
    projections: pd.DataFrame, by_gameweek: pd.DataFrame, event: int, ids: list[int] | None = None
) -> pd.DataFrame:
    """Projections for one gameweek, as a column the optimiser can maximise.

    A club with two fixtures contributes twice and a club with none contributes
    nothing, because `by_gameweek` already holds one row per fixture. That is
    why doubles and blanks need no special casing here either.
    """
    points = by_gameweek[by_gameweek["event"] == event].groupby("id")["xpts"].sum()
    frame = (
        projections if ids is None else projections.loc[[i for i in ids if i in projections.index]]
    )
    frame = frame.copy()
    frame["xpts_gw"] = points.reindex(frame.index).fillna(0.0)
    return frame


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
    prob: pulp.LpProblem | None = None,
    suffix: str = "",
):
    """Shared variables and constraints for any 15-man squad selection.

    Pass an existing `prob` and a `suffix` to add another week's worth of squad
    to one problem, which is what the multi-week planner does. Variable names
    have to differ between weeks or PuLP silently reuses the same column.
    """
    ids = list(pool.index)
    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize) if prob is None else prob

    x = pulp.LpVariable.dicts(f"pick{suffix}", ids, cat="Binary")
    y = pulp.LpVariable.dicts(f"start{suffix}", ids, cat="Binary")
    c = pulp.LpVariable.dicts(f"captain{suffix}", ids, cat="Binary")

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


@dataclass
class WeekPlan:
    """One gameweek of a multi-week plan."""

    event: int
    squad: pd.DataFrame
    xi: pd.DataFrame
    bench: pd.DataFrame
    captain: pd.Series
    transfers_in: pd.DataFrame
    transfers_out: pd.DataFrame
    hits: int
    bank_tenths: int
    free_transfers: int
    projected: float

    @property
    def bank(self) -> float:
        return self.bank_tenths / 10


@dataclass
class MultiWeekPlan:
    weeks: list[WeekPlan]
    projected: float
    approximate_money: bool

    @property
    def hits(self) -> int:
        return sum(w.hits for w in self.weeks)

    @property
    def transfers(self) -> int:
        return sum(len(w.transfers_in) for w in self.weeks)


def planning_pool(
    projections: pd.DataFrame,
    current_ids: list[int],
    pool_size: int = POOL_SIZE,
    points_col: str = "xpts_total",
    min_minutes_share: float = 0.05,
) -> pd.DataFrame:
    """The candidates a multi-week plan is allowed to choose from.

    Every week multiplies the number of binary variables, so the full 600 odd
    players does not solve in the time a slider can wait for. Trimming to the
    best few per position is what buys the extra weeks. It also means the plan
    is optimal over a shortlist rather than over the whole game, which is a real
    limitation and not a rounding error: a cheap enabler ranked just outside the
    pool can be exactly what makes a route affordable.

    Players you already own are always kept, whatever they are projected to do,
    since a plan that cannot see them cannot sell them.
    """
    playing = projections[projections["minutes_share"] >= min_minutes_share]
    keep = []
    for pos, count in SQUAD_LIMITS.items():
        take = max(MIN_POOL_PER_POSITION, round(pool_size * count / 15))
        keep.append(playing[playing["position"] == pos].nlargest(take, points_col))

    pool = pd.concat(keep)
    owned = projections.loc[[i for i in current_ids if i in projections.index]]
    return pd.concat([pool, owned[~owned.index.isin(pool.index)]])


def plan_transfers(
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    current_ids: list[int],
    selling_prices: dict[int, int] | None = None,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    weeks: int = 3,
    max_transfers_per_week: int = 2,
    bench_weight: float = 0.12,
    pool_size: int = POOL_SIZE,
    min_minutes_share: float = 0.05,
) -> MultiWeekPlan:
    """Plan transfers across several gameweeks as one problem.

    `suggest_transfers` solves each week alone, so it will never take a small
    loss now to reach a player it wants later, and it cannot bank a free
    transfer on purpose. This links the weeks: what you own in one week is what
    you owned in the last, plus what came in, minus what went out.

    The money is the weak part and it gets weaker the further ahead it plans.
    Prices are held at today's, so a rise you would have gained from and a fall
    you would have suffered are both invisible, and any player whose purchase
    price is unknown is assumed to sell for what he costs today. Those errors
    accumulate week on week, which is why `approximate_money` comes back set
    whenever selling prices are incomplete, and why the bank shown for the last
    week is worth less than the bank shown for the first.
    """
    selling_prices = selling_prices or {}
    events = sorted(int(e) for e in by_gameweek["event"].unique())[:weeks]
    if not events:
        raise ValueError("No gameweeks to plan over.")

    pool = planning_pool(projections, current_ids, pool_size, min_minutes_share=min_minutes_share)
    owned_now = {i for i in current_ids if i in pool.index}

    prob = pulp.LpProblem("fpl_multiweek", pulp.LpMaximize)
    objective = []
    frames, picks, starts, captains = {}, {}, {}, {}
    moves_in, moves_out, hits, free = {}, {}, {}, {}

    previous = {i: (1 if i in owned_now else 0) for i in pool.index}
    bank = bank_tenths
    free[events[0]] = free_transfers

    for position, event in enumerate(events):
        tag = f"_w{event}"
        frame = gameweek_frame(pool, by_gameweek, event)
        frames[event] = frame

        prob, x, y, c, week_objective = _base_problem(
            frame, "xpts_gw", bench_weight, "xpts_gw", prob=prob, suffix=tag
        )
        picks[event], starts[event], captains[event] = x, y, c

        tin = pulp.LpVariable.dicts(f"in{tag}", list(pool.index), cat="Binary")
        tout = pulp.LpVariable.dicts(f"out{tag}", list(pool.index), cat="Binary")
        moves_in[event], moves_out[event] = tin, tout

        for i in pool.index:
            prob += x[i] == previous[i] + tin[i] - tout[i]
            prob += tin[i] + tout[i] <= 1

        made = pulp.lpSum(tin.values())
        prob += made <= max_transfers_per_week

        taken = pulp.LpVariable(f"hits{tag}", lowBound=0, cat="Integer")
        prob += taken >= made - free[event]
        prob += taken <= made
        hits[event] = taken

        # money carried forward. Anything bought during the plan sells for what
        # it cost, since prices are held constant, so one lookup covers both.
        proceeds = pulp.lpSum(
            selling_prices.get(i, int(pool.loc[i, "now_cost"])) * tout[i] for i in pool.index
        )
        spend = pulp.lpSum(int(pool.loc[i, "now_cost"]) * tin[i] for i in pool.index)
        bank = bank + proceeds - spend
        prob += bank >= 0

        if position + 1 < len(events):
            # transfers not taken as a hit came out of the free allowance, and
            # what is left rolls with one more added, capped at five. A solver
            # could in principle inflate `taken` to bank a free transfer, but
            # that costs four points to save at most four, so it never pays.
            rolled = pulp.LpVariable(
                f"free_w{events[position + 1]}", lowBound=0, upBound=MAX_FREE_TRANSFERS
            )
            prob += rolled <= free[event] - made + taken + 1
            free[events[position + 1]] = rolled

        objective.append(week_objective - TRANSFER_COST * taken)
        previous = x

    prob += pulp.lpSum(objective)

    status = prob.solve(solver())
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"No feasible plan found (solver status: {pulp.LpStatus[status]})")

    return _assemble_weeks(
        events,
        frames,
        picks,
        starts,
        captains,
        moves_in,
        moves_out,
        hits,
        free,
        pool,
        selling_prices,
        bank_tenths,
        current_ids,
    )


def _assemble_weeks(
    events,
    frames,
    picks,
    starts,
    captains,
    moves_in,
    moves_out,
    hits,
    free,
    pool,
    selling_prices,
    bank_tenths,
    current_ids,
) -> MultiWeekPlan:
    """Read one solved multi-week problem back into a plan per gameweek."""
    plans, running_bank, total = [], bank_tenths, 0.0

    for event in events:
        frame = frames[event]
        squad, xi, bench, captain, _ = _assemble(
            frame, picks[event], starts[event], captains[event], "xpts_gw"
        )
        in_ids = [i for i in pool.index if moves_in[event][i].value() > 0.5]
        out_ids = [i for i in pool.index if moves_out[event][i].value() > 0.5]

        running_bank += sum(
            selling_prices.get(i, int(pool.loc[i, "now_cost"])) for i in out_ids
        ) - sum(int(pool.loc[i, "now_cost"]) for i in in_ids)

        taken = round(hits[event].value() or 0)
        scored = float(xi["xpts_gw"].sum() + captain["xpts_gw"]) - TRANSFER_COST * taken
        total += scored

        allowance = free[event]
        plans.append(
            WeekPlan(
                event=event,
                squad=squad,
                xi=xi,
                bench=bench,
                captain=captain,
                transfers_in=pool.loc[in_ids],
                transfers_out=pool.loc[out_ids],
                hits=taken,
                bank_tenths=round(running_bank),
                free_transfers=round(
                    allowance if isinstance(allowance, int) else (allowance.value() or 0)
                ),
                projected=scored,
            )
        )

    held = [i for i in current_ids if i in pool.index]
    return MultiWeekPlan(
        weeks=plans,
        projected=total,
        approximate_money=any(i not in selling_prices for i in held),
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
