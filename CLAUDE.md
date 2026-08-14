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
uv run fpl-manager live --entry 1234567    # what your squad is scoring now
```

Global flags live on the parent parser, not the subcommands. `--horizon`,
`--no-prior`, `--refresh` and `--bench-weight` all go before the subcommand name.

Run `ruff check` and `pytest` before considering any change finished. Both are
fast, so there is no reason to skip either.

## Architecture

Data flows one way. Nothing downstream writes back.

```
api.py  ──▶  data.py  ──▶  projections.py  ──▶  optimiser.py  ──▶  cli.py
             │  │  │           ▲                       ▲
             │  │  ▼       squad.py                chips.py
             │  │ prices.py  (current holdings)   (chip timing)
             │  ▼
             │ live.py  (in-play scoring, this gameweek only)
             ▼
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
| `squad.py` | Loading the user's 15, bank, selling prices. | Projections or optimisation |
| `leagues.py` | Public manager profiles and classic league tables. | Anything that scores or ranks players |
| `cli.py` | Argument parsing and printing. | Model logic of any kind |
| `app.py` | Streamlit view: widgets, layout, caching. | Model logic of any kind |

**`live.py` is a leaf and must stay one.** `projections.py`, `optimiser.py` and
`chips.py` may not import it. They look forward on a six hour cache, it looks at
the last sixty seconds, and joining the two puts numbers that disagree on the
same screen. Live points are a realised outcome, not evidence for a projection.

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

**The live panel polls on a gate, and the gate matters.** `app.py` runs the
live tab inside `@st.fragment(run_every=...)`, and the interval is `None` unless
a match is actually in progress. Left on it costs every visitor a request a
minute forever, including in February on a Tuesday. The fragment holds no
widgets and calls only `load_live_gw`: a widget inside it writing state read
outside forces a full rerun, which is exactly the projection recompute the
fragment exists to avoid.

**Two-level cache TTLs must nest the right way round.** `LIVE_MEMORY_TTL` is 30
seconds against the 60 `api.live` holds on disk. A memory cache that outlives
the disk cache behind it serves stale data twice over.

**`load_season` carries a ttl, and cached functions take `season.data_stamp`.**
Without the ttl, a process keeps its first `Season` forever and the disk ttl
expiring accomplishes nothing, because nothing re-enters `_get`. Without the
stamp, Streamlit is told to skip hashing the `Season`, so a rebuilt one looks
identical to the one it replaced and the old projections keep being served. New
cached functions taking `_season` need the stamp as their last argument.
**A push does not restart the deployed app, and that has broken it once.**
Community Cloud pulls the new code and re-runs `app.py`, but modules already in
`sys.modules` stay as they were. Adding a name to a library module and importing
it from `app.py` in the same push is therefore enough to take the app down: the
new `app.py` asks for something the old, still-loaded `optimiser` does not have,
and every visitor gets an `ImportError` until someone reboots it by hand from
Manage app.

It is misleading while it lasts, because the traceback quotes the module's
`__file__`, which is the checkout path and does hold the new code. The file on
disk is fine. The module object in memory is not. Check out the merge commit and
import it before going looking for a bad merge.

Nothing in the repo prevents this. After a push that adds or renames anything in
`fpl_manager`, load the app and reboot it if it errors.

**Streamlit caching splits by type.** `Season` holds an HTTP session, so it uses
`@st.cache_resource`. Frames use `@st.cache_data`. Passing `Season` into a
cached function needs the `_season` underscore prefix so Streamlit skips hashing
it. Getting this wrong produces a confusing hashing error rather than a clear
one.

**Doubles and blanks are already handled.** `Season.team_fixtures` emits one row
per club per fixture, so a club with two fixtures in a gameweek gets two rows
and a club with none gets zero. Do not add special casing. If you find yourself
writing `if is_double_gameweek`, the design has gone wrong.

`Season.gameweek_shape` is the one exception, because naming them is the point
of it rather than an accident of the arithmetic. It is also the only reader here
that keeps finished fixtures: a club that has played the first leg of a double
still has a double, and dropping played fixtures halfway through a gameweek
would report every club that had already kicked off as blanking. It skips
gameweeks with no fixtures at all, or the far end of the horizon reads as twenty
clubs blanking at once.

**The two live endpoints answer different questions and both are needed.**
`event/{gw}/live/` sums a player's stats across his fixtures, which is the right
answer to "what has he scored" and the wrong one for anything decided per match.
Bonus is awarded per fixture, so it comes from `fixtures/?event=`, where the
bonus points scores arrive already grouped. Mixing them up produces wrong bonus
in exactly the gameweeks people care most about. The per-fixture payload is
cached under `fixtures_gw_{gw}`, deliberately not `fixtures`, so a poll every
minute cannot overwrite the season fixture list the projection reads.

