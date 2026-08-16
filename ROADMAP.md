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

### Verify the live code against a real gameweek

**The highest priority item here.** None of `live.py` has ever met a real match.
It was written pre-season, when `event/{gw}/live/` returns zero elements and no
fixture carries a stats array, so every test behind it runs on synthetic payloads
shaped like the documented one. The empty case is handled and has been checked
against the real API. The bonus ranking and the substitution rule have not.

Check a scored gameweek against the FPL site before trusting a number on it. The
two things most likely to be wrong are bonus in a double gameweek, where the
per-fixture and summed figures diverge, and a substitution in a match that
finished late.

While checking, note that a squad *file* carries no pick order, so `score_squad`
reads its first eleven as the XI and its list order as the bench. Only an entry
id gives the real bench order and the real armband.

### Model

**Captaincy risk.** Everything is a point estimate. That matters least for the
fifteenth pick and most for the captain and Triple Captain, which are decisions
about a ceiling rather than a mean. The smallest useful version is a haul
probability per player from expected goal involvements through a closed-form
Poisson, attached to a captaincy view and nothing else. Phase 5 put the xG
plumbing in, so this is now unblocked. Do not thread it through the objective;
see the rejected list.

**The rest of position-specific scoring.** `component_rate` covers goals, assists
and clean sheets, which is most of where the positions differ. Still missing:

- **Saves.** A shot-stopper at a bad club reads the same as a spectator at a good
  one.
- **The goals-conceded penalty**, one point per two conceded, which is the other
  half of a defender's downside and currently absent while the clean sheet upside
  is modelled.
- **Cards**, and the **defensive contribution** points added in 2025/26. The
  latter has no mention anywhere in the repo.

`saves`, `yellow_cards` and `red_cards` are parsed in `live.py` as realised
stats, but not in `data.py`, so the model cannot see them.
`corners_and_indirect_freekicks_order` is parsed and deliberately unused, since a
corner taker's assists are already in his expected assists.

**Set piece constants are guesses.** `PENALTY_XG_P90` and `FREEKICK_XG_P90` are
set by eye against how many spot kicks a season produces, not fitted. Only
`order == 1` counts, so a second-choice taker gets nothing at all, which is wrong
for the clubs that rotate them.

**`ep_next` is parsed and shown nowhere.** It is FPL's own expected points.
Rejected as a model input below, but a comparison column in the player drill-down
is its one honest use and would be a few lines. Useful precisely because it
disagrees.

### Planning and chips

**Chips are advisory and independent.** All four are priced per gameweek across
the horizon, wildcard over every remaining gameweek since you keep the squad.
Missing: planning two chips together, any sense of a chip being worth saving for
a gameweek beyond the horizon, and the assistant manager chip. Nothing reads
which chips you have already used, either. `evaluate()` takes a `chips` tuple,
but it is a caller-supplied filter and nothing populates it from the API.

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

**Continuous integration.** Nothing runs the suite on a pull request, so a green
run is whatever the last person happened to do locally. This is not
hypothetical. A CBC solver regression reached production and took the deployed
app down, and the test that catches it skips on Windows on ARM because there is
no bundled binary to prefer. A single GitHub Actions workflow running `ruff` and
`pytest` on Linux would have caught it before the merge, and is the highest value
item in this section by some distance.

**PuLP deprecation warnings are back, deliberately.** Reordering
`optimiser.solver()` to silence them is what broke the deploy. `PULP_CBC_CMD` is
not a deprecated alias for a pathed `COIN_CMD`: it resolves the bundled binary
for the running platform and chmods it executable on anything that is not
Windows. If the 4.0 migration is attempted again, `PULP_CBC_CMD` has to stay
first, and it has to be tested on Linux. The warning is cosmetic and the pin is
still below 4.0.

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
this rough. A haul probability on a captaincy view is the useful version of
caring about variance.

**`ep_next` as a model input.** Consuming FPL's own expected points makes the
projection partly a copy of theirs and destroys the ability to explain why a
number is what it is. A comparison column is its only honest use.

**ICT index, influence, creativity, threat.** Composites built from the same
underlying events as expected goals and assists, on an uninterpretable scale, and
lagging. Adding xG made them redundant. Not parsed, and should stay that way.

**`my-team/{id}/`** for true selling prices and real free-transfer counts. Needs
authentication. Read-only is a design decision, not a limitation to be fixed, and
live work will drift towards this if allowed to.

**Routing automatic substitutions through `optimiser.pick_xi`.** It would field a
better XI than FPL actually will, which is a wrong answer stated confidently, and
it would import the optimiser into `live.py`, which has to stay a leaf.

**Tableau or any BI tool**, a static page from a scheduled GitHub Action, and a
FastAPI plus JS front end. All three were ruled out when Streamlit was chosen;
see the Delivery section of `CLAUDE.md` for why.
