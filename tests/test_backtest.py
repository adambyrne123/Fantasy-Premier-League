"""The backtest rewinds a season faithfully and scores what it projects.

The fake's live payload for a played gameweek is each player's season total
over the gameweeks played, so the sum of the payloads is the bootstrap by
construction. That is the same identity the real API has, and the network test
in `test_live_against_the_api.py` checks it there.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fpl_manager import projections
from fpl_manager.backtest import (
    as_of,
    calibration,
    evaluate,
    gameweek_stats,
    load_stats,
    position_bias,
    rank_correlation,
    summarise,
    sweep,
)
from fpl_manager.data import COUNTING_STATS, Season

from .conftest import FakeApi, make_prior


@pytest.fixture
def played() -> Season:
    return Season(FakeApi(played=4))


@pytest.fixture
def stats(played: Season) -> dict[int, pd.DataFrame]:
    return load_stats(played.api, played.gameweeks_played)


def test_gameweek_stats_carries_every_counter(played: Season):
    frame = gameweek_stats(played.api, 1)
    assert list(frame.columns) == list(COUNTING_STATS)
    assert frame.index.name == "id"
    assert len(frame) == len(played.players)
    assert frame.notna().all().all()


def test_the_rewind_to_nought_is_pre_season(played: Season, stats):
    rewound = as_of(played, 0, stats)
    assert rewound.gameweeks_played == 0
    assert rewound.next_gameweek == 1
    for column in COUNTING_STATS:
        assert (rewound.players[column] == 0).all(), column
    assert rewound.players["available"].all()
    assert rewound.players["chance_of_playing_next_round"].isna().all()


def test_the_rewind_sums_the_live_payloads(played: Season, stats):
    rewound = as_of(played, 2, stats)
    expected = stats[1] + stats[2]
    for column in COUNTING_STATS:
        pd.testing.assert_series_equal(
            rewound.players[column],
            expected[column].reindex(rewound.players.index),
            check_names=False,
        )
    assert rewound.gameweeks_played == 2
    assert rewound.next_gameweek == 3
    assert (rewound.team_fixtures(1)["event"] == 3).all()


def test_the_rewind_to_now_is_the_bootstrap(played: Season, stats):
    """The identity the whole approach rests on: what the live payloads add
    up to is what the bootstrap says. The fake honours it by construction and
    the network test checks the real API does."""
    rewound = as_of(played, played.gameweeks_played, stats)
    for column in COUNTING_STATS:
        actual = pd.to_numeric(played.players[column], errors="coerce").fillna(0.0)
        assert np.allclose(rewound.players[column], actual, atol=1e-6), column


def test_the_rewind_leaves_the_season_alone(played: Season, stats):
    before = played.players.copy()
    events = played.events.copy()
    as_of(played, 1, stats)
    pd.testing.assert_frame_equal(played.players, before)
    pd.testing.assert_frame_equal(played.events, events)


def test_a_missing_gameweek_is_an_error_not_a_zero(played: Season, stats):
    with pytest.raises(KeyError):
        as_of(played, 3, {1: stats[1]})


def test_evaluate_scores_every_played_gameweek(played: Season, stats):
    scored = evaluate(played, make_prior(played), stats=stats)
    assert sorted(scored["event"].unique()) == [1, 2, 3, 4]
    assert scored["points"].notna().all()
    assert scored["xpts"].notna().all()
    # form has nothing to say before a gameweek has been played
    assert scored.loc[scored["event"] == 1, "form_baseline"].isna().all()
    assert scored.loc[scored["event"] == 2, "form_baseline"].notna().all()


def test_evaluate_can_stop_early(played: Season, stats):
    scored = evaluate(played, None, through=2, stats=stats)
    assert sorted(scored["event"].unique()) == [1, 2]


def test_evaluate_is_empty_before_the_season(season: Season):
    if season.gameweeks_played:
        pytest.skip("the pre-season half of the fixture")
    assert evaluate(season, None).empty


def test_summarise_has_a_row_per_gameweek_and_a_pooled_one(played: Season, stats):
    scored = evaluate(played, make_prior(played), stats=stats)
    per_gameweek, pooled = summarise(scored)
    assert list(per_gameweek.index) == [1, 2, 3, 4]
    for column in ("bias", "mae", "rmse", "rank", "rank_played", "top50", "minutes_corr"):
        assert np.isfinite(per_gameweek[column]).all(), column
        assert np.isfinite(pooled[column]), column
    assert np.isfinite(pooled["prior_rank"])
    assert np.isfinite(pooled["form_rank"])
    assert pooled["n"] == len(scored)


def test_rank_correlation_is_spearman():
    a = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert rank_correlation(a, a * 10) == pytest.approx(1.0)
    assert rank_correlation(a, -a) == pytest.approx(-1.0)
    assert np.isnan(rank_correlation(a.head(1), a.head(1)))
    # pairs with a hole are dropped rather than poisoning the rest
    holed = pd.Series([1.0, np.nan, 3.0, 4.0])
    assert rank_correlation(holed, a) == pytest.approx(1.0)


def test_calibration_walks_up_the_projection(played: Season, stats):
    table = calibration(evaluate(played, make_prior(played), stats=stats))
    assert len(table) == 10
    assert table["xpts"].is_monotonic_increasing
    assert table["n"].sum() > 0


def test_position_bias_reads_only_those_who_played(played: Season, stats):
    scored = evaluate(played, make_prior(played), stats=stats)
    table = position_bias(scored)
    assert table["n"].sum() == scored["played"].sum()
    assert set(table.index) <= {"GKP", "DEF", "MID", "FWD"}


def test_sweep_restores_the_constant(played: Season, stats):
    original = projections.SHRINKAGE_GAMES
    table = sweep(played, make_prior(played), "SHRINKAGE_GAMES", [2, 6], stats=stats)
    assert original == projections.SHRINKAGE_GAMES
    assert list(table.index) == [2.0, 6.0]
    assert table.index.name == "SHRINKAGE_GAMES"
    assert np.isfinite(table["rank"]).all()
    # the values differ, so the sweep is sweeping something
    assert not np.allclose(table.loc[2.0, "minutes_corr"], table.loc[6.0, "minutes_corr"]) or (
        not np.allclose(table.loc[2.0, "rank"], table.loc[6.0, "rank"])
    )


def test_sweep_restores_the_constant_when_a_run_raises(played: Season, stats, monkeypatch):
    original = projections.SHRINKAGE_GAMES

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("fpl_manager.backtest.evaluate", explode)
    with pytest.raises(RuntimeError):
        sweep(played, None, "SHRINKAGE_GAMES", [3], stats=stats)
    assert original == projections.SHRINKAGE_GAMES


def test_sweep_refuses_a_name_that_is_not_a_constant(played: Season, stats):
    with pytest.raises(ValueError):
        sweep(played, None, "NOT_A_THING", [1], stats=stats)
    with pytest.raises(ValueError):
        sweep(played, None, "project", [1], stats=stats)


def test_a_sweep_of_the_strength_weight_moves_the_answer(played: Season, stats):
    """The default argument trap. `fixture_multiplier` used to bind
    `STRENGTH_WEIGHT` in its signature, so reassigning the constant changed
    nothing and a sweep of it reported the same number every time."""
    table = sweep(played, make_prior(played), "STRENGTH_WEIGHT", [0.0, 1.0], stats=stats)
    assert not np.allclose(table.loc[0.0, "mae"], table.loc[1.0, "mae"])


def test_a_player_missing_from_one_payload_keeps_the_rest(played: Season, stats):
    """A player who joined in September is absent from the August payloads,
    not present at zero. Summing Series aligns him to NaN and a zero-fill then
    wipes the gameweeks he did play, which the real API showed on two players
    the first time the rewind was checked against the bootstrap."""
    pid = int(played.players.index[0])
    trimmed = {gw: frame.drop(index=pid) if gw == 1 else frame for gw, frame in stats.items()}
    rewound = as_of(played, 3, trimmed)
    expected = stats[2].loc[pid, "minutes"] + stats[3].loc[pid, "minutes"]
    assert rewound.players.loc[pid, "minutes"] == pytest.approx(expected)
    # and a player in no payload at all is nought rather than NaN
    gone = {gw: frame.drop(index=pid) for gw, frame in stats.items()}
    assert as_of(played, 3, gone).players.loc[pid, "minutes"] == 0.0
