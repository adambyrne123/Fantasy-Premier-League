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
           + bonus90
```

**Bonus is read at the rate it was earned, not rebuilt.** It is paid off the
bonus points system rather than off any category above, so there is nothing to
rebuild it from, and leaving it out was a systematic understatement that grew
with the blend weight. Measured three gameweeks into 2026/27 on players with
180 minutes or more: the component sat 0.4 to 0.8 per 90 under the observed
rate by position, and bonus per 90 was 0.22 for keepers, 0.25 for defenders,
0.42 for midfielders and 0.54 for forwards, about half the gap. It sits behind
the same weight and `credibility` as every other term, so the minutes cancel
the same way and pre-season it contributes nothing.

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

`COMPONENT_MINUTES` is now used as a denominator as well as a bar, and its value
did not change when that happened, so the rule above still holds: the blend
weight is the same one and no new constant appeared. `PRIOR_MINUTES` is a name
put on a literal that was already in `build_rates`, not an addition. It is the
sample floor on a *finished* season, which is a different question from scaling
one in progress, and naming it is what stops the two being unified by somebody
tidying up.

## The season rollover

**`total_points` changes meaning at the season rollover.** Before the first
deadline the API still serves the previous season's totals, so in early August
`total_points` is last season's return and a week later it is this season's,
with no schema change to notice. `roi.points_source` decides which by reading
the data rather than the calendar, because the reset does not line up neatly
with `gameweeks_played`. Anything new that divides by or ranks on
`total_points` has the same problem and should go through it.

**The rollover catches the expected goals fields too, and nothing but the blend
weight saves them.** Like `total_points`, `minutes` and `expected_goals` still
hold last season's figures before the first deadline, so pre-season
`component_rate` happily builds a rate for 331 players out of data that is a
year old. It does no harm only because the blend weight is 0 until a gameweek is
played.

Once one is, minutes reset to this season's and a player's own numbers fade in
across his first few matches in proportion to `credibility`, reaching full
weight at `COMPONENT_MINUTES`. That used to be a bar rather than a scale, which
meant the term stayed out entirely until roughly GW4 and then arrived whole; see
the credibility ramp section below for why that was wrong and what replaced it.

Anything new reading these fields needs a guard of its own or it will read a
year-old number as though it were current, and a guard that counts minutes is
not one.

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
Its gate is `season.gameweeks_played > 0` and nothing else, and that is the one
doing all the work: a minutes check would pass in August on a three thousand
minute figure from last season, which is the whole trap. **A guard that only
counts minutes is not a guard.**

Because it has no outer weight, it cannot down-weight a short sample the way
`build_rates` does, so it shrinks instead. `attacking_rates` returns a shrunk
pair beside the raw one, `credibility` of a player's own reading and the rest
from a per-position fit of the rate against price. `component_rate` takes the
raw pair, since it is already weighted by `credibility` once in `build_rates`
and taking the shrunk pair would apply the same quantity twice. Above
`COMPONENT_MINUTES` the shrinkage is the identity and the two pairs are equal,
which has a test, so nothing mid-season moved.

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

**This season they stayed zero, and the term is rebuilt from expected goals
when they do.** Checked on 2026-09-08, three gameweeks in: all four ratings
read 0 for all twenty clubs, and `strength_overall_*` had become a 2 to 5
scale rather than the 1000 to 1400 one the ratings used to share. So the
continuous half of the fixture term had been dormant all season and nothing
had noticed, because the fallback is designed to be silent.

`club_strength` is the stand-in. Attack is the club's summed player
`expected_goals` per 90 of its keepers' minutes: a shot is charged to one
player, so summing is right here where it was wrong for conceded. Defence is
the keepers' `expected_goals_conceded` per 90, the raw rate `team_defence_rate`
starts from. Both are shrunk towards the unweighted league mean by
`credibility` on keeper minutes, because an error here is correlated across a
whole club, and on `CLUB_STRENGTH_MINUTES`, ten matches, rather than the three
the clean sheet term uses for the same raw numbers.

**The two scales differ because the two uses tolerate different errors.** The
clean sheet term is bounded by the Poisson it feeds, so a club rate a third out
costs a defender a fraction of a point. The fixture term multiplies every
player at both clubs and was built around FPL ratings that sit within a fifth
of the league mean. Measured on 2026-09-08 with the ratings shrunk on 270: the
rebuilt attack ran from 0.79 to 2.55 goals per 90 and the defence from 0.31 to
2.16, the product of the two ratios was clipped on 37% of fixtures and the
average fixture came out at 1.12 rather than 1.0, which is the normalisation
the headline number being points depends on. A club's expected goals per match
after three games has a standard error near half its mean, so trusting it in
full was the mistake rather than the clip. On 900 the same ratings sit within
about a fifth of the mean and the average fixture is back near 1.0. `fill_strength` puts
them into the fixture frame only where FPL's are absent, and all or nothing
across the frame, because the two are on different scales and a column that
mixed them would rate a club against a number that means something else. A
conceding rate goes in inverted, since FPL's defence rating is higher-is-better
and `strength_multiplier` divides by the opponent's. No home and away split.

`strength_multiplier` is then unchanged: it normalises whichever set it is
given to a league mean of one, so the headline number stays in points, and
its clip is what bounds a club that is genuinely a mismatch. Measured on GW1
to GW3 of 2026/27 with one or two matches behind each club the term was
neutral to slightly positive, top fifty realised 3.75 to 3.85 and rank
unchanged, which is what heavy shrinkage on a tiny sample should look like. It is kept because
the alternative is no continuous signal at all, and `backtest --sweep
STRENGTH_ALPHA` is the way to find out what it is worth once there is a
sample.

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
The minutes term blends by `played / (played + ROLE_SHRINKAGE_GAMES)`, which is
2, and the difference is the subject of the role section below.
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

## The role, faster than the rate

**This season's starts replace last season's three times faster than this
season's points do.** `build_rates` has two blend weights: `weight_now` on
`SHRINKAGE_GAMES` for the scoring rate and its rebuilt half, and
`weight_role` on `ROLE_SHRINKAGE_GAMES` for the start mixture. They used to be
one weight, and the argument for splitting them is that the two things being
blended are not the same kind of evidence. A start is a decision a manager has
already made, three times by GW3, and it persists. A scoring rate off the same
three matches is a sample of something noisy, and leaning on last season for it
is right.

It was measured rather than argued. Rebuilding the season as of each of GW0 to
GW2 of 2026/27 and scoring the projection for the following gameweek, with the
rate weight held at 6: role weight 6 gave a rank correlation with realised
points of 0.481, role 3 gave 0.511, role 2 gave 0.528 and role 1 gave 0.551,
with the minutes term's correlation with realised minutes moving from 0.578 to
0.684 across the same run. Holding the role at 6 and moving the rate weight to
2 gave 0.474. So the whole of the gain is in the role, and none of it is in
the rate. Two was chosen over one because one was best by a little on the
smallest sample there will ever be, and `backtest --sweep ROLE_SHRINKAGE_GAMES`
is the way to move it when there is more.

Nothing else moves. `start_chance`, `sub_chance` and `starter_minutes` all fall
out of the same `mix`, so `captaincy.py` sees the split for nothing and the
mixture identity above still holds exactly.

## The credibility ramp

`points_per_90` used to gate a player's own record on 270 minutes this season.
Below it he contributed nothing and above it he contributed everything, so at
269 minutes his rate was entirely last season's and at 270 it was 36% last
season, 24% this one and 40% rebuilt. **Sixty four percent of a rate arrived on
one minute of football.** Nothing about a player changes between his 269th
minute and his 270th, and the two gates that produced it read the same column
against the same number, so there was no partial state to soften it.

What gave it away is that the minutes term never worked that way.
`minutes_share`, `start_chance` and `sub_chance` have always blended this season
against last by `weight_now`, with no minutes bar at all. Only the scoring rate
threw current-season data away, which makes this a hole rather than a design.

`credibility` is `min(minutes / COMPONENT_MINUTES, 1)`, multiplied into
`weight_now` for both the observed half and the component half. Two facts make
it safe, and the second is the one to keep.

**The minutes cancel.** Below the bar the current-season contribution is

```
credibility * now_rate = (m / 270) * (points * 90 / m) = points / 3
```

with no `m` left in it. A ten minute hat-trick implies 162 points per 90 and
contributes `weight_now * 5`, because what carries through is the points he
actually scored and not the rate they imply. The same cancellation runs through
every `_per_90` term in the component: `credibility * xg90` is `xG / 3`. There
is no `m` small enough to break this, which is why the only guard on the
division is `m > 0`.

**The envelope.** `weight_now * credibility` is monotone in minutes and equals
`weight_now` exactly at `COMPONENT_MINUTES`. So nobody below the bar is ever
trusted more than somebody standing on it already was. The change fills in the
interior of an envelope the model accepted at its edge, and it moves nothing at
or above that edge, which has a test.

**Why linear, and not `m / (m + k)`.** That form is the standard credibility
curve and `weight_now` itself uses it, so the swap looks like a tidy-up and is
not. It never reaches one, so it would cut current-season weight by 8% at three
thousand minutes and more in mid-season, which is repricing GW30 to fix GW1. The
repair that makes it reach one at the bar, `2m / (m + 270)` clipped, is *more*
aggressive below the bar rather than less: 0.5 at ninety minutes against the
linear 0.333. Normalising a concave curve to hit one at a point pushes it up
everywhere beneath that point, and the concavity is claiming trust accumulates
fastest at the start, which is the least defensible thing to claim when the
sample is one match against one opponent. The property is separable from the
shape: `min(1, f(m) / f(COMPONENT_MINUTES))` keeps it for any increasing `f`.

**What the cancellation does not cover.** Four terms in the component have no
minutes to cancel: appearance points, the clean sheet, the conceded charge and
the defensive contribution. The first three are constants or bounded club terms.
The fourth is the one to watch, because `_poisson_at_least` is non-linear and so
a short sample is genuinely misread rather than merely down-weighted: a
midfielder with one tackle in five minutes reads 18 actions per 90 and clears a
bar of 12 at probability 0.94. All four are bounded, so the component below the
bar sits in roughly `[-1, 10]` however few minutes produced it, and the defcon
term's badness is anti-correlated with its weight, which is a second thing the
linear ramp buys and the concave form would not.

**The club rate gets the same ramp, for a reason the per-player terms do not
have.** `team_defence_rate` had no gate of its own and never needed one, because
its only consumer was gated. Once that gate became a ramp, a club's conceding
rate off one keeper's ninety minutes could reach the projection, and unlike
every other term that error is correlated across the twenty-odd players at that
club rather than diversified away: an overstated defence moves five defenders
the same way at once.

So it is shrunk towards the league mean by `credibility` on the club's own
keeper minutes, with `TEAM_DEFENCE_MINUTES` as the scale. Three matters:

- **The scale is a club's minutes, not a player's.** A club banks ninety keeper
  minutes per match however many players it fields, so 270 is three matches.
  It is a separate constant from `COMPONENT_MINUTES` at the same value for the
  same reason `PRIOR_MINUTES` is.
- **Towards the league mean, not towards nothing.** `component_rate` fills an
  absent defence with zero, which reads as no clean sheet points *and* no
  concession charge. The average club is a better answer than that.
- **The mean is unweighted.** Weighting the target by minutes would let the
  clubs with the most football behind them set the number the short-sampled
  clubs are pulled towards, which is the wrong way round.

Measured on the real payload one match into 2026/27: eighteen clubs spread from
0.20 to 3.87 per 90, all off ninety minutes each, shrinking to a range of 1.07
to 2.30 with the league mean unmoved.

**Where it can be worse than the old answer.** One case: a player who faced a
promoted side at home in GW1 has an inflated rate, the ramp reads that as
talent, and `fixture_multiplier` then adjusts for his next opponent on top, so
the opponent is counted twice. The model has the same defect at 270 minutes and
it is sharper at one match, where a single opponent is the whole sample. What
bounds it is the composite weight, which for a ninety minute player in GW1 is
`(1/7) * (1/3)`, so a rate wrong by half moves the projection by about 2%. By
the time the weight is material he has faced two or three different opponents.

## The haul distribution

**The mean already answers the captaincy question, and this does not replace
it.** FPL doubles the captain's score, so his contribution to the expected total
is exactly his expected points, and under a points objective the right captain
is `argmax xpts_gw`. `haul_frame` therefore ranks on the projection. It ranked
on `haul_chance` when it was first written, which stated a decision rule this
model does not hold, and there is now a test that it does not go back.

What the distribution adds is the three things a mean cannot express: that you
are usually maximising rank rather than points, so a rival's position decides
whether you want variance or want to avoid it; that Triple Captain is a one-shot
and cannot be averaged over many weeks; and that a captain most of the field
already has moves your rank very little. The first and third want a rival or the
field and are not built. See `ROADMAP.md`.

With that established: `captaincy.py` puts a distribution on the part of a
gameweek that swings. Goals and assists are independent Poissons on
`attacking_rates`, scaled by the same fixture multiplier the projection uses,
and the points they pay come from the same scoring table.

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

**It does not reconcile with `xpts_next`, and the front end says so.** Four
reasons, and hearing only the first leaves the rest assumed away:

1. `xpts_next` also contains clean sheets, saves, goals conceded, defensive
   contribution and cards. None of them are here, so for a keeper or a defender
   this is an attacking haul chance rather than a haul chance.
2. `points_per_90` is `(1 - w)` of an observed rate plus `w` of the component
   rate, so only `w` of the expected goals ever reaches `xpts_next`. This uses
   them at full weight with no blending at all. Even the attacking part is a
   different number.
3. Below `COMPONENT_MINUTES` the two read different columns as well as
   different weights: the tab takes `xg90_shrunk` and the projection takes raw
   `xg90`. So the one narrow claim that does hold, that the distribution's
   expected goals are `xg90 * minutes_share * multiplier` exactly, holds at and
   above the bar and is approximate below it.
4. Bonus is in the projection, read at the rate it was earned, and not in
   the distribution, which has no rate to draw it from. So this understates
   where the projection now does not, and it bites because the threshold is
   absolute: a forward's goal and assist is nine points on this scale and
   twelve on the real one.

There is no cheap way to make them agree and the tempting expensive way is
wrong. Scaling the rates by `w` is not the same claim as a player being `w`
times as likely to score, and it would drag every haul chance towards zero in
September, which is when the question is live. What can be said, and is, is the
narrow true thing in the mixture paragraph above.

## Measuring it

**Every constant above can be scored, and `backtest.py` is how.** The
bootstrap's season-to-date counters are exactly the sum of the per-gameweek
figures in `event/{gw}/live/`, and `COUNTING_STATS` in `data.py` names them.
So `as_of` rewinds a `Season` to how it stood after any played gameweek,
`evaluate` projects the following one off that and reads it against what was
scored, and `sweep` does it again at each value of a constant. Rank correlation
is Spearman by ranking both sides and taking Pearson, because scipy is not in
the deploy and is not going in for one function.

What the rewind cannot do: availability, the published chance of playing,
prices, set piece duty and ownership all read as they stand today, because the
API keeps no history of them. Availability is opened up entirely, so a player
injured at the time projects as fit, which is why every figure is given twice,
over everyone and over those who played, and why bias over everyone is not the
number to read. Rank correlation is, and then what the top fifty went on to
score.

Two baselines ride along and the model is always read against them. Last
season's points over 38 is the identity `test_projection_is_not_just_last_seasons_points_over_38`
guards against, and this season's points per game is the sort every
spreadsheet does. Three gameweeks in the second out-ranked the model over the
whole pool, 0.686 to 0.518, while the model's top fifty scored more, 3.80 to
3.70, and the difference was the minutes term. That is what the role section
above fixed.

**A default argument that names a constant is bound at import.**
`fixture_multiplier(weight=STRENGTH_WEIGHT)` was, and a sweep of
`STRENGTH_WEIGHT` reported the same number at every value, which reads as a
finding that the weight does not matter. It and `credibility` now read their
constants at call time, and there is a test that a sweep of the weight moves
the answer.

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
