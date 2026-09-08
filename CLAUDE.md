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
uv run fpl-manager backtest                # how accurate the projection has been
uv run fpl-manager backtest --sweep SHRINKAGE_GAMES=3,6,9   # re-score at each value
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
| `backtest.py` | Rewinds the season to any played gameweek and scores what the projection would have said. Sweeps a constant. | Anything the projection reads. It consumes the model and never feeds it |
| `captaincy.py` | Haul and return chances for the armband, and what it is worth against the field. Distributions, not point estimates. | Anything the optimiser reads. It is a leaf on purpose |
| `squad.py` | Loading the user's 15, bank, selling prices, chips spent. | Projections or optimisation |
| `leagues.py` | Public manager profiles, classic league tables, and monthly totals. | Anything that scores or ranks players |
| `elite.py` | What the top managers own and captain, counted off their squads. | Anything forward looking. It counts squads that already exist |
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
argue with.

It also may not import `elite.py`, which is the rule most likely to be broken by
accident now that `field_gain` exists: knowing what the field captains is
exactly what that arithmetic needs, and it takes the shares as an **argument**.
`app.py` holds both halves and does the joining. A test parses the imports.

**`elite.py` is a leaf for a third reason.** It reads the top of the overall
league and counts what those managers own and captain. Nothing that projects or
picks may import it, and `captaincy.py` in particular may not, even though
knowing what the field captains is exactly what the rank question needs: it is
held to `data` and `projections`, so when that arithmetic gets built the shares
go in as an argument rather than as an import. It is dormant until a gameweek
has been scored, since picks are not published before then, and it is the most
expensive thing the app does at one request per manager, which is why a sidebar
toggle gates it.

All three leaf rules have tests in `tests/test_pipeline.py` that parse the
imports, so none of them can be undone by accident.

**`backtest.py` is a consumer and stays one.** It reads realised points off
`event/{gw}/live/` to score the projection, which makes it an outcome rather
than evidence, the same reason `live.py` is a leaf. Nothing that projects or
picks may import it, and the same test file holds that.

`Season` (in `data.py`) is the single object holding a loaded season. Pass it
around rather than re-instantiating, since construction does two API calls.

**There are two front ends and neither is the product.** `app.py` (Streamlit) is
the primary one, `cli.py` is for scripting and quick checks. Both are thin views
over the same library, which is what stops them disagreeing about what the best
squad is. If a function prints or calls `st.`, it belongs in a front end and
nowhere else. `tests/test_app.py` asserts that model logic has not leaked into
`app.py`, so adding a solve there will fail the suite.

**Each tab answers one question, and the single week answer lives in exactly one
of them.** My squad is what you own plus this week's move, Wildcard is what you
would buy starting over, Planner is the route across several gameweeks. Two tabs
solving the same week is two tabs that can disagree about the best move, and
there is a test asserting neither the one week controls nor the planner slider
appear twice. My squad also sits ahead of Players, so **its table must not have
`GW`-prefixed columns**: `tests/test_app.py` finds the player pool by taking the
first dataframe that has one, and a second would shadow it.

**The entry id is remembered in the URL and nowhere else.** `st.query_params`
holds it, seeded into `st.session_state["sidebar_entry"]` before the widget is
built, which is the only point a widget's own key may be assigned. The deploy is
public and multi-tenant so there is nowhere per user to write, a cookie library
would cost packages the deploy is deliberately without, and an entry id is
public anyway. It is a text box rather than a number because an id is not a
quantity, so anything reading `sidebar_entry` gets a string that may be empty.

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

**There are two of every chip, one per half of the season.** The bootstrap
publishes the catalogue and `Season.chip_windows` parses it: wildcard and free
hit from gameweek 2 to 19 and again from 20 to 38, bench boost and triple
captain the same way from gameweek 1. So what a manager has left is a question
about a **gameweek**, never about a season, which is what
`chips.available_chips` answers off `squad.chips_played`. Anything reducing that
to a flat set of names will withdraw a chip the manager is still holding, and
will also miss that wildcard and free hit cannot be played in gameweek 1.

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
`SHRINKAGE_GAMES`, `ROLE_SHRINKAGE_GAMES`, `DIFFICULTY_ALPHA`, `HOME_BONUS`,
`START_RATE_TRUST`,
`SUB_SHARE`, `STARTER_DURATION`, `STRENGTH_WEIGHT`, `STRENGTH_ALPHA`,
`PENALTY_XG_P90`, `FREEKICK_XG_P90`, `COMPONENT_MINUTES`, `PRIOR_MINUTES`,
`SAVE_REMAINDER`, `TEAM_DEFENCE_MINUTES`, `CLUB_STRENGTH_MINUTES`. `PRIOR_MINUTES` is a sample floor on a finished season and
not a tuning knob, and it is deliberately a separate name from
`COMPONENT_MINUTES` even though both are 270. That one scales a season in
progress, this one gates one that is over.

