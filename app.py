"""Streamlit front end.

    uv run streamlit run app.py

This is a view over the library and holds no model logic. Anything that decides
something belongs in `projections.py` or `optimiser.py`, so that the CLI and
this app can never disagree about what the best squad is.
"""

from __future__ import annotations

import json
from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from fpl_manager.api import FplApi
from fpl_manager.chips import best_per_chip
from fpl_manager.chips import evaluate as evaluate_chips
from fpl_manager.data import MAX_PER_CLUB, Season
from fpl_manager.optimiser import (
    MAX_PLAN_WEEKS,
    POOL_SIZE,
    build_squad,
    pick_xi,
    plan_transfers,
    suggest_transfers,
)
from fpl_manager.prices import is_dormant, movers, price_pressure
from fpl_manager.projections import load_prior, project
from fpl_manager.roi import LAST_SEASON, MIN_MINUTES, NOTHING_YET, points_source, roi_frame
from fpl_manager.squad import (
    MySquad,
    load_from_entry,
    merge_prices,
    parse_squad,
    squad_payload,
)

st.set_page_config(page_title="FPL Manager", page_icon="⚽", layout="wide")

CACHE_TTL = 6 * 3600
SQUAD_COLS = ["name", "position", "club", "price", "xpts_next", "xpts_total", "ownership"]
POSITIONS_IN_ORDER = ("GKP", "DEF", "MID", "FWD")
POSITION_COLOURS = {"GKP": "#FFB020", "DEF": "#00C2FF", "MID": "#00E87B", "FWD": "#FF4D6D"}

PITCH_CSS = """
<style>
.pitch {
  background:
    repeating-linear-gradient(to bottom,
      rgba(255,255,255,.05) 0 7%, rgba(0,0,0,0) 7% 14%),
    linear-gradient(180deg, #10794a 0%, #0a5733 100%);
  border: 1px solid rgba(255,255,255,.16);
  border-radius: 14px;
  padding: 16px 10px 4px;
}
.pitch-line { display:flex; justify-content:center; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
.pitch-cap, .bench-cap {
  text-align:center; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase;
  color:rgba(255,255,255,.7); margin-bottom:10px;
}
.shirt {
  position:relative;
  width:108px; background:rgba(255,255,255,.95); border-radius:10px;
  padding:8px 6px; text-align:center; color:#2b0a3d;
  box-shadow:0 2px 6px rgba(0,0,0,.35);
}
.pitch.compact .shirt, .bench-strip.compact .shirt { width:80px; padding:6px 4px; }
.pitch.compact .shirt .nm { font-size:.7rem; }
.pitch.compact .shirt .meta { font-size:.58rem; }
.pitch.compact .shirt .pts { font-size:.92rem; }
.shirt.ring-out { box-shadow:0 0 0 2px #ff2d55, 0 2px 6px rgba(0,0,0,.35); opacity:.72; }
.shirt.ring-in { box-shadow:0 0 0 2px #00E87B, 0 2px 8px rgba(0,232,123,.45); }
.flag, .dot {
  display:inline-block; width:9px; height:9px; border-radius:50%;
  border:1px solid rgba(0,0,0,.35);
}
.flag { position:absolute; top:6px; right:6px; cursor:help; }
.dot { margin-right:3px; vertical-align:middle; }
.flag.out, .dot.out { background:#ff2d55; }
.flag.doubt, .dot.doubt { background:#ffb020; }
.flag.note, .dot.note { background:#79b8ff; }
.shirt .nm {
  font-weight:700; font-size:.8rem; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis;
}
.shirt .meta { font-size:.67rem; color:#5d4d69; margin-top:2px; }
.shirt .pts { font-size:1.1rem; font-weight:700; margin-top:4px; color:#0a5733; }
.badge {
  display:inline-block; font-size:.58rem; font-weight:800; border-radius:999px;
  padding:1px 5px; margin-left:4px; vertical-align:middle;
}
.badge.c { background:#37003C; color:#00E87B; }
.badge.v { background:#ded6e5; color:#37003C; }
.bench-strip {
  margin-top:10px; padding:12px 10px 0; border-radius:12px;
  background:rgba(255,255,255,.05); border:1px dashed rgba(255,255,255,.2);
}
.bench-strip .shirt { width:100px; background:rgba(255,255,255,.8); }
</style>
"""
st.markdown(PITCH_CSS, unsafe_allow_html=True)


