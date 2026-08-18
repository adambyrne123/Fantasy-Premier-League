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

**None of the live code has met a real match yet.** It was written pre-season,
when `event/{gw}/live/` returns zero elements and no fixture carries a stats
array, so every test behind it runs on synthetic payloads shaped like the
documented one. The empty case is handled and verified against the real API,
but the bonus ranking and the substitution rule have only ever been exercised
against a fake. Treat the first matchday of the season as the real test, and
check a scored gameweek against the FPL site before trusting a number on it.

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
