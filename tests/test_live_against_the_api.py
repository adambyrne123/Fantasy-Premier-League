"""What `live.py` works out, checked against what FPL actually did.

Every test in here reaches the real API, so all of them carry the `network`
marker and `addopts` in `pyproject.toml` deselects them. `pytest -m network` is
the way in, and CI never runs them.

They exist because the rest of the live suite runs on payloads written by hand
before the season started, and a shape copied from documentation proves only
that the code agrees with the documentation. These compare against answers FPL
published, all of which are already in payloads `api.py` fetches:

    our bonus arithmetic  vs  the fixture's own bonus block
    our substitutions     vs  `automatic_subs` on the entry's picks
    our starting eleven   vs  the `multiplier` FPL rewrote onto each pick
    our squad total       vs  the gameweek's points in the entry history

Nothing here hardcodes a gameweek. Each test finds the most recent one that can
answer it and skips when none can, so running this mid-weekend reports what is
checkable rather than failing on what is not settled yet. It also means the
double gameweek case starts being covered the first time one is played, which
is the one thing GW1 could not prove.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from fpl_manager.data import Season
from fpl_manager.live import (
    _stat_values,
    bonus_awarded,
    load_live,
    resolve_autosubs,
    score_entry,
)

pytestmark = pytest.mark.network

# a public manager, overridable for anyone else running this. An entry id
# identifies a squad rather than a person and is public either way.
ENTRY = int(os.environ.get("FPL_ENTRY", "3921945"))


@pytest.fixture(scope="module")
def live_season() -> Season:
    """One real season for the module, since constructing it costs two calls."""
    return Season()


@pytest.fixture(scope="module")
def scored(live_season: Season):
    """The most recent gameweek with a match that has had its bonus applied.

    Counts back from the current gameweek rather than from `gameweeks_played`,
    which only moves once FPL audits. The audit being later than everything
    being checked here is half of what this file is about.
    """
    for gameweek in range(live_season.current_gameweek, 0, -1):
        state = load_live(live_season, gameweek)
        if state.fixtures.empty:
            continue
        if (state.fixtures["started"] & state.fixtures["bonus_added"]).any():
            return gameweek, state
    pytest.skip("no gameweek has had bonus applied to any match yet")


@pytest.fixture(scope="module")
def audited(scored):
    """A gameweek FPL has finished processing.

    A stricter gate than the bonus tests use, and deliberately so. The two
    payloads move at different times: a fixture updates at the whistle, which
    is why `played_out` and `bonus_is_final` read it there, but everything on
    an entry is recomputed only when FPL processes the gameweek. Measured on
    GW1 2026/27 with nine of ten matches played out, the entry history said 29
    while its own live figures summed to 35, and `automatic_subs` was still
    empty with a starter who had already been left out.

    So anything compared against the entry payload waits for the audit. Gating
    these on the whistle instead produces a confident failure in the window
    between the two, which was demonstrated rather than guessed at.
    """
    gameweek, state = scored
    if not state.fixtures["finished"].all():
        pytest.skip(f"GW{gameweek} has not been audited, so its entry payload still lags")
    return gameweek, state


# ----------------------------------------------------------------------
# bonus
# ----------------------------------------------------------------------
def test_our_bonus_is_the_bonus_fpl_awarded(live_season: Season, scored):
    """The reason this file exists.

    `bonus_awarded` rebuilds three, two and one from the bonus points system
    with standard competition ranking, which reproduces FPL's published tie
    rules without special casing any of them. That was an argument from the
    rules until this compared it against awards FPL actually made.
    """
    gameweek, _ = scored
    names = live_season.players["name"]

    checked = 0
    for fixture in live_season.api.fixtures_for_event(gameweek):
        identifiers = {b.get("identifier") for b in fixture.get("stats") or []}
        if "bonus" not in identifiers:
            continue

        awarded = _stat_values(fixture, "bonus")
        awarded = awarded[awarded > 0]

        bps = _stat_values(fixture, "bps")
        ours = bonus_awarded(bps[bps > 0])
        ours = ours[ours > 0]

        disagree = {
            str(names.get(element, element)): (
                int(awarded.get(element, 0)),
                int(ours.get(element, 0)),
            )
            for element in set(awarded.index) | set(ours.index)
            if int(awarded.get(element, 0)) != int(ours.get(element, 0))
        }
        assert not disagree, f"GW{gameweek} fixture {fixture['id']}, fpl vs ours: {disagree}"
        checked += 1

    assert checked, "the gameweek was chosen for having an awarded fixture"


def test_dropping_non_positive_scores_never_costs_a_winner(live_season: Season, scored):
    """`provisional_bonus` filters the bonus points system to positive scores,
    on the reasoning that anyone at or below zero has either not been on the
    pitch or has had a bad enough afternoon to be out of it.

    Real scores go a long way below zero, so the filter is doing something.
    This is the assertion that what it removes never placed: it would change an
    answer only if bonus went to a non-positive score, or if a fixture had
    fewer than three positive scores left to rank.
    """
    gameweek, _ = scored
    for fixture in live_season.api.fixtures_for_event(gameweek):
        bps = _stat_values(fixture, "bps")
        if bps.empty:
            continue

        awarded = _stat_values(fixture, "bonus")
        for element in awarded[awarded > 0].index:
            assert bps.get(element, 0) > 0, (
                f"GW{gameweek} fixture {fixture['id']}: bonus went to a non-positive score"
            )

        assert int((bps > 0).sum()) >= 3, (
            f"GW{gameweek} fixture {fixture['id']}: fewer than three positive scores to rank"
        )


def test_bonus_is_applied_at_the_whistle_and_not_at_the_audit(scored):
    """The bug this file was written to find, kept as the guard against it.

    `bonus_is_final` used to be `all_confirmed` and read `finished`, which is
    FPL's audit and lands a day or more after the last whistle. Every played
    match of GW1 2026/27 carried its real bonus with `finished` false, so the
    caption called FPL's own award provisional for the whole weekend.

    Only assertable while a gameweek sits between its bonus and its audit, so
    it skips outside that window rather than pretending to have checked.
    """
    gameweek, state = scored
    fixtures = state.fixtures
    if not (fixtures["bonus_added"] & ~fixtures["finished"]).any():
        pytest.skip(f"GW{gameweek} is not between its bonus and its audit")

    awaiting = fixtures["started"] & ~fixtures["bonus_added"]
    assert state.bonus_is_final == (not awaiting.any()), (
        "the caption has to follow the bonus block, not the audit flag"
    )


def test_a_match_is_over_before_fpl_says_it_is_finished(scored):
    """`played_out` reads the whistle. Anything reading `finished` undercounts,
    which is what left the fixture counter at zero of ten."""
    gameweek, state = scored
    assert (state.fixtures["finished"] <= state.played_out).all(), "audited implies over"
    if not state.played_out.any():
        pytest.skip(f"GW{gameweek} has no match over yet")
    assert int(state.played_out.sum()) >= int(state.fixtures["finished"].sum())


# ----------------------------------------------------------------------
# substitutions
# ----------------------------------------------------------------------
def _resolved(season: Season, gameweek: int, state, strict: bool = False):
    """Our answer for a real entry, beside the payload FPL published for it.

    FPL rewrites `position` on every pick when it processes a gameweek, so
    an audited payload already shows the lineup after its substitutions: the
    man who came on sits in the eleven and the man he replaced sits on the
    bench. Handing that to `resolve_autosubs` asks it to substitute for a
    substitution already made, and it correctly finds nothing to do, which
    is how GW3 2026/27 first read as a disagreement. The named lineup is
    rebuilt by swapping each pair back before resolving.
    """
    payload = season.api.entry_picks(ENTRY, gameweek)
    raw = payload["picks"]

    named = {int(p["element"]): int(p["position"]) for p in raw}
    for sub in payload.get("automatic_subs") or []:
        came_on, went_off = int(sub["element_in"]), int(sub["element_out"])
        named[came_on], named[went_off] = named[went_off], named[came_on]

    picks = pd.DataFrame(
        {"element": list(named)},
        index=list(named.values()),
    )
    if strict:
        _skip_if_the_named_lineup_is_ambiguous(season, state, payload, named)
    elements = list(picks.sort_index()["element"])
    captain = next((int(p["element"]) for p in raw if p.get("is_captain")), None)
    vice = next((int(p["element"]) for p in raw if p.get("is_vice_captain")), None)
    multiplier = max((int(p.get("multiplier") or 0) for p in raw), default=2)

    lineup = resolve_autosubs(
        picks=picks,
        minutes=state.elements["minutes"].reindex(elements).fillna(0.0),
        positions=season.players["position"].reindex(elements),
        settled=state.settled(season.players["team"].reindex(elements)),
        captain=captain,
        vice_captain=vice,
        captain_multiplier=max(multiplier, 2),
    )
    return payload, raw, lineup


def _skip_if_the_named_lineup_is_ambiguous(
    season: Season, state, payload: dict, named: dict
) -> None:
    """Swapping each pair back recovers who was named, and not always where.

    When two starters of the same position both failed to play and only one
    of them was replaced, FPL took off whichever came first in the lineup as
    the manager set it, and the audited payload no longer says which that
    was. GW3 2026/27 for the entry this runs against: two defenders on nought
    minutes, one midfielder on the bench who played, and FPL replaced the one
    the rewritten order puts second. Whether that is FPL walking the eleven
    in an order the payload has lost, or a rule this code does not know, is
    exactly what cannot be told from here, so the case is skipped with the
    facts rather than asserted either way. `ROADMAP.md` carries it. Only the
    two tests that turn on which player it was ask for this; the armband and
    the total are unaffected by which idle defender left.
    """
    minutes = state.elements["minutes"]
    positions = season.players["position"]
    starters = [e for e, pos in named.items() if pos <= 11]
    idle = [e for e in starters if float(minutes.get(e, 0)) == 0]
    for sub in payload.get("automatic_subs") or []:
        went_off = int(sub["element_out"])
        rivals = [e for e in idle if e != went_off and positions.get(e) == positions.get(went_off)]
        if rivals:
            pytest.skip(
                f"{went_off} was replaced while {rivals} of the same position also did not "
                "play, and the audited order cannot say which was named first"
            )


def test_our_substitutions_are_the_ones_fpl_made(live_season: Season, audited):
    gameweek, state = audited
    payload, _, lineup = _resolved(live_season, gameweek, state, strict=True)

    theirs = {
        (int(s["element_out"]), int(s["element_in"])) for s in payload.get("automatic_subs") or []
    }
    assert set(lineup.subs) == theirs, f"GW{gameweek} substitutions differ"


def test_our_eleven_is_the_eleven_that_counted(live_season: Season, audited):
    """Once a gameweek closes FPL rewrites `multiplier` on every pick, so the
    ones it leaves above zero are its own answer to who counted."""
    gameweek, state = audited
    _, raw, lineup = _resolved(live_season, gameweek, state, strict=True)

    theirs = {int(p["element"]) for p in raw if int(p.get("multiplier") or 0) > 0}
    assert set(lineup.starters) == theirs, f"GW{gameweek} eleven differs"


def test_the_armband_ended_up_where_fpl_put_it(live_season: Season, audited):
    gameweek, state = audited
    _, raw, lineup = _resolved(live_season, gameweek, state)

    doubled = [int(p["element"]) for p in raw if int(p.get("multiplier") or 0) > 1]
    if not doubled:
        assert lineup.captain_multiplier == 1, "nobody was doubled, so nor should ours be"
        return
    assert lineup.captain == doubled[0]
    assert lineup.captain_multiplier == max(int(p.get("multiplier") or 0) for p in raw)


# ----------------------------------------------------------------------
# the total
# ----------------------------------------------------------------------
def test_our_total_is_the_total_fpl_recorded(live_season: Season, audited):
    """Gated on the audit, and that gate is the finding.

    A manager's `points` is recomputed only when FPL processes the gameweek, so
    through a weekend it lags the live payload by a day. Measured on GW1
    2026/27 with nine matches played out: the history said 29 while FPL's own
    per player live figures summed to 35 over FPL's own multipliers, the
    difference being every match after Saturday. The site's live total is built
    from `event` live client side, the same way this is, so the recorded figure
    is an oracle only once both have stopped moving.
    """
    gameweek, state = audited
    _, _, lineup = _resolved(live_season, gameweek, state)
    ours = score_entry(state, lineup).total

    history = live_season.api.entry_history(ENTRY)
    recorded = next(
        (h for h in history.get("current") or [] if int(h.get("event", 0)) == gameweek), None
    )
    assert recorded is not None, f"no history row for GW{gameweek}"

    # the recorded figure is net of transfer hits, which are a squad decision
    # rather than a scoring one and nothing in `live.py` knows about them
    assert ours == int(recorded["points"]) + int(recorded.get("event_transfers_cost") or 0)


def test_the_rewind_reproduces_the_bootstrap(live_season: Season):
    """The identity `backtest.as_of` rests on: the bootstrap's season-to-date
    counters are the sum of the live payloads for the gameweeks played.

    Only checked between gameweeks. While one is under way the bootstrap on a
    six hour cache and the live payload on a one minute one are describing
    different moments, and the difference is the match in progress rather
    than a hole in the identity.
    """
    from fpl_manager.backtest import as_of, load_stats
    from fpl_manager.data import COUNTING_STATS

    played = live_season.gameweeks_played
    if not played:
        pytest.skip("nothing has been played")
    if live_season.current_gameweek != played:
        pytest.skip("a gameweek is under way, so the two payloads describe different moments")

    stats = load_stats(live_season.api, played)
    rewound = as_of(live_season, played, stats)
    for column in COUNTING_STATS:
        if column not in live_season.players.columns:
            continue
        actual = pd.to_numeric(live_season.players[column], errors="coerce").fillna(0.0)
        # the expected goals fields are published to two decimals per
        # gameweek and per season, so their sums can differ by rounding
        gap = (rewound.players[column] - actual).abs()
        assert gap.max() <= 0.05 * played + 1e-9, f"{column}: {gap.idxmax()} off by {gap.max()}"
