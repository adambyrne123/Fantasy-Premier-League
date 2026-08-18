"""Projected points per player per gameweek.

The model is deliberately simple and transparent:

    xPts(player, gw) = sum over that club's fixtures in gw of
                       points_per_90 * expected_minutes_share * fixture_multiplier

Each of the three terms is estimated separately so you can inspect or replace
any one of them. `points_per_90` shrinks from last season's rate towards this
season's as gameweeks accumulate, which is what stops week one noise from
dominating in September.

Nothing here is a market-beating model. It is a defensible baseline you can
argue with, which is the point: the output is a ranked list you then override
with things the API cannot see, such as a manager saying in a press conference
that someone is being rested.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from .api import default_cache_dir
from .data import DEFCON_THRESHOLD, Season

PRIOR_CACHE = default_cache_dir() / "prior_season.parquet"

# A copy committed to the repo, for deploys that start with an empty cache.
# Refreshing it is 700 requests, which times out a cold Community Cloud boot.
BUNDLED_PRIOR = Path(__file__).resolve().parent.parent / "prior_season.parquet"
GAMES_IN_SEASON = 38
SHRINKAGE_GAMES = 6.0  # gameweeks of current data needed to half-trust it
DIFFICULTY_ALPHA = 0.09  # points swing per unit of FDR away from average
HOME_BONUS = 0.03

# How much of the fixture term comes from the clubs' attack and defence ratings
# rather than FPL's 1-5 difficulty rating. The rest stays on difficulty, which
# is set by hand and occasionally knows something the ratings do not.
STRENGTH_WEIGHT = 0.6
STRENGTH_ALPHA = 0.35  # how far a full strength gap moves the multiplier
# a rating ratio past these is a mismatch the model should not extrapolate from
STRENGTH_FLOOR = 0.6
STRENGTH_CEILING = 1.5

# How much of a player's start rate comes from what he did last season, with
# the rest coming from what his price implies. Anything short of 1.0 is what
# stops the projection collapsing back into last season's points, see
# `_minutes_share` for why that matters.
START_RATE_TRUST = 0.6
# what a player with nothing to go on either way is assumed to start at
UNKNOWN_START_RATE = 0.35
SUB_SHARE = 0.12  # share of a match a substitute appearance is worth
STARTER_DURATION = 0.85  # share of 90 a starter lasts, absent anything better

PRIOR_COLUMNS = ["prior_season", "prior_points", "prior_minutes", "prior_starts", "prior_end_cost"]

# FPL's scoring, as the component rate reproduces it. Every category is here,
# including the three that cost points, because what a defender stands to lose
# separates him from a forward as much as his clean sheet does. Two of them are
# thresholds within a match rather than rates, so they are estimated rather than
# counted, and `_conceded_points` and `_poisson_at_least` say how.
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 2
# FPL pays one point for an appearance and two for an hour. `component_rate`
# uses the flat two on purpose, because it is a per 90 rate and asking it
# which side of an hour a player finished would thread the minutes term into
# it. `captaincy.py` draws the minutes branch first and can afford the split.
APPEARANCE_MINUTES = 60
SHORT_APPEARANCE_POINTS = 1

# One point per three saves, and only a keeper makes them. Position dicts rather
# than a scalar and a test on position, because that is how the two tables above
# already say "this position only" and it keeps the assembly one expression.
SAVE_POINTS = {"GKP": 1, "DEF": 0, "MID": 0, "FWD": 0}
SAVES_PER_POINT = 3
# Saves are paid in whole threes within a match and the leftovers are lost
# rather than carried, so a rate divided by three overstates by whatever the
# remainder averages. Across a season that is one, the mean of nought, one and
# two. Derived rather than fitted: against a Poisson it is within 0.003 of the
# exact figure for anyone making two or more saves a match, and errs low below.
SAVE_REMAINDER = 1.0

# Minus one per two conceded, and only the two positions that are charged it.
CONCEDED_POINTS = {"GKP": -1, "DEF": -1, "MID": 0, "FWD": 0}
CONCEDED_PER_POINT = 2

# Two points for reaching the defensive contribution threshold in a match, which
# `DEFCON_THRESHOLD` in `data.py` holds because it is a rule about what counts
# rather than part of the scoring table here.
DEFCON_POINTS = {"GKP": 0, "DEF": 2, "MID": 2, "FWD": 2}

YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3

# A first choice penalty taker is worth roughly a goal every eight or nine
# matches over someone otherwise identical, and a first choice direct free kick
# taker rather less. Set by eye against how many spot kicks a season actually
# produces, so they are a starting point to tune rather than a finding.
PENALTY_XG_P90 = 0.055
FREEKICK_XG_P90 = 0.012

# Minutes a player needs this season before his own rates mean anything. Below
# it the component rests on the team's defence and his set piece duty only.
COMPONENT_MINUTES = 270


def fetch_prior_season(season: Season, delay: float = 0.15) -> pd.DataFrame:
    """Pull last season's totals for every player via element-summary.

    One request per player, so a few minutes on a cold cache and cached for 24
    hours after. Only the immediately preceding season survives. `history_past`
    lists Premier League seasons and nothing else, so for a player who spent
    last year abroad or in the Championship its final entry can be several
    years old. Blending that in as though it were last season is worse than
    having nothing at all, because with nothing `_fill_missing_rates` at least
    knows it is guessing.

    Players with no Premier League history come back empty and are handled by
    `_fill_missing_rates`.
    """
    rows = []
    for pid in season.players.index:
        try:
            summary = season.api.element_summary(int(pid))
        except Exception:
            continue
        past = summary.get("history_past") or []
        if not past:
            continue
        last = past[-1]
        rows.append(
            {
                "id": int(pid),
                "prior_season": last.get("season_name"),
                "prior_points": last.get("total_points", 0),
                "prior_minutes": last.get("minutes", 0),
                "prior_starts": last.get("starts"),
                "prior_end_cost": last.get("end_cost"),
            }
        )
        time.sleep(delay)

    if not rows:
        return pd.DataFrame(columns=PRIOR_COLUMNS).rename_axis("id")

    df = pd.DataFrame(rows).set_index("id")
    # the newest season anyone appears in is last season, since history_past
    # never contains the season currently being played
    latest = df["prior_season"].dropna().max()
    return df[df["prior_season"] == latest] if latest else df


def load_prior(season: Season, refetch: bool = False) -> pd.DataFrame | None:
    """Last season's totals, from the cache, the repo, or the API in that order.

    The order is what makes a cold deploy survive. Fetching is one request per
    player and will time out a fresh boot, so a copy committed to the repo is
    tried before the network. Both front ends go through this so they cannot
    disagree about which season's data they are using.
    """
    if not refetch:
        for source in (PRIOR_CACHE, BUNDLED_PRIOR):
            if source.exists():
                return pd.read_parquet(source)

    prior = fetch_prior_season(season)
    if not prior.empty:
        PRIOR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        prior.to_parquet(PRIOR_CACHE)
    return prior


def _fill_missing_rates(players: pd.DataFrame, rate_col: str) -> pd.Series:
    """Estimate a rate for players with no history, from price within position.

    FPL prices new signings roughly in line with what the game expects of them,
    so a per-position fit of rate against price is a reasonable stand-in. It is
    a stand-in, though: a 5.5m promoted midfielder gets a promoted-club-shaped
    projection whatever they actually did in the Championship.
    """
    filled = players[rate_col].copy()
    for _pos, group in players.groupby("position"):
        known = group[group[rate_col].notna() & (group[rate_col] > 0)]
        missing = group.index[group[rate_col].isna()]
        if len(missing) == 0:
            continue
        if len(known) < 10:
            filled.loc[missing] = float(known[rate_col].median() or 0.0)
            continue
        slope, intercept = np.polyfit(known["now_cost"], known[rate_col], 1)
        predicted = intercept + slope * players.loc[missing, "now_cost"]
        filled.loc[missing] = predicted.clip(lower=0.0)
    return filled.fillna(0.0)


def _price_implied(players: pd.DataFrame, observed: pd.Series) -> pd.Series:
    """Per-position straight line fit of some share against price.

    Price is the game's own view of how much a player will feature, and it is
    the only signal available before a ball is kicked that does not come from
    last season. That independence is the entire point of using it.
    """
    fitted = pd.Series(np.nan, index=players.index, dtype=float)
    for _pos, group in players.groupby("position"):
        known = observed.loc[group.index].dropna()
        if len(known) < 10:
            fitted.loc[group.index] = float(known.median()) if len(known) else np.nan
            continue
        slope, intercept = np.polyfit(players.loc[known.index, "now_cost"], known, 1)
        fitted.loc[group.index] = intercept + slope * players.loc[group.index, "now_cost"]
    return fitted.clip(0.0, 1.0)


def _role(starts: pd.Series, minutes: pd.Series, games: int, index) -> tuple[pd.Series, pd.Series]:
    """Split playing time into how often a player starts and how long he lasts.

    Both come back as shares, and NaN wherever there is nothing to go on.
    """
    starts = pd.to_numeric(starts, errors="coerce").reindex(index)
    minutes = pd.to_numeric(minutes, errors="coerce").reindex(index)
    duration = (minutes / starts.replace(0, np.nan) / 90).clip(0, 1)
    if not games:
        return pd.Series(np.nan, index=index, dtype=float), duration
    return (starts / games).clip(0, 1), duration


def _start_mixture(
    players: pd.DataFrame, start_rate: pd.Series, duration: pd.Series
) -> pd.DataFrame:
    """The chance a player starts, and the share of 90 that start is worth.

    `_minutes_share` is these two collapsed into one number, and that number
    stays what everything downstream reads. They are kept apart for
    `captaincy.py`, because a haul is a tail and a tail does not survive being
    scaled. The chance of two goals off `p * lambda` is roughly `p` times
    smaller than the honest mixture of a start and a cameo off the bench.
    """
    implied = _price_implied(players, start_rate)
    blended = np.where(
        start_rate.isna(),
        implied,
        START_RATE_TRUST * start_rate + (1 - START_RATE_TRUST) * implied,
    )
    blended = pd.Series(blended, index=players.index).fillna(UNKNOWN_START_RATE).clip(0, 1)
    lasts = duration.fillna(STARTER_DURATION).clip(0, 1)
    return pd.DataFrame({"start_chance": blended, "start_minutes": blended * lasts})


def _collapse(mixture: pd.DataFrame) -> pd.Series:
    """The mixture as one expected share of 90, which is what the model uses."""
    return (mixture["start_minutes"] + (1 - mixture["start_chance"]) * SUB_SHARE).clip(0, 1)


def _minutes_share(players: pd.DataFrame, start_rate: pd.Series, duration: pd.Series) -> pd.Series:
    """Expected share of 90 minutes in a single match.

    Worth being explicit about why this is not simply minutes over minutes
    available. That formula cancels exactly against `points_per_90`, since
    points/(minutes/90) times minutes/(38*90) is points/38, which left the
    whole projection equal to last season's total points divided by 38. A
    striker who scored at a high rate across half a season was then
    indistinguishable from a plodder who played every week, and no amount of
    re-deriving minutes from `starts` fixes that, because it reconstructs the
    same number.

    So the start rate is shrunk towards what price implies. Price is the one
    input that is not last season's minutes, which is what breaks the identity
    and lets a player's rate and his expected role move independently.
    """
    return _collapse(_start_mixture(players, start_rate, duration))


def team_defence_rate(players: pd.DataFrame) -> pd.Series:
    """Goals each club is expected to concede per 90, by club id.

    Taken off the keepers, because `expected_goals_conceded` is charged to a
    player only while he was on the pitch, and a keeper is on it for all of it.
    Summing outfielders instead would count the same goals once per defender
    and give a number several times too large.

    Empty when nobody has the column or nobody has played, which is what
    pre-season looks like, and callers treat that as unknown rather than as a
    league of clubs that concede nothing.
    """
    if "expected_goals_conceded" not in players.columns:
        return pd.Series(dtype="float64")

    keepers = players[players["position"] == "GKP"]
    conceded = pd.to_numeric(keepers["expected_goals_conceded"], errors="coerce").fillna(0.0)
    minutes = pd.to_numeric(keepers["minutes"], errors="coerce").fillna(0.0)

    by_club = pd.DataFrame({"team": keepers["team"], "xgc": conceded, "minutes": minutes})
    totals = by_club.groupby("team")[["xgc", "minutes"]].sum()
    rate = totals["xgc"] / (totals["minutes"] / 90)
    return rate.replace([np.inf, -np.inf], np.nan).dropna()


def _per_90(
    players: pd.DataFrame, column: str, ninetieths: pd.Series, played_enough: pd.Series
) -> pd.Series:
    """A counting stat as a rate, zero wherever it cannot be one.

    `DataFrame.get` on a column that is not there gives NaN rather than raising,
    so the arithmetic still produces a series of the right shape and the term
    that reads it drops out instead of taking the rest of the rate down with it.
    That is what lets a payload from before a category existed lose one term
    rather than the whole component.
    """
    counted = pd.to_numeric(players.get(column), errors="coerce")
    return (counted / ninetieths).where(played_enough).fillna(0.0)


def _conceded_points(xgc: pd.Series) -> pd.Series:
    """Expected goals conceded per 90 turned into how many are actually charged.

    FPL takes a point per two conceded and the odd one is free, so the charge is
    `floor(C / 2)` rather than `C / 2`. Under the same Poisson the clean sheet
    already assumes, that has a closed form:

        E[floor(C / 2)] = lambda / 2 - (1 - exp(-2 lambda)) / 4

    because `E[C mod 2]` is the chance of an odd count, `(1 - exp(-2 lambda))/2`.
    Exact given the Poisson, so the only assumption here is the one made once
    for the clean sheet and not made again.

    Nothing is double counted against that clean sheet. It pays at nought
    conceded, neither term fires at one, and only this one fires at two or more.
    """
    return xgc / CONCEDED_PER_POINT - (1 - np.exp(-CONCEDED_PER_POINT * xgc)) / 4


def _poisson_at_least(rate: pd.Series, threshold: pd.Series) -> pd.Series:
    """Chance of at least `threshold` events in one match, given a per 90 rate.

    Twelve terms at most, which is why this walks the head of the distribution
    rather than pulling in a special function and a dependency for it. Every
    player carries his own threshold, so each term is subtracted only from the
    rows it is actually below.
    """
    rate = rate.clip(lower=0.0).fillna(0.0)
    threshold = threshold.fillna(0.0)
    cdf = pd.Series(0.0, index=rate.index)
    term = np.exp(-rate)
    highest = int(threshold.max()) if len(threshold) else 0
    for k in range(highest):
        cdf = cdf + term.where(k < threshold, 0.0)
        term = term * rate / (k + 1)
    return (1 - cdf).clip(0.0, 1.0)


def attacking_rates(players: pd.DataFrame) -> pd.DataFrame:
    """Expected goals and assists per 90, with set piece duty and the gate.

    Public because `captaincy.py` puts a distribution on exactly these two
    numbers, and a second definition of a number is how two figures on the same
    screen come to disagree. `ninetieths` and `played_enough` come back beside
    them so a caller inherits `COMPONENT_MINUTES` rather than re-deriving it.

    Reads `minutes` and the expected goals fields, which before the first
    deadline are still last season's. What keeps `component_rate` honest about
    that is the blend weight being zero rather than anything here, so a second
    caller needs a guard of its own. See the season rollover section of
    `docs/model.md`.
    """
    minutes = pd.to_numeric(players.get("minutes"), errors="coerce").fillna(0.0)
    ninetieths = minutes / 90
    played_enough = minutes >= COMPONENT_MINUTES

    # set piece duty is a claim on future chances rather than a record of past
    # ones, which is why it is added rather than being left to the xG above
    order = pd.to_numeric(players.get("penalties_order"), errors="coerce")
    freekicks = pd.to_numeric(players.get("direct_freekicks_order"), errors="coerce")
    xg90 = _per_90(players, "expected_goals", ninetieths, played_enough)
    xg90 = xg90 + (order == 1) * PENALTY_XG_P90 + (freekicks == 1) * FREEKICK_XG_P90

    return pd.DataFrame(
        {
            "xg90": xg90,
            "xa90": _per_90(players, "expected_assists", ninetieths, played_enough),
            "ninetieths": ninetieths,
            "played_enough": played_enough,
        },
        index=players.index,
    )


def component_rate(players: pd.DataFrame, defence: pd.Series | None = None) -> pd.Series:
    """Points per 90 rebuilt from what a player is expected to do, not what he scored.

    A scalar `points_per_90` cannot tell apart a defender who keeps clean sheets
    from a forward who scores, because both collapse into the same number. This
    splits them by the thing FPL actually pays for:

        appearance + expected goals * goal points + expected assists * 3
                   + P(clean sheet) * clean sheet points
                   + saves paid in threes
                   - goals conceded charged in twos
                   + P(defensive contribution) * 2
                   - cards

    Clean sheet probability is the Poisson zero, `exp(-xGC per 90)`, on the
    club's rate rather than the player's. The opponent adjustment deliberately
    does not appear here. It belongs in the fixture term, where the strength
    ratings already handle it, and applying it twice would double count.

    The two threshold terms are estimates rather than counts, and it is worth
    being clear about which way they are wrong. Real defensive action counts are
    overdispersed against a Poisson, because how much defending a player does
    depends on how the match is going, so `_poisson_at_least` understates for
    anyone well below his bar. Both threshold terms are also per 90 where the
    rule is per match, so scaling them by `expected_minutes_share` afterwards is
    linear where the truth is not: the bar does not come down when the minutes
    do, and a part-player is overstated. Neither is fixed here. Threading the
    minutes term into this one would couple two of the three terms the whole
    model exists to keep separable.

    Cards are the one linear term, and they only see the points. A red also
    costs the following match through suspension, which nothing here looks
    forward to, though `available` already drops anyone currently serving one.

    Comes back as NaN for anyone the inputs cannot describe, so the caller can
    fall back rather than being handed a confident zero.
    """
    index = players.index
    needed = {"expected_goals", "expected_assists", "minutes", "position"}
    if not needed <= set(players.columns):
        return pd.Series(np.nan, index=index, dtype="float64")

    attacking = attacking_rates(players)
    xg90, xa90 = attacking["xg90"], attacking["xa90"]
    ninetieths, played_enough = attacking["ninetieths"], attacking["played_enough"]

    position = players["position"]
    goal_points = position.map(GOAL_POINTS).astype("float64")
    cs_points = position.map(CLEAN_SHEET_POINTS).astype("float64")

    if defence is None or defence.empty:
        clean_sheet = pd.Series(np.nan, index=index, dtype="float64")
        conceded = pd.Series(np.nan, index=index, dtype="float64")
    else:
        xgc = players["team"].map(defence).astype("float64")
        clean_sheet = np.exp(-xgc) * cs_points
        # the same Poisson and the same rate as the line above, which is what
        # stops the upside and the downside of one defence disagreeing
        conceded = _conceded_points(xgc) * position.map(CONCEDED_POINTS).astype("float64")

    # Whole threes within a match, so the remainder is lost rather than banked.
    # Dividing the rate by three straight would pay for saves nobody was paid
    # for, which for a busy keeper is worth about a third of a point per 90.
    saves90 = _per_90(players, "saves", ninetieths, played_enough)
    save_points = ((saves90 - SAVE_REMAINDER).clip(lower=0.0) / SAVES_PER_POINT) * position.map(
        SAVE_POINTS
    ).astype("float64")

    defcon90 = _per_90(players, "defensive_contribution", ninetieths, played_enough)
    if not defcon90.any():
        # a payload from before the category existed, where the sum has to be
        # rebuilt from its parts and recoveries count for everyone but defenders
        parts = _per_90(players, "tackles", ninetieths, played_enough) + _per_90(
            players, "clearances_blocks_interceptions", ninetieths, played_enough
        )
        recoveries = _per_90(players, "recoveries", ninetieths, played_enough)
        defcon90 = parts + recoveries.where(position != "DEF", 0.0)

    threshold = position.map(DEFCON_THRESHOLD).astype("float64")
    defcon = _poisson_at_least(defcon90, threshold) * position.map(DEFCON_POINTS).astype("float64")
    # a keeper has no threshold and is not eligible, so he scores none of this
    defcon = defcon.where(position != "GKP", 0.0)

    cards = _per_90(players, "yellow_cards", ninetieths, played_enough) * YELLOW_CARD_POINTS
    cards = cards + _per_90(players, "red_cards", ninetieths, played_enough) * RED_CARD_POINTS

    rate = APPEARANCE_POINTS + xg90 * goal_points + xa90 * ASSIST_POINTS
    rate = rate + clean_sheet.fillna(0.0) + conceded.fillna(0.0)
    # every penalty is filled rather than left NaN, so a club with no conceding
    # rate is charged nothing instead of taking the whole player out
    rate = rate + save_points.fillna(0.0) + defcon.fillna(0.0) + cards.fillna(0.0)

    # Set piece duty adjusts a rate, it does not make one. Someone who has not
    # played has no expected goals and no minutes to divide by, and calling the
    # appearance points alone a scoring rate would be inventing a number rather
    # than measuring one. Pre-season that is everybody.
    #
    # Every term above sits on this side of the gate on purpose. It is what
    # makes all of them inherit `COMPONENT_MINUTES` for nothing, so do not move
    # one below it.
    return rate.where(played_enough).astype("float64")


def build_rates(season: Season, prior: pd.DataFrame | None = None) -> pd.DataFrame:
    """Points per 90 and expected minutes share for every player."""
    p = season.players
    played = season.gameweeks_played
    weight_now = played / (played + SHRINKAGE_GAMES) if played else 0.0

    out = pd.DataFrame(index=p.index)
    out["name"] = p["name"]
    out["position"] = p["position"]
    out["club"] = p["club"]
    out["now_cost"] = p["now_cost"]
    out["price"] = p["price"]
    out["team"] = p["team"]
    out["ownership"] = p["selected_by_percent"]

    if prior is None or prior.empty:
        prior = pd.DataFrame(index=p.index, columns=PRIOR_COLUMNS)
    prior = prior.reindex(p.index)
    for column in PRIOR_COLUMNS:
        if column not in prior.columns:
            prior[column] = np.nan

    prior_minutes = pd.to_numeric(prior["prior_minutes"], errors="coerce")
    prior_points = pd.to_numeric(prior["prior_points"], errors="coerce")

    # points per 90, from each source, only where the sample is worth using
    prior_rate = np.where(
        prior_minutes.fillna(0) >= 270, prior_points / (prior_minutes / 90), np.nan
    )
    now_rate = np.where(p["minutes"] >= 270, p["total_points"] / (p["minutes"] / 90), np.nan)

    out["prior_p90"] = prior_rate
    out["current_p90"] = now_rate
    out["prior_p90"] = _fill_missing_rates(out, "prior_p90")

    blended = np.where(
        np.isnan(now_rate),
        out["prior_p90"],
        weight_now * np.nan_to_num(now_rate) + (1 - weight_now) * out["prior_p90"],
    )
    observed = pd.Series(np.clip(blended, 0, None), index=p.index)

    # What he is expected to do, blended over what he has been scoring, by the
    # same weight that governs current against prior. Pre-season that weight is
    # zero, so this contributes nothing and the old behaviour stands, which is
    # the defined pre-season behaviour every new term needs. It also has to be:
    # FPL publishes the expected goals fields as zero until the season starts.
    out["component_p90"] = component_rate(p, team_defence_rate(p))
    out["points_per_90"] = np.clip(
        np.where(
            out["component_p90"].isna(),
            observed,
            (1 - weight_now) * observed + weight_now * out["component_p90"],
        ),
        0,
        None,
    )

    # expected share of 90 minutes, from how often he starts rather than from
    # the same minutes total that already normalised the rate above
    prior_start_rate, prior_duration = _role(
        prior["prior_starts"], prior_minutes, GAMES_IN_SEASON, p.index
    )
    prior_mix = _start_mixture(out, prior_start_rate, prior_duration)
    prior_share = _collapse(prior_mix)

    if played:
        now_start_rate, now_duration = _role(p["starts"], p["minutes"], played, p.index)
        now_mix = _start_mixture(out, now_start_rate, now_duration)
        mix = weight_now * now_mix + (1 - weight_now) * prior_mix
        share = weight_now * _collapse(now_mix) + (1 - weight_now) * prior_share
    else:
        mix = prior_mix
        share = prior_share

    out["start_rate"] = prior_start_rate
    share = pd.Series(share, index=p.index)

    chance = p["chance_of_playing_next_round"]
    share = np.where(chance.notna(), share * (chance / 100.0), share)
    share = pd.Series(share, index=p.index).where(p["available"], 0.0)

    out["minutes_share"] = share.clip(0, 1)

    # The same share, kept as the two things it is made of. A published doubt
    # and an unavailability cut both sides, so a flagged player loses his cameo
    # along with his start rather than keeping a chance of coming on. What is
    # left over, `1 - start_chance - sub_chance`, is the chance he does not
    # feature at all, and there is a test that these add back up to the share.
    features = pd.Series(1.0, index=p.index).where(chance.isna(), chance / 100.0)
    features = features.where(p["available"], 0.0).clip(0, 1)
    out["start_chance"] = (features * mix["start_chance"]).clip(0, 1)
    out["sub_chance"] = (features * (1 - mix["start_chance"])).clip(0, 1)
    # how long a start lasts, which is undefined rather than infinite for
    # somebody who never starts, so it falls back to the same default `_role`
    # hands anyone with no duration to go on
    out["starter_minutes"] = (mix["start_minutes"] / mix["start_chance"]).where(
        mix["start_chance"] > 0, STARTER_DURATION
    )
    out["status"] = p["status"]
    out["news"] = p["news"]
    # carried through unchanged so a front end can flag a doubt rather than
    # silently showing a reduced projection and letting you wonder why
    out["chance_of_playing"] = chance
    return out


def strength_multiplier(fixtures: pd.DataFrame) -> pd.Series:
    """A fixture's difficulty from the clubs' attack and defence ratings.

    FPL's own 1 to 5 rating is a five step function set before the season and
    rarely revised. The strength ratings are continuous, separate for attack
    and defence and for home and away, and they move as the season goes on, so
    they can tell apart two fixtures the difficulty rating calls identical.

    Normalised so the league average fixture is exactly 1.0. Without that every
    projection shifts by a constant factor and the headline number stops being
    points, which would quietly move the chip comparisons and the budget that
    the greedy baseline is measured against.

    NaN for any fixture missing a rating, which the caller reads as "use the
    difficulty rating instead".

    Pre-season that is every fixture. FPL publishes all six ratings as zero
    until the season is under way, so this term contributes nothing in August
    and the projection rests on the difficulty rating exactly as it did before.
    A zero is treated as absent rather than as a genuinely rated club with no
    attack, which would otherwise divide by it.
    """
    needed = ["attack_for", "defence_against", "attack_against", "defence_for"]
    if not all(c in fixtures.columns for c in needed):
        return pd.Series(np.nan, index=fixtures.index)

    values = fixtures[needed].astype("float64")
    values = values.where(values > 0)

    mean_attack = pd.concat([values["attack_for"], values["attack_against"]]).mean()
    mean_defence = pd.concat([values["defence_for"], values["defence_against"]]).mean()
    if not (mean_attack > 0 and mean_defence > 0):
        return pd.Series(np.nan, index=fixtures.index)

    attack = values["attack_for"] / mean_attack
    defence = mean_defence / values["defence_against"]
    return (attack * defence).clip(STRENGTH_FLOOR, STRENGTH_CEILING)


def fixture_multiplier(
    difficulty: pd.Series,
    is_home: pd.Series,
    strength: pd.Series | None = None,
    weight: float = STRENGTH_WEIGHT,
) -> pd.Series:
    """Convert a fixture's difficulty into a scaling factor.

    With no strength ratings this is FPL's 1 to 5 rating alone, unchanged, so
    a payload without the columns degrades to what the model did before rather
    than failing.

    Where ratings exist the two are blended. Keeping some of the difficulty
    rating is deliberate: FPL sets it by hand and it sometimes carries a view
    on a fixture that the season-long ratings have not caught up with.
    """
    base = 1.0 + (3 - difficulty) * DIFFICULTY_ALPHA

    if strength is not None and weight:
        scaled = 1.0 + (strength - 1.0) * STRENGTH_ALPHA
        blended = base * (1 - weight) + scaled * weight
        base = blended.where(scaled.notna(), base)

    return base + np.where(is_home, HOME_BONUS, -HOME_BONUS)


def project(
    season: Season,
    horizon: int = 6,
    prior: pd.DataFrame | None = None,
    start_gw: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Project points for every player over the next `horizon` gameweeks.

    Returns
    -------
    players:
        One row per player with `xpts_total` over the horizon and `xpts_next`
        for the first gameweek in it.
    by_gameweek:
        Long frame of player by gameweek projections, useful for captaincy and
        for spotting blanks and doubles.
    """
    rates = build_rates(season, prior)
    fixtures = season.team_fixtures(horizon, start_gw=start_gw)
    if fixtures.empty:
        rates["xpts_total"] = 0.0
        rates["xpts_next"] = 0.0
        return rates, pd.DataFrame(columns=["id", "event", "xpts"])

    fixtures = fixtures.copy()
    fixtures["strength"] = strength_multiplier(fixtures)
    fixtures["multiplier"] = fixture_multiplier(
        fixtures["difficulty"], fixtures["is_home"], fixtures["strength"]
    )

    merged = rates.reset_index().merge(fixtures, on="team", how="left")
    merged["xpts"] = (
        merged["points_per_90"] * merged["minutes_share"] * merged["multiplier"]
    ).fillna(0.0)

    # `multiplier` rides along so a consumer can rebuild the same fixture term
    # rather than re-deriving it off a second call to `team_fixtures`
    by_gw = merged[
        [
            "id",
            "name",
            "position",
            "club",
            "event",
            "opponent_short",
            "is_home",
            "multiplier",
            "xpts",
        ]
    ].copy()

    totals = merged.groupby("id")["xpts"].sum()
    first_gw = int(fixtures["event"].min())
    next_gw = merged[merged["event"] == first_gw].groupby("id")["xpts"].sum()

    rates["xpts_total"] = totals.reindex(rates.index).fillna(0.0)
    rates["xpts_next"] = next_gw.reindex(rates.index).fillna(0.0)
    rates["value"] = rates["xpts_total"] / (rates["now_cost"] / 10)
    rates["differential"] = differential_score(rates)
    return rates, by_gw


def differential_score(rates: pd.DataFrame) -> pd.Series:
    """How well a player projects relative to how many people own him.

    Both sides are turned into percentiles before subtracting, so the number
    has no units to argue about and no constant to tune. Positive means the
    projection ranks him higher than the crowd does, which is the definition of
    a differential worth taking.

    It says nothing about whether he is good, only about how contrarian owning
    him is. A punt nobody owns and the model does not rate either scores near
    zero, the same as a premium everybody owns and the model also rates. Read
    it beside `xpts_total`, never instead of it.
    """
    if rates.empty:
        return pd.Series(dtype="float64")

    projected = rates["xpts_total"].rank(pct=True)
    owned = rates["ownership"].fillna(0.0).rank(pct=True)
    return (projected - owned).astype("float64")
