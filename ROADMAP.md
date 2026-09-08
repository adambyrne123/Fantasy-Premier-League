# Roadmap

What is not built, and what was considered and turned down. The second half
matters as much as the first: several of these were argued out once already, and
without the reasoning written down they come back around every few months.

`CLAUDE.md` is the working context for the code as it stands. This file is the
backlog. When something here gets built, delete it from here and describe it
there.

## Where things stand

The tool pulls the public FPL API, projects points per player per gameweek, and
solves for the best legal fifteen under budget, club cap and position quotas. The
projection is three separable terms: a scoring rate, expected minutes, and a
fixture multiplier that blends FPL's one to five rating with the clubs' own
attack and defence ratings. The rate is part observed and part rebuilt from
expected goals, expected assists and a Poisson clean sheet chance, so a defender
and a forward no longer collapse into the same number.

Transfers are planned across up to four gameweeks in a single MILP. All four
chips are priced per gameweek. A live tab scores your squad while matches are
being played, including provisional bonus and automatic substitutions. Two front
ends, `app.py` and `cli.py`, are thin views over the same library.

---

## Open work

Roughly in priority order. The first item is the only one with a deadline
attached to it.

### Finish verifying the live code against a real gameweek

Mostly done. `tests/test_live_against_the_api.py` checks what `live.py` works
out against what FPL published, using the API as its own oracle rather than the
site by eye: the fixture's `bonus` block, `automatic_subs` on the entry picks,
the `multiplier` FPL rewrites onto each pick, and the gameweek total in the
entry history. It is `network` marked, so `pytest -m network` is the way in and
CI never runs it. Nothing in it hardcodes a gameweek, so it keeps working every
week.

Checked against GW1 2026/27 with nine of ten matches played out:

- The bonus ranking is right, across nine fixtures, including two real tie
  cases. Two players sharing a 3 with the next man taking 1, and three sharing
  a 2 with nothing below them, both came out of standard competition ranking
  with no special casing.
- Dropping non-positive bonus points system scores never cost a winner. Real
  scores reach -14 and the lowest winning score was 27.
- The substitutions, the eleven and the armband agreed for a real entry, but
  under a gate that had not opened yet, so they are checked rather than proven.

What is left:

- **Run it once GW1 is audited.** The four tests that read the entry payload
  skip until `finished` is true across the gameweek, so they have never run
  green under their own gate. `uv run pytest -m network` is the whole job.
- **A gameweek with a real automatic substitution in it.** GW1 needed none, so
  the substitution comparison has so far only agreed that nothing happened.
- **A double gameweek.** The case most likely to be wrong, where the
  per-fixture and summed bonus figures diverge, and the one thing GW1 could not
  prove. The check finds its own gameweek, so it starts covering this the first
  time one is played.

Two things found while checking, both now fixed. `all_confirmed` read
`finished` and is now `bonus_is_final`: bonus lands at the whistle, not at the
audit, so the app called FPL's own award provisional all weekend. And
`cmd_live` still counted `finished` fixtures, which is the bug that was fixed in
`app.py` and never carried across to the CLI.

The one to keep in mind when reading any of this: the fixture payload moves at
the whistle and the entry payload moves at the audit. GW1 measured the gap at
about a day, with the entry history reporting 29 while its own live figures
summed to 35.

While checking, note that a squad *file* carries no pick order, so `score_squad`
reads its first eleven as the XI and its list order as the bench. Only an entry
id gives the real bench order and the real armband.

### Model

**A single named rival, rather than the whole field.** Captaincy against the
field is built: `captaincy.field_gain` mixes the field's distributions under
`elite.field_shares`'s `captain_share` and returns the chance you outscore their
armband and what the choice is worth in points, and the Captain tab shows both.
The shares arrive as an argument, since `captaincy.py` may not import `elite`.

What is left is the easier half, which needs no sample at all: one rival read
through `leagues.py`, where the useful number is the chance you finish above him
and it depends on what he captains rather than on what a hundred managers did.