def fdr_css(value: float) -> str:
    """Colour a fixture by FPL's own 1 to 5 difficulty rating.

    Thresholds rather than exact values, because a club with two fixtures in a
    gameweek gets the mean of the two and lands between the integers.
    """
    if pd.isna(value):
        return "background-color:#241c30;color:#6f6580;"
    if value <= 2.0:
        return "background-color:#00d060;color:#05240f;"
    if value <= 2.75:
        return "background-color:#84dd8f;color:#0d2a14;"
    if value <= 3.25:
        return "background-color:#d7d2dd;color:#2b2333;"
    if value <= 4.0:
        return "background-color:#ff5a5f;color:#2b0206;"
    return "background-color:#8b0f2b;color:#ffe9ee;"


# ----------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading season data")
def load_season(refresh_token: int) -> Season:
    """Season holds an HTTP session, so it is a resource rather than data.

    `refresh_token` exists only to give the cache something to invalidate on.

    The forced refresh lasts only as long as construction, which is where the
    two calls worth refreshing happen. Leaving ttl at 0 afterwards would make
    every later call through this season skip the disk cache, and since the
    season is a shared resource that would be every visitor's entry lookup, not
    just the one who pressed the button.
    """
    api = FplApi(ttl=0 if refresh_token else CACHE_TTL)
    season = Season(api)
    api.ttl = CACHE_TTL
    return season


@st.cache_data(show_spinner="Fetching last season's totals, this takes a few minutes")
def cached_prior(_season: Season) -> pd.DataFrame | None:
    return load_prior(_season)


@st.cache_data(show_spinner="Reading transfer activity")
def load_prices(_season: Season) -> pd.DataFrame:
    return price_pressure(_season)


@st.cache_data(show_spinner="Working out returns")
def load_roi(_season: Season, projections: pd.DataFrame) -> pd.DataFrame:
    return roi_frame(_season, projections)


