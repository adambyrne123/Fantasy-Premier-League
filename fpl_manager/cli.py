"""Command line entry point.

python -m fpl_manager build --horizon 6
python -m fpl_manager ticker --horizon 8
python -m fpl_manager players --position MID --top 25
python -m fpl_manager transfers --squad squad.json --free 1 --max 2
python -m fpl_manager xi --squad squad.json
python -m fpl_manager --formation 3-4-3 build
python -m fpl_manager live --entry 1234567
python -m fpl_manager find haaland
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import chips, live
from .api import FplApi
from .data import FORMATIONS, OUTFIELD, Season, format_formation, parse_formation
from .optimiser import MAX_PLAN_WEEKS, build_squad, pick_xi, plan_transfers, suggest_transfers
from .prices import is_dormant, movers, price_pressure
from .projections import BUNDLED_PRIOR, PRIOR_CACHE, load_prior, project
from .squad import MySquad, load_squad, write_squad_file


def _load_prior(season: Season, use_prior: bool) -> pd.DataFrame | None:
    if not use_prior:
        return None
    if not PRIOR_CACHE.exists() and not BUNDLED_PRIOR.exists():
        print("Fetching last season's totals, one request per player. This is slow the")
        print("first time and cached afterwards. Ctrl-C to skip.", file=sys.stderr)
    return load_prior(season)


def _fmt(df: pd.DataFrame, cols: list[str]) -> str:
    view = df[[c for c in cols if c in df.columns]].copy()
    for col in view.select_dtypes("float").columns:
        view[col] = view[col].round(2)
    return view.to_string(index=False)


SQUAD_COLS = ["name", "position", "club", "price", "xpts_next", "xpts_total", "ownership"]
LIVE_COLS = ["name", "position", "club", "minutes", "points", "provisional_bonus", "bps"]
PRICE_COLS = ["name", "position", "club", "price", "owners", "net_transfers", "pressure"]


def _shape(xi: pd.DataFrame, cost: float | None = None) -> str:
    """The formation, and what pinning it cost if it was pinned.

    Only worth saying when there is something to say, so a shape the solver
    would have chosen anyway reads as a plain formation rather than as a
    warning about nothing.
    """
    shape = format_formation(int((xi["position"] == pos).sum()) for pos in OUTFIELD)
    if cost is None or cost > -0.05:
        return shape
    return f"{shape}, {-cost:.1f} pts behind the best shape"


def _print_squad(result, label: str, cost: float | None = None) -> None:
    print(f"\n{label}")
    print(f"Cost {result.cost:.1f}m, projected {result.projected:.1f} pts")
    print(f"\nStarting XI ({_shape(result.xi, cost)})")
    print(_fmt(result.xi, SQUAD_COLS))
    print("\nBench, in order")
    print(_fmt(result.bench, SQUAD_COLS))
    print(f"\nCaptain: {result.captain['name']}  Vice: {result.vice_captain['name']}")


def cmd_build(args, season, projections, by_gw):
    formation = parse_formation(args.formation)
    build = dict(
        budget_tenths=round(args.budget * 10),
        bench_weight=args.bench_weight,
        include=args.include,
        exclude=args.exclude,
    )
    result = build_squad(projections, formation=formation, **build)
    # solved a second time only to price the override, so a run that did not
    # ask for a shape pays nothing for the answer
    cost = result.projected - build_squad(projections, **build).projected if formation else None
    _print_squad(result, f"Squad for GW{season.next_gameweek} onwards", cost)
    if args.save:
        write_squad_file(
            args.save,
            MySquad(
                player_ids=[int(i) for i in result.squad.index],
                bank_tenths=round(args.budget * 10) - result.cost_tenths,
            ),
            season,
        )
        print(f"\nWritten to {args.save}")


def cmd_ticker(args, season, projections, by_gw):
    print(f"\nFixture difficulty, GW{season.next_gameweek} for {args.horizon} weeks")
    print(
        _fmt(
            season.fixture_ticker(args.horizon).reset_index(drop=True),
            ["club", "fixtures", "avg_difficulty"],
        )
    )


def cmd_players(args, season, projections, by_gw):
    view = projections.copy()
    if args.position:
        view = view[view["position"] == args.position.upper()]
    if args.max_price:
        view = view[view["price"] <= args.max_price]
    if args.club:
        view = view[view["club"].str.upper() == args.club.upper()]
    view = view.sort_values(args.sort, ascending=False).head(args.top)
    print(
        _fmt(
            view,
            [
                "name",
                "position",
                "club",
                "price",
                "xpts_next",
                "xpts_total",
                "value",
                "ownership",
                "news",
            ],
        )
    )


def cmd_transfers(args, season, projections, by_gw):
    squad = load_squad(season, args.squad, args.entry)
    free = args.free if args.free is not None else squad.free_transfers
    bank = round(args.bank * 10) if args.bank is not None else squad.bank_tenths

    formation = parse_formation(args.formation)
    moves = dict(
        current_ids=squad.player_ids,
        selling_prices=squad.selling_prices,
        bank_tenths=bank,
        free_transfers=free,
        max_transfers=args.max,
        bench_weight=args.bench_weight,
    )
    result = suggest_transfers(projections, formation=formation, **moves)
    cost = (
        result.projected - suggest_transfers(projections, **moves).projected if formation else None
    )
    if result.transfers_in.empty:
        print("\nNo transfer improves the projection by more than it costs. Roll it.")
    else:
        print("\nOut")
        print(_fmt(result.transfers_out, SQUAD_COLS))
        print("\nIn")
        print(_fmt(result.transfers_in, SQUAD_COLS))
        if result.hits:
            print(f"\nThis takes a hit of {result.hits * 4} points.")
    _print_squad(result, "Resulting squad", cost)


def cmd_xi(args, season, projections, by_gw):
    squad = load_squad(season, args.squad, args.entry)
    frame = squad.frame(projections)
    formation = parse_formation(args.formation)
    xi, bench, captain = pick_xi(frame, formation=formation)
    cost = xi["xpts_next"].sum() - pick_xi(frame)[0]["xpts_next"].sum() if formation else None
    print(f"\nGW{season.next_gameweek} lineup ({_shape(xi, cost)})")
    print(_fmt(xi, SQUAD_COLS))
    print("\nBench, in order")
    print(_fmt(bench, SQUAD_COLS))
    print(f"\nCaptain: {captain['name']}")


def cmd_chips(args, season, projections, by_gw):
    squad = load_squad(season, args.squad, args.entry)
    budget = args.budget_tenths if args.budget_tenths else squad.value_tenths(season)

    table = chips.evaluate(projections, by_gw, squad.player_ids, budget_tenths=budget)
    if table.empty:
        print("\nNot enough of the squad is known to price a chip.")
        return

    print(f"\nChips over GW{season.next_gameweek} for {args.horizon} weeks")
    print(f"Free Hit and Wildcard are budgeted at your team value, {budget / 10:.1f}m\n")
    print("Best gameweek for each chip")
    print(_fmt(chips.best_per_chip(table), ["chip", "event", "gain", "baseline", "detail"]))

    if args.all:
        print("\nEvery gameweek, best first")
        print(_fmt(table, ["chip", "event", "gain", "baseline"]))

    print("\nGain is on top of what that squad scores anyway. Wildcard is the one")
    print("measured over every remaining gameweek rather than one, since you keep")
    print("the squad. The model cannot see rotation or press conferences, so treat")
    print("a close call as a coin toss.")


def cmd_plan(args, season, projections, by_gw):
    squad = load_squad(season, args.squad, args.entry)
    bank = round(args.bank * 10) if args.bank is not None else squad.bank_tenths
    free = args.free if args.free is not None else squad.free_transfers

    weeks = min(args.weeks, MAX_PLAN_WEEKS)
    if args.weeks > MAX_PLAN_WEEKS:
        print(f"Capped at {MAX_PLAN_WEEKS} weeks, past which the solve takes seconds.")

    plan = plan_transfers(
        projections,
        by_gw,
        squad.player_ids,
        selling_prices=squad.selling_prices,
        bank_tenths=bank,
        free_transfers=free,
        weeks=weeks,
        max_transfers_per_week=args.max,
        bench_weight=args.bench_weight,
        formation=parse_formation(args.formation),
    )

    print(f"\nPlan over {len(plan.weeks)} gameweeks, projected {plan.projected:.1f} pts")
    if plan.hits:
        print(f"Taking {plan.hits} hits, costing {plan.hits * 4} points.")
    if plan.approximate_money:
        print("Some selling prices are unknown, so the money here is optimistic.")

    for week in plan.weeks:
        moves = len(week.transfers_in)
        header = f"\nGW{week.event}: " + (f"{moves} transfer(s)" if moves else "roll")
        print(f"{header}, bank {week.bank_tenths / 10:.1f}m, {week.free_transfers} free")
        if moves:
            print("  out " + ", ".join(week.transfers_out["name"].astype(str)))
            print("  in  " + ", ".join(week.transfers_in["name"].astype(str)))
        print(f"  captain {week.captain['name']}, projected {week.projected:.1f}")

    print("\nPrices are held at today's across the horizon, so the further out a")
    print("week is, the less its bank figure is worth.")


def cmd_prices(args, season, projections, by_gw):
    pressure = price_pressure(season)
    if is_dormant(pressure):
        print("\nNo meaningful transfer activity yet, so nothing is close to moving.")
        print("Price pressure needs a gameweek or two of a live season to mean anything.")
        return

    for direction, label in (("rise", "Closest to a rise"), ("fall", "Closest to a fall")):
        table = movers(pressure, direction=direction, top=args.top)
        print(f"\n{label}")
        print(_fmt(table, PRICE_COLS) if len(table) else "  nobody")

    print("\nThe API gives a running total of net transfers since the gameweek opened,")
    print("not a rate, so a player who took five days to gather his looks the same as")
    print("one who did it this morning. Treat this as a watchlist, not a forecast.")


def cmd_live(args, season, projections, by_gw):
    gameweek = args.gameweek or season.current_gameweek
    if gameweek < 1:
        print("\nNo gameweek has started yet, so there is nothing live to show.")
        return

    state = live.load_live(season, gameweek)
    if state.fixtures.empty:
        print(f"\nGW{gameweek} has no fixtures published yet.")
        return

    played = int(state.fixtures["finished"].sum())
    status = "in play" if state.in_play else ("all played" if state.all_settled else "not started")
    print(f"\nGW{gameweek}, {status}. {played} of {len(state.fixtures)} fixtures finished.")
    if not state.all_settled:
        print("Bonus on unfinished matches is provisional and can still move.")

    try:
        squad = load_squad(season, args.squad, args.entry)
    except (RuntimeError, ValueError) as exc:
        print(f"\nNo squad to score: {exc}")
        return

    score = live.score_squad(state, season, squad)
    print(f"\nYour score: {score.total} pts, {score.playing} playing, {score.to_play} to play")
    if score.provisional_bonus:
        print(f"Of which {score.provisional_bonus} is provisional bonus.")
    if not score.lineup.settled:
        print("Not every match is over, so autosubs below are a projection.")

    print("\nStarting XI")
    print(_fmt(live.player_view(state, season, score.lineup.starters), LIVE_COLS))
    print("\nBench")
    print(_fmt(live.player_view(state, season, score.lineup.bench), LIVE_COLS))

    for out, came_in in score.lineup.subs:
        names = season.players["name"]
        print(f"\nAuto sub: {names.get(out, out)} off, {names.get(came_in, came_in)} on")


def cmd_find(args, season, projections, by_gw):
    query = args.query.lower()
    p = season.players
    hits = p[
        p["web_name"].str.lower().str.contains(query)
        | p["full_name"].str.lower().str.contains(query)
    ]
    view = projections.loc[projections.index.intersection(hits.index)]
    print(
        _fmt(
            view.reset_index(),
            ["id", "name", "position", "club", "price", "xpts_next", "xpts_total", "news"],
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fpl_manager", description=__doc__)
    parser.add_argument(
        "--horizon", type=int, default=6, help="gameweeks to project over (default 6)"
    )
    parser.add_argument(
        "--no-prior", action="store_true", help="skip last season's data, use this season only"
    )
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    parser.add_argument("--bench-weight", type=float, default=0.12)
    parser.add_argument(
        "--formation",
        choices=[format_formation(f) for f in FORMATIONS],
        help="pin the shape of the XI instead of letting the solver choose it",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="pick a squad from scratch")
    b.add_argument("--budget", type=float, default=100.0)
    b.add_argument("--include", type=int, nargs="*", help="player ids to force in")
    b.add_argument("--exclude", type=int, nargs="*", help="player ids to rule out")
    b.add_argument("--save", help="write the result to a squad file")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("ticker", help="fixture difficulty by club")
    t.set_defaults(func=cmd_ticker)

    pl = sub.add_parser("players", help="ranked player list")
    pl.add_argument("--position", choices=["GKP", "DEF", "MID", "FWD", "gkp", "def", "mid", "fwd"])
    pl.add_argument("--club")
    pl.add_argument("--max-price", type=float)
    pl.add_argument("--top", type=int, default=20)
    pl.add_argument(
        "--sort",
        default="xpts_total",
        choices=["xpts_total", "xpts_next", "value", "ownership", "differential"],
    )
    pl.set_defaults(func=cmd_players)

    tr = sub.add_parser("transfers", help="suggest this week's moves")
    tr.add_argument("--squad", help="path to squad.json")
    tr.add_argument("--entry", type=int, help="your FPL entry id")
    tr.add_argument("--bank", type=float)
    tr.add_argument("--free", type=int)
    tr.add_argument("--max", type=int, default=2, help="most transfers to consider")
    tr.set_defaults(func=cmd_transfers)

    x = sub.add_parser("xi", help="pick the lineup and captain")
    x.add_argument("--squad")
    x.add_argument("--entry", type=int)
    x.set_defaults(func=cmd_xi)

    ch = sub.add_parser("chips", help="when to play Bench Boost, Triple Captain or Free Hit")
    ch.add_argument("--squad")
    ch.add_argument("--entry", type=int)
    ch.add_argument(
        "--budget-tenths", type=int, help="Free Hit budget, defaults to your team value"
    )
    ch.add_argument("--all", action="store_true", help="show every gameweek, not just the best")
    ch.set_defaults(func=cmd_chips)

    pn = sub.add_parser("plan", help="transfers across several gameweeks as one problem")
    pn.add_argument("--squad")
    pn.add_argument("--entry", type=int)
    pn.add_argument("--bank", type=float)
    pn.add_argument("--free", type=int)
    pn.add_argument("--weeks", type=int, default=3, help=f"up to {MAX_PLAN_WEEKS}")
    pn.add_argument("--max", type=int, default=2, help="most transfers in any one week")
    pn.set_defaults(func=cmd_plan)

    pr = sub.add_parser("prices", help="who is closest to a price rise or fall")
    pr.add_argument("--top", type=int, default=15)
    pr.set_defaults(func=cmd_prices)

    lv = sub.add_parser("live", help="what your squad is scoring right now")
    lv.add_argument("--squad")
    lv.add_argument("--entry", type=int)
    lv.add_argument("--gameweek", type=int, help="defaults to the one under way")
    lv.set_defaults(func=cmd_live)

    f = sub.add_parser("find", help="look up player ids by name")
    f.add_argument("query")
    f.set_defaults(func=cmd_find)

    args = parser.parse_args(argv)

    # Windows consoles still default to a legacy code page, which turns Guehi
    # and Joao Pedro into mojibake. The API is UTF-8, so say so before printing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    api = FplApi(ttl=0 if args.refresh else 6 * 3600)
    season = Season(api)
    prior = _load_prior(season, use_prior=not args.no_prior)
    projections, by_gw = project(season, horizon=args.horizon, prior=prior)

    pd.set_option("display.width", 200)
    args.func(args, season, projections, by_gw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
