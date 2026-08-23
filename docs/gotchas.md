# Gotchas

Things that have caught someone out. `CLAUDE.md` keeps the ones that bite
without warning, wherever you happen to be working. These are the ones you want
in front of you once you are already inside the module concerned.

## The API

**The API is undocumented.** These are the endpoints the official site consumes
internally. Schema changes are possible without notice, so parse defensively and
prefer `.get()` over `[]` on optional fields.

**`entry/{id}/event/{gw}/picks/` returns 404 before the first deadline of a
season.** Anything reading a live squad must handle that and fall back to the
local squad file.

**Picks are published from the deadline, not from the final whistle, and
`gameweeks_played` counts neither.** It counts `finished` events, so from a
Saturday deadline until the last match of that gameweek is scored it reads one
behind, and in GW1 it reads zero while every squad in the game is public. Ask
`Season.current_gameweek` for anything reading a squad that exists now: your
own, the top managers', the live score. That is what `next_gameweek` is not,
either, since it points at the week open for transfers, which during GW1 is GW2.
Three properties that differ by one for a whole weekend, and picking the wrong
one fails only while a gameweek is under way, which is the only time anyone
looks. This shipped once: the live app told the user their squad did not exist
on the opening weekend of the season.

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

## Caching in the app

**Streamlit caching splits by type.** `Season` holds an HTTP session, so it uses
`@st.cache_resource`. Frames use `@st.cache_data`. Passing `Season` into a
cached function needs the `_season` underscore prefix so Streamlit skips hashing
it. Getting this wrong produces a confusing hashing error rather than a clear
one.

**`load_season` carries a ttl, and cached functions take `season.data_stamp`.**
Without the ttl, a process keeps its first `Season` forever and the disk ttl
expiring accomplishes nothing, because nothing re-enters `_get`. Without the
stamp, Streamlit is told to skip hashing the `Season`, so a rebuilt one looks
identical to the one it replaced and the old projections keep being served. New
cached functions taking `_season` need the stamp as their last argument.

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

**`finished` is not the whistle, it is the audit.** FPL sets it only once it has
confirmed a gameweek's data, a day or more after the last match ends, so for
most of a live weekend a match that is plainly over reads
`finished_provisional: true` and `finished: false`. Verified against the live
payload during GW1 on 2026-08-23: eight of ten fixtures had `minutes: 90` and
`finished_provisional` true with `finished` still false, one played two days
earlier. Reading `finished` is why the fixture counter said zero of ten and why
automatic substitutions never resolved during a weekend, which is the one time
anybody is watching. `LiveGameweek.played_out` is the property to ask, and
`settled` goes through it. `Season.club_form` documents the same trap from the
fixtures side.

This used to go on to say that bonus is the separate, later question, applied at
audit time, and that a property reading `finished` was the right one for it.
That was wrong, and the same weekend disproved it. Bonus is applied at the
whistle along with `finished_provisional`: all nine played fixtures of GW1 on
2026-08-23 carried a populated `bonus` block with `finished` still false, so the
caption gated on it read "Provisional" over figures FPL had already awarded.
`LiveGameweek.bonus_is_final` is the replacement and asks whether any started
fixture is still waiting on its `bonus` block, which is exactly the condition
under which `provisional_bonus` has anything to say.

**The fixture payload moves at the whistle and the entry payload moves at the
audit.** The rule behind both of the above, and the one to apply to any new
question. Anything read off `fixtures/?event=` is current within the minute.
Anything hanging off an entry, meaning `automatic_subs`, each pick's
`multiplier` and the gameweek totals in `entry/{id}/history/`, is recomputed
only when FPL processes the gameweek. Measured on GW1 with nine of ten matches
played out: the entry history reported 29 points while its own per player live
figures summed to 35 over its own multipliers, the gap being every match after
Saturday. So an entry total is not an oracle for a live score, and the site's
own live total is built from `event/{gw}/live/` client side, the same way this
is.

**A month is ours, not FPL's, and it costs a request per manager.** There is no
monthly endpoint and no monthly field anywhere in the API.
`Season.gameweeks_in_month` defines a month by **deadline**, so a gameweek
belongs whole to one month rather than being split by kickoff, and
`leagues.month_totals` adds one up per manager out of `entry/{id}/history/`.
It takes the difference of two running totals rather than summing the per
gameweek `points`, because the payload carries `points` and
`event_transfers_cost` side by side and never says whether the first is already
net of the second. A difference of totals cannot be wrong about it. Being one
request per league member, it sits behind a checkbox like The field does.

**Never call `st.stop()` inside a tab.** Tab bodies are one script, so it stops
every tab after the one you are in, not the tab you are in. Worse, it hides
whatever the later tabs would have done with the same bad input: an illegal
uploaded squad used to be caught by a `st.stop()` on the transfer tab, which
meant Chips was never reached to discover it could not price a chip against a
squad with no legal eleven. Handle the failure locally and let the script carry
on. `tests/test_app.py::test_an_illegal_squad_is_an_error_not_a_traceback` is
the regression test, and it only earns its keep because every tab renders.

## Live scoring

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

**The live code is checked against the API rather than against the site.**
`tests/test_live_against_the_api.py` diffs what it works out against what FPL
published: our bonus arithmetic against the fixture's own `bonus` block, our
substitutions against `automatic_subs`, our eleven against the `multiplier` FPL
rewrites onto each pick, and our total against the entry history. Every test in
it carries the `network` marker, which `addopts` deselects, so `pytest -m
network` is the way in and CI never runs it. Nothing in it hardcodes a
gameweek: each test finds the most recent one that can answer it and skips when
none can, which is what makes it worth running again after every weekend rather
than once.

Two gates, and the split is the point. The bonus tests need only a fixture with
its bonus applied. Everything reading the entry payload waits for `finished`
across the gameweek, because of the lag above; gating those on the whistle
produces a confident failure in the window between the two, which was
demonstrated rather than guessed at.

What GW1 proved: the bonus ranking, across nine fixtures and two real tie cases,
and that filtering the bonus points system to positive scores never costs a
winner, on a weekend where scores reached -14 and the lowest winning score was
27. What it could not prove: a gameweek containing an actual automatic
substitution, since none was needed, and a double gameweek, where the
per-fixture and summed bonus figures diverge.

## Fixtures

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

**`finished` on a fixture does not mean the match has been played.** FPL sets it
only once it has audited the gameweek's data, which is a day or more after the
final whistle, and `finished_provisional` is what flips when the referee does.
Bonus is not what it is waiting on: that lands at the whistle, as the live
scoring section above sets out. Checked against the live payload during GW1 on 2026-08-23: six
matches had final scorelines and `finished_provisional` true with `finished`
still false, one of them played two days earlier.

That is a trap for anything reading results rather than scheduling. It is why
`Season.club_form` counts a match by having both scores rather than by the flag,
and there is a test holding it to that. `team_fixtures` and `gameweek_shape` are
unaffected: they use the flag to decide what is still to come, which is what it
is good for, and being a day late in dropping a played fixture costs them
nothing. `live.py` already knew, which is why it carries both flags.

## The solver

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
