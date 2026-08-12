"""Command line entry point.

python -m fpl_manager build --horizon 6
python -m fpl_manager ticker --horizon 8
python -m fpl_manager players --position MID --top 25
python -m fpl_manager transfers --squad squad.json --free 1 --max 2
python -m fpl_manager xi --squad squad.json
python -m fpl_manager find haaland
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import chips
from .api import FplApi
from .data import Season
from .optimiser import build_squad, pick_xi, suggest_transfers
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


def _print_squad(result, label: str) -> None:
    print(f"\n{label}")
    print(f"Cost {result.cost:.1f}m, projected {result.projected:.1f} pts")
    print("\nStarting XI")
    print(_fmt(result.xi, SQUAD_COLS))
    print("\nBench, in order")
    print(_fmt(result.bench, SQUAD_COLS))
    print(f"\nCaptain: {result.captain['name']}  Vice: {result.vice_captain['name']}")


def cmd_build(args, season, projections, by_gw):
    result = build_squad(
        projections,
        budget_tenths=round(args.budget * 10),
        bench_weight=args.bench_weight,
        include=args.include,
        exclude=args.exclude,
    )
    _print_squad(result, f"Squad for GW{season.next_gameweek} onwards")
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

    result = suggest_transfers(
        projections,
        current_ids=squad.player_ids,
        selling_prices=squad.selling_prices,
        bank_tenths=bank,
        free_transfers=free,
        max_transfers=args.max,
        bench_weight=args.bench_weight,
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
    _print_squad(result, "Resulting squad")


def cmd_xi(args, season, projections, by_gw):
    squad = load_squad(season, args.squad, args.entry)
    frame = squad.frame(projections)
    xi, bench, captain = pick_xi(frame)
    print(f"\nGW{season.next_gameweek} lineup")
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
    print(f"Free Hit budget is your team value, {budget / 10:.1f}m\n")
    print("Best gameweek for each chip")
    print(_fmt(chips.best_per_chip(table), ["chip", "event", "gain", "baseline", "detail"]))

    if args.all:
        print("\nEvery gameweek, best first")
        print(_fmt(table, ["chip", "event", "gain", "baseline"]))

    print("\nGain is on top of what that squad scores anyway. The model cannot see")
    print("rotation or press conferences, so treat a close call as a coin toss.")


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
        "--sort", default="xpts_total", choices=["xpts_total", "xpts_next", "value", "ownership"]
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
