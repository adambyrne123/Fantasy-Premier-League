# CLAUDE.md

Context for working on this repo. Read before making changes.

This file holds what bites you without warning, wherever you happen to be
working. The detail that only matters once you are already in a given area lives
in three reference docs, and each section below says when to open them:

| Doc | Read it before |
|---|---|
| `docs/model.md` | changing any projection term |
| `docs/gotchas.md` | touching the API, caching, live scoring or the solver |
| `docs/delivery.md` | changing dependencies or debugging the deployed app |

## What this is

A tool that builds and maintains a Fantasy Premier League squad. It pulls the
public FPL API, projects points per player per gameweek, and solves for the best
legal 15 under budget, club cap and position quotas.

It is read-only against FPL. It advises, the user executes on the site.

## Commands

```bash
uv sync --extra app                        # create env, install deps
uv run streamlit run app.py                # the main front end
uv run pytest                              # full suite, no network
uv run pytest tests/test_pipeline.py -k transfer   # one area
uv run ruff check . --fix
uv run ruff format .

uv run fpl-manager build --horizon 6 --save squad.json
uv run fpl-manager ticker --horizon 8
uv run fpl-manager players --position MID --top 25 --sort value
uv run fpl-manager transfers --squad squad.json --free 1 --max 2
uv run fpl-manager xi --squad squad.json
uv run fpl-manager captains --top 20    # who to captain, and how safe
uv run fpl-manager find salah              # resolve player ids by name
uv run fpl-manager chips --squad squad.json   # when to play each chip
uv run fpl-manager live --entry 1234567    # what your squad is scoring now
```

Global flags live on the parent parser, not the subcommands. `--horizon`,
`--no-prior`, `--refresh`, `--bench-weight` and `--formation` all go before the
subcommand name.

Run `ruff check` and `pytest` before considering any change finished. Both are
fast, so there is no reason to skip either.

## Architecture

Data flows one way. Nothing downstream writes back.

```
api.py  ──▶  data.py  ──▶  projections.py  ──▶  optimiser.py  ──▶  cli.py
             │  │  │        │  ▲                       ▲
             │  │  ▼        ▼ squad.py             chips.py
             │  │ prices.py │ (current holdings)   (chip timing)
             │  ▼           ▼
             │ live.py    captaincy.py
             ▼ (in-play)  (haul chances)
          (raw frames)
```

| Module | Responsibility | Do not put here |
|---|---|---|
| `api.py` | HTTP, disk cache, TTLs. One method per endpoint. | Any parsing or business logic |
| `data.py` | Raw JSON to tidy frames. FPL rule constants. | Anything opinionated about player quality |
| `projections.py` | The points model. Three separable terms. | Selection logic |
| `optimiser.py` | MILP formulation and solving, single week and multi. | Anything that fetches or estimates |
| `chips.py` | What a chip is worth, and in which gameweek. | New MILP formulations, which go in `optimiser.py` |
| `prices.py` | Who is close to a price rise or fall. | Anything the points model reads |
| `live.py` | In-play scoring: live stats, provisional bonus, autosubs. | Anything forward looking, and any selection logic |
| `roi.py` | Points already returned per million. | Projections, which look forward |
| `captaincy.py` | Haul and return chances for the armband. Distributions, not point estimates. | Anything the optimiser reads. It is a leaf on purpose |
| `squad.py` | Loading the user's 15, bank, selling prices. | Projections or optimisation |
| `leagues.py` | Public manager profiles and classic league tables. | Anything that scores or ranks players |
| `cli.py` | Argument parsing and printing. | Model logic of any kind |
| `app.py` | Streamlit view: widgets, layout, caching. | Model logic of any kind |

**`live.py` is a leaf and must stay one.** `projections.py`, `optimiser.py` and
`chips.py` may not import it. They look forward on a six hour cache, it looks at
the last sixty seconds, and joining the two puts numbers that disagree on the
same screen. Live points are a realised outcome, not evidence for a projection.

**`captaincy.py` is a leaf for the opposite reason.** Nothing that picks a
squad may import it. The objective is a mean, and a variance term in it was
argued out once already and turned down as precision theatre on a model this
rough. A haul chance read beside the projection is the useful version of caring
about variance, and one folded into the objective is a squad the user cannot
argue with. Both leaf rules now have tests in `tests/test_pipeline.py` that
parse the imports, so neither can be undone by accident.

`Season` (in `data.py`) is the single object holding a loaded season. Pass it
around rather than re-instantiating, since construction does two API calls.

**There are two front ends and neither is the product.** `app.py` (Streamlit) is
the primary one, `cli.py` is for scripting and quick checks. Both are thin views
over the same library, which is what stops them disagreeing about what the best
squad is. If a function prints or calls `st.`, it belongs in a front end and
nowhere else. `tests/test_app.py` asserts that model logic has not leaked into
`app.py`, so adding a solve there will fail the suite.

## Domain rules that must never be violated

Game rules, not preferences. Any change to `optimiser.py` has to keep all of
them true, and `tests/test_pipeline.py` asserts each one.