**Whether bonus has landed is a per-fixture question.** A gameweek spread over
two days has real bonus on the first day's matches while the second is still
provisional, so any gameweek-wide flag is wrong for half the screen.
`live._has_real_bonus` looks for a `bonus` block in that fixture's stats array.
`event-status/` is keyed by calendar date, so it is fit for a headline and never
for deciding one player's total.

**Parse the fixture `stats` array by `identifier`, never by index.** The order
is not guaranteed and the array is absent entirely before kickoff.

**Autosubs are a rule, not an optimisation.** `live.resolve_autosubs` walks the
bench in the order the manager set it and takes the first legal replacement.
Routing it through `optimiser.pick_xi` would field a better XI than FPL actually
will, which is a wrong answer stated confidently, and would import the optimiser
into a module that must stay a leaf.

**None of the live code has met a real match yet.** It was written pre-season,
when `event/{gw}/live/` returns zero elements and no fixture carries a stats
array, so every test behind it runs on synthetic payloads shaped like the
documented one. The empty case is handled and verified against the real API,
but the bonus ranking and the substitution rule have only ever been exercised
against a fake. Treat the first matchday of the season as the real test, and
check a scored gameweek against the FPL site before trusting a number on it.

**`total_points` changes meaning at the season rollover.** Before the first
deadline the API still serves the previous season's totals, so in early August
`total_points` is last season's return and a week later it is this season's,
with no schema change to notice. `roi.points_source` decides which by reading
the data rather than the calendar, because the reset does not line up neatly
with `gameweeks_played`. Anything new that divides by or ranks on
`total_points` has the same problem and should go through it.

**Pre-season means no current data.** `gameweeks_played` is 0 until late August,
so the shrinkage weight is 0 and projections rest entirely on last season. Any
new model term needs a defined pre-season behaviour.

**PuLP is on the 4.0 API already, while still pinned below 4.0.** Variables are
created through `prob.add_variable` and `prob.add_variable_dicts`, and solves go
through `COIN_CMD`. `LpVariable.dicts` and `PULP_CBC_CMD` are both deprecated in
the 3.x line and neither is used any more, so the suite runs without a single
deprecation warning. Keep it that way: the pin can now be lifted on its own
merits rather than needing a migration first.

**PuLP has no CBC binary for Windows on ARM.** It looks for `solverdir/cbc/win/
arm64/cbc.exe`, which was never shipped, and every solve fails before it starts.
`optimiser.solver()` finds the bundled x64 build instead, which Windows runs
under emulation, and hands `COIN_CMD` its explicit path. Route new solves
through `solver()` rather than constructing a solver inline, or they will work
everywhere except this machine.

## The model

```
xPts(player, gw) = sum over that club's fixtures in gw of
                   points_per_90 * expected_minutes_share * fixture_multiplier
```

Three terms, deliberately separable so any one can be replaced without touching
the others. Tuning constants sit at the top of `projections.py`:
`SHRINKAGE_GAMES`, `DIFFICULTY_ALPHA`, `HOME_BONUS`, `START_RATE_TRUST`,
`SUB_SHARE`, `STARTER_DURATION`, `STRENGTH_WEIGHT`, `STRENGTH_ALPHA`.

**The fixture term blends two difficulty signals.** FPL's 1 to 5 rating is a
five step function set by hand and rarely revised. The six team strength
ratings are continuous, separate for attack and defence and for home and away,
and they move during the season, so they tell apart fixtures the difficulty
rating calls identical. `strength_multiplier` forms
`(own attack / league mean attack) * (league mean defence / opponent defence)`,
clipped to `[STRENGTH_FLOOR, STRENGTH_CEILING]`.

**That normalisation to a league mean of 1.0 is load bearing.** Without it
every projection shifts by a constant factor, the headline number stops being
points, and the chip comparison and the greedy baseline both move underneath
you.

**Passing `strength=None` to `fixture_multiplier` reproduces the old output
exactly**, which is the fallback whenever the ratings are missing. There is a
test asserting it.

**Pre-season the strength ratings are all zero.** FPL publishes them that way
until the season is under way, so in August the term contributes nothing and
the projection rests on the difficulty rating as it always did. A zero is
treated as unrated rather than as a club with no attack, so one late-rated
promoted club cannot divide the rest of the term by zero.

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

**Multi-week planning is one MILP, not a loop of weekly ones.** `plan_transfers`
holds every week in a single problem and links them with
`own[i][w] == own[i][w-1] + in - out`. Solving each week and chaining the
results cannot roll a free transfer on purpose or take a loss now to reach a
player later, which is the entire reason the function exists. `gameweek_frame`
lives in `optimiser.py` rather than `chips.py` because both callers need it and
`chips.py` already imports from the optimiser.

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

Deployed to Streamlit Community Cloud, which is free and public, and live at
https://fantasy-premier-league-ab.streamlit.app/ tracking `main`. Nothing in this
repo is secret, and there are no credentials to leak, so a public app is fine.
Mobile rendering is cramped but usable, which was the accepted trade.

**The deploy installs from `uv.lock`, not `requirements.txt`.** Both files are
present and Community Cloud picks the lockfile, saying so in the build log:

```
WARN: More than one requirements file detected in the repository.
Available options: uv-sync uv.lock, uv requirements.txt, poetry pyproject.toml.
Used: uv-sync with uv.lock
```

So `requirements.txt` is currently dead weight on the deploy. Regenerating it
changes nothing that runs in production, and anything that has to reach the
deployed app belongs in `uv.lock`, which means `uv sync` and committing the
result. Keep it exported anyway, since it is the fallback if the lockfile is
ever dropped and it is what any other host would read:

```bash
uv export --no-dev --extra app --no-hashes --no-emit-project \
    --format requirements-txt -o requirements.txt
```

One consequence of uv-sync winning: it installs the project itself,
`fpl-manager==0.1.0` from the checkout, which is exactly what `--no-emit-project`
keeps out of `requirements.txt`. That is the difference between the two paths,
and it is why the file you regenerate does not describe the environment you get.

Two things keep a cold boot survivable. `prior_season.parquet` is committed, so
`load_prior` reads it rather than making roughly 700 `element-summary` requests
before the first page renders. And `FPL_CACHE_DIR` wants a persisted path in the
app's settings, or every boot re-fetches the rest.

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

- Chips are advisory only, in `chips.py`. All four are priced per gameweek
  across the horizon. Wildcard is the odd one, measured over every remaining
  gameweek rather than one, since you keep the squad, so its gain falls the
  later you play it and is not directly comparable with the other three. What
  is missing is planning two chips together, any sense of a chip being worth
  saving for a gameweek beyond the horizon, and the assistant manager chip.
  Nothing reads which chips you have already used, either.
- Variance. Everything is a point estimate, which matters most for captaincy
  and Triple Captain, both of which are decisions about a ceiling rather than a
  mean. A haul probability per player would be the smallest useful version and
  wants the xG plumbing below first. Do not reach for a mean-variance or
  chance-constrained MILP on a model this rough.
- Position-specific scoring. A defender's clean sheet and a striker's goal both
  collapse into one scalar `points_per_90`. The inputs to fix it are parsed and
  unused: `expected_goals`, `expected_assists`, `expected_goals_conceded`. The
  shape that preserves the three-term separation is a component rate inside
  `points_per_90` (Poisson clean sheets from xGC for GKP and DEF, xGI rates for
  attackers) blended in by the existing `played / (played + SHRINKAGE_GAMES)`
  weight, with the opponent adjustment left in the fixture term where the
  strength ratings already handle it. `_minutes_share` must not be touched.
  Note `FakeApi` emits those three fields as constant zero, so a component
  model would silently evaluate to nothing across the whole suite unless the
  fake is extended in the same commit. Set-piece and penalty duty
  (`penalties_order`, `direct_freekicks_order`,
  `corners_and_indirect_freekicks_order`) are not parsed at all.
- Live scoring across a mini league. `live.py` scores your own squad and
  `leagues.py` reads league tables, but nothing joins them to show what a whole
  league is scoring in progress. The thing worth knowing when it is built is
  that picks are immutable once a deadline passes, and only `event/{gw}/live/`
  needs a short TTL, so a twenty manager league costs twenty long cached
  requests fetched once per gameweek rather than twenty on every rerun.
- Price change prediction is advisory only, in `prices.py`, and deliberately
  does not reach the optimiser. Feeding expected price movement into the MILP
  needs a points-per-0.1m exchange rate, and a wrong one quietly degrades squad
  selection, which is a bad trade for a signal this rough. What is missing is
  any sense of rate: the API gives a running total since the gameweek opened,
  not a time series, so a player who took five days to gather his net transfers
  looks identical to one who did it this morning. Fixing that means storing a
  daily snapshot, which nothing does yet.
- Multi-gameweek planning beyond four weeks. `plan_transfers` links the weeks
  into one MILP, so it can roll a free transfer or take a loss now to reach a
  player later, but five weeks takes five or six seconds against under a second
  for three, and the app caps at `MAX_PLAN_WEEKS`. It also chooses from a
  trimmed pool rather than the whole game. `ENABLER_SHARE` reserves part of
  each position's places for the best points per million so the cheap enablers
  survive the trim, but it is still a shortlist. Prices are held constant
  across the horizon, which is the assumption most worth removing once
  `prices.py` has a rate rather than a running total.
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
  `FPL_CACHE_DIR` to a writable path there or every rerun re-fetches. Be honest
  about what that buys: the Community Cloud filesystem does not survive a
  container restart or a wake from sleep, so it makes reruns cheap within one
  container and nothing more. What keeps a cold boot fast is the committed
  `prior_season.parquet` and a fresh start being two requests.
- Continuous integration. Nothing runs the suite on a pull request, so a green
  run is whatever the last person happened to do locally.
- Refreshing the committed `prior_season.parquet` between seasons. Nothing does
  it automatically, so in August it needs `--refresh` and a manual copy, or the
  projection quietly rests on a season that is two years old.