Two things the built version cannot fix and a rival version inherits. The shares
describe the gameweek just gone, because picks are not published before a
deadline, so this answers what the field captained last week rather than what it
will captain next; the caption says so. And it is goals and assists only, like
everything else `captaincy.py` produces, so for a defender it understates.

**Position-specific scoring is complete, with two loose ends.**
`component_rate` now covers every category FPL pays for.
`corners_and_indirect_freekicks_order` is parsed and deliberately unused, since a
corner taker's assists are already in his expected assists. What is left:

- **Read `element_types[].defensive_contribution_start` once FPL populates it.**
  It is FPL's own copy of `DEFCON_THRESHOLD` and currently comes back empty, so
  the threshold is hardcoded. Assert the payload against the constant rather
  than letting it silently override, and confirm the units against a scored
  season while you are there.
- **The defensive contribution term is a threshold estimated from a mean.**
  `_poisson_at_least` assumes defensive actions are Poisson, and real counts are
  overdispersed, so it understates for anyone below his bar. A negative binomial
  would be better and there is nothing to fit its dispersion against. Revisit
  once there is a season of per-match counts to fit one on.

**The captaincy shrinkage target should be last season's expected goals.** Below
`COMPONENT_MINUTES` the Captain tab shrinks a player's rate towards a
per-position fit against price, because that is the only stable thing available.
The right target is his own previous-season xG per 90, with the price fit as the
fallback for anyone who has none. It is blocked on the parquet:
`fetch_prior_season` stores points, minutes, starts and end cost only.
`history_past` does carry `expected_goals` and `expected_assists`, as strings,
for every season back to 2022/23, so this is a schema change plus a refetch
rather than a modelling problem. Note the refetch would become mandatory rather
than advisable, since `build_rates` backfills absent prior columns with NaN and
a stale parquet would silently not blend.

**The defensive contribution term is still a threshold estimated from a mean,
and the club rate now is not.** `team_defence_rate` is shrunk towards the league
mean by `credibility` on the club's own keeper minutes, which was the open item
here: measured one match into 2026/27 the raw rates ran from 0.20 to 3.87 per
90, all off ninety minutes, and every defender at a club reads the same number
so that error does not wash out across a squad. `docs/model.md` carries the
reasoning, including why the target mean is unweighted.

What that does not fix is the shrinkage target itself. The league mean is the
only stable thing available with no history loaded, and last season's conceding
rate per club would be better. It is blocked on the same parquet the captaincy
shrinkage target is: `fetch_prior_season` stores nothing about clubs at all.

**`FakeApi` is kinder pre-season than the real payload.** It zeroes `minutes`,
`expected_goals` and the counting stats when `played=0`, and the API does not:
before the first deadline it serves last season's figures. So a test asserting a
pre-season guard passes because the fixture handed it zeros rather than because
the guard works, unless it writes the stale values in itself.
`test_the_new_scoring_terms_are_inert_before_the_first_deadline` does. A
`stale_preseason` flag on the fixture would fix it at the source, and it
reparametrises a fixture every test depends on, so it is its own piece of work.

**Set piece constants are guesses.** `PENALTY_XG_P90` and `FREEKICK_XG_P90` are
set by eye against how many spot kicks a season produces, not fitted. Only
`order == 1` counts, so a second-choice taker gets nothing at all, which is wrong
for the clubs that rotate them. They now feed a tail as well as a mean, since
`captaincy.py` reads the same `attacking_rates`, and being wrong about a rate
costs more in a haul chance than in a projection: an error in the rate moves
the mean linearly and the chance of two goals roughly quadratically.

### Planning and chips

**Chips are advisory and independent.** All four are priced per gameweek across
the horizon, wildcard over every remaining gameweek since you keep the squad,
and what you have already spent is now read off your entry and left out.

That turned out to be a bigger question than "which have I used". The bootstrap
publishes a chip catalogue nobody was parsing, and it says there are **two of
every chip**, one per half: wildcard and free hit from gameweek 2 to 19 and
again from 20 to 38, bench boost and triple captain the same way from gameweek
1. So availability is a question about a gameweek rather than about a season,
which is what `chips.available_chips` answers, and `Season.chip_windows` is the
catalogue. It also says wildcard and free hit cannot be played in gameweek 1 at
all, which nothing knew and which was being priced anyway.

