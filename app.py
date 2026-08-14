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
from fpl_manager.live import LiveGameweek, load_live, player_view, score_squad
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

# What every number on screen means, in one place. A column appears in several
# views and a couple of them appear as metrics too, so defining each once is
# what stops the same figure being explained two different ways.
#
# Two of these are worth the words they take. `xpts_next` and `xpts_total` are
# both "projected points" and are not the same number, and they sit next to
# each other constantly.
GLOSSARY = {
    "xpts_next": "Projected points in the next gameweek alone. A club with two "
    "fixtures that week counts both, a club with none scores zero.",
    "xpts_total": "Projected points added up across every gameweek in the "
    "horizon, which is the slider in the sidebar. Not comparable with the next "
    "gameweek figure beside it.",
    "value": "Projected points over the horizon divided by today's price. What "
    "you get per million spent, which is what decides who can be afforded "
    "elsewhere in the squad.",
    "differential": "Projection percentile minus ownership percentile. Positive "
    "means the model rates him higher than the crowd does.",
    "ownership": "Share of all FPL managers who own him, straight from the API.",
    "points_per_90": "The scoring rate the projection is built from. Last "
    "season blended with this one, plus a rate rebuilt from expected goals, "
    "assists and clean sheets once enough of the season has been played.",
    "minutes_share": "Expected share of a full match, so 0.85 means he is "
    "worth about 76 minutes. Built from how often he starts rather than from "
    "his minutes total, which would cancel against the rate beside it.",
    "start_rate": "How often he started last season, shrunk towards what his "
    "price implies. Price is the only input here that is not last season's "
    "minutes, which is what lets his rate and his role move separately.",
    "prior_p90": "Last season's points per 90, before this season is blended in.",
    "component_p90": "Points per 90 rebuilt from expected goals, expected "
    "assists and the club's clean sheet chance, rather than read back off what "
    "he actually scored. Empty until he has 270 minutes this season.",
    "form": "FPL's own figure: average points over the last 30 days.",
    "points_per_game": "FPL's own figure: average points per appearance.",
    "total_points": "Points actually scored so far, straight from the API.",
    "expected_goals": "Expected goals accumulated so far, straight from the API.",
    "expected_assists": "Expected assists accumulated so far, straight from the API.",
    "chance_of_playing": "FPL's published chance he features in the next "
    "gameweek. Blank means nothing has been flagged.",
    "price": "Today's price. What you would pay to buy him now, which is not "
    "what you would get for selling one you already own.",
    "roi": "Points already scored this season per million of today's price. "
    "Backward looking, unlike everything on the Players tab.",
}

CACHE_TTL = 6 * 3600
# shorter than the sixty seconds api.live holds on disk, or the memory cache
# outlives the disk cache behind it and every poll serves stale data twice over
LIVE_MEMORY_TTL = 30
LIVE_POLL = "60s"
POSITIONS_IN_ORDER = ("GKP", "DEF", "MID", "FWD")