- Squad is exactly 15: 2 GKP, 5 DEF, 5 MID, 3 FWD
- Budget 100.0m for a fresh squad
- Maximum 3 players from any one club
- Starting XI is 11, with exactly 1 GKP, at least 3 DEF, at least 2 MID, at
  least 1 FWD
- Exactly one captain, and the captain must be in the XI
- Transfers beyond the free allowance cost 4 points each

Constants live at the top of `data.py`: `SQUAD_LIMITS`, `XI_MIN`, `XI_MAX`,
`BUDGET_TENTHS`, `MAX_PER_CLUB`. Import them, do not re-declare.

**The formation is an outcome, not a constraint.** The XI rules above are a
range, so the solver picks whatever shape scores most and the pitch caption
counts it back off the chosen XI. On real data that is 5-4-1 nearly every time,
which reads like a pin and is not one. Pass `formation` to `build_squad`,
`suggest_transfers`, `plan_transfers` or `pick_xi` to overrule it, built with
`parse_formation` from `FORMATIONS`, which is generated from `XI_MIN`/`XI_MAX`
rather than listed. `formation=None` reproduces the free solve exactly and
there is a test asserting it. `chips.py` is deliberately left free: a chip is
priced against your normal week and both sides of that comparison have to
choose their lineup the same way.

**Selection is a mixed integer programme, not a sort**, and multi-week planning
is one MILP rather than a loop of weekly ones. Both have tests asserting it. See
`docs/model.md` before reformulating either.

## Conventions

**Prices are in tenths of a million, everywhere except display.** The API gives
`now_cost: 55` meaning 5.5m. Keeping it integer makes the budget constraint
exact rather than floating point. Variables holding tenths are suffixed
`_tenths`. Divide by 10 only when printing.

**Player ids are the join key.** The FPL `element` id is the index on every
player frame. Names are for humans and are not unique.

**British spelling in code and docs**: `optimiser`, `behaviour`. The module is
`optimiser.py`.

**App styling lives in two places and nowhere else.** `.streamlit/config.toml`
holds the theme, and `PITCH_CSS` at the top of `app.py` holds the pitch and
shirt rules. Colour a fixture through `fdr_css` rather than a pandas colour map,
since `Styler.background_gradient` drags in matplotlib for one call and the
deploy is 6 packages lighter without it.

**Prose style in comments, docstrings, README and commit messages**: plain and
direct. No em dashes. Explain why a thing is done, not what the line does.
Existing docstrings set the register, match them.

**Do not invoke the bundled `developing-with-streamlit` skill for ordinary `st.`
edits.** It ships inside the installed package at
`.venv/Lib/site-packages/streamlit/.agents/skills/` with around 25 reference
documents, and it costs more context than the edit. It earns that only for a
genuine Streamlit API question.

## The model

```
xPts(player, gw) = sum over that club's fixtures in gw of
                   points_per_90 * expected_minutes_share * fixture_multiplier
```

Three terms, deliberately separable so any one can be replaced without touching
the others. Tuning constants sit at the top of `projections.py`:
`SHRINKAGE_GAMES`, `DIFFICULTY_ALPHA`, `HOME_BONUS`, `START_RATE_TRUST`,
`SUB_SHARE`, `STARTER_DURATION`, `STRENGTH_WEIGHT`, `STRENGTH_ALPHA`,
`PENALTY_XG_P90`, `FREEKICK_XG_P90`, `COMPONENT_MINUTES`, `PRIOR_MINUTES`,
`SAVE_REMAINDER`. `PRIOR_MINUTES` is a sample floor on a finished season and
not a tuning knob, and it is deliberately a separate name from
`COMPONENT_MINUTES` even though both are 270. That one scales a season in
progress, this one gates one that is over.

**The minutes term must not be derived from minutes.** This is the trap the
model already fell into once. If `expected_minutes_share` is computed as
`minutes / (38 * 90)`, it cancels exactly against `points_per_90` and the three
terms collapse into last season's total points. Rebuilding minutes out of
`starts` reconstructs the same number and does not help. There is a test
asserting the identity has not come back.

**Read `docs/model.md` before changing any term.** It has the component rate
formula, why the clean sheet and the conceded charge share one Poisson, the
approximation in the defensive contribution term, and the fixture strength
normalisation that the headline number being points depends on.

## Traps that bite without warning

**The season rollover silently serves last season's numbers.** Before the first
deadline every counting stat is last season's, including `total_points`,
`minutes`, `expected_goals`, `saves`, cards and the defensive contribution
parts. There is no schema change to notice. What protects the model in August is
the blend weight being 0, and now that the 270 minute bar is a scale rather than
a gate that is the **only** thing protecting it. Those fields are read through
`attacking_rates`, which `component_rate` goes through so the rates have one
definition, and everything on that path sits behind the weight.

`captaincy.py` is the exception and reads them in front of the weight, which is
why it carries an explicit `gameweeks_played > 0` gate of its own. Anything new
reading them needs the same, and **a guard that only counts minutes is not a
guard**. Checked against the live payload on 2026-08-19, two days before GW1:
median minutes 581, maximum 3420, and 56% of the game already clearing a 270
minute bar on figures that are entirely last season's.
Anything ranking on `total_points` should go through `roi.points_source`. Full
detail, including what `tests/conftest.py` does and does not simulate, is in
`docs/model.md`.