**Every constant is now a measured quantity, so measure before moving one.**
`fpl-manager backtest` rebuilds the bootstrap as it stood after each played
gameweek out of the live payloads, projects the next one and scores it, with
last season's points over 38 and this season's points per game as the two
baselines it has to beat. `--sweep NAME=v1,v2` re-scores at each value of a
constant. Read rank correlation first, then what the top fifty went on to
score; bias and error are dominated by the half of the game that does not
play. Two things it cannot know: availability and prices as they stood then,
so a player injured at the time projects as fit, and every figure is given
over everyone and over those who played. A default argument that names a
constant binds it at import, which is why `fixture_multiplier` and
`credibility` read theirs at call time: a sweep of a bound one reports the
same number at every value and looks like a finding.

**The role is trusted faster than the rate, on purpose.** `ROLE_SHRINKAGE_GAMES`
governs how fast this season's starts replace last season's in the minutes
term, `SHRINKAGE_GAMES` how fast this season's points replace last season's
in the scoring rate, and the first is a third of the second. A start is a
decision a manager has already made and it persists; a rate off the same
matches is a sample. Measured on GW1 to GW3 of 2026/27, moving the role
alone took the rank correlation with realised points from 0.48 to 0.53 and
moving the rate alone moved nothing.

**FPL's attack and defence ratings are zero this season, for every club.**
Checked on 2026-09-08, three gameweeks in. `club_strength` rebuilds them from
expected goals, the club's summed player xG for attack and the keepers' xGC
for defence, both shrunk to the league mean on `CLUB_STRENGTH_MINUTES`, ten
matches rather than the three the clean sheet term uses, because a
multiplier on every player at both clubs cannot afford the spread three
matches of xG carry, and
`fill_strength` puts them in only where FPL's are absent, all or nothing,
because the two are on different scales. If FPL's come back they win. Before
anything has been played the term is empty and the fixture multiplier is the
1 to 5 rating alone, as it always was in August.

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

**The live payloads move at two different times, and reading the wrong one is
the mistake this code keeps making.** A fixture updates at the whistle:
`finished_provisional` goes true, and the real `bonus` block appears, both a day
or more before FPL sets `finished`. Everything hanging off an entry, meaning
`automatic_subs`, each pick's `multiplier` and the gameweek total in the entry
history, is recomputed only at that audit. Measured on GW1 2026/27 with nine of
ten matches played out: the entry history said 29 while its own live figures
summed to 35.

That has produced the same bug twice. `finished` for "is this match over" left
the fixture counter reading zero of ten and automatic substitutions unresolved
all weekend, fixed by `LiveGameweek.played_out`. `finished` for "is this bonus
FPL's or ours" called FPL's own award provisional for the whole weekend, fixed
by `LiveGameweek.bonus_is_final`, which asks whether any started fixture is
still waiting on its bonus block. Read `played_out` and `bonus_is_final`, never
`finished` directly, and if a third question comes up, work out which payload
answers it before picking a flag. `docs/gotchas.md` carries the detail.

**The bonus ranking is now checked against real awards.**
`tests/test_live_against_the_api.py` diffs it against the `bonus` block FPL
published, along with the substitutions, the eleven and the total, using the API
as its own oracle. It is `network` marked so it stays out of CI and out of a
normal run; `uv run pytest -m network` is the way in, and it is worth running
after a gameweek is audited. Nine GW1 fixtures agreed, tie cases included. Still
unproven: a gameweek containing an actual automatic substitution, and a double,
where the per-fixture and summed bonus figures diverge.

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
  displayed in the comparison on the Players tab, the one that appears when you
  tick two or more rows, so it can disagree with ours. Consuming it would make
  our projection partly a copy of theirs and take away the ability to say why a
  number is what it is. It is deliberately absent from `STAT_VIEWS`, so the
  comparison stays the only place it renders.
- Overstate what the model does. It produces a ranked shortlist to argue with.
  Rotation risk, press conference hints and minutes management are not in the
  API and the output should not imply otherwise.

## Not built yet

The backlog lives in `ROADMAP.md`, including the things that were considered and
turned down, and why.

One item from it will mislead you mid-task if you do not know it:
**`prior_season.parquet` needs a manual refresh between seasons**, or in August
the projection quietly rests on a season two years old.
