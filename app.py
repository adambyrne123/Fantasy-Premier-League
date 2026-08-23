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
from fpl_manager.captaincy import HAUL_POINTS, haul_frame, points_pmf
from fpl_manager.chips import best_per_chip
from fpl_manager.chips import evaluate as evaluate_chips
from fpl_manager.data import (
    FORMATIONS,
    MAX_PER_CLUB,
    Season,
    format_formation,
    parse_formation,
)
from fpl_manager.elite import (
    DEFAULT_SAMPLE,
    elite_entries,
    elite_picks,
    field_shares,
    template_xi,
)
from fpl_manager.leagues import leagues_of, load_manager, past_seasons, standings, with_month
from fpl_manager.live import Lineup, LiveGameweek, load_live, player_view, score_squad
from fpl_manager.optimiser import (
    MAX_PLAN_WEEKS,
    POOL_SIZE,
    build_squad,
    pick_xi,
    plan_transfers,
    suggest_transfers,
)
from fpl_manager.prices import is_dormant, movers, price_pressure
from fpl_manager.projections import COMPONENT_MINUTES, load_prior, project
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
    "ep_next": "Fantasy Premier League's own projection for the next gameweek, "
    "read straight off their API. It is here to disagree with ours, and it is "
    "deliberately not an input to it.",
    "value": "Projected points over the horizon divided by today's price. What "
    "you get per million spent, which is what decides who can be afforded "
    "elsewhere in the squad.",
    "differential": "Projection percentile minus ownership percentile. Positive "
    "means the model rates him higher than the crowd does.",
    "ownership": "Share of all FPL managers who own him, straight from the API.",
    "points_per_90": "The scoring rate the projection is built from. Last "
    "season blended with this one, plus a rate rebuilt from what FPL pays a "
    "player in his position, once enough of the season has been played.",
    "minutes_share": "Expected share of a full match, so 0.85 means he is "
    "worth about 76 minutes. Built from how often he starts rather than from "
    "his minutes total, which would cancel against the rate beside it.",
    "start_rate": "How often he started last season, straight from his starts "
    "and the games available. The shrunk version the model actually uses is "
    "the start chance beside it.",
    "start_chance": "Chance he starts, from how often he has started shrunk "
    "towards what his price implies, and cut by any published doubt over his "
    "fitness. The rest of the minutes term is the chance he comes off the "
    "bench instead, and what is left after both is the chance he does not "
    "feature at all.",
    "xpts_gw": "Projected points in the chosen gameweek. The same projection "
    "as everywhere else, so it includes the clean sheets, saves, defensive "
    "contributions and cards that the two chances beside it leave out.",
    "haul_chance": "Chance of ten or more points from goals, assists and "
    "appearance points in this gameweek, both fixtures counted if he has two. "
    "Bonus is not in the model at all, and a clean sheet and a defensive "
    "contribution are not in this number, so for a defender it is not his "
    "haul chance but his attacking one, and it reads low.",
    "return_chance": "Chance of at least one goal or assist in this gameweek. "
    "The floor beside the ceiling: a player can be a poor haul bet and a good "
    "bet to do something.",
    "prior_p90": "Last season's points per 90, before this season is blended in.",
    "component_p90": "Points per 90 rebuilt from what FPL pays for rather than "
    "read back off what he scored: expected goals and assists, the club's clean "
    "sheet chance and the goals it concedes, saves, defensive contributions and "
    "cards. The last two are chances of clearing a bar in a match, estimated "
    "from an average. Weighted by how much of this season he has played, at "
    f"full weight from {COMPONENT_MINUTES} minutes and faded in below that.",
    "credibility": "How much of the numbers beside this are the player's own "
    "record rather than what his price implies for his position. It reaches "
    f"one at {COMPONENT_MINUTES} minutes this season. A row at 0.3 is mostly a "
    "statement about his price, so read it before reading the rest.",
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
    "elite_ownership": "Share of the top managers sampled who own him, against "
    "the ownership beside it, which is the whole game. The gap between the two "
    "is the interesting part: a player the field owns and the top of the table "
    "does not is a trap, and the other way round is a shortlist.",
    "elite_start_share": "Share of the sampled managers he actually counted "
    "for. Read off what he scored them rather than off the lineup they named, "
    "so an automatic substitution is already in it.",
    "captain_share": "Share of the sampled managers who gave him the armband. "
    "Measured from their squads rather than guessed from ownership, and it is "
    "the thing that decides whether captaining him moves your rank at all.",
    "effective_ownership": "Ownership counting the armband: a captain counts "
    "twice and a triple captain three times. At 1.5 his points land in every "
    "rival total one and a half times over, so a haul from him moves you far "
    "less than the score suggests.",
}

CACHE_TTL = 6 * 3600
# shorter than the sixty seconds api.live holds on disk, or the memory cache
# outlives the disk cache behind it and every poll serves stale data twice over
LIVE_MEMORY_TTL = 30
LIVE_POLL = "60s"
POSITIONS_IN_ORDER = ("GKP", "DEF", "MID", "FWD")
# the formation selector's "let the solver decide" option, named once so the
# widget and the branch reading it cannot drift apart
BEST_SHAPE = "Best"

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
EXTRA_STATS = [
    "form",
    "points_per_game",
    "total_points",
    "expected_goals",
    "expected_assists",
    "ep_next",
]
# FPL sets its deadlines in UK time and shows them that way, so converting to
# the server's timezone would disagree with the site people are playing on.
UK = ZoneInfo("Europe/London")
# how much a run has to change before it is worth acting on. Below about half a
# point of difficulty the swing is inside the noise of FPL's own 1 to 5 rating.
SWING_THRESHOLD = 0.6
POSITION_COLOURS = {"GKP": "#FFB020", "DEF": "#00C2FF", "MID": "#00E87B", "FWD": "#FF4D6D"}
# One colour per player being compared, held steady across every chart in that
# section so the same person stays the same colour. The position palette's own
# values, since a second set of greens and blues would invite reading position
# into a chart that is not about position.
COMPARE_COLOURS = ("#00E87B", "#00C2FF", "#FFB020", "#FF4D6D")
# How many can be read against each other at once. It is the number of colours
# above rather than a taste, since every chart in the comparison gives each
# player one and they have to stay distinguishable. Raising it means adding
# colours that are neither green nor blue, for the reason in the comment above.
COMPARE_MAX = len(COMPARE_COLOURS)
# At most four rows, so the comparison can carry the whole projection at once
# rather than making you switch views to see the rest of it. The stat views
# above exist because the pool is hundreds of rows long, which this is not.
COMPARE_COLUMNS = [
    "price",
    "xpts_next",
    "ep_next",
    "xpts_total",
    "value",
    "differential",
    "ownership",
    "fitness",
]
# The three terms the projection multiplies together, each on its own axis. A
# shared axis would need normalising, and a normalised bar cannot tell you
# whether 0.6 of a match is a lot.
TERM_CHARTS = (
    ("points_per_90", "Scoring rate", "Points per 90"),
    ("minutes_share", "Expected minutes", "Share of a match"),
    ("difficulty", "Fixture difficulty", "Average FDR, lower is easier"),
)

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
.pitch.compact .shirt .pts .v { font-size:.84rem; }
.pitch.compact .shirt .pts .k { font-size:.45rem; }
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
/* Two projections per card, each labelled. They are different spans and the
   card has to say so, or the shirts silently disagree with the total above. */
