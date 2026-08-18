# The model

Read this before changing any projection term. `CLAUDE.md` carries the formula,
the constant list and the one trap the model already fell into. Everything below
is why each term is shaped the way it is, and what breaks if it is reshaped.

`README.md` covers the same ground for a human reader, under "How the projection
works" and "How the selection works". That is the version to read for what the
model does. This one is for what you may not change about it.

## The rebuilt rate

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
below that, which is the right direction to err. It is derived rather than
fitted, which is what makes it the odd one in the constant list.

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

## The season rollover

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
August. The blend weight being 0 is.

Everything the projection reads them through is `component_rate`, which sits
behind that weight, and `attacking_rates`, which `component_rate` itself goes
through so that the expected goals and assists have one definition rather than
two. Reading one in `build_rates` beside the observed rate would put it in
front of the weight, where nothing is standing between it and the projection.

`captaincy.py` is the second consumer of `attacking_rates` and it is **not**
behind the blend weight, because a distribution has nothing to blend against.
It therefore carries a gate of its own, `season.gameweeks_played > 0` as well
as `COMPONENT_MINUTES`, and returns an empty frame until both are satisfied.
The gameweeks check is the one doing the work: the minutes check alone would
pass in August on a three thousand minute figure from last season. Anything
else that grows a second consumer needs the same treatment, and a guard that
only counts minutes is not a guard.

`tests/conftest.py` zeroes these fields pre-season, which is kinder than the real
payload, so a test that wants to prove the guard has to write the stale values in
itself rather than trusting the fixture.

**Pre-season means no current data.** `gameweeks_played` is 0 until late August,
so the shrinkage weight is 0 and projections rest entirely on last season. Any
new model term needs a defined pre-season behaviour.

## The fixture term

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

## The minutes term

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

`build_rates` also publishes the same term as the two things it is made of,
`start_chance` and `starter_minutes`, with `sub_chance` for the cameo branch.
The identity

```
minutes_share == start_chance * starter_minutes + sub_chance * SUB_SHARE
```

holds exactly and has a test. The collapse works because both sources are
blended before being multiplied rather than after: writing `A` for the blended
start chance and `B` for the blended minutes a start is worth, the share is
`B + SUB_SHARE * (1 - A)`, which is `A * (B / A) + (1 - A) * SUB_SHARE`. So
`starter_minutes` is `B / A`, the length of a start weighted by which source
the starts came from. The published doubt and the availability cut multiply `A`
and `B` together and leave `B / A` alone, so the identity survives them, and
what is left over, `1 - start_chance - sub_chance`, is the chance a player does
not feature at all.

## The haul distribution

`captaincy.py` puts a distribution on the part of a gameweek that swings, so
that a captaincy decision is not made off a mean. Goals and assists are
independent Poissons on `attacking_rates`, scaled by the same fixture
multiplier the projection uses, and the points they pay come from the same
scoring table.

**Independence is stated rather than modelled, and it errs both ways.** Goals
and assists share a "his team scored three today" factor, so the true joint
tail is fatter than independent Poissons give and the haul chance is
understated. Against that, a player's goals in a match are closer to a sum of
Bernoullis over his chances than to a Poisson, and a Poisson binomial with the
same mean has the thinner tail, so treating goals as Poisson overstates.
Fitting a correlation would be one more constant set by eye, which is what was
turned down for the negative binomial in the defensive contribution term.

**The role is drawn once for the gameweek, not once per fixture.** Starting is
a property of a player's week rather than of a match. Three branches, weighted
by `start_chance`, `sub_chance` and what is left: within a branch the fixtures
are conditionally independent and their point distributions convolve, which is
what makes a double gameweek arithmetic rather than a special case. Drawing the
role per fixture would say a player who starts four weeks in five starts both
legs of a double 64% of the time, and that error runs in exactly the direction
a Triple Captain is being weighed for.

**The minutes term is a mixture here and a scaling everywhere else**, which is
why `build_rates` publishes both halves. A haul is a tail and a tail does not
survive being scaled: for a rare event `P(2 goals | p * lambda)` is roughly `p`
times smaller than the honest `p * P(2 goals | lambda)`. Expectation is linear
in the branch weights, so the mixture leaves the mean exactly where the
projection has it, at `xg90 * minutes_share * multiplier`.

**Appearance points are split 1 and 2 here and flat 2 in `component_rate`.**
That is not an inconsistency to fix. `component_rate` is a per 90 rate and
asking it which side of an hour a player finished would thread the minutes term
into it. Here the minutes branch is drawn first, so the split is available for
nothing.

**Truncation.** Ten goals or ten assists in one fixture, past which there is
nothing left: at the largest rate this model produces the mass above is smaller
than 1e-8, and the whole distribution sums to one within 1e-9. It is left
summing to slightly under one rather than renormalised, because renormalising
would hide the size of the cut. The points grid sizes itself off the scoring
table rather than being a constant, so changing what a goal pays cannot leave
it stale.

**It does not reconcile with `xpts_next`, and the front end says so.** Three
reasons, and hearing only the first leaves the other two assumed away:

1. `xpts_next` also contains clean sheets, saves, goals conceded, defensive
   contribution and cards. None of them are here, so for a keeper or a defender
   this is an attacking haul chance rather than a haul chance.
2. `points_per_90` is `(1 - w)` of an observed rate plus `w` of the component
   rate, so only `w` of the expected goals ever reaches `xpts_next`. This uses
   them at full weight with no blending at all. Even the attacking part is a
   different number.
3. Bonus is in neither, which makes both understate, but it bites harder here
   because the threshold is absolute. A forward's goal and assist is nine
   points on this scale and twelve on the real one.

There is no cheap way to make them agree and the tempting expensive way is
wrong. Scaling the rates by `w` is not the same claim as a player being `w`
times as likely to score, and it would drag every haul chance towards zero in
September, which is when the question is live. What can be said, and is, is the
narrow true thing in the mixture paragraph above.

## Selection

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
