"""Smoke tests for the command line front end.

The CLI is the second view over the library and had no coverage at all, so a
change that broke printing would only have shown up by running it. These do not
check the wording, they check that every subcommand parses its arguments, walks
the library and prints something without raising.
"""

from __future__ import annotations

import json

import pytest

from fpl_manager import api, cli
from fpl_manager import projections as proj_module
from fpl_manager.data import Season

from .conftest import FakeApi, make_prior


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """The CLI pointed at synthetic data, with no network and no disk cache."""
    fake = FakeApi(played=12)
    for name in ("bootstrap", "fixtures", "live", "fixtures_for_event", "element_summary"):
        monkeypatch.setattr(api.FplApi, name, _delegate(fake, name))
    monkeypatch.setattr(api.FplApi, "_get", lambda self, *a, **kw: {})

    prior_path = tmp_path / "prior_season.parquet"
    make_prior(Season(fake)).to_parquet(prior_path)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", prior_path)
    return fake


def _delegate(fake, name):
    method = getattr(fake, name)
    return lambda self, *args, **kwargs: method(*args, **kwargs)


@pytest.fixture
def squad_file(tmp_path, wired):
    """A solved squad written out, XI first so pick order is a real lineup.

    Solved rather than hand picked, because a squad assembled by taking the
    first few of each position lands four players from one club and the
    transfer solver then has no legal move at all.
    """
    from fpl_manager.optimiser import build_squad, pick_xi
    from fpl_manager.projections import project

    season = Season(wired)
    projections = project(season, horizon=3, prior=make_prior(season))[0]
    result = build_squad(projections)
    xi, bench, _ = pick_xi(result.squad)

    path = tmp_path / "squad.json"
    path.write_text(json.dumps([int(p) for p in [*xi.index, *bench.index]]))
    return str(path)


@pytest.mark.parametrize(
    "argv",
    [
        ["--horizon", "3", "build"],
        ["--horizon", "3", "ticker"],
        ["--horizon", "3", "players", "--position", "MID", "--top", "5"],
        ["--horizon", "3", "players", "--sort", "value", "--max-price", "8.0"],
        ["--horizon", "3", "find", "a"],
        ["--horizon", "3", "prices"],
        ["--horizon", "3", "captains", "--top", "5"],
        ["--horizon", "3", "captains", "--position", "MID"],
    ],
)
def test_a_subcommand_that_needs_no_squad_runs(wired, capsys, argv):
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize(
    "argv",
    [
        ["--horizon", "3", "xi"],
        ["--horizon", "3", "transfers", "--max", "1"],
        ["--horizon", "3", "chips"],
        ["--horizon", "3", "live"],
        ["--horizon", "3", "plan", "--weeks", "2", "--max", "1"],
    ],
)
def test_a_subcommand_that_needs_a_squad_runs(wired, squad_file, capsys, argv):
    assert cli.main([*argv, "--squad", squad_file]) == 0
    assert capsys.readouterr().out.strip()


def test_a_pinned_formation_reaches_the_lineup(wired, squad_file, capsys):
    """The flag is on the parent parser, so it goes before the subcommand."""
    assert cli.main(["--horizon", "3", "--formation", "3-4-3", "xi", "--squad", squad_file]) == 0
    assert "lineup (3-4-3" in capsys.readouterr().out


def test_an_illegal_formation_is_refused_at_the_parser(wired, squad_file):
    with pytest.raises(SystemExit):
        cli.main(["--horizon", "3", "--formation", "2-5-3", "xi", "--squad", squad_file])


def test_a_build_says_what_pinning_the_shape_cost(wired, capsys):
    """A free build names its shape and owes nothing. Pinning a different one
    does, and which shape that is depends on the season, so it is read off the
    free run rather than guessed at."""
    from fpl_manager.data import FORMATIONS, format_formation

    assert cli.main(["--horizon", "3", "build"]) == 0
    free = capsys.readouterr().out
    assert "Starting XI (" in free
    assert "behind the best shape" not in free

    shapes = [format_formation(f) for f in FORMATIONS]
    other = next(f for f in shapes if f"Starting XI ({f})" not in free)
    assert cli.main(["--horizon", "3", "--formation", other, "build"]) == 0
    out = capsys.readouterr().out
    assert f"Starting XI ({other}," in out
    assert "behind the best shape" in out


def test_live_scores_the_gameweek_under_way(wired, squad_file, capsys):
    cli.main(["--horizon", "3", "live", "--squad", squad_file])
    out = capsys.readouterr().out

    assert "Your score:" in out
    assert "Starting XI" in out


def test_live_says_so_rather_than_raising_before_a_kickoff(monkeypatch, wired, squad_file, capsys):
    """Out of season every live endpoint is empty. Saying nothing is being
    played beats a traceback."""
    monkeypatch.setattr(api.FplApi, "live", lambda self, gw: {})
    monkeypatch.setattr(api.FplApi, "fixtures_for_event", lambda self, gw: [])

    assert cli.main(["--horizon", "3", "live", "--squad", squad_file]) == 0
    assert "no fixtures published" in capsys.readouterr().out


def test_build_writes_a_squad_file(wired, tmp_path):
    out = tmp_path / "built.json"
    assert cli.main(["--horizon", "3", "build", "--save", str(out)]) == 0

    saved = json.loads(out.read_text())
    assert len(saved["players"]) == 15


def test_plan_is_capped_at_the_solve_limit(wired, squad_file, capsys):
    """Five weeks takes five or six seconds against under one for three, so the
    cap is a real limit rather than a suggestion, and it has to say so."""
    from fpl_manager.optimiser import MAX_PLAN_WEEKS

    assert cli.main(["--horizon", "6", "plan", "--weeks", "9", "--squad", squad_file]) == 0
    out = capsys.readouterr().out
    assert f"Capped at {MAX_PLAN_WEEKS} weeks" in out


def test_prices_lists_movers_in_both_directions(wired, capsys):
    cli.main(["--horizon", "3", "prices", "--top", "3"])
    out = capsys.readouterr().out

    assert "Closest to a rise" in out
    assert "Closest to a fall" in out
    assert "watchlist, not a forecast" in out


def test_prices_says_it_is_dormant_pre_season(monkeypatch, tmp_path, capsys):
    """Every counter is zero before a ball is kicked, and a table of zeroes
    reads as a prediction rather than as no data."""
    fake = FakeApi(played=0)
    for name in ("bootstrap", "fixtures", "element_summary"):
        monkeypatch.setattr(api.FplApi, name, _delegate(fake, name))
    monkeypatch.setattr(api.FplApi, "_get", lambda self, *a, **kw: {})

    prior_path = tmp_path / "prior_season.parquet"
    make_prior(Season(fake)).to_parquet(prior_path)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", prior_path)

    cli.main(["--horizon", "3", "prices"])
    assert "nothing is close to moving" in capsys.readouterr().out