**Pre-season means no current data.** `gameweeks_played` is 0 until late August,
so projections rest entirely on last season. Any new model term needs a defined
pre-season behaviour.

**A player's own numbers fade in, they do not switch on.** `credibility` is
`min(minutes / COMPONENT_MINUTES, 1)` and multiplies `weight_now` wherever
current-season data is blended. It is exactly 1 at and above 270 minutes, so
nothing mid-season moved when it replaced the bar, and the minutes cancel below
it so a cameo contributes the points scored rather than the rate they imply.
Do not swap it for the `m / (m + k)` form used by `weight_now`; `docs/model.md`
says why, and there is a test that fails if you do.

**Doubles and blanks are already handled.** `Season.team_fixtures` emits one row
per club per fixture. Do not add special casing. If you find yourself writing
`if is_double_gameweek`, the design has gone wrong. `Season.gameweek_shape` is
the one deliberate exception, explained in `docs/gotchas.md`.

**Route solves through `optimiser.solver()`**, never a solver constructed
inline, or they work everywhere except Windows on ARM, where PuLP bundles no CBC
binary.

**Streamlit caching splits by type, and the stamp is not optional.** `Season`
uses `@st.cache_resource`, frames use `@st.cache_data`, and a `Season` passed
into a cached function needs the `_season` underscore prefix. New cached
functions taking `_season` need `season.data_stamp` as their last argument, or a
rebuilt `Season` looks identical to the one it replaced and stale projections
keep being served. See `docs/gotchas.md`.

**A push does not restart the deployed app, and that has taken it down once.**
Modules already in `sys.modules` stay as they were, so adding a name to a
library module and importing it from `app.py` in the same push breaks every
visitor until someone reboots by hand. After a push that adds or renames
anything in `fpl_manager`, load the app and reboot it if it errors.
`docs/delivery.md` explains why the traceback misleads you while it lasts.

**None of the live code has met a real match yet.** It was written pre-season on
synthetic payloads. The empty case is verified against the real API. The bonus
ranking and the substitution rule are not. Treat the first scored gameweek as
the real test.

## Testing

`tests/conftest.py` holds `FakeApi`, which generates a deterministic synthetic
season. The `season` fixture is parametrised over 0 and 12 gameweeks played, so
every test runs once pre-season and once mid-season.

No test touches the network and it must stay that way. New tests use `FakeApi`.
A `network` marker exists for live checks if they ever become necessary, and is
deselected by `addopts` in `pyproject.toml`, so marking a test is enough to keep
it out of a normal run and out of CI. `pytest -m network` is the way back in.

If you add a constraint to the optimiser, add the test that proves it holds.

**`ruff` and `pytest` run on Linux on every push to `main` and every pull
request**, from `.github/workflows/ci.yml`. It installs with
`uv sync --locked --extra app`, and all three parts of that matter: `--extra app`
because `tests/test_app.py` calls `importorskip` and would otherwise let the
suite pass by not running, and `--locked` because Community Cloud installs from
`uv.lock` rather than from `pyproject.toml`, so drift between the two is worth
failing on.

Linux is the point rather than a default. `test_the_bundled_solver_is_preferred_over_a_hand_found_one`
skips on Windows on ARM, where PuLP bundles no binary, so on the machine this was
written on it has never reached its assertion. It now fails rather than skips on
Linux, because a test that is allowed to skip on the one platform it exists to
cover is the same as not having it, and that is the test that would have caught
the regression which took the deploy down.

## Delivery

Streamlit, decided, and deployed to Community Cloud at
https://fantasy-premier-league-ab.streamlit.app/ tracking `main`. The deploy
installs from `uv.lock`, not `requirements.txt`, so anything that has to reach
the deployed app needs `uv sync` and the lockfile committed.

Read `docs/delivery.md` before changing dependencies, regenerating
`requirements.txt`, or debugging the live app. It also records what was
considered and rejected, so those do not get re-proposed.

## Do not

- Add login, credential handling or anything that posts to FPL. Read-only is a
  design decision, not a limitation to be fixed.
- Bypass the cache in `api.py` by calling `requests` directly.
- Hardcode gameweek numbers or the current season. Use `Season.next_gameweek`
  and `Season.gameweeks_played`.
- Read `ep_next` from anywhere but the front end. It is FPL's own projection,
  displayed in the head to head section of the Players tab so it can disagree
  with ours. Consuming it would make our projection partly a copy of theirs and
  take away the ability to say why a number is what it is.
- Overstate what the model does. It produces a ranked shortlist to argue with.
  Rotation risk, press conference hints and minutes management are not in the
  API and the output should not imply otherwise.

## Not built yet

The backlog lives in `ROADMAP.md`, including the things that were considered and
turned down, and why.

One item from it will mislead you mid-task if you do not know it:
**`prior_season.parquet` needs a manual refresh between seasons**, or in August
the projection quietly rests on a season two years old.
