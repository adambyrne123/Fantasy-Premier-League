# fpl-manager

[![CI](https://github.com/adambyrne123/Fantasy-Premier-League/actions/workflows/ci.yml/badge.svg)](https://github.com/adambyrne123/Fantasy-Premier-League/actions/workflows/ci.yml)

A command line tool for building an FPL squad and keeping it maintained through
the season. It pulls from the public Fantasy Premier League API, projects points
per player per gameweek, and solves for the best legal squad under the budget,
club cap and position quotas.

It is read-only. Making transfers requires logging in to your FPL account, and
this tool does not handle your credentials. It tells you what to do, you do it
on the site.

## Setup

```bash
uv sync --extra app
uv run streamlit run app.py
```

On Windows, uv installs with `winget install --id=astral-sh.uv -e`. Without uv,
a plain virtual environment does the same job, though you then resolve versions
yourself rather than getting the locked set:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e . streamlit matplotlib pytest ruff
```

PuLP ships with the CBC solver, so there is nothing else to install. The one
exception is Windows on ARM, where PuLP has no native binary and the optimiser
falls back to the bundled x64 build under emulation. That is handled in
`optimiser.py` and needs nothing from you.

The Streamlit app is the main way in: sliders for horizon, budget and bench
weight, lock and ban lists, a fixture heatmap, a transfer planner and a live
matchday view, all re-solving as you change them. The CLI below does the same
things without a browser.

Responses are cached on disk under `FPL_CACHE_DIR`, defaulting to
`~/.cache/fpl_manager`. On Streamlit Community Cloud set it to a writable path,
which makes reruns cheap within one container. It does not survive a restart or
a wake from sleep, and nothing can make it: what keeps a cold boot fast is the
committed `prior_season.parquet` and the fact that a fresh start is two
requests.

## CLI

## Usage

Before the season starts, build a squad from scratch:

```bash
uv run fpl-manager build --horizon 6 --save squad.json
```

The first run fetches last season's totals for every player, which is one
request each and takes a few minutes. It caches for 24 hours after that. Add
`--no-prior` to skip it, though pre-season the projections will be close to
useless without it.

Other commands:

```bash
uv run fpl-manager ticker --horizon 8              # fixture difficulty by club
uv run fpl-manager players --position MID --top 25 # ranked player list
uv run fpl-manager players --sort value --max-price 6.0
uv run fpl-manager players --sort differential     # who the crowd underrates
uv run fpl-manager find salah                      # look up player ids
uv run fpl-manager transfers --squad squad.json --free 1 --max 2
uv run fpl-manager plan --squad squad.json --weeks 3   # several weeks at once
uv run fpl-manager xi --squad squad.json           # lineup and captain
uv run fpl-manager captains --top 20               # who to captain, and how safe
uv run fpl-manager chips --squad squad.json        # when to play each chip
uv run fpl-manager prices                          # who is close to moving
uv run fpl-manager live --entry 1234567            # what you are scoring now
```

The shape of the eleven is the solver's choice unless you make it yours.
`--formation` pins it, and goes before the subcommand with the other global
flags:

```bash
uv run fpl-manager --formation 3-4-3 build
```

Pinning changes which fifteen get bought, not just who starts, and the output
says how many points the shape gave up against the one it would have picked.

Once the season is running you can pull your squad straight from the API with
`--entry YOUR_ENTRY_ID` instead of `--squad`. Your entry id is the number in the
URL when you view your points page. Note that the public endpoint does not
expose your bank or your selling prices, so keep the squad file up to date if
you want the transfer planner to get the money right.

## The squad file

```json
{
  "entry_id": 1234567,
  "bank": 0.5,
  "free_transfers": 1,
  "players": [
    { "id": 351, "name": "Haaland (MCI)", "purchase_price": 14.0 },
    { "id": 328, "name": "Salah (LIV)", "selling_price": 14.2 }
  ]
}
```

`build --save` writes this for you. Prices go in as the numbers the site shows,
so 14.0 rather than 140, and `name` is a label for whoever edits the file rather
than something the tool reads back. A bare list of ids works too, which is what
the app's download button produces.

Give either what you paid or what the player sells for. From `purchase_price`
the tool works the selling price out itself, and an explicit `selling_price`
overrides that, since the figure on the site beats any reconstruction of it.

Selling price matters. FPL returns your purchase price plus half of any rise,
rounded down to the nearest 0.1, so a player who has gone up 0.4 sells for 0.2
more than you paid rather than 0.4. If you leave both out the planner assumes
current price and will think you have more money than you do.

## How the projection works

```
xPts(player, gw) = sum over that club's fixtures in gw of
                   points_per_90 * expected_minutes_share * fixture_multiplier
```

Three terms, each estimated separately so you can inspect or replace any one of
them in `projections.py`.

**points_per_90** blends last season's rate with this season's, weighted by
`played / (played + 6)`. In August that is entirely last season. By November it
is roughly two thirds this season. Players with no Premier League history get a
per-position fit of rate against price, which is a stand-in and nothing more:
a promoted club's 5.5m midfielder gets a promoted-club-shaped number regardless
of what they actually did in the Championship.

By the same weight it also blends in a rate rebuilt from what a player is
expected to do rather than what he happened to score. Expected goals and assists
priced at what FPL pays for them in his position, plus a clean sheet chance from
how much his club concedes, taken as `exp(-xGC per 90)`. That is what stops a
defender and a forward collapsing into the same number, which a single scalar
rate cannot avoid. Penalty and free kick duty are added on top, because a player
who has just been given the job has a claim on chances his past numbers cannot
show yet.

The rest of the scoring is there too, including the parts that cost points:

- **Saves**, a point per three, and only for keepers. They are paid in whole
  threes within a match and the leftovers are lost, so the rate is docked what
  the remainder averages rather than simply divided by three.
- **Goals conceded**, minus one per two, for keepers and defenders. It comes off
  the same club rate as the clean sheet above, so the upside and the downside of
  one defence cannot disagree. A defender at a leaky club now carries a net
  negative defensive term, which the old rate had no way to say.
- **Defensive contributions**, two points for enough tackles, clearances, blocks,
  interceptions and recoveries in a match. Be aware this one is an estimate: FPL
  pays for clearing a bar within a match and the API gives a season average, so
  the chance of clearing it is modelled rather than counted, and it understates
  for anyone who falls well short of his bar.
- **Cards**, minus one and minus three. Small, but systematically larger for
  defenders and holding midfielders, which is more position separation.

The whole rebuilt rate contributes nothing in August, and deliberately. Until a
gameweek has been played the blend weight is zero, so it cannot contribute at
all. After that a player's own numbers fade in with his minutes, at full weight
from 270 and proportionally below that, rather than being switched on at a
threshold. That matters more than it sounds: before the first deadline the API
is still serving last season's minutes and counts, so the weight is the only
thing standing between the projection and a year-old number.

The fade replaced a hard 270 minute bar, which meant a player contributed
nothing until his 270th minute and then contributed everything. Sixty four
percent of his rate arrived on one minute of football, which is a step function
of an arbitrary number rather than a model. The minutes cancel out of the fade,
so a ten minute cameo contributes the points he actually scored rather than the
four figure rate they imply.

**expected_minutes_share** is how often a player starts multiplied by how long
he lasts when he does, rather than his share of the minutes available. The
difference matters more than it sounds: minutes over minutes available cancels
against `points_per_90` and reduces the whole projection to last season's points
divided by 38, which cannot tell a high scorer over half a season apart from a
plodder who played every week. The start rate is shrunk towards the rate implied
by price, since price is the only signal that does not come from last season's
minutes. The result is then scaled by `chance_of_playing_next_round` when FPL
publishes one, and forced to zero for anyone flagged injured, suspended or
unavailable.

Only the season that just finished counts. `history_past` lists Premier League
seasons and nothing else, so for someone who spent last year in another league
its most recent entry can be years old. Those players are treated as having no
history rather than being credited with form from three seasons ago.

**fixture_multiplier** blends two views of how hard a fixture is. FPL's own 1
to 5 difficulty rating maps onto a scaling factor of roughly 0.84 to 1.16 plus
a small home adjustment, and that is combined with the clubs' attack and
defence ratings, which are continuous, split by home and away, and move during
the season. The second is what tells apart two fixtures FPL rates the same. It
is normalised so the average fixture is exactly 1.0, which keeps the headline
number in points.

FPL publishes those ratings as zero until the season is under way, so in August
the fixture term is the difficulty rating alone, exactly as it was before. Any
club missing a rating falls back the same way rather than failing. Constants
sit at the top of `projections.py` if you want a different shape.

Because the projection is built per fixture rather than per gameweek, doubles
and blanks come out correctly without any special casing. A club with two
fixtures in a gameweek gets two contributions, a club with none gets zero.

## Captaincy, and what the average already answers

The armband doubles a score, so a captain's contribution to your expected total
is exactly his expected points. If you are maximising points, the right captain
is the top of the projection and no distribution is needed. The Captain tab is
ranked on that, and anyone telling you the haul chance is the decision is
selling you something.

The distribution earns its place in three narrower cases, and they are worth
being precise about:

- **You are usually chasing a rank, not a points total.** Against a rival what
  matters is the chance you finish above him. Behind, the volatile captain is
  right even at slightly lower expected points, and ahead the steady one is.
- **Triple Captain is a one-shot**, so you cannot average over many weeks and
  the ceiling matters in a way it does not for a weekly decision.
- **Effective ownership.** If most of the field captains the same player,
  captaining him barely moves your rank whatever he scores.

The first and the third need a rival or the field, which these numbers do not
have on their own. That is on the roadmap rather than built.

With that said, the Captain tab puts a distribution on the part of a gameweek
that swings.
Goals and assists as independent Poissons on the same expected rates the
projection uses, drawn per fixture so a double gameweek is counted rather than
special-cased, and mixed over whether a player starts, comes on or does not
feature. Two numbers come out: the chance of ten or more points, and the chance
of at least one goal or assist.

Read them for what they are. Neither includes bonus, which the model has no term
for anywhere, so a forward's goal and assist is nine points here and twelve on
the real thing. Neither includes clean sheets or defensive contributions, so for
a defender this is an attacking haul chance rather than a haul chance, and the
tab starts filtered to midfielders and forwards for that reason. And neither
will add up to the xPts column beside it, because that blends what a player has
been scoring against what he is expected to do while this uses the expected side
alone. Making the two agree would mean throwing one of them away.

It is deliberately not wired into squad selection. A squad picked partly on a
distribution you cannot see is one you cannot argue with, and arguing with it is
what this is for.

## How the selection works

Squad selection is a knapsack problem with side constraints, so it goes to a
mixed integer solver rather than a greedy loop. Sorting by points per million
and taking the best available looks reasonable and is reliably a few points
short, because what actually binds is the interaction between the club cap and
the cheap enabler slots, not raw value. The offline tests confirm the gap on
synthetic data.

The model picks the 15, the starting XI and the captain in one go. Bench players
count at `--bench-weight` of their projection, 0.12 by default. Lower it towards
0 for an aggressive build with two non-playing punts on the bench, raise it
towards 0.3 if you want a squad you can actually rotate.

Transfer planning adds a penalty of 4 points for every move beyond your free
transfers, and a budget constraint driven by selling prices, then re-solves. If
nothing clears the bar it tells you to roll.

## Chips

`chips` prices Bench Boost, Triple Captain and Free Hit in every gameweek of the
horizon and tells you which week each is worth most in. Gain is measured against
what your squad scores anyway, with the lineup and captain already picked
optimally for that gameweek, so Bench Boost is worth your bench rather than your
whole squad. Free Hit is solved fresh against your team value.

Add `--all` to see every gameweek rather than just the best one. Wildcard has no
gameweek of its own to be spent on, so it is not priced here: it is `build` at
your current team value.

The horizon bounds the answer. If the best Bench Boost week is GW12 and you are
projecting six weeks, the tool cannot see it, so widen `--horizon` before
trusting a recommendation to play a chip now.

## Live scoring

`live` and the app's Live tab score your squad while the matches are on. Bonus
is worked out from the bonus points system exactly as FPL does, three, two and
one per fixture with ties sharing, so it is right before the official points
land and stops being a guess as soon as they do. Automatic substitutions are
applied by the same rule FPL uses, walking your bench in the order you set it
and taking the first legal replacement, rather than picking the best available
one. Nothing is substituted until a player's matches are actually over, since a
player on no minutes at half past three has not blanked yet.

The app polls once a minute, and only while a match is in progress. Load your
squad with an entry id rather than a file if you want the real captain and
bench order, since a squad file records neither.

One caveat worth knowing. This was written before the season started, when the
live endpoints return nothing at all, so every test behind it runs against
synthetic data shaped like the real payload. The empty case is handled, but the
bonus ranking and the substitution rule have never seen an actual match. Check
the first gameweek against the FPL site before trusting it.

## What it does not do

- Anything a press conference tells you. Rotation risk, a manager hinting at
  resting someone, a returning player easing back in: none of that is in the
  API. Treat the output as a ranked shortlist to argue with, not an answer.
- Predict price changes. `prices` reads net transfers, which the API gives as a
  running total rather than a rate, so somebody who took five days to gather
  his looks identical to somebody who did it this morning. It is a watchlist.
- Tell you which chip to play with any confidence. All four are priced, but each
  is priced on its own, the horizon bounds the answer, and nothing knows which
  ones you have already used.
- Score a live gameweek that anybody has checked. The live view was written
  before the season started, when the endpoints return nothing, so its bonus and
  substitution logic has only ever run against synthetic data. Check it against
  the FPL site the first time you use it in anger.

`ROADMAP.md` has the rest, including what was considered and turned down.

## Tests

```bash
uv run pytest
```

Runs the whole pipeline against synthetic payloads shaped like the real API, at
both zero and twelve gameweeks played, and checks every FPL rule the optimiser
is meant to respect. No network access required.

The same suite, plus `ruff check` and `ruff format --check`, runs on Linux on
every push to `main` and every pull request. Linux is the point rather than a
convenience: PuLP ships no CBC binary for Windows on ARM, so the test that
guards how the solver is chosen cannot run on the machine this was written on,
and a regression in exactly that area once took the deployed app down.
