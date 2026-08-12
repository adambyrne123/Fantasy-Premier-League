# CLAUDE.md

Context for working on this repo. Read before making changes.

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
uv run fpl-manager find salah              # resolve player ids by name
uv run fpl-manager chips --squad squad.json   # when to play each chip
```

Global flags live on the parent parser, not the subcommands. `--horizon`,
`--no-prior`, `--refresh` and `--bench-weight` all go before the subcommand name.

Run `ruff check` and `pytest` before considering any change finished. Both are
fast, so there is no reason to skip either.

## Architecture

Data flows one way. Nothing downstream writes back.

```
api.py  ──▶  data.py  ──▶  projections.py  ──▶  optimiser.py  ──▶  cli.py
                              ▲                       ▲
                          squad.py                chips.py
                    (current holdings, money)   (chip timing)
```

| Module | Responsibility | Do not put here |
|---|---|---|
| `api.py` | HTTP, disk cache, TTLs. One method per endpoint. | Any parsing or business logic |
| `data.py` | Raw JSON to tidy frames. FPL rule constants. | Anything opinionated about player quality |
| `projections.py` | The points model. Three separable terms. | Selection logic |
| `optimiser.py` | MILP formulation and solving. | Anything that fetches or estimates |
| `chips.py` | What a chip is worth, and in which gameweek. | New MILP formulations, which go in `optimiser.py` |
| `squad.py` | Loading the user's 15, bank, selling prices. | Projections or optimisation |
| `cli.py` | Argument parsing and printing. | Model logic of any kind |
| `app.py` | Streamlit view: widgets, layout, caching. | Model logic of any kind |

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

## Gotchas

**The API is undocumented.** These are the endpoints the official site consumes
internally. Schema changes are possible without notice, so parse defensively and
prefer `.get()` over `[]` on optional fields.

**`entry/{id}/event/{gw}/picks/` returns 404 before the first deadline of a
season.** Anything reading a live squad must handle that and fall back to the
local squad file.

**Selling price is not current price.** FPL returns purchase price plus half of
any rise, rounded down to 0.1. The API does not expose it without auth, so it
comes from `squad.json`. If missing, the planner assumes current price and
overstates spending power.

**`element-summary` is one request per player.** Roughly 700 calls for a full
prior-season pull, rate limited by a `time.sleep` in `fetch_prior_season` and
cached to `prior_season.parquet`. Do not remove the delay and do not call it in
a loop from anywhere else.

**Cache location is `FPL_CACHE_DIR`, defaulting to `~/.cache/fpl_manager`.**
Anything running in a container or CI runner must set it to a persisted path,
or every run re-fetches. `PRIOR_CACHE` in `projections.py` derives from it.

**Streamlit caching splits by type.** `Season` holds an HTTP session, so it uses
`@st.cache_resource`. Frames use `@st.cache_data`. Passing `Season` into a
cached function needs the `_season` underscore prefix so Streamlit skips hashing
it. Getting this wrong produces a confusing hashing error rather than a clear
one.

**Doubles and blanks are already handled.** `Season.team_fixtures` emits one row
per club per fixture, so a club with two fixtures in a gameweek gets two rows
and a club with none gets zero. Do not add special casing. If you find yourself
writing `if is_double_gameweek`, the design has gone wrong.

**Pre-season means no current data.** `gameweeks_played` is 0 until late August,
so the shrinkage weight is 0 and projections rest entirely on last season. Any
new model term needs a defined pre-season behaviour.

**PuLP is pinned below 4.0.** The current formulation uses `LpVariable.dicts`
and `PULP_CBC_CMD`, both deprecated in the 3.x line. Unpinning means migrating
to `prob.add_variable_dicts` and `COIN_CMD` first. That is a contained job in
`optimiser.py` and worth doing, but not as a side effect of something else.

**PuLP has no CBC binary for Windows on ARM.** It looks for `solverdir/cbc/win/
arm64/cbc.exe`, which was never shipped, and every solve fails before it starts.
`optimiser.solver()` falls back to the bundled x64 build, which Windows runs
under emulation. Route new solves through `solver()` rather than constructing
`PULP_CBC_CMD` inline, or they will work everywhere except this machine. Note
that `PULP_CBC_CMD` rejects an explicit path, so the fallback uses `COIN_CMD`.

## The model

```
xPts(player, gw) = sum over that club's fixtures in gw of
                   points_per_90 * expected_minutes_share * fixture_multiplier
