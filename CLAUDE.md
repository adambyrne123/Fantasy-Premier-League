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
`--no-prior`, `--refresh`, `--bench-weight` and `--formation` all go before the
subcommand name.

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

**The rollover catches the expected goals fields too, and `COMPONENT_MINUTES` is
what saves them.** Like `total_points`, `minutes` and `expected_goals` still hold
last season's figures before the first deadline, so pre-season
`component_rate` happily builds a rate for 331 players out of data that is a
year old. It does no harm only because the blend weight is 0 until a gameweek
is played. Once one is, minutes reset to this season's and the 270 minute gate
fails for everybody, so the term stays out until roughly GW4 when there is
enough of this season to mean anything. That is the intended progression rather
than a happy accident, and anything new reading these fields needs the same
guard or it will read a year-old number as though it were current.

The list above is not exhaustive and reading it as though it were is the trap.
Every counting stat rolls over the same way: `saves`, `yellow_cards`,
`red_cards`, `defensive_contribution`, `tackles`, `recoveries` and
`clearances_blocks_interceptions` are all last season's until the first
deadline. Checked live on 2026-08-16, five days out from GW1: players were
carrying three thousand minutes and last season's expected goals. So the 270
minute gate is wide open pre-season and is **not** what protects the model in
August. The blend weight being 0 is. All of these are read inside
`component_rate` and nowhere else, which is what keeps them behind that weight.
Reading one in `build_rates` beside the observed rate would put it in front of
the weight, where nothing is standing between it and the projection.

`tests/conftest.py` zeroes these fields pre-season, which is kinder than the real
payload, so a test that wants to prove the guard has to write the stale values in
itself rather than trusting the fixture.

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
`SUB_SHARE`, `STARTER_DURATION`, `STRENGTH_WEIGHT`, `STRENGTH_ALPHA`,
`PENALTY_XG_P90`, `FREEKICK_XG_P90`, `COMPONENT_MINUTES`, `SAVE_REMAINDER`.

`SAVE_REMAINDER` is derived rather than fitted, so it is the odd one in that
list. See the saves paragraph below for where 1.0 comes from before changing it.

**`points_per_90` is part rate observed and part rate rebuilt.**
`component_rate` reconstructs what FPL actually pays a player for, rather than
reading back what he happened to score:

```
appearance + xG90 * goal points for his position + xA90 * 3
           + P(clean sheet) * clean sheet points for his position
           + max(0, saves90 - SAVE_REMAINDER) / 3      (keepers)
           - E[floor(goals conceded / 2)]              (keepers and defenders)
           + P(defensive contribution) * 2             (outfielders)
           - yellows90 - 3 * reds90
```

Clean sheet probability is the Poisson zero, `exp(-xGC per 90)`, on the club's
rate rather than the player's. That is what stops a defender and a forward
collapsing into the same scalar, which is what the old single rate did.

**The clean sheet and the conceded charge come from one Poisson and one rate,
and that is the point.** `_conceded_points` is `lambda / 2 - (1 - exp(-2 lambda))
/ 4`, which is `E[floor(C / 2)]` exactly, on the same club rate the clean sheet
reads. Nothing is double counted: the clean sheet pays at nought conceded,
neither term fires at one, and only the charge fires at two or more. Modelling
the upside off one distribution and the downside off another is how the two come
to disagree about the same defence, so keep them together.

**Saves are paid in whole threes and the remainder is lost.** Dividing the rate
by three pays for saves nobody was paid for, worth about a third of a point per
90 to a busy keeper. `SAVE_REMAINDER` subtracts what the remainder averages,
which is 1.0, the mean of nought, one and two. Against a Poisson that lands
within 0.003 of exact for anyone making two or more saves a match and errs low
below that, which is the right direction to err.

**The defensive contribution term is a threshold estimated from a mean, and it
is the one real approximation here.** FPL pays 2 points for reaching
`DEFCON_THRESHOLD` actions in a match, and the API gives a season count, so
`_poisson_at_least` turns the per 90 rate into a chance of clearing the bar.
Three ways that is wrong, all worth knowing before trusting it: real action
counts are overdispersed against a Poisson, so it understates for anyone well
below his bar; it is per 90 where the rule is per match, so scaling by
`expected_minutes_share` afterwards overstates a part-player, because the bar
does not come down when his minutes do; and it assumes his rate is steady across
fixtures. A negative binomial would be better in principle and there is nothing
here to fit its dispersion against, so it would be one more constant set by eye.

Do not fix the per-match problem by threading `minutes_share` into
`component_rate`. It would couple two of the three terms the model exists to keep
separable.

**The threshold lives in `data.py` and the points values live in
`projections.py`, and the split is deliberate.** `DEFCON_THRESHOLD` is a rule
about what counts, which is what `data.py` holds. `DEFCON_POINTS`, `SAVE_POINTS`
and `CONCEDED_POINTS` are the scoring table `component_rate` reproduces, which
already lives beside `GOAL_POINTS`. FPL publishes its own copy of the threshold
as `element_types[].defensive_contribution_start` and serves it empty, so
reading it would add a branch that never runs in favour of a fallback that
always does.

**`defensive_contribution` is a count of actions, not points already scored**,
and it is exactly the sum of the actions its position counts: tackles plus
clearances, blocks and interceptions, plus recoveries for everyone except
defenders. Verified against the live payload. `component_rate` rebuilds it from
those parts when the column is absent, which is what a payload from before
2025/26 looks like.

**The club's conceding rate comes off the keepers, and it has to.**
`expected_goals_conceded` is charged to a player only while he is on the pitch,
so summing outfielders counts the same goals once per defender and lands several
times too high. A keeper is on the pitch for all of it.

**The opponent adjustment is deliberately not in the component rate.** It lives
in the fixture term, where the strength ratings already handle it. Putting a
defender's opponent into both terms would count it twice.

**Set piece duty adjusts a rate and never creates one.** `penalties_order` and
`direct_freekicks_order` add expected goals, because a taker who has just been
given the job has a claim on chances his past xG cannot show. Someone with no
minutes still gets no rate at all: appearance points on their own are an
invented number, not a measured one.

**The component blends in by the same `played / (played + SHRINKAGE_GAMES)`
weight** that governs current against prior, so no new constant appears and a
bad component estimate can only move the answer partway. Pre-season that weight
is 0 and this contributes nothing, which is also required: FPL publishes the
expected goals fields as zero until the season is under way.

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

Be honest about what that second one buys. The Community Cloud filesystem does
not survive a container restart or a wake from sleep, so it makes reruns cheap
within one container and nothing more. What actually keeps a cold boot fast is
the committed parquet and a fresh start being two requests.

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

The backlog lives in `ROADMAP.md`, including the things that were considered and
turned down, and why. Keep it there rather than here: this file is read in full
every session and is long enough already.

Two of those items will mislead you mid-task if you do not know them, so they
are repeated here:

- **None of the live code has met a real match.** It was written pre-season, when
  the live endpoints return nothing, so every test behind it runs on synthetic
  payloads. The empty case is verified against the real API. The bonus ranking
  and the substitution rule are not. Treat the first scored gameweek as the real
  test.
- **`prior_season.parquet` needs a manual refresh between seasons**, or in August
  the projection quietly rests on a season two years old.
