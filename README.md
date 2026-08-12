# fpl-manager

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
weight, lock and ban lists, a fixture heatmap and a transfer planner, all
re-solving as you change them. The CLI below does the same things without a
browser.

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
uv run fpl-manager find salah                      # look up player ids
uv run fpl-manager transfers --squad squad.json --free 1 --max 2
uv run fpl-manager xi --squad squad.json           # lineup and captain
uv run fpl-manager chips --squad squad.json        # when to play each chip
```

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

**fixture_multiplier** maps FPL's own 1 to 5 difficulty rating onto a scaling
factor of roughly 0.84 to 1.16, plus a small home adjustment. Constants sit at
the top of `projections.py` if you want a different shape.

Because the projection is built per fixture rather than per gameweek, doubles
and blanks come out correctly without any special casing. A club with two
fixtures in a gameweek gets two contributions, a club with none gets zero.

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

## What it does not do

- Plan two chips together, or weigh a chip now against a better week later than
  the horizon reaches.
- Plan transfers more than one gameweek ahead. Each week is solved on its own,
  so it will not bank a transfer or route to a target player over two weeks.
- Price change prediction. Worth adding if you care about team value.
- Anything a press conference tells you. Rotation risk, a manager hinting at
  resting someone, a returning player easing back in: none of that is in the
  API. Treat the output as a ranked shortlist to argue with, not an answer.

## Tests

```bash
uv run pytest
```

Runs the whole pipeline against synthetic payloads shaped like the real API, at
both zero and twelve gameweeks played, and checks every FPL rule the optimiser
is meant to respect. No network access required.
