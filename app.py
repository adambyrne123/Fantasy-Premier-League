"""Streamlit front end.

    uv run streamlit run app.py

This is a view over the library and holds no model logic. Anything that decides
something belongs in `projections.py` or `optimiser.py`, so that the CLI and
this app can never disagree about what the best squad is.
"""

from __future__ import annotations

import json
from html import escape
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from fpl_manager.api import FplApi
from fpl_manager.chips import best_per_chip
from fpl_manager.chips import evaluate as evaluate_chips
from fpl_manager.data import MAX_PER_CLUB, Season
from fpl_manager.leagues import leagues_of, load_manager, past_seasons, standings
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
POSITIONS_IN_ORDER = ("GKP", "DEF", "MID", "FWD")

# One table answering several questions, rather than one wide table answering
# none of them well. Model terms has no equivalent on the stats sites and is
# the point of ours: the three separable terms laid out so a ranking that looks
# wrong can be argued with instead of taken on faith.
STAT_VIEWS = {
    "Projection": ["price", "xpts_next", "xpts_total", "value", "ownership"],
    "Model terms": ["price", "points_per_90", "minutes_share", "start_rate", "prior_p90"],
    "Form and attack": [
        "price",
        "form",
        "points_per_game",
        "total_points",
        "expected_goals",
        "expected_assists",
    ],
    "Availability": ["price", "fitness", "chance_of_playing", "minutes_share", "ownership"],
}
# read straight off the API rather than through the model, so they are the
# check on it rather than a restatement of it
EXTRA_STATS = ["form", "points_per_game", "total_points", "expected_goals", "expected_assists"]
# FPL sets its deadlines in UK time and shows them that way, so converting to
# the server's timezone would disagree with the site people are playing on.
UK = ZoneInfo("Europe/London")
POSITION_COLOURS = {"GKP": "#FFB020", "DEF": "#00C2FF", "MID": "#00E87B", "FWD": "#FF4D6D"}

PITCH_CSS = """
<style>
:root {
  --line: rgba(255,255,255,.16);
  --line-soft: rgba(255,255,255,.08);
  --muted: rgba(255,255,255,.62);
  --radius: 12px;
  --card: #1F1830;
  --shadow: 0 1px 2px rgba(0,0,0,.30), 0 4px 14px rgba(0,0,0,.22);
}
.statusbar {
  display:flex; flex-wrap:wrap; gap:0;
  border:1px solid var(--line-soft); border-radius:var(--radius);
  background:var(--card); box-shadow:var(--shadow);
  margin-bottom:18px; overflow:hidden;
}
.statusbar .cell {
  flex:1 1 auto; min-width:132px; padding:9px 14px;
  border-right:1px solid var(--line-soft);
}
.statusbar .cell:last-child { border-right:0; }
.statusbar .k {
  display:block; font-size:.62rem; letter-spacing:.11em;
  text-transform:uppercase; color:var(--muted);
}
.statusbar .v { font-size:.94rem; font-weight:700; }
.statusbar .v.soon { color:#FFB020; }
.statusbar .v.stale { color:#ff2d55; }
/* on a phone the cells wrap, and left to grow freely the two long ones each
   take a row of their own and push the tabs off the screen */
@media (max-width: 640px) {
  .statusbar .cell { flex-basis:44%; min-width:0; padding:8px 10px; }
  .statusbar .v { font-size:.8rem; }
}
.pitch {
  background:
    repeating-linear-gradient(to bottom,
      rgba(255,255,255,.05) 0 7%, rgba(0,0,0,0) 7% 14%),
    linear-gradient(180deg, #10794a 0%, #0a5733 100%);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 10px 4px;
}
.pitch-line { display:flex; justify-content:center; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
.pitch-cap, .bench-cap {
  text-align:center; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase;
  color:var(--muted); margin-bottom:10px;
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
  margin-top:10px; padding:12px 10px 0; border-radius:var(--radius);
  background:rgba(255,255,255,.05); border:1px dashed var(--line);
}
.bench-strip .shirt { width:100px; background:rgba(255,255,255,.8); }
</style>
"""
st.markdown(PITCH_CSS, unsafe_allow_html=True)


