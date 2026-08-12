"""Pipeline tests. Every FPL rule the optimiser must respect has an assertion here.

If you add a constraint to `optimiser.py`, add the test that proves it holds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_manager.data import MAX_PER_CLUB, SQUAD_LIMITS, XI_MIN, Season
from fpl_manager.optimiser import (
    build_squad,
    pick_xi,
    plan_transfers,
    planning_pool,
    suggest_transfers,
)
from fpl_manager.projections import project

from .conftest import N_TEAMS, SQUAD_SIZE_PER_TEAM


# ----------------------------------------------------------------------
# data layer
# ----------------------------------------------------------------------
def test_player_frame_is_complete(season: Season):
    assert len(season.players) == N_TEAMS * SQUAD_SIZE_PER_TEAM
    assert season.players["position"].isin(SQUAD_LIMITS).all()
    assert season.players["club"].notna().all()


def test_next_gameweek_follows_finished_events(season: Season):
    assert season.next_gameweek == season.gameweeks_played + 1


def test_fixture_ticker_covers_every_club(season: Season):
    ticker = season.fixture_ticker(horizon=6)
    assert len(ticker) == N_TEAMS
    assert ticker["avg_difficulty"].between(1, 5).all()


def test_team_fixtures_emits_one_row_per_fixture(season: Season):
    """Doubles and blanks fall out of the row count, with no special casing."""
    horizon = 4
    tf = season.team_fixtures(horizon)
    counts = tf.groupby(["team", "event"]).size()
    assert counts.max() >= 1
    assert tf["event"].nunique() <= horizon


# ----------------------------------------------------------------------
# projections
# ----------------------------------------------------------------------
def test_every_player_gets_a_projection(projections: pd.DataFrame):
    assert projections["xpts_total"].notna().all()
    assert (projections["xpts_total"] >= 0).all()


def test_minutes_share_is_a_fraction(projections: pd.DataFrame):
    assert projections["minutes_share"].between(0, 1).all()


def test_unavailable_players_project_no_minutes(season: Season, projections: pd.DataFrame):
    injured = season.players.index[season.players["status"] == "i"]
    assert (projections.loc[injured, "minutes_share"] == 0).all()


def test_shrinkage_shifts_weight_towards_current_season(prior: pd.DataFrame):
    """Pre-season leans entirely on prior data, mid-season blends it."""
    from .conftest import FakeApi

    pre = Season(FakeApi(played=0))
    mid = Season(FakeApi(played=12))
    pre_proj = project(pre, horizon=6, prior=prior.reindex(pre.players.index))[0]
    mid_proj = project(mid, horizon=6, prior=prior.reindex(mid.players.index))[0]
    assert pre_proj["current_p90"].isna().all()
    assert mid_proj["current_p90"].notna().any()


def test_projection_survives_missing_prior_data(season: Season):
    """New signings and promoted clubs still get a price-based estimate."""
    projections, _ = project(season, horizon=6, prior=None)
    assert projections["points_per_90"].notna().all()
    assert (projections["points_per_90"] >= 0).all()


# ----------------------------------------------------------------------
# squad building: these are game rules, not preferences
# ----------------------------------------------------------------------
@pytest.fixture
def squad(projections: pd.DataFrame):
    return build_squad(projections, budget_tenths=1000)


def test_squad_has_fifteen_players(squad):
    assert len(squad.squad) == 15


def test_squad_is_within_budget(squad):
    assert squad.cost_tenths <= 1000


def test_squad_meets_position_quotas(squad):
    assert squad.squad["position"].value_counts().to_dict() == SQUAD_LIMITS


def test_squad_respects_club_cap(squad):
    assert squad.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_starting_eleven_is_a_legal_formation(squad):
    assert len(squad.xi) == 11
    counts = squad.xi["position"].value_counts()
    assert counts.get("GKP", 0) == 1
    for pos, minimum in XI_MIN.items():
        assert counts.get(pos, 0) >= minimum


def test_captain_starts_and_vice_differs(squad):
    assert squad.captain.name in squad.xi.index
    assert squad.vice_captain.name != squad.captain.name


def test_forced_inclusions_are_honoured(projections: pd.DataFrame):
    target = int(projections.sort_values("price", ascending=False).index[0])
    result = build_squad(projections, include=[target])
    assert target in result.squad.index


def test_exclusions_are_honoured(projections: pd.DataFrame):
    banned = int(projections.sort_values("xpts_total", ascending=False).index[0])
    result = build_squad(projections, exclude=[banned])
    assert banned not in result.squad.index


def test_solver_beats_greedy_value_picking(projections: pd.DataFrame, squad):
    """The reason selection is a MILP rather than a sort.

    If this starts failing, something in the formulation has broken rather than
    the greedy baseline having got cleverer.
    """
    picked, spend, by_pos, by_club = [], 0, {}, {}
    for pid, row in projections.sort_values("value", ascending=False).iterrows():
        pos, club = row["position"], row["club"]
        if by_pos.get(pos, 0) >= SQUAD_LIMITS[pos] or by_club.get(club, 0) >= MAX_PER_CLUB:
            continue
        if spend + row["now_cost"] > 1000:
            continue
        picked.append(pid)
        spend += row["now_cost"]
        by_pos[pos] = by_pos.get(pos, 0) + 1
        by_club[club] = by_club.get(club, 0) + 1

    greedy_xi = pick_xi(projections.loc[picked], points_col="xpts_total")[0]
    assert squad.xi["xpts_total"].sum() >= greedy_xi["xpts_total"].sum()


def test_tighter_budget_gives_a_cheaper_squad(projections: pd.DataFrame, squad):
    lean = build_squad(projections, budget_tenths=850)
    assert lean.cost_tenths <= 850
    assert lean.projected <= squad.projected


# ----------------------------------------------------------------------
# transfers
# ----------------------------------------------------------------------
def test_transfer_plan_respects_the_cap(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    plan = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    assert len(plan.transfers_in) <= 2
    assert len(plan.transfers_in) == len(plan.transfers_out)


def test_transfer_plan_keeps_squad_legal(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    plan = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    assert len(plan.squad) == 15
    assert plan.squad["position"].value_counts().to_dict() == SQUAD_LIMITS
    assert plan.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_transfers_stay_inside_the_bank(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    bank = 15
    plan = suggest_transfers(
        projections, owned, bank_tenths=bank, free_transfers=1, max_transfers=2
    )
    if plan.transfers_in.empty:
        pytest.skip("solver chose to roll the transfer")
    assert plan.transfers_in["now_cost"].sum() <= plan.transfers_out["now_cost"].sum() + bank


def test_selling_prices_constrain_spending(projections: pd.DataFrame, squad):
    """Undervaluing the squad should never buy a more expensive replacement."""
    owned = [int(i) for i in squad.squad.index]
    poor = {i: int(projections.loc[i, "now_cost"] * 0.7) for i in owned}
    plan = suggest_transfers(
        projections, owned, selling_prices=poor, bank_tenths=0, free_transfers=1, max_transfers=1
    )
    if plan.transfers_in.empty:
        pytest.skip("solver chose to roll the transfer")
    proceeds = sum(poor[int(i)] for i in plan.transfers_out.index)
    assert plan.transfers_in["now_cost"].sum() <= proceeds


def test_no_free_transfers_discourages_churn(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    free = suggest_transfers(projections, owned, bank_tenths=15, free_transfers=1, max_transfers=2)
    costly = suggest_transfers(
        projections, owned, bank_tenths=15, free_transfers=0, max_transfers=2
    )
    assert len(costly.transfers_in) <= len(free.transfers_in) + 1
    assert costly.hits == max(0, len(costly.transfers_in))


# ----------------------------------------------------------------------
# multi-gameweek transfer planning
# ----------------------------------------------------------------------
@pytest.fixture
def horizon(season: Season, prior: pd.DataFrame):
    """Projections plus the per-gameweek frame the planner links weeks with."""
    return project(season, horizon=4, prior=prior)


@pytest.fixture
def multiweek(horizon, squad):
    projections, by_gameweek = horizon
    return plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        free_transfers=1,
        weeks=3,
    )


def test_a_plan_covers_every_week_asked_for(multiweek):
    assert len(multiweek.weeks) == 3
    assert [w.event for w in multiweek.weeks] == sorted(w.event for w in multiweek.weeks)


def test_every_week_is_a_legal_squad(multiweek):
    """The rules hold in each week separately, not just at the end."""
    for week in multiweek.weeks:
        assert len(week.squad) == 15
        assert week.squad["position"].value_counts().to_dict() == SQUAD_LIMITS
        assert week.squad["club"].value_counts().max() <= MAX_PER_CLUB


def test_every_week_fields_a_legal_eleven(multiweek):
    for week in multiweek.weeks:
        assert len(week.xi) == 11
        assert len(week.bench) == 4
        for pos, minimum in XI_MIN.items():
            assert (week.xi["position"] == pos).sum() >= minimum
        assert week.captain.name in set(week.xi.index)


def test_each_week_changes_by_exactly_its_transfers(multiweek, squad):
    """The link constraint is the whole point, so hold it to the letter."""
    held = {int(i) for i in squad.squad.index}
    for week in multiweek.weeks:
        expected = held - set(week.transfers_out.index) | set(week.transfers_in.index)
        assert set(week.squad.index) == expected
        assert len(week.transfers_in) == len(week.transfers_out)
        held = expected


def test_the_bank_never_goes_negative(multiweek):
    for week in multiweek.weeks:
        assert week.bank_tenths >= 0


def test_the_transfer_cap_holds_in_every_week(horizon, squad):
    projections, by_gameweek = horizon
    plan = plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        weeks=3,
        max_transfers_per_week=1,
    )
    for week in plan.weeks:
        assert len(week.transfers_in) <= 1


def test_a_quiet_week_banks_a_free_transfer(horizon, squad):
    """Rolling is the thing single-week planning cannot do."""
    projections, by_gameweek = horizon
    plan = plan_transfers(
        projections,
        by_gameweek,
        current_ids=[int(i) for i in squad.squad.index],
        bank_tenths=10,
        free_transfers=1,
        weeks=3,
    )
    for previous, following in zip(plan.weeks, plan.weeks[1:], strict=False):
        if not len(previous.transfers_in):
            assert following.free_transfers <= 5
            assert following.free_transfers >= previous.free_transfers


def test_unknown_selling_prices_are_declared(horizon, squad):
    """The money error compounds over weeks, so it must not be silent."""
    projections, by_gameweek = horizon
    owned = [int(i) for i in squad.squad.index]
    vague = plan_transfers(projections, by_gameweek, owned, bank_tenths=10, weeks=2)
    assert vague.approximate_money

    known = {i: int(projections.loc[i, "now_cost"]) for i in owned}
    exact = plan_transfers(
        projections, by_gameweek, owned, selling_prices=known, bank_tenths=10, weeks=2
    )
    assert not exact.approximate_money


def test_selling_prices_constrain_the_whole_plan(horizon, squad):
    """Undervaluing the squad must bind in every week, not only the first."""
    projections, by_gameweek = horizon
    owned = [int(i) for i in squad.squad.index]
    poor = {i: int(projections.loc[i, "now_cost"] * 0.6) for i in owned}
    plan = plan_transfers(
        projections, by_gameweek, owned, selling_prices=poor, bank_tenths=0, weeks=3
    )
    bank = 0
    for week in plan.weeks:
        bank += sum(
            poor.get(int(i), int(projections.loc[i, "now_cost"])) for i in week.transfers_out.index
        )
        bank -= sum(int(projections.loc[i, "now_cost"]) for i in week.transfers_in.index)
        assert bank >= 0


def test_the_pool_always_keeps_what_you_own(horizon, squad):
    """A plan that cannot see a player it owns cannot sell him."""
    projections, _ = horizon
    owned = [int(i) for i in squad.squad.index]
    pool = planning_pool(projections, owned, pool_size=40)
    assert set(owned) <= set(pool.index)


def test_planning_needs_a_gameweek_to_plan_over(horizon, squad):
    projections, by_gameweek = horizon
    with pytest.raises(ValueError, match="No gameweeks"):
        plan_transfers(
            projections,
            by_gameweek.iloc[0:0],
            current_ids=[int(i) for i in squad.squad.index],
        )


# ----------------------------------------------------------------------
# lineup
# ----------------------------------------------------------------------
def test_lineup_splits_eleven_and_four(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    xi, bench, captain = pick_xi(projections.loc[owned])
    assert len(xi) == 11
    assert len(bench) == 4
    assert captain.name in xi.index


def test_lineup_meets_formation_minimums(projections: pd.DataFrame, squad):
    owned = [int(i) for i in squad.squad.index]
    xi, _, _ = pick_xi(projections.loc[owned])
    counts = xi["position"].value_counts()
    assert counts.get("GKP", 0) == 1
    for pos, minimum in XI_MIN.items():
        assert counts.get(pos, 0) >= minimum


# ----------------------------------------------------------------------
# prior season loading: the thing that decides whether a cold deploy boots
# ----------------------------------------------------------------------
def test_prior_prefers_the_cache(season: Season, prior: pd.DataFrame, tmp_path, monkeypatch):
    from fpl_manager import projections as proj_module

    cached = tmp_path / "cached.parquet"
    prior.to_parquet(cached)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", cached)
    monkeypatch.setattr(proj_module, "BUNDLED_PRIOR", tmp_path / "absent.parquet")

    assert len(proj_module.load_prior(season)) == len(prior)


def test_prior_falls_back_to_the_committed_copy(
    season: Season, prior: pd.DataFrame, tmp_path, monkeypatch
):
    """With an empty cache the repo copy is used rather than 700 requests."""
    from fpl_manager import projections as proj_module

    bundled = tmp_path / "prior_season.parquet"
    prior.to_parquet(bundled)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", tmp_path / "absent.parquet")
    monkeypatch.setattr(proj_module, "BUNDLED_PRIOR", bundled)

    def explode(*args, **kwargs):
        raise AssertionError("fetched from the network when a committed copy existed")

    monkeypatch.setattr(proj_module, "fetch_prior_season", explode)
    assert len(proj_module.load_prior(season)) == len(prior)


# ----------------------------------------------------------------------
# prior season: only the season that just finished counts
# ----------------------------------------------------------------------
def test_only_the_most_recent_season_is_kept(season: Season):
    """`history_past` holds Premier League seasons only.

    A player who spent last year abroad has an entry that is years old, and
    treating it as last season's form is worse than admitting we know nothing.
    """
    from fpl_manager.projections import fetch_prior_season

    from .conftest import LAST_SEASON

    prior = fetch_prior_season(season, delay=0.0)
    assert not prior.empty
    assert (prior["prior_season"] == LAST_SEASON).all()


def test_players_with_only_stale_history_are_dropped(season: Season):
    from fpl_manager.projections import fetch_prior_season

    prior = fetch_prior_season(season, delay=0.0)
    stale = [int(i) for i in season.players.index if i % 5 == 0 and i % 7 != 0]
    assert stale, "fixture should produce some stale-history players"
    assert not set(stale) & set(prior.index)


def test_starts_are_captured(season: Season):
    """The minutes model needs starts, so the fetch has to bring them back."""
    from fpl_manager.projections import fetch_prior_season

    prior = fetch_prior_season(season, delay=0.0)
    assert "prior_starts" in prior.columns
    assert prior["prior_starts"].notna().all()


# ----------------------------------------------------------------------
# the minutes model must not collapse back into last season's points
# ----------------------------------------------------------------------
def test_projection_is_not_just_last_seasons_points_over_38(season: Season):
    """The identity this replaced.

    points/(minutes/90) times minutes/(38*90) is exactly points/38, so the old
    formulation made the rate and the minutes terms cancel. If that identity
    ever comes back, every player's per-gameweek projection equals their prior
    total over 38 and the two terms are decorative.
    """
    from fpl_manager.projections import build_rates

    from .conftest import make_prior

    prior = make_prior(season)
    rates = build_rates(season, prior)
    joined = rates.join(prior[["prior_points", "prior_minutes"]])
    solid = joined[(joined["prior_minutes"] >= 900) & (joined["minutes_share"] > 0)]
    assert len(solid) > 20

    per_gameweek = solid["points_per_90"] * solid["minutes_share"]
    collapsed = solid["prior_points"] / 38
    assert not np.allclose(per_gameweek, collapsed, rtol=0.02)


def test_rate_and_minutes_can_move_independently(season: Season):
    """Two players with the same prior total should not be interchangeable.

    One scoring at a high rate over few starts is a different proposition from
    one grinding out the same total every week, and the model has to be able to
    tell them apart.
    """
    from fpl_manager.projections import build_rates

    from .conftest import make_prior

    prior = make_prior(season)
    rates = build_rates(season, prior).join(prior[["prior_points", "prior_minutes"]])
    rates = rates[(rates["prior_minutes"] > 0) & (rates["prior_points"] > 0)]
    rates["per_gw"] = rates["points_per_90"] * rates["minutes_share"]

    # bucket by prior total, then check the projections inside a bucket differ
    buckets = rates.groupby(rates["prior_points"].round(-1))["per_gw"].agg(["size", "std"])
    populated = buckets[buckets["size"] >= 3]
    assert len(populated) > 0
    assert (populated["std"].fillna(0) > 0).any()