# One table answering several questions, rather than one wide table answering
# none of them well. Model terms has no equivalent on the stats sites and is
# the point of ours: the three separable terms laid out so a ranking that looks
# wrong can be argued with instead of taken on faith.
STAT_VIEWS = {
    "Projection": ["price", "xpts_next", "xpts_total", "value", "differential", "ownership"],
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
# how much a run has to change before it is worth acting on. Below about half a
# point of difficulty the swing is inside the noise of FPL's own 1 to 5 rating.
SWING_THRESHOLD = 0.6
POSITION_COLOURS = {"GKP": "#FFB020", "DEF": "#00C2FF", "MID": "#00E87B", "FWD": "#FF4D6D"}

# Player photographs and club kits, served by the same CDNs the FPL site uses.
# Nothing here fetches them: these are strings handed to the browser, so the
# cache in `api.py` is not involved and neither is the network from our side.
#
# A player with no photograph gets a 403 rather than a 404, and it is the cheap
# fringe players who are missing, which is exactly who the optimiser buys to
# enable a squad. So a face always carries a fallback to the club kit and never
# stands on its own.
FACE_URL = "https://resources.premierleague.com/premierleague/photos/players/110x140/p{}.png"
KIT_URL = "https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_{}{}-66.png"
BADGE_URL = "https://resources.premierleague.com/premierleague/badges/70/t{}.png"
BADGE_COLUMN = st.column_config.ImageColumn("", width="small")
SWING_COLUMNS = {
    "badge": BADGE_COLUMN,
    "club": "Club",
    "now": st.column_config.NumberColumn("Now", format="%.2f"),
    "later": st.column_config.NumberColumn("Later", format="%.2f"),
    "swing": st.column_config.NumberColumn("Swing", format="%+.2f"),
    "now_games": st.column_config.NumberColumn("Games now", format="%d"),
    "later_games": st.column_config.NumberColumn("Games later", format="%d"),
}

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
/* Markings as an SVG background layer rather than pseudo-elements, since the
   element has only two and the shape has to stretch to whatever height the
   formation ends up being. preserveAspectRatio="none" is what allows that. */
.pitch {
  background:
    url('data:image/svg+xml;utf8,\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 140" preserveAspectRatio="none">\
<g fill="none" stroke="rgba(255,255,255,.20)" stroke-width="0.5">\
<rect x="1" y="1" width="98" height="138"/>\
<line x1="1" y1="70" x2="99" y2="70"/>\
<circle cx="50" cy="70" r="13"/>\
<rect x="28" y="1" width="44" height="20"/>\
<rect x="39" y="1" width="22" height="8"/>\
<rect x="28" y="119" width="44" height="20"/>\
<rect x="39" y="131" width="22" height="8"/>\
</g><g fill="rgba(255,255,255,.20)">\
<circle cx="50" cy="70" r="1"/>\
</g></svg>') center/100% 100% no-repeat,
    repeating-linear-gradient(to bottom,
      rgba(255,255,255,.05) 0 7%, rgba(0,0,0,0) 7% 14%),
    linear-gradient(180deg, #10794a 0%, #0a5733 100%);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 10px 4px;
}
/* The kit is the object's fallback, so it renders only when the photograph
   does not. Both are sized to fill the box, since an object takes its natural
   size otherwise and the photographs are 220 by 280. */
.mug {
  display:block; width:44px; height:56px; margin:0 auto 2px;
  border-radius:6px; background-color:rgba(55,0,60,.08); overflow:hidden;
}
.mug object, .mug img {
  display:block; width:100%; height:100%; pointer-events:none;
  object-fit:cover; object-position:top center;
}
.mug img { object-fit:contain; object-position:center; padding:3px; }
.pitch.compact .mug, .bench-strip.compact .mug { width:34px; height:43px; }
/* FPL's photographs go stale after a transfer, so a player can appear in the
   kit of the club he has just left. The crest is read off the live team code
   and is always current, which makes it the thing to trust on the card. */
.crest { position:absolute; top:5px; left:5px; width:15px; height:15px; opacity:.95; }
.state {
  border:1px solid var(--line-soft); border-radius:var(--radius);
  background:var(--card); padding:18px 20px; margin:4px 0 8px;
}
.state h4 { margin:0 0 6px; font-size:1rem; font-weight:700; }
.state p { margin:0; color:var(--muted); font-size:.86rem; line-height:1.5; }
.pd-head { display:flex; align-items:center; gap:14px; margin-bottom:4px; }
.pd-head h4 { margin:0; font-size:1.25rem; font-weight:700; }
.pd-head p { margin:2px 0 0; color:var(--muted); font-size:.86rem; }
.mug.big { width:66px; height:84px; margin:0; border-radius:8px; }
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
/* Last in the sheet on purpose. These override the card rules above, and at
   equal specificity the later rule is the one that wins.

   A phone leaves about 320px of pitch. At the full card width only three fit
   on a row, so a five man midfield wraps onto two and the formation stops
   being readable, which is the one thing this layout exists to show. Five
   across has to clear the gaps as well as the cards, which leaves 58px each. */
@media (max-width: 640px) {
  .pitch .shirt, .bench-strip .shirt { width:58px; padding:5px 2px; }
  .pitch .mug, .bench-strip .mug { width:30px; height:38px; }
  .pitch-line { gap:5px; margin-bottom:10px; }
  .shirt .nm { font-size:.6rem; }
  .shirt .meta { font-size:.5rem; }
  .shirt .pts { font-size:.8rem; margin-top:2px; }
  .badge { font-size:.5rem; padding:0 3px; margin-left:2px; }
}
</style>
"""
st.markdown(PITCH_CSS, unsafe_allow_html=True)


def how_to_read(body: str) -> None:
    """A collapsed note explaining what a page's numbers mean.

    Collapsed because this is read once and then never again, and an open block
    of prose on every tab would push the actual numbers below the fold.
    """
    with st.expander("How to read this", icon=":material/help:"):
        st.markdown(body)


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
@st.cache_resource(ttl=CACHE_TTL, show_spinner="Loading season data")
def load_season(refresh_token: int) -> Season:
    """Season holds an HTTP session, so it is a resource rather than data.

    `refresh_token` exists only to give the cache something to invalidate on.

    The ttl is what makes the disk cache expiring mean anything. Without it a
    process keeps its first Season forever, nothing re-enters the fetch, and
    the six hour disk ttl quietly never fires on a long running deploy.

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


# Everything below takes `stamp` as its last argument and never reads it.
# Streamlit is told not to hash the Season, so a rebuilt one looks identical to
# the one it replaced and these would keep serving results computed from data
# that has since been refetched. `season.data_stamp` is the hashable stand-in.
@st.cache_data(show_spinner="Fetching last season's totals, this takes a few minutes")
def cached_prior(_season: Season, stamp: str) -> pd.DataFrame | None:
    return load_prior(_season)


@st.cache_data(show_spinner="Reading transfer activity")
def load_prices(_season: Season, stamp: str) -> pd.DataFrame:
    return price_pressure(_season)


@st.cache_data(show_spinner="Working out returns")
def load_roi(_season: Season, projections: pd.DataFrame, stamp: str) -> pd.DataFrame:
    return roi_frame(_season, projections)


@st.cache_data(show_spinner="Projecting")
def load_projections(
    _season: Season, horizon: int, use_prior: bool, stamp: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-player totals and the per-gameweek frame chip timing needs."""
    prior = cached_prior(_season, stamp) if use_prior else None
    return project(_season, horizon=horizon, prior=prior)


@st.cache_data(show_spinner=False)
def player_images(_season: Season) -> pd.DataFrame:
    """Face, club kit and club badge URL for every player, keyed by player id.

    Built from `code` and `team_code`, which ride along on the player frame
    because `_build_players` keeps every column the payload came with. Keyed by
    id so it reindexes onto any projection-derived frame without a merge, ids
    being the join key everywhere here.

    Keepers wear a different kit from the outfielders, which is the `_1` in the
    filename and the only reason position matters to this.

    Comes back empty if either id is missing rather than raising. This is an
    undocumented API and the whole feature is decoration, so losing the faces is
    a fair price for a schema change. Taking the page down over a photograph is
    not.
    """
    players = _season.players
    if not {"code", "team_code"} <= set(players.columns):
        return pd.DataFrame(columns=["face", "kit", "badge"])

    keeper = players["position"].eq("GKP").map({True: "_1", False: ""})
    return pd.DataFrame(
        {
            "face": players["code"].map(FACE_URL.format),
            "kit": [
                KIT_URL.format(code, suffix)
                for code, suffix in zip(players["team_code"], keeper, strict=True)
            ],
            "badge": players["team_code"].map(BADGE_URL.format),
        },
        index=players.index,
    )


@st.cache_data(show_spinner=False)
def club_badges(_season: Season) -> pd.Series:
    """Badge URL for every club, keyed by the short name the tables show.

    Taken off the player frame rather than the club frame, because `team_code`
    rides along there while `_build_teams` subsets its columns and drops it.
    Going the long way round keeps this to the front end, where a schema change
    costs a missing badge rather than a broken page.
    """
    players = _season.players
    if not {"club", "team_code"} <= set(players.columns):
        return pd.Series(dtype="object")
    codes = players.groupby("club")["team_code"].first()
    return codes.map(BADGE_URL.format)


def with_badges(frame: pd.DataFrame, badges: pd.Series, on: str = "club") -> pd.DataFrame:
    """Put a badge column in front of a club-keyed table."""
    if badges.empty or on not in frame.columns:
        return frame
    out = frame.copy()
    out.insert(0, "badge", out[on].map(badges))
    return out


@st.cache_data(show_spinner=False)
def load_gameweek_shape(_season: Season, horizon: int) -> pd.DataFrame:
    return _season.gameweek_shape(horizon)


@st.cache_data(show_spinner=False)
def load_swings(_season: Season, window: int) -> pd.DataFrame:
    return _season.fixture_swings(window)


@st.cache_data(show_spinner="Pricing chips")
def cached_chips(
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    squad_ids: tuple[int, ...],
    budget_tenths: int,
) -> pd.DataFrame:
    """Chip timing, cached because it is two solves per gameweek.

    Six seconds over a six week horizon, which is fine once and far too slow to
    sit behind a slider that re-runs the tab on every nudge. Takes the ids as a
    tuple so the cache can hash them.
    """
    return evaluate_chips(projections, by_gameweek, list(squad_ids), budget_tenths)


@st.cache_data(ttl=LIVE_MEMORY_TTL, show_spinner=False)
def load_live_gw(_season: Season, gameweek: int) -> LiveGameweek:
    """This gameweek's live state, on its own short lease.

    Deliberately not routed through `load_season`, which holds bootstrap for six
    hours. Rebuilding the season every minute would drag the whole projection
    through a recompute for data that did not change.

    The ttl is shorter than the sixty seconds `api.live` holds on disk, since a
    memory cache outliving the disk cache behind it serves stale data twice
    over. It takes no stamp, because it is keyed on the gameweek and expires on
    its own rather than when bootstrap moves.
    """
    return load_live(_season, gameweek)


@st.cache_data(show_spinner=False)
def fixture_runs(_season: Season, horizon: int, stamp: str) -> tuple[pd.DataFrame, pd.DataFrame]:
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
        cells.append(_cell("Deadline", "Passed, see Live", "soon"))
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

    def g(key):
        """The tooltip for a column, or nothing if it needs no explaining."""
        return {"help": GLOSSARY[key]} if key in GLOSSARY else {}

    config = {
        "badge": st.column_config.ImageColumn("", width="small", pinned=True),
        "name": st.column_config.TextColumn("Player", pinned=True),
        "position": st.column_config.TextColumn("Pos", width="small"),
        "club": st.column_config.TextColumn("Club", width="small"),
        "price": st.column_config.NumberColumn("Price", format="%.1f", width="small", **g("price")),
        "xpts_next": st.column_config.NumberColumn("xPts next", format="%.1f", **g("xpts_next")),
        "xpts_total": st.column_config.ProgressColumn(
            f"xPts {horizon} GW",
            format="%.1f",
            min_value=0.0,
            max_value=max_xpts,
            **g("xpts_total"),
        ),
        "value": st.column_config.NumberColumn("Pts per m", format="%.2f", **g("value")),
        "differential": st.column_config.NumberColumn(
            "Differential", format="%+.2f", **g("differential")
        ),
        "ownership": st.column_config.NumberColumn("Owned %", format="%.1f", **g("ownership")),
        "points_per_90": st.column_config.NumberColumn(
            "Pts per 90", format="%.2f", **g("points_per_90")
        ),
        "minutes_share": st.column_config.NumberColumn(
            "Mins share", format="%.2f", **g("minutes_share")
        ),
        "start_rate": st.column_config.NumberColumn("Start rate", format="%.2f", **g("start_rate")),
        "prior_p90": st.column_config.NumberColumn(
            "Last season p90", format="%.2f", **g("prior_p90")
        ),
        "form": st.column_config.NumberColumn("Form", format="%.1f", **g("form")),
        "points_per_game": st.column_config.NumberColumn(
            "PPG", format="%.1f", **g("points_per_game")
        ),
        "total_points": st.column_config.NumberColumn("Total", format="%d", **g("total_points")),
        "expected_goals": st.column_config.NumberColumn("xG", format="%.2f", **g("expected_goals")),
        "expected_assists": st.column_config.NumberColumn(
            "xA", format="%.2f", **g("expected_assists")
        ),
        "chance_of_playing": st.column_config.NumberColumn(
            "Chance %", format="%.0f", **g("chance_of_playing")
        ),
        "fitness": st.column_config.TextColumn("Fitness", width="medium"),
    }
    for col in gw_cols:
        config[col] = st.column_config.TextColumn(
            col,
            width="small",
            help="Opponent that gameweek. Upper case at home, lower case away, "
            "coloured green for an easy fixture and red for a hard one. Two "
            "opponents means a double gameweek, a dash means a blank.",
        )
    return config


def pool_table(
    view: pd.DataFrame,
    labels: pd.DataFrame,
    difficulty: pd.DataFrame,
    columns: list[str],
    horizon: int,
    key: str,
    images: pd.DataFrame | None = None,
) -> int | None:
    """The pool, with each player's fixture run beside his numbers.

    Reading a projection without the run that produced it means holding two
    tabs in your head at once. Returns the id of the selected player, or None.
    """
    gw_cols = [f"GW{int(c)}" for c in labels.columns]
    table = view[["name", "position", "club", *columns]]

    # the badge goes first so the club reads before the name, which is how you
    # scan a list of players you half recognise
    if images is not None and not images.empty:
        table = table.copy()
        table.insert(0, "badge", images["badge"].reindex(table.index))

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
def player_detail(
    row: pd.Series, weeks: pd.DataFrame, horizon: int, images: pd.DataFrame | None = None
) -> None:
    """Why this player is ranked where he is.

    The projection is three terms multiplied together and the total says
    nothing about which of them is driving it. A cheap player with a high
    scoring rate and a thin minutes share is a completely different
    proposition from one the other way round, and a column of totals cannot
    tell you which you are looking at.
    """
    severity, note = availability(row)
    st.markdown(
        f'<div class="pd-head">{_mug(images, row.name, "mug big")}'
        f"<div><h4>{escape(str(row['name']))}</h4>"
        f"<p>{escape(str(row['club']))} · {escape(str(row['position']))} · "
        f"{row['price']:.1f}m · {row['ownership']:.1f}% owned</p></div></div>",
        unsafe_allow_html=True,
    )
    if severity:
        {"out": st.error, "doubt": st.warning, "note": st.info}[severity](note)

    a, b, c = st.columns(3)
    a.metric("Points per 90", f"{row['points_per_90']:.2f}", help=GLOSSARY["points_per_90"])
    b.metric(
        "Expected minutes", f"{row['minutes_share']:.0%} of 90", help=GLOSSARY["minutes_share"]
    )
    c.metric(
        f"Projected, {horizon} GW",
        f"{row['xpts_total']:.1f} pts",
        help=GLOSSARY["xpts_total"],
    )

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


def _mug(images: pd.DataFrame | None, player_id, css: str = "mug") -> str:
    """A player's face, falling back to his club kit if there is no photograph.

    Roughly half of the cheapest players have no photograph and the CDN answers
    403 for them, which is the bench of any squad the optimiser builds. Leaving
    a hole there is the worst case, since a cheap defender is exactly who you
    cannot identify from the name alone.

    The kit is `object` fallback content rather than a layer behind the face or
    an `onerror` swap. Two earlier attempts got this wrong and both are worth
    recording, because neither failed loudly:

    Streamlit sanitises the HTML it renders and strips every `on*` attribute, so
    an `onerror` handler is removed before it can run.

    Putting the kit behind the face as a second background does fire, but every
    FPL photograph is a cut-out with a transparent background, so the kit shows
    through around the player on every card rather than only the missing ones.

    `object` is the only one of the three that is genuinely conditional: the
    fallback renders when, and only when, the photograph fails to load.
    """
    if images is None or player_id not in images.index:
        return ""
    face, kit = images.loc[player_id, "face"], images.loc[player_id, "kit"]
    return (
        f'<span class="{css}"><object data="{escape(face)}" type="image/png">'
        f'<img src="{escape(kit)}" alt=""></object></span>'
    )


def _shirt(
    row: pd.Series, badge: str = "", highlight: str = "", images: pd.DataFrame | None = None
) -> str:
    mark = f'<span class="badge {badge.lower()}">{badge}</span>' if badge else ""
    severity, note = availability(row)
    flag = f'<span class="flag {severity}" title="{escape(note)}"></span>' if severity else ""
    ring = f" ring-{highlight}" if highlight else ""
    crest = ""
    if images is not None and row.name in images.index:
        crest = f'<img class="crest" src="{escape(images.loc[row.name, "badge"])}" alt="">'
    return (
        f'<div class="shirt{ring}">{flag}{crest}{_mug(images, row.name)}'
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
    images: pd.DataFrame | None = None,
) -> None:
    """Lay the XI out on a pitch, in formation, the way the FPL site does.

    Reading a lineup is a spatial job. A flat table makes you count defenders to
    work out the shape, which is the one thing the layout should tell you at a
    glance. `highlight` rings individual players, which is how the transfer
    view shows what is leaving and what is arriving.

    `images` is passed in rather than read from a global because this has three
    call sites and a global that one of them forgot would fail silently, as a
    pitch of nameless cards rather than an error.
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
                images,
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
        strip = "".join(
            _shirt(row, "", highlight.get(pid, ""), images) for pid, row in bench.iterrows()
        )
        markup += (
            f'<div class="bench-strip{size}"><div class="bench-cap">Bench, in order</div>'
            f'<div class="pitch-line">{strip}</div></div>'
        )

    st.markdown(markup, unsafe_allow_html=True)


def empty_state(title: str, body: str) -> None:
    """Say what is missing, why, and when it will fill.

    Most of the empty places in this app are empty for a reason that is not the
    user's doing: no gameweek has been scored, or no postponement has been
    announced yet. A one line info bar reads as something being broken, which
    for the months before a season starts is most of the page.
    """
    st.markdown(
        f'<div class="state"><h4>{escape(title)}</h4><p>{escape(body)}</p></div>',
        unsafe_allow_html=True,
    )


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
stamp = season.data_stamp
images = player_images(season)
badges = club_badges(season)
projections, by_gameweek = load_projections(season, horizon, use_prior, stamp)
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

(
    build_tab,
    players_tab,
    roi_tab,
    fixtures_tab,
    transfers_tab,
    chips_tab,
    leagues_tab,
    live_tab,
) = st.tabs(
    # Live goes last deliberately. `tests/test_app.py` finds the player pool by
    # looking for the first dataframe with a GW column, so a tab rendering one
    # ahead of it would shadow the pool and fail several tests confusingly.
    ["Squad", "Players", "ROI", "Fixtures", "Transfers", "Chips", "Leagues", "Live"]
)

with build_tab:
    st.subheader("Build a squad")
    how_to_read(
        "The best fifteen the model can buy under the budget, the three per club "
        "cap and the position quotas. It is a solved answer, not a sorted list: "
        "picking the best points per million one at a time is reliably a few "
        "points worse, because what binds is having enough cheap players to "
        "afford the expensive ones.\n\n"
        "- **Projected over the horizon** counts the starting eleven with the "
        "captain doubled. The bench is not in it.\n"
        "- **The number on each shirt** is the next gameweek alone, so the "
        "shirts and the headline are different units.\n"
        "- **Bench weight** in the sidebar decides how much the bench counts when "
        "choosing the fifteen. Low gives two cheap punts, high gives a squad you "
        "can rotate.\n"
        "- **Coloured dots** flag injuries and doubts. Hover one to read it.\n\n"
        "Rotation, press conferences and minutes management are not in the API, "
        "so treat this as a shortlist to argue with."
    )
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
    a.metric(
        "Squad cost",
        f"{result.cost:.1f}m",
        f"{budget - result.cost:+.1f}m in bank",
        help="What the fifteen cost at today's prices, against the budget set "
        "in the sidebar. A fresh squad starts with 100.0m.",
    )
    b.metric(
        f"Projected over {horizon} GW",
        f"{result.projected:.0f} pts",
        help="The starting eleven only, added up across the horizon, with the "
        "captain counted twice. The four on the bench are not in this number, "
        "since they only score if someone ahead of them does not play.",
    )
    c.metric(
        "Captain",
        result.captain["name"],
        help="Whoever the solver expects most from over the horizon. His "
        "points double, so this is a bigger decision than any single transfer.",
    )

    formation_view(
        result.xi, result.bench, result.captain.name, result.vice_captain.name, images=images
    )
    st.caption(
        "The number on each shirt is that player's projection for the **next "
        "gameweek only**, which is why they do not add up to the total above."
    )
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
    how_to_read(
        "Every player the model has a projection for. Hover any column header "
        "for what that number is.\n\n"
        "The projection is three things multiplied together, and the **Model "
        "terms** view above the table splits them out:\n\n"
        "`points per 90` times `expected minutes` times `fixture difficulty`\n\n"
        "That separation is the point. A high projection because someone scores "
        "freely is a different bet from a high projection because he plays every "
        "minute against weak opposition, and a single total cannot tell you "
        "which. Select any row to see the working behind it.\n\n"
        "**xPts next** and **xPts over the horizon** are both projected points "
        "and are not comparable with each other."
    )

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
        "Sort by",
        [
            "xpts_total",
            "xpts_next",
            "value",
            "differential",
            "ownership",
            "form",
            "points_per_game",
        ],
        help="Differential ranks a player's projection against how many people own "
        "him. Positive means the model rates him higher than the crowd does.",
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
        empty_state(
            "No players match",
            "Nothing in the pool clears every filter at once. Widen the price or ownership "
            "range, or clear the search, to see players again.",
        )
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
        labels, difficulty = fixture_runs(season, horizon, stamp)
        chosen = pool_table(shown, labels, difficulty, columns, horizon, key="pool", images=images)

        # only open on a change, or closing the dialog would reopen it at once
        if chosen is not None and st.session_state.get("inspected") != chosen:
            st.session_state.inspected = chosen
            player_detail(
                view.loc[chosen], player_weeks(by_gameweek, chosen), horizon, images=images
            )
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
    pressure = load_prices(season, stamp)
    if is_dormant(pressure):
        empty_state(
            "No transfer activity yet",
            "Price pressure is read from how many people have transferred a player in or "
            "out this gameweek, and nobody has yet. This fills in once the season is under "
            "way and the counters start moving.",
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
    how_to_read(
        "Points already **scored** per million of the price today. Everything on "
        "the Players tab looks forward; this looks back.\n\n"
        "Useful for spotting who has actually earned their place rather than who "
        "the model likes, and the two disagreeing is worth a look either way. A "
        "player well ahead of his projection has been lucky, in form, or is "
        "being underrated, and the table cannot tell you which.\n\n"
        "Priced at the cost today, not what he cost when he scored them, so anyone "
        "who has risen sharply looks worse here than he was."
    )
    points_from = points_source(season)
    roi = load_roi(season, projections, stamp)

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
            k1.metric(
                "Best return",
                top["name"],
                f"{top['roi']:.1f} pts per m",
                delta_color="off",
                help=GLOSSARY["roi"],
            )
            k2.metric(
                "Players ranked",
                len(roi_view),
                help="How many players clear the minutes filter below. Anyone who has "
                "barely played is left out, since a few points off one substitute "
                "appearance divides into a flattering rate.",
            )
            k3.metric(
                "Median return",
                f"{roi_view['roi'].median():.1f} pts per m",
                help="The midpoint of those players, which is what makes a given "
                "return good or bad rather than merely large.",
            )

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
                config["badge"] = BADGE_COLUMN
                st.dataframe(
                    with_badges(roi_view.head(40)[roi_cols], badges),
                    hide_index=True,
                    width="stretch",
                    column_config=config,
                )
            with movers_up:
                if "gap" in roi_view.columns:
                    st.caption("Projected to return more than they have")
                    st.dataframe(
                        with_badges(
                            roi_view.nlargest(15, "gap")[
                                ["name", "position", "club", "roi", "projected_roi"]
                            ],
                            badges,
                        ),
                        hide_index=True,
                        width="stretch",
                        column_config={
                            "badge": BADGE_COLUMN,
                            "name": "Player",
                            "position": "Pos",
                            "club": "Club",
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
    how_to_read(
        "Who each club plays, coloured by how hard it is. Green is easy, red is "
        "hard, and the middle band is deliberately the dimmest thing here so it "
        "does not draw the eye towards the fixtures that matter least.\n\n"
        "Upper case is at home, lower case away. Two opponents in a cell is a "
        "double gameweek, a dash is a blank.\n\n"
        "Difficulty blends the one to five rating FPL publishes with the attack "
        "and defence ratings of the two clubs, which move during the season and "
        "can tell apart two fixtures the rating calls identical.\n\n"
        "**Swings** below compare the next few gameweeks against the ones after, "
        "so a positive swing means it gets easier and is a reason to buy early."
    )
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
        with_badges(ticker.head(10), badges),
        hide_index=True,
        width="stretch",
        column_config={
            "badge": BADGE_COLUMN,
            "club": "Club",
            "fixtures": st.column_config.NumberColumn("Games", format="%d"),
            "avg_difficulty": st.column_config.NumberColumn("Avg difficulty", format="%.2f"),
        },
    )

    st.divider()
    st.subheader("Blanks and doubles")
    st.caption(
        "Which clubs play twice in a gameweek, and which do not play at all. This is when "
        "chips are worth playing and when a squad quietly stops fielding eleven. Blanks and "
        "doubles appear mid-season, once cup ties and European fixtures force postponements, "
        "so an empty table here means none have been announced yet."
    )
    radar_weeks = st.slider("Gameweeks to scan", 4, 20, 12, key="radar_horizon")
    shape = load_gameweek_shape(season, radar_weeks)

    if shape.empty:
        empty_state(
            "Nothing irregular coming",
            f"Every club plays exactly once in each of the next {radar_weeks} gameweeks on "
            "the published fixture list. Blanks and doubles appear mid-season, when cup "
            "ties and European fixtures force postponements, and this fills in on its own "
            "as they are announced.",
        )
    else:
        summary = (
            shape.assign(clubs=shape["club"])
            .groupby(["event", "shape"])["clubs"]
            .apply(lambda names: ", ".join(sorted(names)))
            .unstack(fill_value="")
            .reindex(columns=["double", "blank"], fill_value="")
            .reset_index()
        )
        st.dataframe(
            summary,
            hide_index=True,
            width="stretch",
            column_config={
                "event": st.column_config.NumberColumn("GW", format="%d", width="small"),
                "double": st.column_config.TextColumn("Playing twice", width="large"),
                "blank": st.column_config.TextColumn("Not playing", width="large"),
            },
        )

    st.divider()
    st.subheader("Fixture swings")
    swing_window = st.slider("Gameweeks either side", 2, 5, 3, key="swing_window")
    swings = load_swings(season, swing_window)
    st.caption(
        f"The next {swing_window} gameweeks against the {swing_window} after them. This is "
        "about timing rather than quality: a club can have a kind run overall and still be "
        "the wrong buy this week."
    )

    if swings.empty:
        st.info("Not enough fixtures published to compare one block against the next.")
    else:
        easing, worsening = st.columns(2)
        with easing:
            st.caption("Hard now, easier later. Worth waiting for.")
            st.dataframe(
                with_badges(swings[swings["swing"] >= SWING_THRESHOLD].head(8), badges),
                hide_index=True,
                width="stretch",
                column_config=SWING_COLUMNS,
            )
        with worsening:
            st.caption("Easy now, harder later. Use them, then plan the exit.")
            st.dataframe(
                with_badges(swings[swings["swing"] <= -SWING_THRESHOLD].tail(8).iloc[::-1], badges),
                hide_index=True,
                width="stretch",
                column_config=SWING_COLUMNS,
            )
        st.caption(
            "Games either side are shown because a swing resting on one fixture is a much "
            "weaker signal than one resting on three, and a blank gameweek is what makes "
            "the difference."
        )

with transfers_tab:
    st.subheader("Transfer planner")
    how_to_read(
        "What to do with the squad you already own. Every gain here is **net of "
        "the four point hit** for going beyond your free transfers, so a positive "
        "number already pays for itself.\n\n"
        "The multi week planner solves all the weeks as one problem rather than "
        "one week at a time, which is what lets it hold a transfer this week to "
        "afford someone next week. Solving each week alone can never do that.\n\n"
        "**Selling prices matter and are not public.** FPL gives you back what "
        "you paid plus half of any rise, so a player who has gone up 0.4 sells "
        "for 0.2 more than you paid. Load a squad.json with your purchase prices "
        "or the planner will think you have more money than you do."
    )
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
                help="What the move is worth after the cost of making it. Already net "
                "of any four point hit, so anything positive is worth doing on the "
                "model's numbers alone.",
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
            m1.metric(
                f"Projected over {plan_weeks} GW",
                f"{route.projected:.0f} pts",
                help="What the whole route scores, the starting eleven each week with "
                "the captain doubled, already net of any hits.",
            )
            m2.metric(
                "Transfers",
                route.transfers,
                help="Moves across the whole route, not per week. The weeks are solved "
                "together, so it can sit still now to afford someone later.",
            )
            m3.metric(
                "Hits",
                f"{route.hits * 4} pts",
                delta_color="off",
                help="Points given up for going beyond your free transfers, four each. "
                "Already subtracted from the projection beside it.",
            )

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
                images=images,
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
                images=images,
            )
        flag_legend()

with chips_tab:
    st.subheader("Chip timing")
    how_to_read(
        "What each chip is worth in each gameweek, measured against what this "
        "squad scores that week anyway with the lineup and captain already picked "
        "as well as they can be. So Bench Boost is worth your bench, not your "
        "whole squad.\n\n"
        "**Wildcard is not comparable with the other three.** You keep the squad, "
        "so it is measured over every remaining gameweek rather than one, and its "
        "gain falls the longer you leave it.\n\n"
        "The horizon bounds the answer. If the best week is beyond it the tool "
        "cannot see it, so widen the slider before trusting advice to play one "
        "now. Nothing here knows which chips you have already used."
    )
    st.caption(
        "Gain is what the chip adds on top of what your squad scores anyway, "
        "with the lineup and captain already picked optimally for that gameweek."
    )

    if my_squad is None:
        st.info("Load your squad in the sidebar to price your chips.")
    else:
        team_value = my_squad.value_tenths(season)
        table = cached_chips(projections, by_gameweek, tuple(my_squad.player_ids), team_value)

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
                    help="What the chip adds on top of what this squad scores in that "
                    "gameweek anyway, with the lineup and captain already picked as "
                    "well as they can be without it.",
                )

            st.caption(
                f"Free Hit and Wildcard are priced against your team value, "
                f"{team_value / 10:.1f}m. Wildcard is the odd one out: you keep the "
                f"squad, so it is worth every remaining gameweek rather than one, "
                f"and its gain falls the longer you leave it."
            )
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
    how_to_read(
        "Anyone's public record, and the classic leagues they are in. The entry "
        "id is the number in the URL when you look at a points page.\n\n"
        "Leagues you joined are sorted above the ones FPL put you in by club and "
        "country, since those are rarely the ones you care about.\n\n"
        "Everything here is read only and public. Nothing on this page needs a "
        "login, and the tool never asks for one.\n\n"
        "**This is a record, not a projection.** Nothing on this page feeds the "
        "model, and no league table shows what anyone is scoring in progress."
    )
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
        m1.metric(
            "Overall points",
            f"{manager.overall_points:,}",
            help="Points scored across the whole season so far, as FPL reports them.",
        )
        m2.metric(
            "Overall rank",
            f"{manager.overall_rank:,}" if manager.overall_rank else "Not ranked yet",
            help="Position among every FPL manager. Nobody has a rank until a "
            "gameweek has been scored, which is what the pre-season reading means.",
        )
        m3.metric(
            "Seasons played",
            manager.seasons_played,
            help="How many previous seasons this manager has a record for. FPL "
            "publishes only the seasons they actually played.",
        )

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
                empty_state(
                    f"{info['name']} has no table yet",
                    "Nobody has a rank before anyone has scored, so the FPL API returns an "
                    "empty league until the first gameweek is settled. The league itself is "
                    "real and this fills in then.",
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


with live_tab:
    st.subheader("Live scoring")
    how_to_read(
        "Your squad as the matches are played. Refreshes itself once a minute "
        "while a game is in progress and sits still otherwise.\n\n"
        "**Bonus is provisional until a match ends.** It is worked out from the "
        "bonus points system the same way FPL does, three, two and one to the top "
        "scorers in each match with ties sharing, and it can still move. Once "
        "real bonus is awarded that match switches to the real figure.\n\n"
        "**Automatic substitutions only resolve once the matches are over.** "
        "Somebody on no minutes at half past three has not blanked, he "
        "has not kicked off.\n\n"
        "Load your squad by entry id rather than a file if you want the real "
        "captain and bench order, since a squad file records neither."
    )
    live_gw = season.current_gameweek

    if live_gw < 1:
        st.info("No gameweek has started yet, so there is nothing to score.")
    else:
        # Polling is gated on a match actually being in progress. Left running
        # it costs every visitor a request a minute forever, including on a
        # Tuesday. The state is read once here, outside the fragment, purely to
        # decide the interval.
        opening = load_live_gw(season, live_gw)
        polling = LIVE_POLL if opening.in_play else None

        @st.fragment(run_every=polling)
        def live_panel() -> None:
            """Reruns on its own, so a poll never re-enters the projection.

            Everything it needs beyond the live state is closed over from the
            enclosing script run, and it holds no widgets: one inside a fragment
            writing state read outside it forces a full rerun, which is the
            whole thing this avoids.
            """
            state = load_live_gw(season, live_gw)

            if state.fixtures.empty:
                st.info(f"GW{live_gw} has no fixtures published yet.")
                return

            finished = int(state.fixtures["finished"].sum())
            total = len(state.fixtures)
            fetched = state.fetched_at
            age = _relative(pd.Timestamp(fetched) - pd.Timestamp.now(tz="UTC")) if fetched else "-"

            cells = [
                _cell("Gameweek", f"GW{live_gw}"),
                _cell(
                    "Fixtures",
                    f"{finished} of {total} finished",
                    "soon" if state.in_play else "",
                ),
                _cell("Bonus", "Final" if state.all_settled else "Provisional"),
                _cell("Updated", age if polling else "Not polling"),
            ]
            st.markdown(f'<div class="statusbar">{"".join(cells)}</div>', unsafe_allow_html=True)

            if my_squad is None:
                st.info("Load your squad in the sidebar to see what it is scoring.")
                return

            score = score_squad(state, season, my_squad)
            cols = st.columns(3)
            cols[0].metric(
                "Points",
                score.total,
                help="Your eleven with the captain doubled, including bonus that has "
                "not been awarded yet. Substitutions are applied only once a player's "
                "matches are over.",
            )
            cols[1].metric(
                "Playing",
                f"{score.playing} of {len(score.lineup.starters)}",
                help="How many of your eleven have been on the pitch. The rest either "
                "have not kicked off or did not make the matchday squad.",
            )
            cols[2].metric(
                "Provisional bonus",
                score.provisional_bonus,
                delta_color="off",
                help="Bonus worked out from the bonus points system the same way FPL "
                "does, for matches that have not finished. It is already counted in "
                "the total and can still move.",
            )

            if not score.lineup.settled:
                st.caption(
                    "Matches are still being played, so bonus and any automatic "
                    "substitutions below can still change."
                )
            if my_squad.captain_id is None:
                st.caption(
                    "This squad came from a file, which records no captain and no bench "
                    "order. Load an entry id for the real lineup."
                )

            for out, came_in in score.lineup.subs:
                names = season.players["name"]
                st.caption(f"Auto sub: {names.get(out, out)} off, {names.get(came_in, came_in)} on")

            for label, ids in (
                ("Starting XI", score.lineup.starters),
                ("Bench", score.lineup.bench),
            ):
                st.markdown(f"**{label}**")
                st.dataframe(
                    player_view(state, season, ids),
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "name": "Player",
                        "position": "Pos",
                        "club": "Club",
                        "minutes": st.column_config.NumberColumn("Mins", format="%d"),
                        "points": st.column_config.NumberColumn("Pts", format="%d"),
                        "provisional_bonus": st.column_config.NumberColumn(
                            "Prov bonus", format="%d"
                        ),
                        "bps": st.column_config.NumberColumn("BPS", format="%d"),
                        "goals_scored": st.column_config.NumberColumn("G", format="%d"),
                        "assists": st.column_config.NumberColumn("A", format="%d"),
                    },
                    column_order=[
                        "name",
                        "position",
                        "club",
                        "minutes",
                        "goals_scored",
                        "assists",
                        "bps",
                        "provisional_bonus",
                        "points",
                    ],
                )

        live_panel()
        st.caption(
            "Bonus is worked out from the bonus points system the same way FPL does, "
            "and is a projection until a match ends. Automatic substitutions only "
            "resolve once a player's fixtures are over."
        )
