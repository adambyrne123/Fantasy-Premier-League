"""Smoke test for the Streamlit front end.

Runs the real `app.py` through Streamlit's AppTest harness with the API swapped
for synthetic data. It will not catch a bad layout, but it does catch the thing
that actually breaks a Streamlit app in practice, which is an exception thrown
somewhere down a tab the developer did not click on before shipping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_manager import api
from fpl_manager import projections as proj_module

APP = Path(__file__).resolve().parent.parent / "app.py"

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from .conftest import FakeApi, make_prior  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    """The app wired to synthetic data, with no network and no disk cache."""
    fake = FakeApi(played=0)
    monkeypatch.setattr(api.FplApi, "bootstrap", lambda self: fake.bootstrap())
    monkeypatch.setattr(api.FplApi, "fixtures", lambda self: fake.fixtures())
    monkeypatch.setattr(api.FplApi, "_get", lambda self, *a, **kw: {})

    from fpl_manager.data import Season

    prior_path = tmp_path / "prior_season.parquet"
    make_prior(Season(fake)).to_parquet(prior_path)
    monkeypatch.setattr(proj_module, "PRIOR_CACHE", prior_path)

    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    return AppTest.from_file(str(APP), default_timeout=120)


def test_app_starts_without_exception(app):
    at = app.run()
    assert not at.exception


def test_every_tab_renders(app):
    at = app.run()
    assert not at.exception
    labels = [tab.label for tab in at.tabs] if at.tabs else []
    for expected in ["Squad", "Players", "Fixtures", "Transfers", "Chips"]:
        assert expected in labels


def test_squad_summary_is_shown(app):
    at = app.run()
    assert not at.exception
    assert any("Squad cost" in m.label for m in at.metric)


def test_changing_horizon_reruns_cleanly(app):
    at = app.run()
    at.sidebar.slider[0].set_value(10).run()
    assert not at.exception


def test_lowering_budget_is_handled(app):
    """An infeasible budget should surface as an error, not a traceback."""
    at = app.run()
    budget = at.sidebar.number_input[0]
    budget.set_value(80.0).run()
    assert not at.exception


def test_player_pool_chart_is_built(app):
    """The scatter is the whole point of the Players tab.

    A chart with a bad spec produces no element and throws nothing, so the app
    still looks healthy while the tab is empty. Assert the element exists.
    """
    at = app.run()
    assert not at.exception
    assert len(at.get("vega_lite_chart")) >= 1


def test_app_holds_no_model_logic():
    """The front end is a view. If it starts deciding things, split it out."""
    text = APP.read_text()
    for banned in ["LpProblem", "points_per_90 *", "def project("]:
        assert banned not in text, f"model logic leaked into app.py: {banned}"