Still missing: planning two chips together, any sense of a chip being worth
saving for a gameweek beyond the horizon, and the assistant manager chip, which
is absent from the catalogue this season and so cannot be priced off it.

**Planning stops at four gameweeks.** `MAX_PLAN_WEEKS` is 4 because three weeks
solves in about 0.7s and four in about 1.5s, but five jumps to five or six, past
what a slider can re-solve on. It also chooses from a trimmed pool rather than
the whole game. `ENABLER_SHARE` reserves 30% of each position's places for the
best points per million so cheap enablers survive the trim, but it is still a
shortlist.

**`planning_pool` ranks on a different column from the objective.** It shortlists
on `xpts_total` while each week's MILP maximises `xpts_gw`, because
`plan_transfers` never passes `points_col`. Recorded as open rather than as a
bug: ranking a multi-week pool on the horizon total is defensible, and
`ENABLER_SHARE` already fixed the symptom that made it matter. Measure before
changing it.

**Prices are held constant across the horizon.** The assumption most worth
removing, and it needs the price rate below first.

**`pick_xi` orders the bench by sorting on points.** No bench weighting and no
autosub probability, so the order it suggests is not the order that maximises
what actually comes on. `live.resolve_autosubs` now knows the real substitution
rule, so the two could be joined.

**The Squad headline mixes two spans.** `build_squad` takes
`points_col="xpts_total"` and `captain_col="xpts_next"`, so the figure is the
eleven over the horizon plus the captain's next gameweek once more. That is
defensible, since the armband is a weekly decision and counting it across the
whole run would overstate it badly. It is explained on screen and the cards
reconcile with it exactly. Left as a choice to revisit rather than a defect: the
alternatives are counting the captain over the horizon, which overstates, or
reporting everything per gameweek.

### The field

**Transfers in and out among the top managers.** `elite.py` counts one
gameweek's squads. Two gameweeks and a diff gives what the best managers are
actually moving, which is the other half of what makes ownership worth reading,
and it is what the public spreadsheets show as their most transferred lists. The
cost is what stops it being obvious: it doubles a fetch that is already one
request per manager, and the second gameweek is only useful while it is recent.
Sample fewer managers rather than fetching twice as many payloads.

**The field describes the gameweek just gone, and could describe this one.**
`app.py` asks for `season.gameweeks_played`, so the shares are the last settled
gameweek. Picks are published the moment a deadline passes, so from the deadline
onwards the current gameweek is readable and is the more useful question: what
the top managers are playing this week, armband included, rather than what they
played last week. Two things make it a separate piece of work rather than
changing one argument. `elite_entries` reads a league table that only moves when
a gameweek is scored, so the sample would be last week's managers with this
week's squads, which is fine but needs saying on screen. And mid-gameweek the
multipliers have not been rewritten by automatic substitutions yet, so
`elite_start_share` would read the lineup named rather than the one that
counted, which is the right answer for planning and the wrong one for a record.

**Nothing reads the chips the field has played.** `elite_picks` carries
`active_chip` per manager and nothing counts it. How many of the top hundred
have used their wildcard is a real input to chip timing and it is one groupby
away, and it belongs beside `chips.py` output rather than inside it, since
`chips.py` prices a chip against your own squad and this is a fact about other
people.

### Live

**A price rate rather than a running total.** `prices.py` computes
`net transfers / owners`, but the API gives a total since the gameweek opened
rather than a series, so somebody who took five days to gather his transfers
looks identical to somebody who did it this morning. Fixing it means storing a
daily snapshot, which nothing does. This is also what would let `plan_transfers`
stop holding prices constant.

**Live scoring across a mini league.** `live.py` scores your own squad and
`leagues.py` reads league tables, and nothing joins them. `leagues.py` imports
only `pandas` and `.data`. The thing worth knowing when it is built is that picks
are immutable once a deadline passes and only `event/{gw}/live/` needs a short
TTL, so a twenty manager league is twenty long-cached requests fetched once per
gameweek rather than twenty on every rerun.