```

Three terms, deliberately separable so any one can be replaced without touching
the others. Tuning constants sit at the top of `projections.py`:
`SHRINKAGE_GAMES`, `DIFFICULTY_ALPHA`, `HOME_BONUS`, `START_RATE_TRUST`,
`SUB_SHARE`, `STARTER_DURATION`.

**The minutes term must not be derived from minutes.** This is the trap the
model already fell into once. If `expected_minutes_share` is computed as
`minutes / (38 * 90)`, it cancels exactly against `points_per_90`, because
`points/(minutes/90) * minutes/(38*90)` is `points/38`. The three terms then
collapse into last season's total points and the separation is decorative. It
does not help to rebuild minutes out of `starts`, since that reconstructs the
same number.

`_minutes_share` avoids this by shrinking a player's start rate towards what his
price implies, `START_RATE_TRUST` setting the mix. Price is the only input that
is not last season's minutes, so it is what lets a player's scoring rate and his
expected role move independently. There is a test asserting the identity has not
come back. If it fails, the minutes term has stopped doing anything.

`points_per_90` blends last season and this season by `played / (played + 6)`.
Players with no Premier League history get a per-position fit of rate against
price. That fallback is weak and is documented as weak. Do not present it as
better than it is.

Selection is a mixed integer programme, not a sort. Greedy points-per-million
looks reasonable and is reliably a few points short, because what binds is the
interaction between the club cap and the cheap enabler slots. There is a test
asserting the solver beats greedy, so if it starts failing, the formulation has
broken rather than the baseline having got cleverer.

## Testing

`tests/conftest.py` holds `FakeApi`, which generates a deterministic synthetic
season. The `season` fixture is parametrised over 0 and 12 gameweeks played, so
every test runs once pre-season and once mid-season.

No test touches the network and it must stay that way. New tests use `FakeApi`.
A `network` marker exists for live checks if they ever become necessary, and is
deselected by default.

If you add a constraint to the optimiser, add the test that proves it holds.

## Delivery

Streamlit, decided. Interactivity was the priority, and re-solving is fast
enough (well under a second for a full 15-man build) that sliders re-run the
optimiser live rather than needing a submit button.

Deployment target is Streamlit Community Cloud, which is free and public. Nothing
in this repo is secret, and there are no credentials to leak, so a public app is
fine. Mobile rendering is cramped but usable, which was the accepted trade.

Rejected, with reasons, so they do not get re-proposed:

- **Tableau or any BI tool.** Ruled out by the user.
- **Static page from a scheduled GitHub Action.** Cheaper and simpler, but no
  interactivity, which was the thing being optimised for.
- **FastAPI plus a JS front end.** More layout control, far more surface area
  for a solo project where the interesting work is the model.

Consequence worth knowing: there is no machine readable output layer and none is
needed. Streamlit imports the library directly. Do not add a `--json` mode
speculatively.

## Do not

- Add login, credential handling or anything that posts to FPL. Read-only is a
  design decision, not a limitation to be fixed.
- Bypass the cache in `api.py` by calling `requests` directly.
- Hardcode gameweek numbers or the current season. Use `Season.next_gameweek`
  and `Season.gameweeks_played`.
- Overstate what the model does. It produces a ranked shortlist to argue with.
  Rotation risk, press conference hints and minutes management are not in the
  API and the output should not imply otherwise.

## Not built yet

- Chips are advisory only, in `chips.py`. Bench Boost, Triple Captain and Free
  Hit are priced per gameweek across the horizon, and wildcard is still `build`
  at current budget. What is missing is planning two chips together, and any
  sense of a chip being worth saving for a gameweek beyond the horizon.
- Price change prediction, which matters for team value over a season.
- Multi-gameweek transfer planning. Each week is solved in isolation, so it will
  not plan a two-week route to a target player.
- Deployment itself. The prerequisites are done: `prior_season.parquet` is
  committed so a cold boot reads it instead of making 700 requests, and
  `requirements.txt` is exported from `uv.lock`, since Community Cloud cannot
  read uv lockfiles. Regenerate it whenever dependencies change, or the deploy
  quietly drifts from what you test against:

  ```bash
  uv export --no-dev --extra app --no-hashes --no-emit-project \
      --format requirements-txt -o requirements.txt
  ```

  `--no-emit-project` matters. Community Cloud runs `streamlit run app.py` from
  the checkout, so `fpl_manager` is already importable and emitting it would put
  an unresolvable local file path in the file. What is left is outside the code,
  being a git repo, a GitHub remote and the Community Cloud app itself. Set
  `FPL_CACHE_DIR` to a persisted path there or every run re-fetches.
- Refreshing the committed `prior_season.parquet` between seasons. Nothing does
  it automatically, so in August it needs `--refresh` and a manual copy, or the
  projection quietly rests on a season that is two years old.