.shirt .pts {
  display:flex; gap:4px; margin-top:5px;
  border-top:1px solid rgba(0,0,0,.10); padding-top:4px;
}
.shirt .pts .box { flex:1 1 0; min-width:0; }
.shirt .pts .k {
  display:block; font-size:.5rem; letter-spacing:.05em;
  text-transform:uppercase; color:#7c6d88; white-space:nowrap;
}
.shirt .pts .v { display:block; font-size:1rem; font-weight:700; line-height:1.15; }
.shirt .pts .v.next { color:#0a5733; }
/* the horizon reads as the quieter of the two, since the week in front of you
   is what a lineup decision turns on */
.shirt .pts .v.span { color:#4a3a57; }
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
  .shirt .pts { margin-top:3px; gap:2px; padding-top:3px; }
  .shirt .pts .v { font-size:.7rem; }
  .shirt .pts .k { font-size:.42rem; letter-spacing:0; }
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


@st.cache_data(show_spinner="Working out haul chances")
def load_captaincy(
    _season: Season,
    projections: pd.DataFrame,
    by_gameweek: pd.DataFrame,
    event: int,
    stamp: str,
) -> pd.DataFrame:
    return haul_frame(_season, projections, by_gameweek, event=event)


@st.cache_data(show_spinner=False)
def cached_month(
    _season: Season, table: pd.DataFrame, events: tuple[int, ...], stamp: str
) -> pd.DataFrame:
    """A league table with the month added, cached on the table it was given.

    One request per manager in the league, so this is cached hard and only
    reached from a checkbox. The events tuple is in the key because a league
    read in August and the same league read in September are different answers.
    """
    return with_month(_season, table, list(events))


@st.cache_data(show_spinner="Reading the top managers' squads")
def load_field(
    _season: Season, gameweek: int, sample: int, stamp: str
) -> tuple[pd.DataFrame, int, int]:
    """What the top managers own and captain, plus how many of them answered.

    The two counts are returned rather than worked out on screen because
    they are the honest caption for the shares: a manager whose picks could
    not be read is out of the sample entirely, and a table that says a
    hundred when it read ninety four is wrong about every number on it.

    Much the most expensive thing this app does, at one request a manager,
    which is why nothing calls it until the sidebar toggle is on.
    """
    entries = elite_entries(_season, sample=sample)
    picks = elite_picks(_season, entries, gameweek)
    resolved = 0 if picks.empty else int(picks["entry_id"].nunique())
    return field_shares(picks), len(entries), resolved


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


@st.cache_data(show_spinner=False)
def load_club_form(_season: Season, window: int, stamp: str) -> pd.DataFrame:
    return _season.club_form(window)


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
    # a gameweek under way is neither played nor pre-season, and calling the
    # opening weekend of the season pre-season is how this read on the first
    # Saturday: GW1 kicked off, nothing finished, so the count was still zero
    if season.current_gameweek > played:
        state = f"GW{season.current_gameweek} under way"
    elif played:
        state = f"{played} played"
    else:
        state = "pre-season"
    cells = [_cell("Gameweek", f"GW{season.next_gameweek} next · {state}")]

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
        "ep_next": st.column_config.NumberColumn("FPL xPts", format="%.1f", **g("ep_next")),
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
        "start_chance": st.column_config.NumberColumn(
            "Start chance", format="%.2f", **g("start_chance")
        ),
        "xpts_gw": st.column_config.NumberColumn("xPts GW", format="%.1f", **g("xpts_gw")),
        "haul_chance": st.column_config.NumberColumn(
            f"{HAUL_POINTS}+ pts", format="percent", **g("haul_chance")
        ),
        "return_chance": st.column_config.NumberColumn(
            "Any return", format="percent", **g("return_chance")
        ),
        "credibility": st.column_config.NumberColumn(
            "His own", format="percent", **g("credibility")
        ),
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
        "elite_ownership": st.column_config.NumberColumn(
            "Top 100 %", format="percent", **g("elite_ownership")
        ),
        "elite_start_share": st.column_config.NumberColumn(
            "Top 100 start %", format="percent", **g("elite_start_share")
        ),
        "captain_share": st.column_config.NumberColumn(
            "Captained by", format="percent", **g("captain_share")
        ),
        "effective_ownership": st.column_config.NumberColumn(
            "Effective own.", format="%.2f", **g("effective_ownership")
        ),
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


def pool_frame(
    view: pd.DataFrame,
    labels: pd.DataFrame,
    difficulty: pd.DataFrame,
    columns: list[str],
    images: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """A set of players with their fixture runs attached, and the run's colours.

    Split out of `pool_table` because the comparison further down the tab wants
    the same table over a different set of rows. A run coloured one way in one
    place and another way twenty lines below is worse than either.
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
    return table, colours, gw_cols


def pool_table(
    view: pd.DataFrame,
    labels: pd.DataFrame,
    difficulty: pd.DataFrame,
    columns: list[str],
    horizon: int,
    key: str,
    images: pd.DataFrame | None = None,
    multi: bool = False,
) -> list[int]:
    """The pool, with each player's fixture run beside his numbers.

    Reading a projection without the run that produced it means holding two
    tabs in your head at once. Returns the ids of the ticked players, in the
    order the table lists them.

    `multi` puts a tick box on every row, which is how the Players tab asks for
    a comparison: the players you want to weigh against each other are the ones
    you have just read, so naming them again in a separate widget is a step
    that does not need to exist.
    """
    table, colours, gw_cols = pool_frame(view, labels, difficulty, columns, images)

    selection = st.dataframe(
        table.style.apply(lambda _: colours, axis=None),
        hide_index=True,
        width="stretch",
        column_config=pool_column_config(horizon, gw_cols, float(max(view["xpts_total"].max(), 1))),
        on_select="rerun",
        selection_mode="multi-row" if multi else "single-row",
        key=key,
    )
    # a selection comes back as row positions, so it is resolved against the
    # table that produced it and never held on to as positions
    return [int(table.index[r]) for r in selection["selection"]["rows"]]


def player_working(
    row: pd.Series, weeks: pd.DataFrame, horizon: int, images: pd.DataFrame | None = None
) -> None:
    """Why this player is ranked where he is.

    The projection is three terms multiplied together and the total says
    nothing about which of them is driving it. A cheap player with a high
    scoring rate and a thin minutes share is a completely different
    proposition from one the other way round, and a column of totals cannot
    tell you which you are looking at.

    Renders in the page rather than in a dialog. Ticking rows is how a
    comparison is built now, so a modal on the first tick would have to be
    dismissed before the second could be reached.
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
        + ", weighted by how much of the season has been played and how much of "
        "it he has played."
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
    row: pd.Series,
    badge: str = "",
    highlight: str = "",
    images: pd.DataFrame | None = None,
    labels: tuple[str, str] = ("Next", "Span"),
    value_format: str = ".1f",
) -> str:
    """One player's card, carrying both projections rather than one.

    The next gameweek and the whole horizon are different numbers and both
    matter: the first decides who starts this week, the second decides who is
    worth owning at all. Showing one unlabelled meant the shirts never added up
    to the headline above them and nothing on screen said why.

    `labels` names the two spans, so the card says GW1 and 6 GW rather than
    leaving the reader to work out which is which.

    `value_format` exists because not every caller is showing a projection. A
    live score is a whole number of points, and printing it as 6.0 makes a
    settled fact look like an estimate.
    """
    mark = f'<span class="badge {badge.lower()}">{badge}</span>' if badge else ""
    severity, note = availability(row)
    flag = f'<span class="flag {severity}" title="{escape(note)}"></span>' if severity else ""
    ring = f" ring-{highlight}" if highlight else ""
    crest = ""
    if images is not None and row.name in images.index:
        crest = f'<img class="crest" src="{escape(images.loc[row.name, "badge"])}" alt="">'

    # a frame without the horizon column still renders, showing the one span it
    # does have, since the transfer views build their own frames
    next_label, span_label = labels
    spans = [(next_label, row["xpts_next"], "next")]
    if "xpts_total" in row.index and pd.notna(row["xpts_total"]):
        spans.append((span_label, row["xpts_total"], "span"))
    cells = "".join(
        f'<span class="box"><span class="k">{escape(label)}</span>'
        f'<span class="v {tone}">{value:{value_format}}</span></span>'
        for label, value, tone in spans
    )

    return (
        f'<div class="shirt{ring}">{flag}{crest}{_mug(images, row.name)}'
        f'<div class="nm">{escape(str(row["name"]))}{mark}</div>'
        f'<div class="meta">{escape(str(row["club"]))} · {row["price"]:.1f}m</div>'
        f'<div class="pts">{cells}</div>'
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
    labels: tuple[str, str] = ("Next", "Span"),
    cost: float | None = None,
    value_format: str = ".1f",
) -> None:
    """Lay the XI out on a pitch, in formation, the way the FPL site does.

    Reading a lineup is a spatial job. A flat table makes you count defenders to
    work out the shape, which is the one thing the layout should tell you at a
    glance. `highlight` rings individual players, which is how the transfer
    view shows what is leaving and what is arriving.

    `cost` is what pinning this shape gave up against the shape the solver would
    have chosen. It goes in the caption rather than a metric because an override
    you have forgotten you set is the thing worth being told about, and the
    caption is already where the eye goes to read the shape.

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
                labels,
                value_format,
            )
            for pid, row in line.iterrows()
        )
        lines.append(f'<div class="pitch-line">{shirts}</div>')

    shape = format_formation(int((xi["position"] == pos).sum()) for pos in POSITIONS_IN_ORDER[1:])
    # a shape pinned to what the solver wanted anyway has cost nothing, and
    # saying "0.0 pts behind" reads as a warning where there is nothing to warn
    # about
    behind = (
        f" · {-cost:.1f} pts behind the best shape" if cost is not None and cost < -0.05 else ""
    )
    size = " compact" if compact else ""
    markup = (
        f'<div class="pitch{size}"><div class="pitch-cap">Formation {shape}{behind}</div>'
        f"{''.join(lines)}</div>"
    )

    if bench is not None and not bench.empty:
        strip = "".join(
            _shirt(row, "", highlight.get(pid, ""), images, labels, value_format)
            for pid, row in bench.iterrows()
        )
        markup += (
            f'<div class="bench-strip{size}"><div class="bench-cap">Bench, in order</div>'
            f'<div class="pitch-line">{strip}</div></div>'
        )

    st.markdown(markup, unsafe_allow_html=True)


def live_pitch(
    state: LiveGameweek,
    season: Season,
    lineup: Lineup,
    projections: pd.DataFrame,
    images: pd.DataFrame | None = None,
    vice_id: int | None = None,
) -> bool:
    """The live gameweek on a pitch, the way the points page on the FPL site is.

    False if none of the squad could be drawn, which happens only when every
    player has left the game.

    The shirts want a projections row for the name, club, price and fitness
    flag, and the live numbers ride on top of the two projection columns
    `formation_view` already reads. That is the same borrowing the elite
    template does, and it is what keeps one pitch renderer rather than three.

    Points rather than minutes on the left, because the left number is the one
    the eye lands on and this page is called live scoring. Minutes are the
    honest second number: nine points off twenty minutes and nine off ninety are
    different weeks, and the pitch would otherwise not say which you had.
    """
    seen = [i for i in (*lineup.starters, *lineup.bench) if i in projections.index]
    if not seen:
        return False

    numbers = player_view(state, season, seen)
    pitch = projections.loc[seen].assign(
        xpts_next=numbers["points"].astype(float),
        xpts_total=numbers["minutes"].astype(float),
    )
    # the captain's shirt carries what he actually counted for, since a doubled
    # haul is the difference between a good week and a bad one and a shirt
    # showing the undoubled figure would not add up to the total above it
    if lineup.captain in pitch.index:
        pitch.loc[lineup.captain, "xpts_next"] *= lineup.captain_multiplier

    xi = pitch.loc[[i for i in lineup.starters if i in pitch.index]]
    bench = pitch.loc[[i for i in lineup.bench if i in pitch.index]]
    formation_view(
        xi,
        bench,
        lineup.captain,
        vice_id,
        images=images,
        labels=("Pts", "Mins"),
        value_format=".0f",
    )
    return True


def your_lineup(squad: MySquad, current: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Your eleven and your bench, in your order, or None if that is not known.

    Only an entry publishes pick order and the armband. A squad file carries
    fifteen ids and says nothing about how they were arranged, so there is
    nothing to show and the caller falls back to the solved lineup.

    A squad with a player who has left the game cannot be split either. The
    first eleven ids are the eleven only while all fifteen are still there, and
    guessing which of the fourteen was benched would show a lineup that was
    never picked.
    """
    if squad.captain_id is None:
        return None
    held = [i for i in squad.player_ids if i in current.index]
    if len(held) != len(squad.player_ids):
        return None
    return current.loc[held[:11]], current.loc[held[11:]]


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


def compare_weeks(
    by_gameweek: pd.DataFrame, names: dict[int, str], events: list[int]
) -> pd.DataFrame:
    """Several players' runs on one frame, aligned gameweek by gameweek.

    `player_weeks` drops a gameweek its player's club does not play in, which is
    right for one run on its own and wrong beside another: the bars stop lining
    up under each other and a blank reads as a week nobody asked about. Filling
    the gap with a zero says what a blank actually scores.
    """
    frames = []
    for pid, name in names.items():
        weeks = player_weeks(by_gameweek, pid)
        run = pd.DataFrame({"event": events})
        run = (
            run.merge(weeks[["event", "xpts", "fixture"]], on="event", how="left")
            if not weeks.empty
            else run.assign(xpts=pd.NA, fixture=pd.NA)
        )
        run["xpts"] = run["xpts"].fillna(0.0).astype(float)
        run["fixture"] = run["fixture"].fillna("—")
        run["label"] = "GW" + run["event"].astype(str)
        run["name"] = name
        frames.append(run)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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


def compare_scale(names: list[str]) -> alt.Scale:
    """A colour per compared player, the same one in every chart below."""
    return alt.Scale(domain=names, range=list(COMPARE_COLOURS[: len(names)]))


def compare_panel(
    pool: pd.DataFrame,
    ids: list[int],
    by_gameweek: pd.DataFrame,
    labels: pd.DataFrame,
    difficulty: pd.DataFrame,
    horizon: int,
    images: pd.DataFrame | None = None,
) -> None:
    """Two to four players read against each other rather than one at a time.

    A ranked list answers "who is best" and the question people actually have is
    "which of these three", which the list can only answer by being read in
    several passes. The totals come first because that is what is being argued
    about, the runs second because that is usually what separates them, and the
    three terms last because that is what says whether two similar totals are
    the same bet.
    """
    picked = pool.loc[ids]
    picked = picked.assign(
        fitness=[availability(row)[1] or "No news" for _, row in picked.iterrows()]
    )
    names = [str(n) for n in picked["name"]]
    scale = compare_scale(names)

    table, colours, gw_cols = pool_frame(picked, labels, difficulty, COMPARE_COLUMNS, images)
    st.dataframe(
        table.style.apply(lambda _: colours, axis=None),
        hide_index=True,
        width="stretch",
        column_config=pool_column_config(
            horizon, gw_cols, float(max(picked["xpts_total"].max(), 1))
        ),
    )
    st.caption(
        "**FPL xPts** is Fantasy Premier League's own projection for the next gameweek. "
        "It is theirs, not ours, and it is not an input to ours, so the players where the "
        "two disagree are the ones worth a second look."
    )

    events = [int(c) for c in labels.columns]
    weeks = compare_weeks(by_gameweek, dict(zip(ids, names, strict=True)), events)
    if weeks.empty:
        empty_state(
            "No fixtures to compare",
            "None of these clubs has a fixture inside the horizon, so there is no run to put "
            "side by side. Widen the horizon in the sidebar.",
        )
    else:
        st.altair_chart(
            alt.Chart(weeks)
            .mark_bar(cornerRadiusEnd=3)
            .encode(
                x=alt.X("label:N", title=None, sort=[f"GW{e}" for e in events]),
                xOffset=alt.XOffset("name:N", sort=names),
                y=alt.Y("xpts:Q", title="Projected points"),
                color=alt.Color("name:N", title="Player", scale=scale),
                tooltip=[
                    alt.Tooltip("name:N", title="Player"),
                    alt.Tooltip("label:N", title="Gameweek"),
                    alt.Tooltip("fixture:N", title="Fixture"),
                    alt.Tooltip("xpts:Q", title="xPts", format=".2f"),
                ],
            )
            .properties(height=260)
        )
        st.caption(
            "Upper case is at home, lower case away, and a double gameweek shows both. A bar "
            "at zero is a blank rather than a week left out."
        )

    terms = picked[["name", "points_per_90", "minutes_share"]].assign(
        difficulty=difficulty.reindex(picked["team"]).set_axis(picked.index).mean(axis=1)
    )
    for column, (field, heading, axis) in zip(st.columns(3), TERM_CHARTS, strict=True):
        with column:
            st.caption(heading)
            st.altair_chart(
                alt.Chart(terms)
                .mark_bar(cornerRadiusEnd=3)
                .encode(
                    y=alt.Y("name:N", title=None, sort=names),
                    x=alt.X(f"{field}:Q", title=axis),
                    color=alt.Color("name:N", scale=scale, legend=None),
                    tooltip=[
                        alt.Tooltip("name:N", title="Player"),
                        alt.Tooltip(f"{field}:Q", title=heading, format=".2f"),
                    ],
                )
                .properties(height=150)
            )
    st.caption(
        "The three terms the projection multiplies together, each on its own axis because a "
        "normalised bar cannot say whether 0.6 of a match is a lot. Difficulty is FPL's own 1 "
        "to 5 rating averaged over the run, which is what the fixture term is built from "
        "rather than the term itself, and it is the one panel here where a shorter bar is "
        "the better one."
    )


def forget_entry() -> None:
    """Drop the remembered entry id, from the box and from the URL.

    A callback rather than an inline branch, because a widget's own key cannot
    be assigned once the widget has been drawn this run, and the button that
    calls this sits below the box it clears.
    """
    st.session_state["sidebar_entry"] = ""
    st.query_params.clear()


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
    load_captaincy.clear()
    load_field.clear()
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
    shape_choice = st.selectbox(
        "Formation",
        [BEST_SHAPE] + [format_formation(f) for f in FORMATIONS],
        help="The solver picks the shape it likes best unless you name one here. "
        "Pinning a shape changes which fifteen it buys, not just who starts, and "
        "the pitch says what the choice costs.",
    )
formation = parse_formation(None if shape_choice == BEST_SHAPE else shape_choice)

season = load_season(st.session_state.refresh_token)
stamp = season.data_stamp
images = player_images(season)
badges = club_badges(season)
projections, by_gameweek = load_projections(season, horizon, use_prior, stamp)
lookup = name_lookup(projections)
# Named once, so the three pitches and the caption under them cannot disagree
# about which span is which. Both follow the horizon slider.
span_labels = (f"GW{season.next_gameweek}", f"{horizon} GW")

st.sidebar.divider()
# the gameweek, player count and data age used to live here as a caption, and
# now sit in the status bar at the top of the page instead
if not use_prior and season.gameweeks_played == 0:
    st.sidebar.warning("No prior data and no gameweeks played. Projections are guesswork.")

# The entry id is remembered in the URL rather than anywhere on the server. The
# deploy is multi tenant and public, so there is nowhere per user to write, and a
# query parameter costs no dependency and makes the loaded app bookmarkable. An
# entry id is public anyway: it is the number in your own points page URL on the
# FPL site.
remembered = st.query_params.get("entry", "")

my_squad = None
with st.sidebar.expander("Your squad", expanded=True):
    st.caption("Loaded once here, and read by My squad, Planner, Chips and Live.")
    sources = ["Nothing loaded", "squad.json", "FPL entry id"]
    source = st.radio("Load from", sources, index=2 if remembered else 0, key="squad_source")

    if source == "squad.json":
        upload = st.file_uploader("squad.json", type="json")
        if upload:
            try:
                my_squad = parse_squad(json.loads(upload.getvalue()), season)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                st.error(f"Could not read that file: {exc}")
    elif source == "FPL entry id":
        # Seeded before the widget exists, which is the only point a widget's own
        # key may be assigned. A text box rather than a number, because an id is
        # not a quantity: steppers imply it can be nudged, and a box reading 0
        # implies something is already loaded.
        if "sidebar_entry" not in st.session_state:
            st.session_state["sidebar_entry"] = remembered
        typed = st.text_input(
            "Entry id",
            key="sidebar_entry",
            placeholder="e.g. 3921945",
            help="The number in the URL of your points page on the FPL site.",
        )
        st.caption("Only works once a deadline has passed.")

        entry_id = int(typed) if typed.strip().isdigit() else None
        if typed.strip() and entry_id is None:
            st.error("An entry id is just digits, the number in your points page URL.")

        paid = st.file_uploader(
            "Optional: squad.json, for what you paid",
            type="json",
            help="The entry publishes who you own and your bank, but never what you paid, "
            "which is what selling prices are worked out from.",
        )
        if entry_id:
            try:
                my_squad = load_from_entry(season, entry_id)
                if paid:
                    merge_prices(my_squad, parse_squad(json.loads(paid.getvalue())), season)
                else:
                    my_squad.resolve_selling_prices(season)
            except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                st.error(f"Could not load that entry: {exc}")
                my_squad = None

        # Only an id that actually loaded is worth putting in a URL, so this
        # sits after the load rather than beside the box.
        if my_squad is not None:
            st.query_params["entry"] = str(entry_id)
            st.caption("Bookmark this page and the id comes back with it.")
            st.button("Forget this id", on_click=forget_entry, width="stretch")

    if my_squad is not None:
        missing = my_squad.missing_from(projections)
        if missing:
            st.warning(f"{len(missing)} owned players are no longer in the game.")

# Off by default and read here rather than inside a tab, because both the
# Captain and the Leagues tab want it and the tab bodies run in source order:
# a toggle living in Leagues would not reach Captain until the rerun after it
# was clicked.
with st.sidebar.expander("The field", expanded=False):
    st.caption(
        "What the best managers own and captain. One request per manager, so it is off "
        "until asked for and cached hard once loaded."
    )
    field_on = st.toggle(
        "Read the top managers",
        value=False,
        help="Samples the overall league and counts their squads. Empty until the first "
        "deadline has passed, since picks are not published before then.",
    )
    field_sample = st.slider(
        "Managers to sample", 20, DEFAULT_SAMPLE, DEFAULT_SAMPLE, 20, disabled=not field_on
    )

field, field_asked, field_resolved = pd.DataFrame(), 0, 0
# `current_gameweek` rather than the finished count, because picks are published
# from the deadline. Gating on finished gameweeks leaves this empty for the whole
# of a live gameweek, which is exactly when it is worth reading.
if field_on and season.current_gameweek:
    field, field_asked, field_resolved = load_field(
        season, season.current_gameweek, field_sample, stamp
    )


# ----------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------
status_bar(season, len(projections))

(
    my_squad_tab,
    build_tab,
    players_tab,
    captain_tab,
    roi_tab,
    fixtures_tab,
    planner_tab,
    chips_tab,
    leagues_tab,
    live_tab,
) = st.tabs(
    # Each tab answers one question. My squad is what you own and the one move
    # this week, Wildcard is what you would buy starting over, Planner is the
    # route across several gameweeks. The single week answer lives on My squad
    # and nowhere else, so the two pages cannot disagree about the best move.
    #
    # Anything rendering a fixture run goes after Players, and Live goes last.
    # `tests/test_app.py` finds the player pool by looking for a dataframe with
    # a GW column, so a tab putting one ahead of Players would shadow the pool
    # and fail several tests confusingly. My squad sits ahead of Players and so
    # must keep GW columns off its table. Captain renders a run too, which is
    # why it sits where it does rather than beside the squad it is about.
    [
        "My squad",
        "Wildcard",
        "Players",
        "Captain",
        "ROI",
        "Fixtures",
        "Planner",
        "Chips",
        "Leagues",
        "Live",
    ]
)

with my_squad_tab:
    st.subheader("My squad")
    how_to_read(
        "The fifteen you actually own, laid out the way you have them set up, "
        "and the one move worth making this week.\n\n"
        "- **The pitch is your lineup, not the model's.** Where the two disagree "
        "the caption says what the difference is worth. Read that as an opinion "
        "rather than a correction: nothing here has seen a press conference or "
        "knows who you have a feeling about.\n"
        "- **Every gain is net of the four point hit** for going beyond your free "
        "transfers, so a positive number already pays for itself.\n"
        "- **Selling prices are not public.** FPL gives you back what you paid "
        "plus half of any rise, so a player who has gone up 0.4 sells for 0.2 "
        "more than you paid. The entry publishes who you own and your bank but "
        "never what you paid, so load a squad.json alongside it or the money "
        "here is optimistic.\n\n"
        "This answers the coming gameweek only. Several gameweeks solved as one "
        "problem, so a transfer can be held this week to afford someone next "
        "week, is the **Planner** tab."
    )

    if my_squad is None:
        empty_state(
            "No squad loaded",
            "Open the Your squad panel in the sidebar and give it your FPL entry id, "
            "which is the number in the URL of your points page on the FPL site. The id "
            "is kept in this page's address, so bookmarking the page saves typing it "
            "again.",
        )
    else:
        unpriced = my_squad.unpriced()
        if unpriced:
            st.warning(
                f"No purchase price for {len(unpriced)} of {len(my_squad.player_ids)} players, "
                "so they are valued at today's price. FPL pays back what you paid plus half "
                "of any rise, so this overstates what you can raise by selling them and the "
                "move below may not be affordable. Load a squad.json to fix it."
            )

        # squad.frame drops ids the projections no longer know about, which a
        # plain .loc would raise on once a player leaves the game mid-season
        current = my_squad.frame(projections)
        mine = your_lineup(my_squad, current)

        s1, s2, s3 = st.columns(3)
        s1.metric(
            "In the bank",
            f"{my_squad.bank:.1f}m",
            help="Straight off the entry. What is left over after the fifteen you own.",
        )
        s2.metric(
            "Team value",
            f"{my_squad.value_tenths(season) / 10:.1f}m",
            help="What the squad would raise if you sold it all, plus the bank. Selling "
            "prices are not public, so this is only exact with a squad.json loaded.",
        )
        s3.metric(
            "Free transfers",
            my_squad.free_transfers,
            help="Worked out from your transfer history, so it is an estimate rather than "
            "a reading. Correct it below if it is wrong.",
        )

        # the armband goes in the caption rather than a fourth metric, because a
        # metric is a box of a fixed width and most players are named too long
        # for it. The rest of the app already says "· captain X" this way.
        armband = ""
        if my_squad.captain_id is not None and my_squad.captain_id in current.index:
            title = "triple captain" if my_squad.captain_multiplier == 3 else "captain"
            armband = f" · {title} {current.loc[my_squad.captain_id, 'name']}"

        st.divider()
        st.caption(f"As you have it{armband}")
        # Filled in after the move below has been solved, because the shirts ring
        # whoever is leaving and that is not known until then. A slot rather than
        # putting the controls above the pitch, so the page still reads in the
        # order you want to think in: what you have, then what to do about it.
        lineup_slot = st.container()

        st.divider()
        st.caption("This week's move")
        t1, t2, t3 = st.columns(3)
        bank = t1.number_input("Bank (m)", 0.0, 20.0, my_squad.bank_tenths / 10, 0.1)
        free = t2.number_input("Free transfers", 0, 5, my_squad.free_transfers)
        max_moves = t3.number_input("Max transfers to consider", 1, 5, 2)

        plan_args = dict(
            current_ids=my_squad.player_ids,
            selling_prices=my_squad.selling_prices,
            bank_tenths=round(bank * 10),
            free_transfers=int(free),
            max_transfers=int(max_moves),
            bench_weight=bench_weight,
        )
        # Two rules here, both learned the hard way. No st.stop(), because this
        # is the first tab and stopping the script would blank every other one,
        # and an uploaded file nobody can plan out of is an ordinary thing to
        # hold. And `plan` is assigned last, because a squad with no legal
        # eleven in it gets past the transfer solve and falls over in `pick_xi`,
        # so binding the plan first leaves everything below reading half solved
        # state through an except branch that thought it had handled the failure.
        plan = best_xi = best_bench = best_captain = after_cost = None
        try:
            solved = suggest_transfers(projections, formation=formation, **plan_args)
            best_xi, best_bench, best_captain = pick_xi(current, formation=formation)
            after_cost = solved.projected - suggest_transfers(projections, **plan_args).projected
            plan = solved
        except RuntimeError as exc:
            # an illegal squad has no legal plan to reach, and anyone can upload
            # a hand-edited file, so this must not be a traceback
            st.error(
                f"{exc}. Check the squad is fifteen players, with no more than "
                f"{MAX_PER_CLUB} from any one club."
            )

        # Back to the top of the page, now that the move is known. Only one
        # "before" pitch, and it is your own lineup rather than a solved one, so
        # the players leaving are ringed on the eleven you actually picked.
        leaving = {} if plan is None else dict.fromkeys(plan.transfers_out.index, "out")
        with lineup_slot:
            if mine is None:
                st.info(
                    "No lineup to show, so the model's best eleven is here instead. A squad "
                    "file records who you own and nothing about how you lined them up, and a "
                    "squad with someone who has since left the game cannot be split into "
                    "eleven and four either. An entry id and a full fifteen gives you the "
                    "team you actually picked."
                )
                if best_xi is not None:
                    formation_view(
                        best_xi,
                        best_bench,
                        best_captain.name,
                        None,
                        highlight=leaving,
                        images=images,
                        labels=span_labels,
                    )
            else:
                your_xi, your_bench = mine
                formation_view(
                    your_xi,
                    your_bench,
                    my_squad.captain_id,
                    my_squad.vice_captain_id,
                    highlight=leaving,
                    images=images,
                    labels=span_labels,
                )
                # what your own selection gives up against the solved one. An
                # opinion worth one line, and only when there is a difference to
                # have an opinion about
                behind = (
                    0.0
                    if best_xi is None
                    else best_xi["xpts_total"].sum() - your_xi["xpts_total"].sum()
                )
                if behind > 0.05:
                    st.caption(
                        f"The model would start a different eleven, worth {behind:+.1f} pts "
                        f"more over {horizon} gameweeks. It cannot see a press conference, "
                        "so read that as an argument rather than an instruction."
                    )
            flag_legend()

        if plan is None:
            pass  # the error above already said why there is no move to show
        elif plan.transfers_in.empty:
            st.success("No move clears the cost of making it. Roll the transfer.")
        else:
            # solved against solved, so the gain is what the transfer is worth
            # rather than what the transfer plus a lineup change is worth
            gain = plan.xi["xpts_total"].sum() - best_xi["xpts_total"].sum() - 4 * plan.hits
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

            # One pitch, not a pair. The before is the lineup at the top of the
            # page with the outgoing players already ringed, and drawing a
            # solved "now" beside this would put three near identical pitches on
            # one page for the sake of a comparison the metric above already
            # makes.
            st.caption(f"After the move · captain {plan.captain['name']}")
            formation_view(
                plan.xi,
                plan.bench,
                plan.captain.name,
                plan.vice_captain.name,
                highlight={i: "in" for i in plan.transfers_in.index},
                images=images,
                labels=span_labels,
                cost=after_cost if formation else None,
            )

with build_tab:
    st.subheader("Build a squad from scratch")
    how_to_read(
        "The best fifteen the model can buy under the budget, the three per club "
        "cap and the position quotas, ignoring whatever you currently own. That "
        "makes it the wildcard page: a free hit or a fresh season is the only "
        "time you can act on it wholesale. To improve the squad you have, use "
        "**My squad**.\n\n"
        "It is a solved answer, not a sorted list: "
        "picking the best points per million one at a time is reliably a few "
        "points worse, because what binds is having enough cheap players to "
        "afford the expensive ones.\n\n"
        "- **Each card shows two projections.** The left is the next gameweek "
        "on its own, the right is the whole horizon. Both are labelled on the "
        "card, and they answer different questions: who to start and captain "
        "this week, against who is worth owning at all.\n"
        "- **Projected over the horizon** at the top is the eleven right hand "
        "figures added together, plus the captain's next gameweek once more. "
        "The armband is a weekly decision, so it counts for one gameweek rather "
        "than the whole run, and the bench is not in the number at all.\n"
        "- **Bench weight** in the sidebar decides how much the bench counts when "
        "choosing the fifteen. Low gives two cheap punts, high gives a squad you "
        "can rotate.\n"
        "- **Formation** in the sidebar is free by default, so the shape on the "
        "pitch is one the solver chose rather than one it was given. Pin a shape "
        "there and the caption says what pinning it cost.\n"
        "- **Coloured dots** flag injuries and doubts. Hover one to read it.\n\n"
        "Rotation, press conferences and minutes management are not in the API, "
        "so treat this as a shortlist to argue with."
    )
    left, right = st.columns(2)
    locked = left.multiselect("Must include", options=sorted(lookup), key="locks")
    banned = right.multiselect("Rule out", options=sorted(lookup), key="bans")

    result = shape_cost = None
    try:
        result = build_squad(
            projections,
            budget_tenths=round(budget * 10),
            bench_weight=bench_weight,
            include=[lookup[n] for n in locked],
            exclude=[lookup[n] for n in banned],
            formation=formation,
        )
        # what the pinned shape gave up. Solving free as well doubles the work
        # for this tab, which is under half a second, and is the only way to
        # know whether the override is costing anything
        shape_cost = (
            result.projected
            - build_squad(
                projections,
                budget_tenths=round(budget * 10),
                bench_weight=bench_weight,
                include=[lookup[n] for n in locked],
                exclude=[lookup[n] for n in banned],
            ).projected
            if formation
            else None
        )
    except RuntimeError as exc:
        # no st.stop(): this is the second of ten tabs, and locking six players
        # into a 90m budget would take the other eight down with it
        st.error(f"{exc}. Try relaxing the locks, raising the budget or freeing the formation.")
        result = shape_cost = None

    if result is not None:
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
            help="The starting eleven added up across the horizon, plus the "
            "captain's next gameweek once more. The armband is a weekly decision, "
            "so it is worth one gameweek here rather than six. The four on the "
            "bench are not counted at all, since they only score if someone ahead "
            "of them does not play.",
        )
        c.metric(
            "Captain",
            result.captain["name"],
            help="Whoever the solver expects most from over the horizon. His "
            "points double, so this is a bigger decision than any single transfer.",
        )

        formation_view(
            result.xi,
            result.bench,
            result.captain.name,
            result.vice_captain.name,
            images=images,
            labels=span_labels,
            cost=shape_cost,
        )
        st.caption(
            f"Each card carries both projections. **{span_labels[0]}** is the next "
            f"gameweek on its own, which is what a lineup or captain decision turns on. "
            f"**{span_labels[1]}** is the whole horizon added up, which is what decides "
            f"whether he is worth owning at all. The headline above is the eleven "
            f"**{span_labels[1]}** figures added together, plus the captain's "
            f"**{span_labels[0]}** once more for the armband, and no bench."
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
        "which.\n\n"
        "**Tick a row** to see the working behind that player. **Tick two to "
        "four** and they are read against each other instead: their numbers, "
        "their fixture runs and the three terms side by side, each player the "
        "same colour in every chart. Three players who all look plausible is "
        "the ordinary transfer question, and a ranked list can only answer it "
        "by being read several times over.\n\n"
        "Ticks reach the rows listed below, so a comparison across positions "
        "or price brackets needs the filters wide enough to show both players.\n\n"
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

    # above the empty guard because the comparison at the foot of the tab reads
    # the same runs, and it works off the whole pool rather than this view
    labels, difficulty = fixture_runs(season, horizon, stamp)

    # an empty filter result is an ordinary thing to do, not an error, so it
    # must not reach st.stop() and take every tab below this one down with it
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
            f"{sort_by.replace('_', ' ')}. Tick a row for the working behind the projection, "
            "or tick two or more to read them against each other."
        )
        ticked = pool_table(
            shown, labels, difficulty, columns, horizon, key="pool", images=images, multi=True
        )

        # Streamlit drops a dataframe selection whenever the data under it
        # changes, and the horizon slider changes every projection in the table.
        # Left alone that wipes a comparison halfway through the thought it
        # exists to support, since reading two players over three gameweeks and
        # then over six is the point of the slider. So the picks are held here
        # by id, and the table only gets to speak when it is showing the same
        # rows it was showing last run.
        listed = tuple(shown.index)
        if st.session_state.get("pool_rows") != listed:
            # the rows moved, so an empty selection is Streamlit clearing up
            # rather than anyone unticking anything
            st.session_state["pool_rows"] = listed
            picked = [i for i in st.session_state.get("compared", []) if i in view.index]
        else:
            picked = ticked
        st.session_state["compared"] = picked

        if picked and not ticked:
            held = ", ".join(str(view.loc[i, "name"]) for i in picked)
            st.caption(f"Still reading {held}. The ticks cleared when the table changed.")
            spare, _ = st.columns([1, 4])
            if spare.button("Clear", key="clear_compared", width="stretch"):
                st.session_state["compared"] = []
                st.rerun()

        if len(picked) > COMPARE_MAX:
            st.info(
                f"Reading the first {COMPARE_MAX}. Past that the grouped bars below stop being "
                "separable and the table scrolls sideways on anything smaller than a laptop."
            )
            picked = picked[:COMPARE_MAX]

        if not picked:
            st.caption("Nothing ticked.")
        elif len(picked) == 1:
            player_working(
                view.loc[picked[0]], player_weeks(by_gameweek, picked[0]), horizon, images=images
            )
        else:
            # `pool` rather than `view`, so a compared player carries his whole
            # row even when the filters above are hiding most of the columns
            compare_panel(pool, picked, by_gameweek, labels, difficulty, horizon, images=images)

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

with captain_tab:
    st.subheader("Captain")
    how_to_read(
        "**The armband doubles a score, so if you are after points the answer "
        "is the top of the xPts column.** That is why this table is ranked on "
        "the projection rather than on the two chances beside it. No "
        "distribution is needed to pick a captain, and a table sorted by haul "
        "chance would be claiming otherwise.\n\n"
        "The chances are here for the three things the average cannot say:\n\n"
        "- **You are usually chasing a rank, not a points total.** Against a "
        "rival what matters is the chance you finish above him. Behind, the "
        "volatile captain is right even at slightly lower expected points. "
        "Ahead, the steady one is.\n"
        "- **Triple Captain is a one-shot**, so you cannot average over many "
        "weeks and the ceiling matters more than it does for a decision you "
        "make every week. The Chips tab prices the week, this says who.\n"
        "- **If most of the field captains the same player**, captaining him "
        "barely moves your rank whatever he scores. This app does not know "
        "what the field is doing, so read the owned column beside these.\n\n"
        f"- **{HAUL_POINTS}+ pts** is the chance of a haul: goals, assists and "
        "appearance points reaching double figures, counting both matches if "
        "his club has two that week.\n"
        "- **Any return** is the chance of at least one goal or assist. Read it "
        "as the floor under the ceiling beside it.\n"
        "- **The scatter** shows where the two disagree. Players above the "
        "trend have a ceiling their average is hiding, which is what makes "
        "them worth a second look when you need variance.\n\n"
        "Three things are deliberately not in these two numbers, and they "
        "matter:\n\n"
        "- **Bonus**, which the model has no term for anywhere. A real ten "
        "point week for a forward is a goal, an assist and three bonus. Here "
        "that is nine and does not clear the bar, so the haul chance sits "
        "below the real thing rather than at it.\n"
        "- **Clean sheets, saves and defensive contributions.** So for a "
        "keeper or a defender this is not a haul chance at all, only an "
        "attacking one, which is why the filter starts on midfielders and "
        "forwards.\n"
        "- **Any agreement with the xPts column.** That figure blends what a "
        "player has been scoring against what he is expected to do, and this "
        "uses the expected side at full weight. They will not add up, and "
        "making them add up would mean throwing away one of the two."
    )

    events = sorted(int(e) for e in by_gameweek["event"].dropna().unique())
    if not events:
        empty_state(
            "No fixtures to captain into",
            "There is no gameweek in the horizon to put a distribution on. Widen the "
            "horizon in the sidebar, or wait for the fixtures to be published.",
        )
    else:
        picked_gw = st.selectbox(
            "Gameweek", events, format_func=lambda e: f"GW{e}", key="captain_gw"
        )
        haul = load_captaincy(season, projections, by_gameweek, picked_gw, stamp)

    if events and haul.empty:
        empty_state(
            "Not enough of this season yet",
            "These are built from this season's expected goals and assists, and no "
            "gameweek has been finished yet. Before the first deadline the API is "
            "still serving last season's figures under the same field names, so this "
            "stays empty on purpose rather than handing you a year old number that "
            "looks current. It fills in as soon as a gameweek has been scored, and "
            "sharpens over the following month as players build up minutes.",
        )
    elif events:
        wanted = st.multiselect(
            "Positions",
            ["GKP", "DEF", "MID", "FWD"],
            default=["MID", "FWD"],
            help="Defenders and keepers score most of their points from clean sheets, "
            "which is not in these two numbers.",
            key="captain_positions",
        )
        view = haul[haul["position"].isin(wanted or ["GKP", "DEF", "MID", "FWD"])]

        if view.empty:
            empty_state(
                "Nobody in those positions",
                "No player in the positions chosen has played a minute of this season, "
                "so there is nothing to put a distribution on. Add a position back to "
                "see the rest.",
            )
        else:
            labels, difficulty = fixture_runs(season, horizon, stamp)
            shown = projections.loc[view.index].join(
                view[["xpts_gw", "haul_chance", "return_chance"]]
            )
            captain_columns = [
                "price",
                "xpts_gw",
                "haul_chance",
                "return_chance",
                "credibility",
                "ownership",
            ]
            field_note = ""
            if not field.empty:
                # a player nobody in the sample owns was captained by nobody in
                # it either, which is a zero rather than a blank
                shown = shown.join(field[["captain_share", "effective_ownership"]]).fillna(
                    {"captain_share": 0.0, "effective_ownership": 0.0}
                )
                captain_columns += ["captain_share", "effective_ownership"]
                field_note = (
                    f" The last two columns are what {field_resolved} of the best managers "
                    "did with the armband last gameweek, not what they will do with it "
                    "next."
                )
            st.caption(
                f"Top {min(len(shown), 40)} of {len(shown)} by projected points for "
                f"GW{picked_gw}, which is the captaincy answer if you are after points. "
                f"Both chances are goals and assists only.{field_note}"
            )
            pool_table(
                shown.head(40),
                labels,
                difficulty,
                captain_columns,
                horizon,
                key="captain_pool",
                images=images,
            )

            st.divider()
            st.caption(
                "Projected points against the chance of a haul. Along the line the two "
                "agree; above it is a player whose ceiling the average is hiding."
            )
            plot = shown.head(60).reset_index()
            st.altair_chart(
                alt.Chart(plot)
                .mark_circle(size=110, opacity=0.75)
                .encode(
                    x=alt.X("xpts_gw:Q", title=f"Projected points, GW{picked_gw}"),
                    y=alt.Y(
                        "haul_chance:Q",
                        title=f"Chance of {HAUL_POINTS}+",
                        axis=alt.Axis(format="%"),
                    ),
                    color=alt.Color("position:N", title="Position"),
                    tooltip=[
                        alt.Tooltip("name:N", title="Player"),
                        alt.Tooltip("club:N", title="Club"),
                        alt.Tooltip("xpts_gw:Q", title="xPts", format=".2f"),
                        alt.Tooltip("haul_chance:Q", title=f"{HAUL_POINTS}+", format=".1%"),
                        alt.Tooltip("return_chance:Q", title="Any return", format=".1%"),
                    ],
                )
                .interactive()
            )

            best = list(shown.head(3).index)
            spread = points_pmf(season, projections, by_gameweek, best, event=picked_gw)
            if not spread.empty:
                st.caption(
                    "The distribution behind the top three, one bar per points total. "
                    "The tail on the right is what the armband is actually buying."
                )
                # `player` rather than `name`, which the comparison charts on the
                # Players tab colour by and which `tests/test_app.py` counts
                # across every chart on the page
                spread = spread.assign(player=spread["id"].map(projections["name"]))
                st.altair_chart(
                    alt.Chart(spread)
                    .mark_bar(opacity=0.7)
                    .encode(
                        x=alt.X("points:Q", title="Points"),
                        y=alt.Y("probability:Q", title="Chance", axis=alt.Axis(format="%")),
                        color=alt.Color("player:N", title="Player"),
                    )
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
    st.divider()
    st.caption("Club form, last five played")
    st.caption(
        "What each club has actually scored and conceded, with the scorelines behind it. "
        "It is a record and it feeds nothing: the difficulty above already blends FPL's "
        "attack and defence ratings, which move during the season off these same results, "
        "so putting them into the projection as well would mostly count them twice."
    )
    form_window = st.slider("Matches to look back over", 3, 10, 5, key="form_window")
    club_form = load_club_form(season, form_window, stamp)

    if club_form.empty:
        empty_state(
            "Nothing played yet",
            "Club form is built from finished matches, and none have been played. This "
            "fills in from the first gameweek.",
        )
    else:
        st.dataframe(
            with_badges(club_form, badges),
            hide_index=True,
            width="stretch",
            column_config={
                "badge": st.column_config.ImageColumn("", width="small"),
                "club": st.column_config.TextColumn("Club", pinned=True),
                "games": st.column_config.NumberColumn("Played", format="%d"),
                "scored": st.column_config.NumberColumn("For", format="%d"),
                "conceded": st.column_config.NumberColumn("Against", format="%d"),
                "scored_per_game": st.column_config.NumberColumn("For per game", format="%.2f"),
                "conceded_per_game": st.column_config.NumberColumn(
                    "Against per game", format="%.2f"
                ),
                "goal_difference": st.column_config.NumberColumn("GD", format="%+d"),
                "results": st.column_config.ListColumn("Matches", width="large"),
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

with planner_tab:
    st.subheader("Multi week planner")
    how_to_read(
        "The route across the next few gameweeks, solved as one problem rather "
        "than one week at a time. That is what lets it hold a transfer this week "
        "to afford someone next week, and solving each week on its own can never "
        "do it.\n\n"
        "Every gain here is **net of the four point hit** for going beyond your "
        "free transfers, so a positive number already pays for itself.\n\n"
        "**Selling prices matter and are not public.** FPL gives you back what "
        "you paid plus half of any rise, so a player who has gone up 0.4 sells "
        "for 0.2 more than you paid. Load a squad.json with your purchase prices "
        "or the planner will think you have more money than you do, and the "
        "further ahead it plans the worse that gets.\n\n"
        "The single move for the coming gameweek is on **My squad**. This tab is "
        "for the route rather than the next step."
    )

    if my_squad is None:
        empty_state(
            "No squad loaded",
            "A route has to start from somewhere. Open the Your squad panel in the "
            "sidebar and give it your FPL entry id, then come back.",
        )
    else:
        unpriced = my_squad.unpriced()
        if unpriced:
            st.warning(
                f"No purchase price for {len(unpriced)} of {len(my_squad.player_ids)} players, "
                "so they are valued at today's price. That overstates what you can raise by "
                "selling them, and a route spends that money several times over. Load a "
                "squad.json to fix it."
            )

        max_weeks = min(MAX_PLAN_WEEKS, horizon)
        if max_weeks < 2:
            st.info(
                "Widen the projection horizon in the sidebar. There is no route to plan "
                "across a single gameweek, and My squad already answers that one."
            )
        else:
            p1, p2 = st.columns(2)
            plan_weeks = p1.slider("Gameweeks to plan", 2, max_weeks, min(3, max_weeks))
            per_week = p2.slider("Transfers per week at most", 1, 2, 1)
            p3, p4 = st.columns(2)
            plan_bank = p3.number_input(
                "Bank (m)", 0.0, 20.0, my_squad.bank_tenths / 10, 0.1, key="planner_bank"
            )
            plan_free = p4.number_input(
                "Free transfers", 0, 5, my_squad.free_transfers, key="planner_free"
            )

            route = None
            try:
                with st.spinner("Planning"):
                    route = plan_transfers(
                        projections,
                        by_gameweek,
                        current_ids=my_squad.player_ids,
                        selling_prices=my_squad.selling_prices,
                        bank_tenths=round(plan_bank * 10),
                        free_transfers=int(plan_free),
                        weeks=int(plan_weeks),
                        max_transfers_per_week=int(per_week),
                        bench_weight=bench_weight,
                        formation=formation,
                    )
            except (RuntimeError, ValueError) as exc:
                # no st.stop(), which would take Chips, Leagues and Live down
                # with it. An unplannable squad is an ordinary thing to hold.
                st.error(f"Could not plan those gameweeks: {exc}")

            if route is not None:
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
                            week.transfers_out.iterrows(),
                            week.transfers_in.iterrows(),
                            strict=False,
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
        # A chip is priced against what the squad scores anyway, and a squad
        # with no legal eleven in it has no anyway. Anyone can upload one, and
        # until My squad stopped calling st.stop() on the same squad this tab
        # was never reached to find out.
        try:
            table = cached_chips(projections, by_gameweek, tuple(my_squad.player_ids), team_value)
        except RuntimeError as exc:
            st.error(f"{exc}. There is nothing to price a chip against.")
            table = None

        if table is None:
            pass  # the error above is the whole story
        elif table.empty:
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

    # This box follows the squad loaded in the sidebar, and a widget's `value`
    # is only its default on the first render, so following has to be done by
    # writing the key. Without it, loading one id and then another leaves this
    # tab reporting on the first, which reads as the app being wrong about who
    # you are rather than as a stale default.
    #
    # It follows `entry_id` off the loaded squad rather than the sidebar text,
    # so a half typed or wrong id does not drag this tab along with it. Typing
    # someone else in here still sticks, since only a change of loaded squad
    # writes the key, and that is the point of the box: the tab is for any
    # public manager, not only for you.
    loaded_entry = my_squad.entry_id if my_squad is not None else None
    if st.session_state.get("league_follows") != loaded_entry:
        st.session_state["league_follows"] = loaded_entry
        if loaded_entry:
            st.session_state["league_entry"] = int(loaded_entry)

    entry = st.number_input(
        "Manager entry id",
        min_value=0,
        step=1,
        value=0,
        key="league_entry",
        help="Starts as whoever is loaded in the sidebar. Change it to read any other "
        "manager, and it stays changed until you load a different squad.",
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
        st.caption("Where they sit in each of their leagues")
        if joined.empty:
            st.info("No classic leagues on this entry.")
        else:
            ranks = joined.assign(
                kind=joined["system"].map({True: "Automatic", False: "Joined"}),
            )
            st.dataframe(
                ranks[["name", "kind", "rank"]],
                hide_index=True,
                width="stretch",
                column_config={
                    "name": "League",
                    "kind": st.column_config.TextColumn("Kind", width="small"),
                    "rank": st.column_config.NumberColumn(
                        "Their rank",
                        format="%d",
                        help="Where they stand in that league. Empty until the first "
                        "gameweek has been scored, since nobody has a rank before then.",
                    ),
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
                month_events = season.gameweeks_in_month()
                month_name = f"{pd.Timestamp.now(tz='UTC'):%B}"
                # One request per manager in the league, so it is asked for
                # rather than assumed, the same bargain The field makes.
                show_month = st.checkbox(
                    f"Add {month_name} points and rank",
                    key="league_month",
                    disabled=not month_events,
                    help="Reads every manager's history to add up the month, which is one "
                    "request each and the slowest thing on this tab."
                    if month_events
                    else "No gameweek deadlines fall in this month, so there is no month "
                    "to add up.",
                )
                if show_month and month_events:
                    with st.spinner(f"Adding up {month_name}"):
                        table = cached_month(season, table, tuple(month_events), stamp)

                st.caption(f"{info['name']}, page {info['page']}")
                if show_month and month_events:
                    span = (
                        f"GW{month_events[0]}"
                        if len(month_events) == 1
                        else f"GW{month_events[0]} to GW{month_events[-1]}"
                    )
                    st.caption(
                        f"{month_name} is {span}, placed by deadline, so a gameweek is "
                        "whole to one month. The month rank is inside this league only."
                    )
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
                        "month_points": st.column_config.NumberColumn(
                            f"{month_name} pts",
                            format="%d",
                            help="Points scored across every gameweek whose deadline falls "
                            "in this month, hits included. Empty for anyone whose history "
                            "could not be read.",
                        ),
                        "month_rank": st.column_config.NumberColumn(
                            f"{month_name} rank",
                            format="%d",
                            help="Their place in this league over the month alone, worked "
                            "out here since FPL publishes no monthly rank.",
                        ),
                        "entry_id": st.column_config.NumberColumn("Entry", format="%d"),
                        "last_rank": None,
                    },
                )
                if info["has_next"]:
                    st.caption("Showing the first fifty. Later pages are not loaded.")

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
    st.caption("The elite template")

    if not field_on:
        st.info(
            "Turn on **The field** in the sidebar to read what the best managers own "
            "and captain. It is off by default because it is one request per manager."
        )
    elif season.current_gameweek == 0:
        empty_state(
            "No squads to read yet",
            "Picks are published once a deadline has passed, so before the first one the "
            "overall league has no table and every squad is a 404. This fills in as soon "
            "as the season starts.",
        )
    elif field.empty:
        empty_state(
            "Nobody's picks came back",
            f"The overall league was asked for {field_asked or field_sample} managers and "
            "none of their squads could be read. That is usually the API rather than the "
            "ids, so it is worth trying again.",
        )
    else:
        st.caption(
            f"{field_resolved} of the top {field_asked} managers overall, as they lined up "
            f"in GW{season.current_gameweek}. That is a small sample, they are the top of a "
            "table that rewards having been right rather than being right next week, and "
            "this is the gameweek under way rather than the one being planned. Read it to "
            "know what you are up against, not to copy it."
        )

        template, template_bench = template_xi(field, season)
        # `formation_view` labels two numbers per shirt and takes them from the
        # two projection columns, so the template borrows those names to show
        # ownership instead. The labels are what the reader actually sees.
        pitch = projections.loc[template.index].assign(
            xpts_next=template["elite_start_share"] * 100,
            xpts_total=template["elite_ownership"] * 100,
        )
        pitch_bench = projections.loc[template_bench.index].assign(
            xpts_next=template_bench["elite_start_share"] * 100,
            xpts_total=template_bench["elite_ownership"] * 100,
        )
        armband = field["captain_share"].idxmax()
        formation_view(
            pitch,
            pitch_bench,
            armband,
            None,
            images=images,
            labels=("Start %", "Own %"),
        )
        st.caption(
            "The armband marks whoever the sample captained most. The bench here is the "
            "four they own and start least, not an order anybody chose."
        )

        st.divider()
        st.caption("Where the top of the table disagrees with everyone else")
        divergence = (
            projections[["name", "position", "club", "price", "ownership", "xpts_total"]]
            .join(field, how="inner")
            .assign(elite_gap=lambda f: f["elite_ownership"] * 100 - f["ownership"])
        )
        biggest = pd.concat(
            [
                divergence.nlargest(10, "elite_gap"),
                divergence.nsmallest(10, "elite_gap"),
            ]
        ).sort_values("elite_gap", ascending=False)
        st.dataframe(
            with_badges(biggest.reset_index(), badges),
            hide_index=True,
            width="stretch",
            column_config={
                "badge": st.column_config.ImageColumn("", width="small"),
                "name": st.column_config.TextColumn("Player", pinned=True),
                "position": st.column_config.TextColumn("Pos", width="small"),
                "club": st.column_config.TextColumn("Club", width="small"),
                "price": st.column_config.NumberColumn("Price", format="%.1f"),
                "ownership": st.column_config.NumberColumn(
                    "Everyone %", format="%.1f", help=GLOSSARY["ownership"]
                ),
                "elite_ownership": st.column_config.NumberColumn(
                    "Top 100 %", format="percent", help=GLOSSARY["elite_ownership"]
                ),
                "elite_gap": st.column_config.NumberColumn(
                    "Gap",
                    format="%+.1f",
                    help="Top managers' ownership minus everyone's, in percentage points. "
                    "Positive is a player the best are on and the field is not.",
                ),
                "captain_share": st.column_config.NumberColumn(
                    "Captained by", format="percent", help=GLOSSARY["captain_share"]
                ),
                "effective_ownership": st.column_config.NumberColumn(
                    "Effective own.", format="%.2f", help=GLOSSARY["effective_ownership"]
                ),
                "xpts_total": st.column_config.NumberColumn(
                    f"xPts {horizon} GW", format="%.1f", help=GLOSSARY["xpts_total"]
                ),
                "id": None,
                "elite_start_share": None,
            },
        )
        st.caption(
            "Ten each way. A positive gap is somebody the best managers are on and the "
            "rest of the game is not, which is a shortlist and not a verdict: they bought "
            "him at some point in the past and our own projection for him is in the last "
            "column to argue with."
        )


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

            # The pitch first, because a lineup is a spatial thing and the
            # points page on the FPL site is the shape everyone already reads a
            # live gameweek in. The numbers behind each shirt are the same ones
            # the tables below carry, so the tables stay for anyone who wants to
            # sort by BPS rather than look at a formation.
            drawn = live_pitch(
                state,
                season,
                score.lineup,
                projections,
                images,
                vice_id=my_squad.vice_captain_id,
            )
            if not drawn:
                st.warning("None of this squad is in the current player list, so no pitch.")

            with st.expander("Every number behind those shirts"):
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