### Infrastructure and upkeep

**PuLP deprecation warnings are back, deliberately.** Reordering
`optimiser.solver()` to silence them is what broke the deploy. `PULP_CBC_CMD` is
not a deprecated alias for a pathed `COIN_CMD`: it resolves the bundled binary
for the running platform and chmods it executable on anything that is not
Windows. If the 4.0 migration is attempted again, `PULP_CBC_CMD` has to stay
first, and it has to be tested on Linux. The warning is cosmetic and the pin is
still below 4.0. What blocked a second attempt was having nowhere to test it,
and CI is now that Linux run.

**Refreshing `prior_season.parquet` between seasons.** Nothing does it
automatically, so in August it needs `--refresh` and a manual copy or the
projection quietly rests on a season two years old. `load_prior(refetch=True)`
exists but no caller passes it, and it writes to `PRIOR_CACHE` rather than to the
committed copy, so even the manual path is two steps.

**Regenerating `requirements.txt`** whenever dependencies change, or the deploy
drifts from what you test against. The command is in `CLAUDE.md` and nothing
automates it.

---

## Considered and rejected

Kept with the reasoning so they are not re-proposed and re-argued from scratch.

**A wildcard MILP.** `build_squad` at current team value already is the wildcard
answer. The only open question is which week, which is `chips.evaluate` shaped
and is now built. A fifteen-transfer variant of `plan_transfers` is the same
problem with a constraint removed: slower, and no more informative.

**Price movement into the optimiser.** Needs a points-per-0.1m exchange rate, and
a wrong one quietly degrades squad selection, which is a bad trade for a signal
this rough. Build the rate, surface it, stop there.

**Mean-variance or chance-constrained objectives.** Precision theatre on a model
this rough. The useful version of caring about variance is the Captain tab,
which prices the ceiling separately and leaves the objective a mean. It is
deliberately not wired into the optimiser: a squad picked partly on a
distribution nobody can see is one you cannot argue with, which is the whole
thing this tool is for. `tests/test_pipeline.py` parses the imports and fails
if `captaincy.py` reaches any module that chooses a squad, so this stays
decided rather than needing to be re-argued.

**`ep_next` as a model input.** Consuming FPL's own expected points makes the
projection partly a copy of theirs and destroys the ability to explain why a
number is what it is. A comparison column is its only honest use, and it now has
one on the Players tab, in the comparison that appears when you tick two or more
rows, where it sits beside ours and is read by nothing else.

**ICT index, influence, creativity, threat.** Composites built from the same
underlying events as expected goals and assists, on an uninterpretable scale, and
lagging. Adding xG made them redundant. Not parsed, and should stay that way.
Every public FPL spreadsheet carries them prominently, so this one comes back
round whenever one of those is read for ideas.

**`my-team/{id}/`** for true selling prices and real free-transfer counts. Needs
authentication. Read-only is a design decision, not a limitation to be fixed, and
live work will drift towards this if allowed to.

**Routing automatic substitutions through `optimiser.pick_xi`.** It would field a
better XI than FPL actually will, which is a wrong answer stated confidently, and
it would import the optimiser into `live.py`, which has to stay a leaf.

**Recent club form as a term in the fixture multiplier.** `Season.club_form`
counts what each club has scored and conceded lately and the Fixtures tab shows
it, and it deliberately feeds nothing. `strength_multiplier` already blends
FPL's attack and defence ratings, which are continuous, separate for home and
away, and move during the season off these same results, so putting the results
in again would mostly count them twice. It would also need a weight, and there
is nothing to fit one against. If this is revisited, the case to make is that
the published ratings lag the results rather than that recent form is
informative, and it should be measured before it is written.

**Tableau or any BI tool**, a static page from a scheduled GitHub Action, and a
FastAPI plus JS front end. All three were ruled out when Streamlit was chosen;
see the Delivery section of `CLAUDE.md` for why.