def fdr_css(value: float) -> str:
    """Colour a fixture by FPL's own 1 to 5 difficulty rating.

    Thresholds rather than exact values, because a club with two fixtures in a
    gameweek gets the mean of the two and lands between the integers.

    The middle band is deliberately the dimmest thing on the grid. A bright
    neutral on a dark page draws the eye hardest towards the fixtures that
    should influence a decision least, which is backwards.
    """
    if pd.isna(value):
        return "background-color:#241c30;color:#6f6580;"
    if value <= 2.0:
        return "background-color:#00d060;color:#05240f;"
    if value <= 2.75:
        return "background-color:#5faa6d;color:#07200d;"
    if value <= 3.25:
        return "background-color:#3b3450;color:#b3aac4;"
    if value <= 4.0:
        return "background-color:#e0455f;color:#2b0206;"
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


@st.cache_data(show_spinner=False)
def fixture_runs(_season: Season, horizon: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Opponent labels and difficulty per club per gameweek, keyed by club id.

    Home is upper case and away is lower case, which is how fixture tickers
    encode it and saves the table a legend. Keyed by club id rather than name
    because that is what the projections carry, so it reindexes onto players
    without a lookup.

    Doubles and blanks need nothing special here. `team_fixtures` emits a row
    per fixture, so a double joins both opponents into the cell and averages
    the difficulty that colours it, and a blank produces no row at all.
    """
    tf = _season.team_fixtures(horizon).copy()
    tf["label"] = tf["opponent_short"].where(tf["is_home"], tf["opponent_short"].str.lower())

    difficulty = tf.pivot_table(index="team", columns="event", values="difficulty", aggfunc="mean")
    labels = tf.groupby(["team", "event"])["label"].apply(" ".join).unstack()

    clubs, events = _season.teams.index, difficulty.columns
    return (
        labels.reindex(index=clubs, columns=events).fillna(""),
        difficulty.reindex(index=clubs, columns=events),
    )


# ----------------------------------------------------------------------
# display helpers
# ----------------------------------------------------------------------
def _relative(delta: pd.Timedelta) -> str:
    """A rough "in 3 days" or "20 minutes ago", depending on the sign."""
    ahead = delta.total_seconds() > 0
    seconds = int(abs(delta.total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            phrase = f"{count} {unit}{'' if count == 1 else 's'}"
            return f"in {phrase}" if ahead else f"{phrase} ago"
    return "in under a minute" if ahead else "just now"


def _cell(key: str, value: str, tone: str = "") -> str:
    return (
        f'<div class="cell"><span class="k">{escape(key)}</span>'
        f'<span class="v {tone}">{escape(value)}</span></div>'
    )


def status_bar(season: Season, players: int) -> None:
    """The facts that say whether the rest of the page is worth acting on.

    The deadline and the age of the data are the two the app never showed, and
    they are the two that decide whether a projection is still current. Both
    are read off shared state rather than the session, since the disk cache is
    shared by everyone using a deployed app.
    """
    now = pd.Timestamp.now(tz="UTC")
    played = season.gameweeks_played
    cells = [
        _cell(
            "Gameweek",
            f"GW{season.next_gameweek} next"
            + (f" · {played} played" if played else " · pre-season"),
        )
    ]

    deadline = season.next_deadline
    if deadline is None:
        cells.append(_cell("Deadline", "None left this season"))
    elif deadline <= now:
        cells.append(_cell("Deadline", "Passed, gameweek under way"))
    else:
        gap = deadline - now
        tone = "soon" if gap < pd.Timedelta(hours=24) else ""
        cells.append(
            _cell("Deadline", f"{deadline.tz_convert(UK):%a %d %b, %H:%M} · {_relative(gap)}", tone)
        )

    cells.append(_cell("Players projected", str(players)))

    fetched = season.api.fetched_at("bootstrap")
    if fetched is None:
        cells.append(_cell("FPL data", "Not cached"))
    else:
        age = pd.Timestamp(fetched) - now
        tone = "stale" if -age > pd.Timedelta(seconds=CACHE_TTL) else ""
        cells.append(_cell("FPL data", f"Fetched {_relative(age)}", tone))

    st.markdown(f'<div class="statusbar">{"".join(cells)}</div>', unsafe_allow_html=True)


def pool_column_config(horizon: int, gw_cols: list[str], max_xpts: float) -> dict:
    """Labels and formats for every column any stat view can put on screen.

    One dict covering all of them rather than one per view, so a column reads
    the same whichever view you found it in. The player name is pinned because
    the fixture run makes the table wider than a phone.
    """
    config = {
        "name": st.column_config.TextColumn("Player", pinned=True),
        "position": st.column_config.TextColumn("Pos", width="small"),
        "club": st.column_config.TextColumn("Club", width="small"),
        "price": st.column_config.NumberColumn("Price", format="%.1f", width="small"),
        "xpts_next": st.column_config.NumberColumn("xPts next", format="%.1f"),
        "xpts_total": st.column_config.ProgressColumn(
            f"xPts {horizon} GW", format="%.1f", min_value=0.0, max_value=max_xpts
        ),
        "value": st.column_config.NumberColumn("Pts per m", format="%.2f"),
        "ownership": st.column_config.NumberColumn("Owned %", format="%.1f"),
        "points_per_90": st.column_config.NumberColumn("Pts per 90", format="%.2f"),
        "minutes_share": st.column_config.NumberColumn("Mins share", format="%.2f"),
        "start_rate": st.column_config.NumberColumn("Start rate", format="%.2f"),
        "prior_p90": st.column_config.NumberColumn("Last season p90", format="%.2f"),
        "form": st.column_config.NumberColumn("Form", format="%.1f"),
        "points_per_game": st.column_config.NumberColumn("PPG", format="%.1f"),
        "total_points": st.column_config.NumberColumn("Total", format="%d"),
        "expected_goals": st.column_config.NumberColumn("xG", format="%.2f"),
        "expected_assists": st.column_config.NumberColumn("xA", format="%.2f"),
        "chance_of_playing": st.column_config.NumberColumn("Chance %", format="%.0f"),
        "fitness": st.column_config.TextColumn("Fitness", width="medium"),
    }
    for col in gw_cols:
        config[col] = st.column_config.TextColumn(col, width="small")
    return config


def pool_table(
    view: pd.DataFrame,
    labels: pd.DataFrame,
    difficulty: pd.DataFrame,
    columns: list[str],
    horizon: int,
    key: str,
) -> int | None:
    """The pool, with each player's fixture run beside his numbers.

    Reading a projection without the run that produced it means holding two
    tabs in your head at once. Returns the id of the selected player, or None.
    """
    gw_cols = [f"GW{int(c)}" for c in labels.columns]
    table = view[["name", "position", "club", *columns]]

    if gw_cols:
        runs = labels.reindex(view["team"]).set_axis(view.index)
        runs.columns = gw_cols
        table = pd.concat([table, runs.replace("", "—")], axis=1)

    colours = pd.DataFrame("", index=table.index, columns=table.columns)
    if gw_cols:
        fdr = difficulty.reindex(view["team"]).to_numpy()
        colours[gw_cols] = pd.DataFrame(
            [[fdr_css(v) for v in row] for row in fdr], index=table.index, columns=gw_cols
        )

    selection = st.dataframe(
        table.style.apply(lambda _: colours, axis=None),
        hide_index=True,
        width="stretch",
        column_config=pool_column_config(horizon, gw_cols, float(max(view["xpts_total"].max(), 1))),
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )
    rows = selection["selection"]["rows"]
    return int(table.index[rows[0]]) if rows else None


@st.dialog("Player detail", width="large")
def player_detail(row: pd.Series, weeks: pd.DataFrame, horizon: int) -> None:
    """Why this player is ranked where he is.

    The projection is three terms multiplied together and the total says
    nothing about which of them is driving it. A cheap player with a high
    scoring rate and a thin minutes share is a completely different
    proposition from one the other way round, and a column of totals cannot
    tell you which you are looking at.
    """
    severity, note = availability(row)
    st.markdown(f"#### {escape(str(row['name']))}")
    st.caption(
        f"{row['club']} · {row['position']} · {row['price']:.1f}m · {row['ownership']:.1f}% owned"
    )
    if severity:
        {"out": st.error, "doubt": st.warning, "note": st.info}[severity](note)

    a, b, c = st.columns(3)
    a.metric("Points per 90", f"{row['points_per_90']:.2f}")
    b.metric("Expected minutes", f"{row['minutes_share']:.0%} of 90")
    c.metric(f"Projected, {horizon} GW", f"{row['xpts_total']:.1f} pts")

    current = row["current_p90"]
    st.caption(
        "Scoring rate times expected minutes times a fixture multiplier, summed over the "
        f"run below. The rate blends last season at {row['prior_p90']:.2f} per 90 with this "
        + (f"season at {current:.2f}" if pd.notna(current) else "season, which has no sample yet")
        + ", weighted by how much of the season has been played."
    )

    if weeks.empty:
        st.info("No fixtures for this club inside the horizon.")
        return

    st.altair_chart(
        alt.Chart(weeks)
        .mark_bar(color=POSITION_COLOURS.get(row["position"], "#00E87B"), cornerRadiusEnd=3)
        .encode(
            x=alt.X("label:N", title=None, sort=list(weeks["label"])),
            y=alt.Y("xpts:Q", title="Projected points"),
            tooltip=[
                alt.Tooltip("label:N", title="Gameweek"),
                alt.Tooltip("fixture:N", title="Fixture"),
                alt.Tooltip("xpts:Q", title="xPts", format=".2f"),
            ],
        )
        .properties(height=210)
    )
    st.caption("Upper case is at home, lower case away. A double gameweek shows both.")


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


def player_weeks(by_gameweek: pd.DataFrame, player_id: int) -> pd.DataFrame:
    """One row per gameweek for a player, with doubles summed into their week."""
    gw = by_gameweek[by_gameweek["id"] == player_id]
    if gw.empty:
        return gw
    gw = gw.assign(
        fixture=gw["opponent_short"].where(gw["is_home"], gw["opponent_short"].str.lower())
    )
    weeks = gw.groupby("event", as_index=False).agg(
        xpts=("xpts", "sum"), fixture=("fixture", " ".join)
    )
    weeks["label"] = "GW" + weeks["event"].astype(str)
    return weeks


def zoomable(chart: alt.Chart, key: str) -> alt.Chart:
    """Hand the chart the mouse wheel only once it has been asked for.

    An Altair chart made `.interactive()` claims the wheel for zooming, so
    scrolling the page past a tall scatter zooms the chart instead of moving
    the page, and you arrive somewhere further down having quietly rescaled it.

    Vega can gate zooming behind a click, but only by arming it from a point
    selection, which fires on marks rather than on the plot area and clears
    itself when you click the background. A control you can see is both plainer
    and harder to trigger by accident.
    """
    zoom = st.toggle(
        "Zoom and pan",
        key=key,
        help="Off by default so that scrolling the page cannot rescale the chart. "
        "Turn it on to zoom with the wheel and drag to pan.",
    )
    return chart.interactive() if zoom else chart


def position_scale(frame: pd.DataFrame) -> alt.Scale:
    """Position colours, restricted to the positions actually on the chart.

    Handing Altair the full domain when only forwards are plotted leaves three
    dead entries in the legend.
    """
    present = [p for p in POSITIONS_IN_ORDER if p in set(frame["position"])]
    return alt.Scale(domain=present, range=[POSITION_COLOURS[p] for p in present])


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
# the gameweek, player count and data age used to live here as a caption, and
# now sit in the status bar at the top of the page instead
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
        entry_id = st.number_input("Entry id", min_value=0, step=1, value=0, key="sidebar_entry")
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
status_bar(season, len(projections))

build_tab, players_tab, roi_tab, fixtures_tab, transfers_tab, chips_tab, leagues_tab = st.tabs(
    ["Squad", "Players", "ROI", "Fixtures", "Transfers", "Chips", "Leagues"]
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

    # the raw API stats are joined on rather than modelled, so the Form and
    # attack view is a check on the projection instead of a restatement of it
    pool = projections.join(season.players.reindex(columns=EXTRA_STATS))

    s1, s2 = st.columns([3, 1])
    search = s1.text_input("Search", placeholder="Player or club name")
    include_unavailable = s2.toggle(
        "Include unavailable",
        help="Injured, suspended and out of the squad are projected at zero minutes, so "
        "they are hidden by default. Turn this on to see them anyway.",
    )

    f1, f2, f3, f4, f5 = st.columns(5)
    pos = f1.selectbox("Position", ["All", "GKP", "DEF", "MID", "FWD"])
    club = f2.selectbox("Club", ["All", *sorted(season.teams["short_name"])])
    max_price = f3.slider("Max price", 3.5, 16.0, 16.0, 0.1)
    max_own = f4.slider("Max ownership %", 0.0, 100.0, 100.0, 1.0)
    sort_by = f5.selectbox(
        "Sort by", ["xpts_total", "xpts_next", "value", "ownership", "form", "points_per_game"]
    )

    view = pool[(pool["price"] <= max_price) & (pool["ownership"] <= max_own)]
    if not include_unavailable:
        view = view[view["minutes_share"] > 0]
    if pos != "All":
        view = view[view["position"] == pos]
    if club != "All":
        view = view[view["club"] == club]
    if search:
        hay = view["name"].str.lower() + " " + view["club"].str.lower()
        view = view[hay.str.contains(search.strip().lower(), regex=False)]
    view = view.sort_values(sort_by, ascending=False)

    # an empty filter result is an ordinary thing to do, not an error, so it
    # must not reach st.stop() and take the other four tabs down with it
    if view.empty:
        st.info("Nothing matches those filters. Widen them to see players again.")
    else:
        stat_view = st.segmented_control(
            "Stat view", list(STAT_VIEWS), default="Projection", label_visibility="collapsed"
        )
        columns = STAT_VIEWS[stat_view or "Projection"]

        show_all = st.toggle("Show more rows", help="Up to 250, rather than the top 40.")
        shown = view.head(250 if show_all else 40)
        if "fitness" in columns:
            shown = shown.assign(
                fitness=[availability(row)[1] or "No news" for _, row in shown.iterrows()]
            )

        st.caption(
            f"Showing {len(shown)} of {len(view)} players, ranked by "
            f"{sort_by.replace('_', ' ')}. Click a row for the working behind the projection."
        )
        labels, difficulty = fixture_runs(season, horizon)
        chosen = pool_table(shown, labels, difficulty, columns, horizon, key="pool")

        # only open on a change, or closing the dialog would reopen it at once
        if chosen is not None and st.session_state.get("inspected") != chosen:
            st.session_state.inspected = chosen
            player_detail(view.loc[chosen], player_weeks(by_gameweek, chosen), horizon)
        elif chosen is None:
            st.session_state.inspected = None

        st.divider()
        scatter, leaders = st.columns([3, 2])
        plot = view.head(200).reset_index()
        with scatter:
            st.caption("Points against price. Bigger dots are more owned.")
            dots = (
                alt.Chart(plot)
                .mark_circle(opacity=0.75)
                .encode(
                    x=alt.X("price:Q", title="Price (m)", scale=alt.Scale(zero=False, nice=True)),
                    y=alt.Y("xpts_total:Q", title=f"Projected points, next {horizon} GW"),
                    color=alt.Color("position:N", title="Position", scale=position_scale(plot)),
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
            st.altair_chart(zoomable((dots + tags).properties(height=430), "pool_zoom"))

        with leaders:
            st.caption("Most projected points per million")
            best = view.nlargest(14, "value").reset_index()
            st.altair_chart(
                alt.Chart(best)
                .mark_bar(cornerRadiusEnd=3)
                .encode(
                    y=alt.Y("name:N", title=None, sort="-x"),
                    x=alt.X("value:Q", title="Points per million"),
                    color=alt.Color("position:N", title="Position", scale=position_scale(best)),
                    tooltip=[
                        alt.Tooltip("name:N", title="Player"),
                        alt.Tooltip("club:N", title="Club"),
                        alt.Tooltip("price:Q", title="Price", format=".1f"),
                        alt.Tooltip("value:Q", title="Pts per m", format=".2f"),
                    ],
                )
                .properties(height=430)
            )

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
            st.altair_chart(zoomable((scatter + labels).properties(height=430), "roi_zoom"))

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


with leagues_tab:
    st.subheader("Managers and mini leagues")
    st.caption(
        "Public data for any manager id, which is the number in the URL of their points page "
        "on the FPL site. Ranks and league tables stay empty until the first gameweek has "
        "been scored, which is how the API behaves rather than a fault here."
    )

    entry = st.number_input(
        "Manager entry id",
        min_value=0,
        step=1,
        value=int(st.session_state.get("sidebar_entry") or 0),
        key="league_entry",
    )

    manager, history, joined = None, None, None
    if entry:
        # a mistyped id is an ordinary thing to do, so it reports itself rather
        # than reaching st.stop() and taking the rest of the page with it
        try:
            manager = load_manager(season, int(entry))
            history = past_seasons(season, int(entry))
            joined = leagues_of(season, int(entry))
        except RuntimeError as exc:
            st.error(str(exc))

    if manager is None:
        st.info("Enter a manager id to see their record and the leagues they are in.")
    else:
        st.markdown(f"#### {escape(manager.name)} · {escape(manager.team_name)}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Overall points", f"{manager.overall_points:,}")
        m2.metric(
            "Overall rank",
            f"{manager.overall_rank:,}" if manager.overall_rank else "Not ranked yet",
        )
        m3.metric("Seasons played", manager.seasons_played)

        st.divider()
        st.caption("Previous seasons")
        if history.empty:
            st.info("No previous seasons. This is their first.")
        else:
            st.altair_chart(
                alt.Chart(history)
                .mark_bar(color="#00E87B", cornerRadiusEnd=3)
                .encode(
                    x=alt.X("season_name:N", title=None, sort=list(history["season_name"])),
                    y=alt.Y("total_points:Q", title="Points"),
                    tooltip=[
                        alt.Tooltip("season_name:N", title="Season"),
                        alt.Tooltip("total_points:Q", title="Points", format="d"),
                        alt.Tooltip("rank:Q", title="Final rank", format=","),
                    ],
                )
                .properties(height=220)
            )
            st.dataframe(
                history,
                hide_index=True,
                width="stretch",
                column_config={
                    "season_name": "Season",
                    "total_points": st.column_config.NumberColumn("Points", format="%d"),
                    "rank": st.column_config.NumberColumn("Final rank", format="%d"),
                },
            )

        st.divider()
        st.caption("Their classic leagues, the ones they joined listed first")
        if joined.empty:
            st.info("No classic leagues on this entry.")
        else:
            names = {
                f"{row['name']}{' (automatic)' if row['system'] else ''}": int(row["id"])
                for _, row in joined.iterrows()
            }
            picked = st.selectbox("League table", options=list(names), key="league_pick")
            table, info = standings(season, names[picked])

            if table.empty:
                st.info(
                    f"{info['name']} has no table yet. Classic leagues fill in once the first "
                    "gameweek has been scored."
                )
            else:
                st.caption(f"{info['name']}, page {info['page']}")
                st.dataframe(
                    table,
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "rank": st.column_config.NumberColumn("Rank", format="%d"),
                        "movement": st.column_config.NumberColumn(
                            "Moved",
                            format="%+d",
                            help="Places climbed since the last gameweek. Empty for a new entry.",
                        ),
                        "team": "Team",
                        "manager": "Manager",
                        "gameweek": st.column_config.NumberColumn("GW", format="%d"),
                        "total": st.column_config.NumberColumn("Total", format="%d"),
                        "entry_id": st.column_config.NumberColumn("Entry", format="%d"),
                        "last_rank": None,
                    },
                )
                if info["has_next"]:
                    st.caption("Showing the first fifty. Later pages are not loaded.")