@st.cache_data(show_spinner="Projecting")
def load_projections(
    _season: Season, horizon: int, use_prior: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-player totals and the per-gameweek frame chip timing needs."""
    prior = cached_prior(_season) if use_prior else None
    return project(_season, horizon=horizon, prior=prior)


# ----------------------------------------------------------------------
# display helpers
# ----------------------------------------------------------------------
def squad_table(df: pd.DataFrame) -> None:
    st.dataframe(
        df[SQUAD_COLS],
        hide_index=True,
        width="stretch",
        column_config={
            "name": "Player",
            "position": "Pos",
            "club": "Club",
            "price": st.column_config.NumberColumn("Price", format="%.1f"),
            "xpts_next": st.column_config.NumberColumn("xPts next", format="%.1f"),
            "xpts_total": st.column_config.ProgressColumn(
                "xPts horizon",
                format="%.1f",
                min_value=0.0,
                max_value=float(max(df["xpts_total"].max(), 1)),
            ),
            "ownership": st.column_config.NumberColumn("Owned %", format="%.1f"),
        },
    )


STATUS_WORDS = {
    "i": "Injured",
    "s": "Suspended",
    "u": "Unavailable",
    "n": "Not in the squad",
    "d": "Doubtful",
}


def availability(row: pd.Series) -> tuple[str, str]:
    """Severity and wording for a player's fitness flag.

    FPL publishes three separate signals and they do not always agree, so the
    hardest one wins. A player flagged injured is out whatever the percentage
    says, and a percentage below 100 is a doubt even when the status letter
    still reads available.
    """
    status = str(row.get("status") or "a")
    chance = row.get("chance_of_playing")
    news = str(row.get("news") or "").strip()

    if status in {"i", "s", "u", "n"}:
        return "out", news or STATUS_WORDS[status]
    if status == "d" or (pd.notna(chance) and chance < 100):
        wording = f"{int(chance)}% chance of playing" if pd.notna(chance) else STATUS_WORDS["d"]
        return "doubt", news or wording
    if news:
        return "note", news
    return "", ""


def _shirt(row: pd.Series, badge: str = "", highlight: str = "") -> str:
    mark = f'<span class="badge {badge.lower()}">{badge}</span>' if badge else ""
    severity, note = availability(row)
    flag = f'<span class="flag {severity}" title="{escape(note)}"></span>' if severity else ""
    ring = f" ring-{highlight}" if highlight else ""
    return (
        f'<div class="shirt{ring}">{flag}'
        f'<div class="nm">{escape(str(row["name"]))}{mark}</div>'
        f'<div class="meta">{escape(str(row["club"]))} · {row["price"]:.1f}m</div>'
        f'<div class="pts">{row["xpts_next"]:.1f}</div>'
        "</div>"
    )


def formation_view(
    xi: pd.DataFrame,
    bench: pd.DataFrame | None,
    captain_id,
    vice_id,
    *,
    compact: bool = False,
    highlight: dict | None = None,
) -> None:
    """Lay the XI out on a pitch, in formation, the way the FPL site does.

    Reading a lineup is a spatial job. A flat table makes you count defenders to
    work out the shape, which is the one thing the layout should tell you at a
    glance. `highlight` rings individual players, which is how the transfer
    view shows what is leaving and what is arriving.
    """
    highlight = highlight or {}
    lines = []
    for pos in POSITIONS_IN_ORDER:
        line = xi[xi["position"] == pos]
        if line.empty:
            continue
        shirts = "".join(
            _shirt(
                row,
                "C" if pid == captain_id else "V" if pid == vice_id else "",
                highlight.get(pid, ""),
            )
            for pid, row in line.iterrows()
        )
        lines.append(f'<div class="pitch-line">{shirts}</div>')

    shape = "-".join(str(int((xi["position"] == pos).sum())) for pos in POSITIONS_IN_ORDER[1:])
    size = " compact" if compact else ""
    markup = (
        f'<div class="pitch{size}"><div class="pitch-cap">Formation {shape}</div>'
        f"{''.join(lines)}</div>"
    )

    if bench is not None and not bench.empty:
        strip = "".join(_shirt(row, "", highlight.get(pid, "")) for pid, row in bench.iterrows())
        markup += (
            f'<div class="bench-strip{size}"><div class="bench-cap">Bench, in order</div>'
            f'<div class="pitch-line">{strip}</div></div>'
        )

    st.markdown(markup, unsafe_allow_html=True)


def flag_legend() -> None:
    st.caption(
        "Fitness flags: "
        '<span class="dot out"></span> out&nbsp;&nbsp; '
        '<span class="dot doubt"></span> doubtful&nbsp;&nbsp; '
        '<span class="dot note"></span> news. Hover a dot to read it.',
        unsafe_allow_html=True,
    )


def name_lookup(projections: pd.DataFrame) -> dict[str, int]:
    return {
        f"{row['name']} ({row['club']}, {row['price']:.1f})": int(pid)
        for pid, row in projections.iterrows()
    }


# ----------------------------------------------------------------------
# sidebar
# ----------------------------------------------------------------------
st.sidebar.title("FPL Manager")

if "refresh_token" not in st.session_state:
    st.session_state.refresh_token = 0
if st.sidebar.button("Refresh FPL data", width="stretch"):
    # Streamlit's caches are process wide and there is no per-session clear, so
    # this refetches for everyone using the app, not just whoever clicked. Clear
    # the three functions that hold FPL data rather than every cache there is.
    st.session_state.refresh_token += 1
    load_season.clear()
    load_projections.clear()
    load_prices.clear()
    load_roi.clear()
    cached_prior.clear()
st.sidebar.caption("Prices change daily. Refreshing refetches for everyone on the app.")

with st.sidebar.expander("Projection", expanded=True):
    horizon = st.slider("Gameweeks to project over", 1, 12, 6)
    use_prior = st.toggle("Use last season's data", value=True)

with st.sidebar.expander("Squad building", expanded=False):
    bench_weight = st.slider(
        "Bench weight",
        0.0,
        0.4,
        0.12,
        0.02,
        help="How much a bench player's projection counts. Low for two punts on the "
        "bench, high for a squad you can rotate.",
    )
    budget = st.number_input("Budget (m)", 80.0, 120.0, 100.0, 0.1)

season = load_season(st.session_state.refresh_token)
projections, by_gameweek = load_projections(season, horizon, use_prior)
lookup = name_lookup(projections)

st.sidebar.divider()
st.sidebar.caption(
    f"GW{season.next_gameweek} next · {season.gameweeks_played} played · {len(projections)} players"
)
if not use_prior and season.gameweeks_played == 0:
    st.sidebar.warning("No prior data and no gameweeks played. Projections are guesswork.")

my_squad = None
with st.sidebar.expander("Your squad", expanded=True):
    st.caption("Loaded once here, used by both Transfers and Chips.")
    source = st.radio("Load from", ["Nothing loaded", "squad.json", "FPL entry id"])

    if source == "squad.json":
        upload = st.file_uploader("squad.json", type="json")
        if upload:
            try:
                my_squad = parse_squad(json.loads(upload.getvalue()), season)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                st.error(f"Could not read that file: {exc}")
    elif source == "FPL entry id":
        entry_id = st.number_input("Entry id", min_value=0, step=1, value=0)
        st.caption("Only works once a deadline has passed.")
        paid = st.file_uploader(
            "Optional: squad.json, for what you paid",
            type="json",
            help="The entry publishes who you own and your bank, but never what you paid, "
            "which is what selling prices are worked out from.",
        )
        if entry_id:
            try:
                my_squad = load_from_entry(season, int(entry_id))
                if paid:
                    merge_prices(my_squad, parse_squad(json.loads(paid.getvalue())), season)
                else:
                    my_squad.resolve_selling_prices(season)
            except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                st.error(f"Could not load that entry: {exc}")
                my_squad = None

    if my_squad is not None:
        missing = my_squad.missing_from(projections)
        if missing:
            st.warning(f"{len(missing)} owned players are no longer in the game.")


# ----------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------
build_tab, players_tab, roi_tab, fixtures_tab, transfers_tab, chips_tab = st.tabs(
    ["Squad", "Players", "ROI", "Fixtures", "Transfers", "Chips"]
)

with build_tab:
    st.subheader("Build a squad")
    left, right = st.columns(2)
    locked = left.multiselect("Must include", options=sorted(lookup), key="locks")
    banned = right.multiselect("Rule out", options=sorted(lookup), key="bans")

    try:
        result = build_squad(
            projections,
            budget_tenths=round(budget * 10),
            bench_weight=bench_weight,
            include=[lookup[n] for n in locked],
            exclude=[lookup[n] for n in banned],
        )
    except RuntimeError as exc:
        st.error(f"{exc}. Try relaxing the locks or raising the budget.")
        st.stop()

    a, b, c = st.columns(3)
    a.metric("Squad cost", f"{result.cost:.1f}m", f"{budget - result.cost:+.1f}m in bank")
    b.metric(f"Projected over {horizon} GW", f"{result.projected:.0f} pts")
    c.metric("Captain", result.captain["name"])

    formation_view(result.xi, result.bench, result.captain.name, result.vice_captain.name)
    flag_legend()

    st.download_button(
        "Download as squad.json",
        data=json.dumps(squad_payload(MySquad.from_frame(result.squad), season), indent=2),
        file_name="squad.json",
        mime="application/json",
        help="Carries what each player costs today, which is what you paid if you buy "
        "them now. Upload it back to plan transfers with the right selling prices.",
    )

with players_tab:
    st.subheader("Player pool")
    f1, f2, f3, f4 = st.columns(4)
    pos = f1.selectbox("Position", ["All", "GKP", "DEF", "MID", "FWD"])
    max_price = f2.slider("Max price", 3.5, 16.0, 16.0, 0.1)
    max_own = f3.slider("Max ownership %", 0.0, 100.0, 100.0, 1.0)
    sort_by = f4.selectbox("Sort by", ["xpts_total", "xpts_next", "value", "ownership"])

    view = projections[
        (projections["price"] <= max_price)
        & (projections["ownership"] <= max_own)
        & (projections["minutes_share"] > 0)
    ]
    if pos != "All":
        view = view[view["position"] == pos]
    view = view.sort_values(sort_by, ascending=False)

    plot = view.head(200).reset_index()
    present = [p for p in POSITIONS_IN_ORDER if p in set(plot["position"])]
    dots = (
        alt.Chart(plot)
        .mark_circle(opacity=0.75)
        .encode(
            x=alt.X("price:Q", title="Price (m)", scale=alt.Scale(zero=False, nice=True)),
            y=alt.Y("xpts_total:Q", title=f"Projected points, next {horizon} GW"),
            color=alt.Color(
                "position:N",
                title="Position",
                scale=alt.Scale(domain=present, range=[POSITION_COLOURS[p] for p in present]),
            ),
            size=alt.Size("ownership:Q", title="Owned %", scale=alt.Scale(range=[25, 400])),
            tooltip=[
                alt.Tooltip("name:N", title="Player"),
                alt.Tooltip("club:N", title="Club"),
                alt.Tooltip("position:N", title="Pos"),
                alt.Tooltip("price:Q", title="Price", format=".1f"),
                alt.Tooltip("xpts_next:Q", title="xPts next", format=".1f"),
                alt.Tooltip("xpts_total:Q", title="xPts horizon", format=".1f"),
                alt.Tooltip("value:Q", title="Pts per m", format=".2f"),
                alt.Tooltip("ownership:Q", title="Owned %", format=".1f"),
            ],
        )
    )
    tags = (
        alt.Chart(plot.nlargest(10, "value"))
        .mark_text(align="left", dx=9, dy=-7, fontSize=11, color="#ECE8F2")
        .encode(x="price:Q", y="xpts_total:Q", text="name:N")
    )
    st.altair_chart((dots + tags).properties(height=430).interactive())

    st.divider()
    st.subheader("Price pressure")
    pressure = load_prices(season)
    if is_dormant(pressure):
        st.info(
            "No transfers have been made yet this gameweek, so there is nothing to read. "
            "This fills in once the season is under way."
        )
    else:
        st.caption(
            "Net transfers this gameweek as a share of the players who already own them, "
            "which is roughly what FPL's undisclosed thresholds scale with. It is a running "
            "total rather than a rate, and the counters reset at the daily price update, so "
            "read it as a shortlist to check rather than a forecast."
        )
        rising, falling = st.columns(2)
        for column, direction, heading in (
            (rising, "rise", "Closest to rising"),
            (falling, "fall", "Closest to falling"),
        ):
            with column:
                st.caption(heading)
                moving = movers(pressure, direction, top=10)
                if moving.empty:
                    st.caption("Nobody, on current numbers.")
                    continue
                st.dataframe(
                    moving[["name", "position", "club", "price", "net_transfers", "pressure"]],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "name": "Player",
                        "position": "Pos",
                        "club": "Club",
                        "price": st.column_config.NumberColumn("Price", format="%.1f"),
                        "net_transfers": st.column_config.NumberColumn(
                            "Net transfers", format="%d"
                        ),
                        "pressure": st.column_config.NumberColumn("Pressure", format="%.2f"),
                    },
                )
        st.caption(
            "Price moves are not in the projection. A rise is worth chasing only if you "
            "wanted the player anyway."
        )

    st.divider()
    ranked, best_value = st.columns([3, 2])
    with ranked:
        st.caption(f"Ranked by {sort_by.replace('_', ' ')}")
        squad_table(view.head(40))
    with best_value:
        st.caption("Most points per million")
        st.dataframe(
            view.nlargest(15, "value")[["name", "position", "club", "price", "value"]],
            hide_index=True,
            width="stretch",
            column_config={
                "name": "Player",
                "position": "Pos",
                "club": "Club",
                "price": st.column_config.NumberColumn("Price", format="%.1f"),
                "value": st.column_config.NumberColumn("Pts per m", format="%.2f"),
            },
        )

with roi_tab:
    st.subheader("Return on investment")
    points_from = points_source(season)
    roi = load_roi(season, projections)

    if points_from == NOTHING_YET:
        st.info(
            "No points have been scored yet this season, so every return is zero. "
            "This fills in from the first gameweek onwards."
        )
    else:
        whose = "last season's points" if points_from == LAST_SEASON else "this season's points"
        st.caption(
            f"Points divided by price, using {whose} against today's price. "
            "The Players tab ranks by *projected* points per million, which is the "
            "forward-looking version of the same idea. This one is what a player has "
            "already returned."
        )
        if points_from == LAST_SEASON:
            st.warning(
                "FPL has not reset its counters for the new season yet, so these are "
                "2025/26 totals. They will drop to zero at the first deadline and build "
                "up again from there."
            )

        r1, r2, r3 = st.columns(3)
        roi_pos = r1.selectbox("Position", ["All", "GKP", "DEF", "MID", "FWD"], key="roi_pos")
        roi_max_price = r2.slider("Max price", 3.5, 16.0, 16.0, 0.1, key="roi_price")
        min_minutes = r3.slider(
            "Minimum minutes",
            0,
            2000,
            MIN_MINUTES,
            30,
            help="A player can post a flattering rate off one substitute appearance. "
            "This is how much football he has to have played to be ranked.",
        )

        roi_view = roi[(roi["price"] <= roi_max_price) & (roi["minutes"] >= min_minutes)]
        if roi_pos != "All":
            roi_view = roi_view[roi_view["position"] == roi_pos]

        if roi_view.empty:
            st.info("Nobody clears those filters. Try lowering the minimum minutes.")
        else:
            top = roi_view.iloc[0]
            k1, k2, k3 = st.columns(3)
            k1.metric("Best return", top["name"], f"{top['roi']:.1f} pts per m", delta_color="off")
            k2.metric("Players ranked", len(roi_view))
            k3.metric("Median return", f"{roi_view['roi'].median():.1f} pts per m")

            chart = roi_view.head(200).reset_index()
            present = [p for p in POSITIONS_IN_ORDER if p in set(chart["position"])]
            scatter = (
                alt.Chart(chart)
                .mark_circle(opacity=0.75)
                .encode(
                    x=alt.X("price:Q", title="Price (m)", scale=alt.Scale(zero=False, nice=True)),
                    y=alt.Y("points:Q", title="Points"),
                    color=alt.Color(
                        "position:N",
                        title="Position",
                        scale=alt.Scale(
                            domain=present, range=[POSITION_COLOURS[p] for p in present]
                        ),
                    ),
                    size=alt.Size("roi:Q", title="Pts per m", scale=alt.Scale(range=[25, 400])),
                    tooltip=[
                        alt.Tooltip("name:N", title="Player"),
                        alt.Tooltip("club:N", title="Club"),
                        alt.Tooltip("price:Q", title="Price", format=".1f"),
                        alt.Tooltip("points:Q", title="Points", format="d"),
                        alt.Tooltip("roi:Q", title="Pts per m", format=".2f"),
                        alt.Tooltip("minutes:Q", title="Minutes", format="d"),
                    ],
                )
            )
            labels = (
                alt.Chart(chart.nlargest(10, "roi"))
                .mark_text(align="left", dx=9, dy=-7, fontSize=11, color="#ECE8F2")
                .encode(x="price:Q", y="points:Q", text="name:N")
            )
            st.altair_chart((scatter + labels).properties(height=430).interactive())

            roi_cols = ["name", "position", "club", "price", "points", "minutes", "roi"]
            config = {
                "name": "Player",
                "position": "Pos",
                "club": "Club",
                "price": st.column_config.NumberColumn("Price", format="%.1f"),
                "points": st.column_config.NumberColumn("Points", format="%d"),
                "minutes": st.column_config.NumberColumn("Minutes", format="%d"),
                "roi": st.column_config.ProgressColumn(
                    "Pts per m",
                    format="%.1f",
                    min_value=0.0,
                    max_value=float(max(roi_view["roi"].max(), 1)),
                ),
            }
            if "gap" in roi_view.columns:
                roi_cols = [*roi_cols, "projected_roi", "gap"]
                config["projected_roi"] = st.column_config.NumberColumn(
                    "Projected pts per m", format="%.2f"
                )
                config["gap"] = st.column_config.NumberColumn("Gap", format="%+.2f")

            best, movers_up = st.columns([3, 2])
            with best:
                st.caption("Best return per million")
                st.dataframe(
                    roi_view.head(40)[roi_cols],
                    hide_index=True,
                    width="stretch",
                    column_config=config,
                )
            with movers_up:
                if "gap" in roi_view.columns:
                    st.caption("Projected to return more than they have")
                    st.dataframe(
                        roi_view.nlargest(15, "gap")[["name", "position", "roi", "projected_roi"]],
                        hide_index=True,
                        width="stretch",
                        column_config={
                            "name": "Player",
                            "position": "Pos",
                            "roi": st.column_config.NumberColumn("Returned", format="%.1f"),
                            "projected_roi": st.column_config.NumberColumn(
                                "Projected", format="%.2f"
                            ),
                        },
                    )
                    st.caption(
                        "A large gap is someone the model likes more than his record does, "
                        "which is what an injury or a new signing looks like."
                    )

            st.caption(
                "Price is today's price, not what anyone paid. A player who has risen "
                "scores worse here than he did for whoever bought him early, which is "
                "right for deciding what to buy now and unfair as a verdict on the buy."
            )

with fixtures_tab:
    st.subheader(f"Fixture ticker, next {horizon} gameweeks")
    st.caption(
        "Each cell is the opponent and where the game is played, coloured by FPL's own "
        "difficulty rating. Green is kind, red is not. Clubs are sorted easiest run first. "
        "A club with two fixtures in a gameweek shows both, and a blank shows a dash."
    )

    difficulty, labels = season.fixture_grid(horizon)
    order = difficulty.mean(axis=1).sort_values().index
    grid = difficulty.loc[order]
    shown = labels.loc[order].replace("", "—")
    columns = [f"GW{int(c)}" for c in grid.columns]
    grid.columns, shown.columns = columns, columns

    colours = pd.DataFrame(
        [[fdr_css(v) for v in row] for row in grid.to_numpy()],
        index=grid.index,
        columns=columns,
    )
    st.dataframe(
        shown.style.apply(lambda _: colours, axis=None),
        width="stretch",
        height=min(760, 44 + 35 * len(shown)),
    )

    st.caption("Kindest runs over the horizon")
    ticker = season.fixture_ticker(horizon).reset_index(drop=True)
    st.dataframe(
        ticker.head(10),
        hide_index=True,
        width="stretch",
        column_config={
            "club": "Club",
            "fixtures": st.column_config.NumberColumn("Games", format="%d"),
            "avg_difficulty": st.column_config.NumberColumn("Avg difficulty", format="%.2f"),
        },
    )

with transfers_tab:
    st.subheader("Transfer planner")
    squad = my_squad

    if squad is None:
        st.info("Load your squad in the sidebar to see suggested moves.")
    else:
        unpriced = squad.unpriced()
        if unpriced:
            st.warning(
                f"No purchase price for {len(unpriced)} of {len(squad.player_ids)} players, "
                "so they are valued at today's price. FPL pays back what you paid plus half "
                "of any rise, so this overstates what you can raise by selling them and the "
                "plan below may not be affordable. Load a squad.json to fix it."
            )

        t1, t2, t3 = st.columns(3)
        bank = t1.number_input("Bank (m)", 0.0, 20.0, squad.bank_tenths / 10, 0.1)
        free = t2.number_input("Free transfers", 0, 5, squad.free_transfers)
        max_moves = t3.number_input("Max transfers to consider", 1, 5, 2)

        try:
            plan = suggest_transfers(
                projections,
                current_ids=squad.player_ids,
                selling_prices=squad.selling_prices,
                bank_tenths=round(bank * 10),
                free_transfers=int(free),
                max_transfers=int(max_moves),
                bench_weight=bench_weight,
            )
        except RuntimeError as exc:
            # an illegal squad has no legal plan to reach, and anyone can upload
            # a hand-edited file, so this must not be a traceback
            st.error(
                f"{exc}. Check the squad is fifteen players, with no more than "
                f"{MAX_PER_CLUB} from any one club."
            )
            st.stop()

        # squad.frame drops ids the projections no longer know about, which a
        # plain .loc would raise on once a player leaves the game mid-season
        current = squad.frame(projections)
        current_xi, current_bench, current_captain = pick_xi(current)
        gain = plan.xi["xpts_total"].sum() - current_xi["xpts_total"].sum() - 4 * plan.hits

        if plan.transfers_in.empty:
            st.success("No move clears the cost of making it. Roll the transfer.")
        else:
            summary, moves = st.columns([1, 2])
            summary.metric(
                f"Net gain over {horizon} gameweeks",
                f"{gain:+.1f} pts",
                f"{plan.hits * 4} point hit" if plan.hits else "No hit",
                delta_color="off",
            )
            with moves:
                st.caption("Moves")
                for (_, going), (_, coming) in zip(
                    plan.transfers_out.iterrows(), plan.transfers_in.iterrows(), strict=False
                ):
                    st.markdown(
                        f"**{escape(str(going['name']))}** ({going['club']}, {going['price']:.1f}m)"
                        f" → **{escape(str(coming['name']))}** "
                        f"({coming['club']}, {coming['price']:.1f}m), "
                        f"{coming['xpts_total'] - going['xpts_total']:+.1f} pts over the horizon"
                    )

        st.divider()
        if st.toggle(
            "Plan across several gameweeks",
            help="Solves the weeks as one problem instead of one week at a time, so it can "
            "roll a free transfer or take a small loss now to reach a player later. Takes a "
            "second or two.",
        ):
            p1, p2 = st.columns(2)
            plan_weeks = p1.slider("Gameweeks to plan", 2, min(MAX_PLAN_WEEKS, horizon), 3)
            per_week = p2.slider("Transfers per week at most", 1, 2, 1)

            try:
                with st.spinner("Planning"):
                    route = plan_transfers(
                        projections,
                        by_gameweek,
                        current_ids=squad.player_ids,
                        selling_prices=squad.selling_prices,
                        bank_tenths=round(bank * 10),
                        free_transfers=int(free),
                        weeks=int(plan_weeks),
                        max_transfers_per_week=int(per_week),
                        bench_weight=bench_weight,
                    )
            except (RuntimeError, ValueError) as exc:
                st.error(f"Could not plan those gameweeks: {exc}")
                st.stop()

            m1, m2, m3 = st.columns(3)
            m1.metric(f"Projected over {plan_weeks} GW", f"{route.projected:.0f} pts")
            m2.metric("Transfers", route.transfers)
            m3.metric("Hits", f"{route.hits * 4} pts", delta_color="off")

            for week in route.weeks:
                with st.container(border=True):
                    head, money = st.columns([3, 1])
                    hit = f" · {week.hits * 4} point hit" if week.hits else ""
                    head.markdown(
                        f"**GW{week.event}** · captain {escape(str(week.captain['name']))}{hit}"
                    )
                    money.caption(f"{week.bank:.1f}m in bank · {week.free_transfers} free")
                    if week.transfers_in.empty:
                        st.caption("No move. Roll the transfer.")
                        continue
                    for (_, going), (_, coming) in zip(
                        week.transfers_out.iterrows(), week.transfers_in.iterrows(), strict=False
                    ):
                        st.markdown(
                            f"**{escape(str(going['name']))}** ({going['club']}) → "
                            f"**{escape(str(coming['name']))}** ({coming['club']}, "
                            f"{coming['price']:.1f}m)"
                        )

            st.caption(
                f"Chosen from the best {POOL_SIZE} or so players by projection plus everyone "
                "you own, not the whole game, because every extra week multiplies the solve. "
                "Prices are held at today's, so the bank shown for the last week is a rougher "
                "number than the one shown for the first."
                + (
                    " Some of your selling prices are unknown, which makes that worse the "
                    "further ahead it plans."
                    if route.approximate_money
                    else ""
                )
            )

        st.divider()
        before, after = st.columns(2)
        with before:
            st.caption(f"Now · captain {current_captain['name']}")
            formation_view(
                current_xi,
                current_bench,
                current_captain.name,
                None,
                compact=True,
                highlight={i: "out" for i in plan.transfers_out.index},
            )
        with after:
            st.caption(f"After · captain {plan.captain['name']}")
            formation_view(
                plan.xi,
                plan.bench,
                plan.captain.name,
                plan.vice_captain.name,
                compact=True,
                highlight={i: "in" for i in plan.transfers_in.index},
            )
        flag_legend()

with chips_tab:
    st.subheader("Chip timing")
    st.caption(
        "Gain is what the chip adds on top of what your squad scores anyway, "
        "with the lineup and captain already picked optimally for that gameweek."
    )

    if my_squad is None:
        st.info("Load your squad in the sidebar to price your chips.")
    else:
        team_value = my_squad.value_tenths(season)
        table = evaluate_chips(projections, by_gameweek, my_squad.player_ids, team_value)

        if table.empty:
            st.warning("Not enough of the squad is known to price a chip.")
        else:
            best = best_per_chip(table)
            for col, (_, row) in zip(st.columns(len(best)), best.iterrows(), strict=False):
                col.metric(
                    row["chip"],
                    f"+{row['gain']:.1f} pts",
                    f"GW{int(row['event'])} · {row['detail']}",
                    delta_color="off",
                )

            st.caption(f"Free Hit is priced against your team value, {team_value / 10:.1f}m.")
            st.line_chart(
                table.pivot(index="event", columns="chip", values="gain"),
                height=320,
            )
            st.dataframe(
                table,
                hide_index=True,
                width="stretch",
                column_config={
                    "chip": "Chip",
                    "event": st.column_config.NumberColumn("GW", format="%d"),
                    "gain": st.column_config.NumberColumn("Gain", format="%.1f"),
                    "baseline": st.column_config.NumberColumn("Squad scores", format="%.1f"),
                    "detail": "What it buys you",
                },
            )
            st.caption(
                "Rotation, press conferences and minutes management are not in the API. "
                "A gap of a point or two between gameweeks is inside the noise."
            )
