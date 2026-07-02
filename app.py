
# -*- coding: utf-8 -*-
"""
NFL PROP ENGINE — Railway / Streamlit ready
Built from the MLB engine structure: clean UI, player cards, projections, pure upside,
alt ladder, CLV, before/after save, grading, learning dashboard.

This app is safe to run before NFL props are live. It attempts live Underdog lines first;
when no NFL prop feed is available, it shows clearly labeled preseason/demo examples so
the UI and workflow can be tested without confusing them as real bets.
"""

import os, json, math, time, difflib, unicodedata, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

APP_VERSION = "NFL v2.8 — BAYESIAN MARKOV + ADVANCED SIM ASSIST + XGB"
LOCAL_DIR = Path(os.getenv("STORAGE_DIR", "nfl_engine"))
LOCAL_DIR.mkdir(parents=True, exist_ok=True)

PICK_LOG = LOCAL_DIR / "nfl_before_snapshots.json"
AFTER_LOG = LOCAL_DIR / "nfl_after_snapshots.json"
RESULT_LOG = LOCAL_DIR / "nfl_results.json"
LEARN_FILE = LOCAL_DIR / "nfl_learning.json"
CLV_FILE = LOCAL_DIR / "nfl_clv_tracker.json"
LINE_HISTORY_FILE = LOCAL_DIR / "nfl_line_history.json"
REQUEST_LOG = LOCAL_DIR / "request_log.json"
BOARD_CACHE_FILE = LOCAL_DIR / "nfl_last_pulled_board.json"
MONEYLINE_CACHE_FILE = LOCAL_DIR / "nfl_last_pulled_moneylines.json"
USAGE_FILE = LOCAL_DIR / "nfl_player_usage.csv"
TEAM_CONTEXT_FILE = LOCAL_DIR / "nfl_team_context.json"
INJURY_FILE = LOCAL_DIR / "nfl_injuries.json"

# Phase 6 database outputs. These are built from last-season NFL data and then reused
# by the projection engine the same way the MLB app reuses saved pitcher/team context.
PHASE6_DIR = LOCAL_DIR / "phase6_nfl_database"
PHASE6_DIR.mkdir(parents=True, exist_ok=True)
PHASE6_PLAYER_LOG_FILE = PHASE6_DIR / "nfl_player_logs_last_season.csv"
PHASE6_PLAYER_SUMMARY_FILE = PHASE6_DIR / "nfl_player_summary_last_season.csv"
PHASE6_TEAM_CONTEXT_FILE = PHASE6_DIR / "nfl_team_context_last_season.json"
PHASE6_DEFENSE_RANK_FILE = PHASE6_DIR / "nfl_defense_ranks_last_season.csv"
PHASE6_TRAVEL_FILE = PHASE6_DIR / "nfl_travel_stadium_context.csv"

PHASE6_RAW_DIR = PHASE6_DIR / "_raw_cache"
PHASE6_RAW_DIR.mkdir(parents=True, exist_ok=True)
PHASE6_MANIFEST_FILE = PHASE6_DIR / "phase6_manifest.json"
PHASE6_TEAM_ADVANCED_FILE = PHASE6_DIR / "nfl_team_advanced_last_season.csv"
PHASE6_TRENCH_FILE = PHASE6_DIR / "nfl_trench_context_last_season.csv"
PHASE6_RED_ZONE_FILE = PHASE6_DIR / "nfl_red_zone_usage_last_season.csv"
PHASE6_OT_FILE = PHASE6_DIR / "nfl_overtime_context_last_season.csv"

NFL_LAST_SEASON = int(os.getenv("NFL_LAST_SEASON", "2025"))

UNDERDOG_URLS = [
    "https://api.underdogfantasy.com/beta/v6/over_under_lines",
    "https://api.underdogfantasy.com/beta/v5/over_under_lines",
    "https://api.underdogfantasy.com/beta/v4/over_under_lines",
    "https://api.underdogfantasy.com/beta/v3/over_under_lines",
    "https://api.underdogfantasy.com/beta/v2/over_under_lines",
    "https://api.underdogfantasy.com/v1/over_under_lines",
]

# Underdog labels vary by season/API version. Keep aliases broad, then hard-filter to NFL.
NFL_PROP_ALIASES = {
    "Passing Yards": ["passing yards", "pass yards", "pass yds", "qb passing yards"],
    "Passing TDs": ["passing tds", "passing touchdowns", "pass tds", "pass touchdowns"],
    "Interceptions": ["interceptions", "passing interceptions", "ints", "qb interceptions"],
    "Rushing Yards": ["rushing yards", "rush yards", "rush yds"],
    "Receiving Yards": ["receiving yards", "rec yards", "receiving yds"],
    "Receptions": ["receptions", "rec", "catches"],
    "Fantasy Points": ["fantasy points", "fantasy score"],
    "Anytime TD": ["anytime td", "anytime touchdown", "td scorer", "touchdown scorer"],
    "Pass Attempts": ["pass attempts", "passing attempts", "attempted passes", "qb attempts"],
    "Completions": ["completions", "passing completions", "completed passes"],
    "Rush Attempts": ["rush attempts", "rushing attempts", "carries", "rushing attempts +"],
    "Longest Reception": ["longest reception", "longest catch", "long reception"],
    "Longest Rush": ["longest rush", "longest carry", "long rush"],
    "Kicking Points": ["kicking points", "kicker points"],
    "Field Goals Made": ["field goals made", "fg made", "made field goals"],
    "Tackles + Assists": ["tackles + assists", "tackles and assists", "combined tackles", "tackles assists"],
    "Sacks": ["sacks", "player sacks", "defensive sacks"],
}
NFL_SPORT_TERMS = ["nfl", "football", "national football", "nfl_", "american football"]
NON_NFL_BLOCK_TERMS = ["mlb", "baseball", "nba", "wnba", "basketball", "nhl", "hockey", "soccer", "tennis", "golf", "mma", "ufc"]

PROP_CONFIG = {
    "Passing Yards": {"stat": "pass_yds", "sigma": 42, "base": 235, "volume_key": "pass_attempts"},
    "Passing TDs": {"stat": "pass_tds", "sigma": 0.85, "base": 1.55, "volume_key": "pass_attempts"},
    "Interceptions": {"stat": "interceptions", "sigma": 0.65, "base": 0.72, "volume_key": "pass_attempts"},
    "Rushing Yards": {"stat": "rush_yds", "sigma": 24, "base": 49, "volume_key": "carries"},
    "Receiving Yards": {"stat": "rec_yds", "sigma": 27, "base": 52, "volume_key": "routes"},
    "Receptions": {"stat": "receptions", "sigma": 1.9, "base": 4.3, "volume_key": "targets"},
    "Fantasy Points": {"stat": "fantasy_pts", "sigma": 6.5, "base": 14.2, "volume_key": "usage"},
    "Anytime TD": {"stat": "anytime_td", "sigma": 0.28, "base": 0.34, "volume_key": "red_zone"},
    "Pass Attempts": {"stat": "pass_attempts", "sigma": 5.8, "base": 33.5, "volume_key": "pass_attempts"},
    "Completions": {"stat": "completions", "sigma": 4.8, "base": 21.8, "volume_key": "pass_attempts"},
    "Rush Attempts": {"stat": "rush_attempts", "sigma": 4.2, "base": 13.5, "volume_key": "carries"},
    "Longest Reception": {"stat": "longest_rec", "sigma": 7.5, "base": 22.5, "volume_key": "air_yards"},
    "Longest Rush": {"stat": "longest_rush", "sigma": 6.8, "base": 15.5, "volume_key": "carries"},
    "Kicking Points": {"stat": "kicking_points", "sigma": 3.1, "base": 7.4, "volume_key": "team_total"},
    "Field Goals Made": {"stat": "fg_made", "sigma": 1.05, "base": 1.7, "volume_key": "team_total"},
    "Tackles + Assists": {"stat": "tackles_ast", "sigma": 2.4, "base": 6.6, "volume_key": "def_snaps"},
    "Sacks": {"stat": "sacks", "sigma": 0.55, "base": 0.45, "volume_key": "pass_rush"},
}

# ---------- MLB-style strictness gates ported to NFL ----------
# These do not change the raw projection. They decide what becomes an official/watch play.
MIN_NFL_BETTABLE_PROB = 0.62
MIN_NFL_ELITE_PROB = 0.68
MIN_NFL_DATA_SCORE = 82
MIN_NFL_ELITE_SCORE = 90
MIN_NFL_EDGE_UNITS = {
    "Passing Yards": 18.0,
    "Rushing Yards": 9.0,
    "Receiving Yards": 10.0,
    "Receptions": 0.85,
    "Fantasy Points": 2.5,
    "Passing TDs": 0.35,
    "Interceptions": 0.25,
    "Anytime TD": 0.14,
    "Pass Attempts": 4.0,
    "Completions": 3.0,
    "Rush Attempts": 2.5,
    "Longest Reception": 5.0,
    "Longest Rush": 4.5,
    "Kicking Points": 2.0,
    "Field Goals Made": 0.75,
    "Tackles + Assists": 1.5,
    "Sacks": 0.25,
}
MAX_RECOMMENDED_KELLY = 0.02
NFL_CALIBRATION_MIN_SAMPLES = 10
NFL_CALIBRATION_MAX_SHIFT_PCT = 0.06
NFL_PROJECTION_STABILITY_MIN = 55
NFL_VOLATILITY_TAX_HIGH = 10
NFL_VOLATILITY_TAX_MED = 4

# ---------- Full NFL data modules ----------
# These files are optional. The app runs without them, but if you add them later,
# they immediately override the preseason role defaults.
# nfl_player_usage.csv supported columns:
# player,team,position,snap_share,route_participation,target_share,air_yards_share,red_zone_touch_share,
# targets_pg,receptions_pg,rush_attempts_pg,carries_share,pass_attempts_pg,pressure_rate,ol_rank,
# injury_status,def_role_rank,coverage_grade,matchup_role,weather_risk
# nfl_team_context.json supported keys by team abbreviation:
# {"KC":{"pace":54,"pass_rate":61,"plays_pg":64,"spread":-3.5,"game_total":48.5,"def_pass_rank":12,"def_run_rank":8}}
USAGE_FIELDS = [
    "snap_share","route_participation","target_share","air_yards_share","red_zone_touch_share",
    "targets_pg","receptions_pg","rush_attempts_pg","carries_share","pass_attempts_pg",
    "pressure_rate","ol_rank","def_role_rank","coverage_grade","weather_risk"
]
ROLE_SAFETY_MINIMUMS = {
    "Receiving Yards": {"snap_share":62, "route_participation":68, "target_share":14},
    "Receptions": {"snap_share":60, "route_participation":66, "target_share":16},
    "Rushing Yards": {"snap_share":42, "carries_share":36},
    "Passing Yards": {"snap_share":98, "pass_attempts_pg":27},
    "Fantasy Points": {"snap_share":58},
    "Anytime TD": {"red_zone_touch_share":10},
    "Pass Attempts": {"snap_share":98, "pass_attempts_pg":27},
    "Completions": {"snap_share":98, "pass_attempts_pg":27},
    "Rush Attempts": {"snap_share":42, "rush_attempts_pg":7},
    "Longest Reception": {"snap_share":55, "route_participation":62, "air_yards_share":12},
    "Longest Rush": {"snap_share":35, "rush_attempts_pg":5},
    "Kicking Points": {},
    "Field Goals Made": {},
    "Tackles + Assists": {"snap_share":60},
    "Sacks": {"snap_share":48, "pressure_rate":8},
}

# Offensive/Underdog-focused board. Defensive props were intentionally removed
# because tackles/sacks are less stable and do not fit the MLB-style projection workflow.
EXCLUDED_NFL_PROPS = {"Tackles + Assists", "Sacks"}
for _excluded_prop in list(EXCLUDED_NFL_PROPS):
    NFL_PROP_ALIASES.pop(_excluded_prop, None)
    PROP_CONFIG.pop(_excluded_prop, None)
    MIN_NFL_EDGE_UNITS.pop(_excluded_prop, None)
    ROLE_SAFETY_MINIMUMS.pop(_excluded_prop, None)

# Full 32-team stadium/travel map for Phase 6. Noise factors are intentionally small:
# they tax road QB/pass volume and feed player-card context without overpowering usage data.
TEAM_STADIUM_COORDS = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227),
    "BUF": (42.7738, -78.7869), "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167),
    "CIN": (39.0955, -84.5160), "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945),
    "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373),
    "KC": (39.0490, -94.4839), "LAC": (33.9535, -118.3392), "LAR": (33.9535, -118.3392),
    "LV": (36.0908, -115.1830), "MIA": (25.9580, -80.2389), "MIN": (44.9738, -93.2580),
    "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158),
    "SEA": (47.5952, -122.3316), "SF": (37.4030, -121.9700), "TB": (27.9759, -82.5033),
    "TEN": (36.1665, -86.7713), "WAS": (38.9077, -76.8645),
}

STADIUM_ENV = {
    "SEA": {"stadium":"Lumen Field", "crowd":"EXTREME", "noise":0.96, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "KC": {"stadium":"Arrowhead Stadium", "crowd":"EXTREME", "noise":0.965, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "BUF": {"stadium":"Highmark Stadium", "crowd":"LOUD", "noise":0.975, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "PHI": {"stadium":"Lincoln Financial Field", "crowd":"LOUD", "noise":0.975, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "NO": {"stadium":"Caesars Superdome", "crowd":"LOUD", "noise":1.015, "surface":"Turf", "roof":"Dome", "altitude":0},
    "DET": {"stadium":"Ford Field", "crowd":"MODERATE", "noise":1.018, "surface":"Turf", "roof":"Dome", "altitude":0},
    "MIN": {"stadium":"U.S. Bank Stadium", "crowd":"LOUD", "noise":1.015, "surface":"Turf", "roof":"Dome", "altitude":0},
    "ATL": {"stadium":"Mercedes-Benz Stadium", "crowd":"MODERATE", "noise":1.012, "surface":"Turf", "roof":"Retractable", "altitude":0},
    "DAL": {"stadium":"AT&T Stadium", "crowd":"MODERATE", "noise":1.012, "surface":"Turf", "roof":"Retractable", "altitude":0},
    "DEN": {"stadium":"Empower Field", "crowd":"LOUD", "noise":0.985, "surface":"Grass", "roof":"Outdoor", "altitude":5280},
    "GB": {"stadium":"Lambeau Field", "crowd":"LOUD", "noise":0.970, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "CHI": {"stadium":"Soldier Field", "crowd":"MODERATE", "noise":0.975, "surface":"Grass", "roof":"Outdoor", "altitude":0},
}

DEMO_BOARD = [
    {"player":"Patrick Mahomes", "team":"KC", "opp":"LAC", "home_away":"HOME", "position":"QB", "prop":"Passing Yards", "line":285.5, "source":"DEMO", "matchup":"LAC @ KC", "snap_share":100, "pass_attempts_pg":37, "spread":-4.5, "game_total":49.5, "pace":54, "pressure_rate":21, "ol_rank":7},
    {"player":"Josh Allen", "team":"BUF", "opp":"NYJ", "home_away":"HOME", "position":"QB", "prop":"Rushing Yards", "line":39.5, "source":"DEMO", "matchup":"NYJ @ BUF", "snap_share":100, "rush_attempts_pg":7.5, "carries_share":18, "red_zone_touch_share":19, "spread":-6.5, "game_total":46.0, "pace":53},
    {"player":"Justin Jefferson", "team":"MIN", "opp":"GB", "home_away":"HOME", "position":"WR", "prop":"Receiving Yards", "line":89.5, "source":"DEMO", "matchup":"GB @ MIN", "snap_share":91, "route_participation":94, "target_share":29, "air_yards_share":39, "red_zone_touch_share":20, "spread":-2.5, "game_total":47.5, "def_role_rank":22, "coverage_grade":47},
    {"player":"Christian McCaffrey", "team":"SF", "opp":"SEA", "home_away":"AWAY", "position":"RB", "prop":"Rushing Yards", "line":74.5, "source":"DEMO", "matchup":"SF @ SEA", "snap_share":79, "rush_attempts_pg":18, "carries_share":66, "target_share":15, "red_zone_touch_share":34, "spread":-3.0, "game_total":44.5, "def_role_rank":18},
    {"player":"Travis Kelce", "team":"KC", "opp":"LAC", "home_away":"HOME", "position":"TE", "prop":"Receptions", "line":5.5, "source":"DEMO", "matchup":"LAC @ KC", "snap_share":78, "route_participation":78, "target_share":21, "air_yards_share":20, "red_zone_touch_share":25, "spread":-4.5, "game_total":49.5, "def_role_rank":20},
    {"player":"Amon-Ra St. Brown", "team":"DET", "opp":"CHI", "home_away":"HOME", "position":"WR", "prop":"Receptions", "line":6.5, "source":"DEMO", "matchup":"CHI @ DET", "snap_share":88, "route_participation":91, "target_share":27, "air_yards_share":28, "red_zone_touch_share":18, "spread":-5.5, "game_total":48.0, "def_role_rank":19},
    {"player":"Joe Burrow", "team":"CIN", "opp":"BAL", "home_away":"HOME", "position":"QB", "prop":"Pass Attempts", "line":35.5, "source":"DEMO", "matchup":"BAL @ CIN", "snap_share":100, "pass_attempts_pg":37, "spread":1.5, "game_total":48.5, "pace":55, "pass_rate":63},
    {"player":"Jahmyr Gibbs", "team":"DET", "opp":"CHI", "home_away":"HOME", "position":"RB", "prop":"Rush Attempts", "line":13.5, "source":"DEMO", "matchup":"CHI @ DET", "snap_share":61, "rush_attempts_pg":13, "carries_share":48, "red_zone_touch_share":22, "spread":-5.5, "game_total":48.0},
    {"player":"Brandon Aubrey", "team":"DAL", "opp":"PHI", "home_away":"HOME", "position":"K", "prop":"Field Goals Made", "line":1.5, "source":"DEMO", "matchup":"PHI @ DAL", "spread":1.5, "game_total":46.5},
    {"player":"Micah Parsons", "team":"DAL", "opp":"PHI", "home_away":"HOME", "position":"EDGE", "prop":"Sacks", "line":0.5, "source":"DEMO", "matchup":"PHI @ DAL", "snap_share":82, "pressure_rate":16, "spread":1.5, "game_total":46.5},
]

st.set_page_config(page_title="NFL Prop Engine", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.stApp{background:radial-gradient(circle at top,#081a2e 0%,#071014 42%,#020407 100%);color:#fff;}
.block-container{padding-top:1.0rem;max-width:1600px;}
h1,h2,h3{color:#fff}.small-muted{color:#aeb7c2;font-size:13px}.big-title{font-size:42px;font-weight:950;letter-spacing:-1px}.sub-title{color:#c4ced8;margin-top:-8px}.hero-panel{background:linear-gradient(135deg,rgba(0,50,100,.86),rgba(4,8,14,.96));border:1px solid rgba(80,170,255,.38);border-radius:26px;padding:22px;box-shadow:0 0 34px rgba(0,128,255,.18);margin-bottom:18px}.pick-card{background:linear-gradient(145deg,#08121c,#071015);border:1px solid rgba(80,170,255,.28);border-radius:22px;padding:18px;box-shadow:0 0 24px rgba(0,128,255,.12);margin-bottom:14px}.green-card{background:linear-gradient(145deg,#002016,#06130d);border:1px solid rgba(0,255,150,.42);border-radius:22px;padding:18px}.warn-card{background:linear-gradient(145deg,#251a00,#100c00);border:1px solid rgba(255,190,70,.45);border-radius:22px;padding:18px}.player-name{font-size:22px;font-weight:950}.badge{display:inline-block;padding:5px 10px;border-radius:999px;background:#09243a;border:1px solid rgba(80,170,255,.45);color:#d4ecff;font-weight:800;margin:3px 4px 3px 0}.good-badge{background:#002916;border-color:rgba(0,255,135,.55);color:#b5ffd9}.yellow-badge{background:#2b1d00;border-color:rgba(255,210,70,.55);color:#ffe2a1}.red-badge{background:#2b0000;border-color:rgba(255,75,75,.55);color:#ffc0c0}.kpi-strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:12px 0 18px 0}.kpi-box{background:linear-gradient(145deg,#08121c,#071015);border:1px solid rgba(80,170,255,.25);border-radius:18px;padding:14px;min-height:92px}.kpi-label{font-size:12px;color:#aeb7c2;font-weight:850;text-transform:uppercase;letter-spacing:.04em}.kpi-value{font-size:26px;font-weight:950;margin-top:5px}.kpi-sub{font-size:12px;color:#cfd6df;margin-top:4px}.progress-wrap{width:100%;height:12px;border-radius:99px;background:#020407;overflow:hidden;border:1px solid rgba(255,255,255,.08)}.progress-green{height:100%;border-radius:99px;background:linear-gradient(90deg,#00d66b,#46ff9a)}.progress-orange{height:100%;border-radius:99px;background:linear-gradient(90deg,#ff8c00,#ffbf30)}.progress-red{height:100%;border-radius:99px;background:linear-gradient(90deg,#ff2d2d,#ff7272)}.section-title-pro{margin-top:20px;margin-bottom:10px;font-size:24px;font-weight:950;border-left:5px solid #48a7ff;padding-left:12px}.stTabs [data-baseweb="tab"]{color:#b8c3cf;font-weight:850}.stTabs [aria-selected="true"]{color:#58ff9a!important;border-bottom:3px solid #58ff9a}.metric-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:12px}.click-more{border-top:1px solid rgba(255,255,255,.12);padding-top:8px;margin-top:8px}@media(max-width:1100px){.kpi-strip{grid-template-columns:repeat(2,minmax(0,1fr));}}
</style>
""", unsafe_allow_html=True)

# ---------- helpers ----------
def now_iso(): return datetime.now().isoformat(timespec="seconds")
def safe_float(x, default=None):
    try:
        if x is None or x == "": return default
        return float(x)
    except Exception: return default
def clamp(x, lo, hi): return max(lo, min(hi, x))
def load_json(path, default):
    try:
        if Path(path).exists(): return json.loads(Path(path).read_text())
    except Exception: pass
    return default
def save_json(path, data):
    try: Path(path).write_text(json.dumps(data, indent=2))
    except Exception: pass

def clear_json_file(path, empty_value=None):
    """Clear one saved app log safely without deleting the file path itself."""
    if empty_value is None:
        empty_value = []
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(empty_value, indent=2))
        return True
    except Exception:
        return False

def clear_board_logs(clear_learning=False, clear_line_history=False):
    """Clear board snapshot/grade logs. Phase 6 historical database files are not touched."""
    cleared = []
    for label, path, empty in [
        ("Before snapshots", PICK_LOG, []),
        ("After snapshots", AFTER_LOG, []),
        ("Final graded results", RESULT_LOG, []),
    ]:
        if clear_json_file(path, empty):
            cleared.append(label)
    if clear_learning:
        if clear_json_file(LEARN_FILE, {}):
            cleared.append("Learning calibration")
    if clear_line_history:
        if clear_json_file(CLV_FILE, {}):
            cleared.append("CLV tracker")
        if clear_json_file(LINE_HISTORY_FILE, {}):
            cleared.append("Line history")
    return cleared
def strip_accents(text):
    try: return "".join(ch for ch in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(ch))
    except Exception: return str(text or "")
def norm(s):
    s=strip_accents(s).lower().replace("."," ").replace("'","").replace("-"," ")
    return " ".join(s.split())
def request_log(source,status,msg=""):
    rows=load_json(REQUEST_LOG,[]); rows.append({"time":now_iso(),"source":source,"status":status,"message":str(msg)[:300]}); save_json(REQUEST_LOG,rows[-400:])

def edge_requirement(prop):
    return MIN_NFL_EDGE_UNITS.get(str(prop or ""), 1.0)

def decimal_odds(odds):
    odds=safe_float(odds)
    if odds is None: return None
    return 1 + odds/100 if odds > 0 else 1 + 100/abs(odds)

def expected_value(prob, odds=-110):
    dec=decimal_odds(odds)
    if prob is None or dec is None: return None
    return (prob*(dec-1)) - (1-prob)

def kelly_fraction(prob, odds=-110):
    dec=decimal_odds(odds)
    if prob is None or dec is None: return 0.0
    b=dec-1; q=1-prob
    if b <= 0: return 0.0
    return float(clamp(((b*prob)-q)/b, 0, MAX_RECOMMENDED_KELLY))

def update_clv_snapshot(player_name, prop, source, line):
    if line is None: return 0.0
    data=load_json(CLV_FILE,{})
    today=datetime.now().strftime("%Y-%m-%d")
    key=f"{today}|{norm(player_name)}|{prop}|{source}"
    line=float(line)
    old=data.get(key)
    if not old:
        data[key]={"player":player_name,"prop":prop,"source":source,"open_line":line,"latest_line":line,"last_updated":now_iso()}
        save_json(CLV_FILE,data)
        return 0.0
    open_line=safe_float(old.get("open_line"), line) or line
    old["latest_line"]=line; old["last_updated"]=now_iso(); data[key]=old; save_json(CLV_FILE,data)
    return round(line-open_line,2)

def track_line_delta(player_name, prop, source, line):
    if line is None: return 0.0
    hist=load_json(LINE_HISTORY_FILE,{})
    key=f"{norm(player_name)}|{prop}|{source}"
    rows=hist.get(key,[])
    rows.append({"t":now_iso(),"line":safe_float(line)})
    hist[key]=rows[-40:]
    save_json(LINE_HISTORY_FILE,hist)
    if len(hist[key]) < 2: return 0.0
    first=safe_float(hist[key][0].get("line")); last=safe_float(hist[key][-1].get("line"))
    return None if first is None or last is None else round(last-first,2)

def calibration_scale(player, prop):
    results=load_json(RESULT_LOG,[])
    rows=[r for r in results if norm(r.get("player"))==norm(player) and r.get("prop")==prop and r.get("actual") is not None and r.get("projection") is not None]
    if len(rows) < NFL_CALIBRATION_MIN_SAMPLES:
        return 1.0, f"Calibration warming up ({len(rows)}/{NFL_CALIBRATION_MIN_SAMPLES})"
    recent=rows[-40:]
    errs=[]
    for r in recent:
        proj=safe_float(r.get("projection")); act=safe_float(r.get("actual"))
        if proj and act is not None: errs.append((act-proj)/max(1,proj))
    if not errs: return 1.0, "Calibration neutral"
    bias=float(np.mean(errs))
    scale=clamp(1+(bias*0.35), 1-NFL_CALIBRATION_MAX_SHIFT_PCT, 1+NFL_CALIBRATION_MAX_SHIFT_PCT)
    return scale, f"True calibration x{scale:.3f} from {len(rows)} graded rows"

def projection_stability_score(p10, p90, mean, prop):
    width=(safe_float(p90,0) or 0) - (safe_float(p10,0) or 0)
    base_sigma=PROP_CONFIG.get(prop,{}).get("sigma", max(1, safe_float(mean,1) or 1))
    ratio=width/max(1,base_sigma*2.56)
    score=100 - max(0,(ratio-1.0)*38)
    return int(clamp(score,0,100))

def official_rejection_reasons(p):
    reasons=[]
    prop=p.get("prop")
    prob=safe_float(p.get("fair_prob"),0) or 0
    edge_abs=abs(safe_float(p.get("edge"),0) or 0)
    score=safe_float(p.get("data_score"),0) or 0
    stability=safe_float(p.get("stability_score"),0) or 0
    if p.get("source") == "DEMO": reasons.append("Demo line only")
    if safe_float(p.get("line")) is None: reasons.append("No real line")
    if safe_float(p.get("projection")) is None: reasons.append("No projection")
    if prob < MIN_NFL_BETTABLE_PROB: reasons.append(f"Prob below {MIN_NFL_BETTABLE_PROB:.0%}")
    if edge_abs < edge_requirement(prop): reasons.append(f"Edge below {edge_requirement(prop)} for {prop}")
    if score < MIN_NFL_DATA_SCORE: reasons.append(f"Data score below {MIN_NFL_DATA_SCORE}")
    if stability < NFL_PROJECTION_STABILITY_MIN: reasons.append("Projection too unstable")
    if str(p.get("volatility")) == "HIGH": reasons.append("High volatility tax")
    if p.get("injury_risk") in ["HIGH", "EXTREME"]: reasons.append(f"Injury/role risk: {p.get('injury_risk')}")
    if safe_float(p.get("usage_quality"),100) < 68: reasons.append("Usage data/role quality too weak")
    if p.get("defense_risk") == "HIGH" and prob < 0.66: reasons.append("Tough defensive role matchup")
    if safe_float(p.get("collapse_prob"),0) >= 0.24 and prob < 0.69: reasons.append("High collapse-branch risk")
    if p.get("game_script_risk") == "HIGH" and prob < 0.67: reasons.append("Game-script risk on non-elite edge")
    return reasons

def build_signal(p):
    """Readable NFL signal layer.

    Important: official rejection reasons still exist for the Official/Best Edge filter,
    but the player card should not say "PASS — UNDER" all day. The card should
    show the model direction first: OVER / UNDER / LEAN OVER / LEAN UNDER.
    """
    reasons=official_rejection_reasons(p)
    side=str(p.get("pick") or "PASS").upper()
    prob=safe_float(p.get("fair_prob"),0) or 0
    score=safe_float(p.get("data_score"),0) or 0
    edge_abs=abs(safe_float(p.get("edge"),0) or 0)
    req=edge_requirement(p.get("prop"))

    if side in ["NO LINE", "PASS"] or safe_float(p.get("line")) is None:
        return "🚫 NO LINE", "PASS", reasons

    elite=(not reasons and prob>=MIN_NFL_ELITE_PROB and score>=MIN_NFL_ELITE_SCORE and edge_abs>=req*1.35)
    strong=(not reasons and prob>=MIN_NFL_BETTABLE_PROB and score>=MIN_NFL_DATA_SCORE and edge_abs>=req)

    if elite:
        return f"🔥 {side}", "BET", reasons
    if strong:
        return f"✅ {side}", "BET", reasons

    # LEAN means there is enough direction to track, but not enough for official.
    if prob >= 0.57 or edge_abs >= req*0.55:
        return f"⚠️ LEAN {side}", "LEAN", reasons

    # Thin/no-edge spots should not be forced into OVER/UNDER.
    return "🚫 PASS", "PASS", reasons

def get_secret(key, default=""):
    try: return st.secrets[key]
    except Exception: return os.getenv(key, default)

@st.cache_data(ttl=180, show_spinner=False)
def safe_get_json(url):
    try:
        r=requests.get(url,headers={"User-Agent":"Mozilla/5.0 NFLPropEngine/1.0","Accept":"application/json,*/*"},timeout=12)
        if r.status_code!=200:
            request_log(url,f"HTTP {r.status_code}",r.text[:200]); return None
        return r.json()
    except Exception as e:
        request_log(url,"REQUEST_ERROR",e); return None

# ---------- live prop intake ----------
def _blob(item):
    try:
        return json.dumps(item, default=str).lower()
    except Exception:
        return str(item).lower()

def _deep_get(obj, keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

def _first_existing(obj, keys):
    for k in keys:
        if isinstance(obj, dict) and obj.get(k) not in [None, "", []]:
            return obj.get(k)
    return None

def _collect_player_bank(objects):
    """Build id -> player metadata from Underdog included objects/appearances."""
    bank = {}
    for o in objects:
        if not isinstance(o, dict):
            continue
        oid = o.get("id") or o.get("player_id") or o.get("appearance_id")
        first = o.get("first_name") or _deep_get(o, ["player", "first_name"])
        last = o.get("last_name") or _deep_get(o, ["player", "last_name"])
        full = o.get("player_name") or o.get("display_name") or o.get("full_name") or o.get("name")
        if first and last:
            full = f"{first} {last}"
        if full and oid:
            bank[str(oid)] = {
                "player": str(full),
                "team": o.get("team_abbr") or o.get("team") or _deep_get(o, ["team", "abbr"]) or _deep_get(o, ["team", "abbreviation"]),
                "position": o.get("position") or _deep_get(o, ["player", "position"]),
            }
    return bank

def looks_nfl(item):
    b = _blob(item)
    if any(term in b for term in NON_NFL_BLOCK_TERMS) and not any(term in b for term in NFL_SPORT_TERMS):
        return False
    # NFL props may not explicitly say NFL, so recognized NFL market names count too.
    return any(term in b for term in NFL_SPORT_TERMS) or prop_name_from_blob(b) is not None

def prop_name_from_blob(blob):
    b = str(blob or "").lower()
    for prop, aliases in NFL_PROP_ALIASES.items():
        if any(alias in b for alias in aliases):
            return prop
    return None

def _extract_line_value(o):
    direct_keys = ["stat_value", "line", "value", "over_under", "threshold", "target", "total"]
    for k in direct_keys:
        v = o.get(k) if isinstance(o, dict) else None
        if isinstance(v, dict):
            continue
        fv = safe_float(v)
        if fv is not None:
            return fv
    # Some UD versions nest the line under over_under/stat/option objects.
    for path in [
        ["over_under", "stat_value"], ["over_under", "line"], ["over_under", "value"],
        ["over_under_line", "stat_value"], ["appearance_stat", "stat_value"],
        ["stat", "value"], ["option", "line"], ["projection", "line"],
    ]:
        fv = safe_float(_deep_get(o, path))
        if fv is not None:
            return fv
    return None

def _extract_player_from_obj(o, player_bank):
    for k in ["player_name", "player", "athlete_name", "display_name", "full_name", "name"]:
        v = o.get(k) if isinstance(o, dict) else None
        if isinstance(v, str) and len(v.split()) >= 2 and prop_name_from_blob(v) is None:
            return v
    first = o.get("first_name") if isinstance(o, dict) else None
    last = o.get("last_name") if isinstance(o, dict) else None
    if first and last:
        return f"{first} {last}"
    ids = []
    if isinstance(o, dict):
        for k in ["player_id", "appearance_id", "athlete_id"]:
            if o.get(k) is not None:
                ids.append(str(o.get(k)))
        for path in [
            ["over_under", "appearance_stat", "appearance", "player_id"],
            ["over_under", "appearance", "player_id"],
            ["appearance_stat", "appearance", "player_id"],
            ["relationships", "appearance", "data", "id"],
            ["relationships", "player", "data", "id"],
        ]:
            v = _deep_get(o, path)
            if v is not None:
                ids.append(str(v))
    for pid in ids:
        if pid in player_bank:
            return player_bank[pid].get("player")
    # Fallback: title often contains player + market, so strip the prop label.
    title = _first_existing(o, ["title", "description", "label"]) if isinstance(o, dict) else None
    if isinstance(title, str):
        clean = title
        for aliases in NFL_PROP_ALIASES.values():
            for alias in aliases:
                clean = clean.replace(alias, "").replace(alias.title(), "")
        clean = " ".join(clean.replace("over", " ").replace("under", " ").split())
        if len(clean.split()) >= 2:
            return clean
    return None

def _extract_team_pos(o, player, player_bank):
    team = _first_existing(o, ["team_abbr", "team", "team_code", "abbr"]) if isinstance(o, dict) else None
    position = _first_existing(o, ["position", "pos"]) if isinstance(o, dict) else None
    if not team or not position:
        for meta in player_bank.values():
            if norm(meta.get("player")) == norm(player):
                team = team or meta.get("team")
                position = position or meta.get("position")
                break
    return team or "NFL", position or ""

def _extract_matchup(o):
    if not isinstance(o, dict):
        return ""
    for k in ["matchup", "game", "event_title", "scheduled_at"]:
        v = o.get(k)
        if isinstance(v, str) and len(v) <= 80:
            return v
    home = _deep_get(o, ["game", "home_team"])
    away = _deep_get(o, ["game", "away_team"])
    if away and home:
        return f"{away} @ {home}"
    return ""

def _extract_price(o):
    for k in ["payout_multiplier", "price", "odds", "american_odds"]:
        v = o.get(k) if isinstance(o, dict) else None
        fv = safe_float(v)
        if fv is not None:
            return fv
    return None

def flatten(obj):
    out=[]
    if isinstance(obj,dict):
        out.append(obj)
        for v in obj.values(): out.extend(flatten(v))
    elif isinstance(obj,list):
        for x in obj: out.extend(flatten(x))
    return out

@st.cache_data(ttl=120, show_spinner=False)
def fetch_underdog_nfl_props(cache_bust=0):
    """Pull live Underdog NFL props when available.

    Safety behavior:
    - Tries multiple Underdog endpoint versions.
    - Hard-filters to recognized NFL player prop markets.
    - Returns [] when NFL props are not live, so the UI falls back to DEMO/manual mode.
    - Logs endpoint status to request_log.json for debugging in Railway/Streamlit.
    """
    rows=[]
    endpoint_debug=[]
    for url in UNDERDOG_URLS:
        data=safe_get_json(url)
        if not data:
            endpoint_debug.append({"url":url,"status":"NO_DATA","rows":0})
            continue
        objects=flatten(data)
        player_bank=_collect_player_bank(objects)
        url_rows=0
        for o in objects:
            if not isinstance(o, dict):
                continue
            blob=_blob(o)
            if not looks_nfl(o):
                continue
            prop=prop_name_from_blob(blob)
            if not prop or prop not in PROP_CONFIG:
                continue
            line=_extract_line_value(o)
            if line is None:
                continue
            player=_extract_player_from_obj(o, player_bank)
            if not player or player.lower() in ["unknown player", "over", "under"]:
                continue
            team, position = _extract_team_pos(o, player, player_bank)
            rows.append({
                "player":str(player),
                "team":team,
                "opp":o.get("opponent") or o.get("opp") or "",
                "home_away":str(o.get("home_away") or o.get("home_or_away") or ""),
                "position":position,
                "prop":prop,
                "line":line,
                "price":_extract_price(o),
                "source":"Underdog",
                "source_url":url,
                "matchup":_extract_matchup(o),
                "underdog_id":str(o.get("id") or o.get("over_under_line_id") or ""),
            })
            url_rows += 1
        endpoint_debug.append({"url":url,"status":"OK","rows":url_rows,"objects":len(objects)})
        # Prefer newest successful endpoint. If v6/v5 has rows, don't mix duplicate older endpoints.
        if url_rows > 0:
            break
    # dedupe: keep first/newest endpoint version.
    seen=set(); clean=[]
    for r in rows:
        key=(norm(r["player"]),r["prop"],safe_float(r["line"]),r.get("matchup",""))
        if key not in seen:
            seen.add(key); clean.append(r)
    request_log("UNDERDOG_NFL_LIVE_PULL", "FOUND" if clean else "NO_NFL_ROWS", endpoint_debug)
    return clean[:500]

@st.cache_data(ttl=120, show_spinner=False)
def fetch_underdog_nfl_moneylines(cache_bust=0):
    """Scan Underdog feeds for NFL moneyline/winner markets when Underdog posts them.

    Some Underdog endpoints only expose player over/under props. This function is intentionally
    defensive: it returns an empty list if moneyline-style markets are not present instead of
    creating fake prices.
    """
    rows=[]; endpoint_debug=[]
    money_terms=["moneyline", "money line", "match winner", "game winner", "winner", "to win"]
    for url in UNDERDOG_URLS:
        data=safe_get_json(url)
        if not data:
            endpoint_debug.append({"url":url,"status":"NO_DATA","rows":0}); continue
        objects=flatten(data); url_rows=0
        for o in objects:
            if not isinstance(o, dict): continue
            blob=_blob(o)
            if not looks_nfl(o): continue
            if not any(t in blob for t in money_terms): continue
            if prop_name_from_blob(blob) is not None:
                continue
            team=_first_existing(o,["team","team_abbr","team_code","title","name","display_name","option_title","choice"]) or "NFL"
            matchup=_extract_matchup(o)
            price=_extract_price(o)
            # Underdog may use payout multipliers instead of American odds; keep exact raw value visible.
            rows.append({
                "team_or_side": str(team),
                "matchup": matchup,
                "market": "Money Line",
                "price_or_payout": price if price is not None else _first_existing(o,["payout","payout_multiplier","odds","price"]),
                "source": "Underdog",
                "source_url": url,
                "underdog_id": str(o.get("id") or o.get("market_id") or ""),
                "raw_label": str(_first_existing(o,["title","description","label","name"]) or "")[:120],
            })
            url_rows += 1
        endpoint_debug.append({"url":url,"status":"OK","rows":url_rows,"objects":len(objects)})
        if url_rows > 0:
            break
    seen=set(); clean=[]
    for r in rows:
        key=(norm(r.get("team_or_side")), norm(r.get("matchup")), str(r.get("price_or_payout")))
        if key not in seen:
            seen.add(key); clean.append(r)
    request_log("UNDERDOG_NFL_MONEYLINE_PULL", "FOUND" if clean else "NO_MONEYLINE_ROWS", endpoint_debug)
    return clean[:200]


def save_last_pulled_board(live_rows, moneyline_rows=None):
    """Persist the latest real Underdog pull so the board can be inspected and reused.

    This mirrors the MLB workflow: pull the board, keep the exact slate snapshot, then
    allow Save BEFORE / AFTER to operate on the full current board.
    """
    live_rows = live_rows or []
    moneyline_rows = moneyline_rows or []
    payload = {
        "pulled_at": now_iso(),
        "source": "Underdog",
        "row_count": len(live_rows),
        "rows": live_rows,
    }
    save_json(BOARD_CACHE_FILE, payload)
    save_json(MONEYLINE_CACHE_FILE, {
        "pulled_at": now_iso(),
        "source": "Underdog",
        "row_count": len(moneyline_rows),
        "rows": moneyline_rows,
    })
    return payload

def load_last_pulled_board():
    data = load_json(BOARD_CACHE_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return data
    return {"pulled_at": None, "row_count": 0, "rows": []}

def load_last_pulled_moneylines():
    data = load_json(MONEYLINE_CACHE_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return data
    return {"pulled_at": None, "row_count": 0, "rows": []}


# ---------- MLB-style stable seeds + Phase 6 NFL database ----------
def stable_projection_seed(*parts):
    """Deterministic simulation seed borrowed from the MLB engine pattern.

    Same player/prop/line/input context = same projection on refresh.
    This avoids random card movement when nothing meaningful changed.
    """
    try:
        raw = "|".join([str(p) for p in parts])
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)
    except Exception:
        return 20260628

def great_circle_miles(team_a, team_b):
    a = TEAM_STADIUM_COORDS.get(str(team_a or "").upper())
    b = TEAM_STADIUM_COORDS.get(str(team_b or "").upper())
    if not a or not b:
        return None
    lat1, lon1 = np.radians(a[0]), np.radians(a[1])
    lat2, lon2 = np.radians(b[0]), np.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = np.sin(dlat/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin(dlon/2)**2
    return round(float(3958.8 * 2 * np.arcsin(np.sqrt(h))), 1)

def nflverse_url(release, filename):
    return f"https://github.com/nflverse/nflverse-data/releases/download/{release}/{filename}"


def _phase6_existing_database_ready():
    """True when the saved Phase 6 database can be reused without downloading again."""
    required = [PHASE6_PLAYER_LOG_FILE, PHASE6_PLAYER_SUMMARY_FILE, USAGE_FILE, TEAM_CONTEXT_FILE]
    return all(Path(x).exists() and Path(x).stat().st_size > 50 for x in required)

def _read_csv_cached(cache_path):
    try:
        if Path(cache_path).exists() and Path(cache_path).stat().st_size > 50:
            return pd.read_csv(cache_path)
    except Exception as e:
        request_log(cache_path, "CACHE_READ_ERROR", e)
    return pd.DataFrame()

def _download_csv_with_persistent_cache(label, urls, cache_name, force_refresh=False):
    """Download once, save locally, and reuse on every future app run.

    This is intentionally different from st.cache_data: it writes the raw dataset into
    nfl_engine/phase6_nfl_database/_raw_cache so Railway/GitHub/Streamlit deployments
    can run from saved files instead of pulling the same last-season data every day.
    """
    cache_path = PHASE6_RAW_DIR / cache_name
    if cache_path.exists() and not force_refresh:
        df = _read_csv_cached(cache_path)
        if not df.empty:
            request_log(label, "LOCAL_CACHE", f"{cache_name} rows={len(df)}")
            return df
    last_error = ""
    for url in urls:
        try:
            df = pd.read_csv(url)
            if not df.empty:
                df.to_csv(cache_path, index=False)
                request_log(label, "DOWNLOADED", f"{url} rows={len(df)} -> {cache_path}")
                return df
        except Exception as e:
            last_error = str(e)[:300]
            request_log(label, "URL_FAILED", f"{url} :: {last_error}")
    # If fresh download fails, keep using the prior saved copy instead of wiping the app.
    df = _read_csv_cached(cache_path)
    if not df.empty:
        request_log(label, "FALLBACK_LOCAL_CACHE", f"download failed but cache exists rows={len(df)}")
        return df
    request_log(label, "NO_DATA", last_error)
    return pd.DataFrame()

def fetch_nflverse_player_weekly_stats(season=NFL_LAST_SEASON, force_refresh=False):
    season = int(season)
    urls = [
        nflverse_url("player_stats", f"stats_player_week_{season}.csv"),
        nflverse_url("player_stats", f"player_stats_{season}.csv"),
        nflverse_url("player_stats", f"stats_player_week_{season}.csv.gz"),
    ]
    df = _download_csv_with_persistent_cache("NFLVERSE_PLAYER_WEEKLY", urls, f"stats_player_week_{season}.csv", force_refresh)
    if not df.empty:
        if "season" in df.columns:
            df = df[df["season"].astype(str) == str(season)].copy()
        if "week" in df.columns:
            df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
        # Keep regular-season rows when a game/season type column exists.
        for tcol in ["season_type", "game_type"]:
            if tcol in df.columns:
                vals = df[tcol].astype(str).str.upper()
                reg_mask = vals.isin(["REG", "REGULAR", "REGULAR_SEASON", "R"])
                if reg_mask.any():
                    df = df[reg_mask].copy()
                break
    return df

def fetch_nflverse_schedules(season=NFL_LAST_SEASON, force_refresh=False):
    season = int(season)
    urls = [
        nflverse_url("schedules", "schedules.csv"),
        nflverse_url("schedules", f"schedules_{season}.csv"),
        nflverse_url("schedules", "schedules.csv.gz"),
    ]
    df = _download_csv_with_persistent_cache("NFLVERSE_SCHEDULES", urls, f"schedules_{season}.csv", force_refresh)
    if not df.empty:
        if "season" in df.columns:
            df = df[df["season"].astype(str) == str(season)].copy()
        for tcol in ["game_type", "season_type"]:
            if tcol in df.columns:
                vals = df[tcol].astype(str).str.upper()
                reg_mask = vals.isin(["REG", "REGULAR", "REGULAR_SEASON", "R"])
                if reg_mask.any():
                    df = df[reg_mask].copy()
                break
    request_log("NFLVERSE_SCHEDULES", "READY", f"{season} rows={len(df)}")
    return df

def fetch_nflverse_snap_counts(season=NFL_LAST_SEASON, force_refresh=False):
    season = int(season)
    urls = [
        nflverse_url("snap_counts", f"snap_counts_{season}.csv"),
        nflverse_url("snap_counts", f"snap_counts_{season}.csv.gz"),
    ]
    df = _download_csv_with_persistent_cache("NFLVERSE_SNAP_COUNTS", urls, f"snap_counts_{season}.csv", force_refresh)
    if not df.empty:
        if "season" in df.columns:
            df = df[df["season"].astype(str) == str(season)].copy()
        if "week" in df.columns:
            df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
    return df

def fetch_nflverse_pbp(season=NFL_LAST_SEASON, force_refresh=False):
    """Optional full play-by-play pull for penalties, fumbles, red zone, EPA, OT, and trench proxies.

    If this file is too large or unavailable in a deployment, the builder still completes from
    weekly player stats + schedules + snaps. If it succeeds once, it is saved and reused.
    """
    season = int(season)
    urls = [
        nflverse_url("pbp", f"play_by_play_{season}.csv.gz"),
        nflverse_url("play_by_play", f"play_by_play_{season}.csv.gz"),
        nflverse_url("pbp", f"play_by_play_{season}.csv"),
        nflverse_url("play_by_play", f"play_by_play_{season}.csv"),
    ]
    df = _download_csv_with_persistent_cache("NFLVERSE_PBP", urls, f"play_by_play_{season}.csv", force_refresh)
    if not df.empty:
        if "season" in df.columns:
            df = df[df["season"].astype(str) == str(season)].copy()
        if "week" in df.columns:
            df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
        for tcol in ["season_type", "game_type"]:
            if tcol in df.columns:
                vals = df[tcol].astype(str).str.upper()
                reg_mask = vals.isin(["REG", "REGULAR", "REGULAR_SEASON", "R"])
                if reg_mask.any():
                    df = df[reg_mask].copy()
                break
    return df

def _phase6_sum_cols(df, cols):
    return [c for c in cols if c in df.columns]


def _clean_numeric(df, cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _safe_group_sum(df, group_cols, sum_map):
    """Aggregate only columns that exist and return a clean dataframe."""
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    actual = {}
    for src, out in sum_map.items():
        if src in work.columns:
            work[src] = pd.to_numeric(work[src], errors="coerce").fillna(0)
            actual[src] = out
    if not actual:
        return pd.DataFrame()
    grouped = work.groupby(group_cols, dropna=False)[list(actual.keys())].sum(numeric_only=True).reset_index()
    grouped = grouped.rename(columns=actual)
    return grouped

def _build_player_weekly_from_pbp(pbp, season=NFL_LAST_SEASON):
    """Fallback player weekly builder from nflfastR play-by-play.

    This fixes the NO_PLAYER_WEEKLY_DATA case. If the nflverse weekly-player file
    fails or is missing for the selected season, the app builds weekly player logs
    directly from play-by-play, saves them locally, and then uses them just like the
    normal weekly-stat file on every future run.
    """
    if pbp is None or pbp.empty:
        return pd.DataFrame()
    df = pbp.copy()
    if "season" in df.columns:
        df = df[df["season"].astype(str) == str(int(season))].copy()
    if "week" in df.columns:
        df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
    for tcol in ["season_type", "game_type"]:
        if tcol in df.columns:
            vals = df[tcol].astype(str).str.upper()
            reg_mask = vals.isin(["REG", "REGULAR", "REGULAR_SEASON", "R"])
            if reg_mask.any():
                df = df[reg_mask].copy()
            break
    if df.empty:
        return pd.DataFrame()
    for c in ["pass_attempt","complete_pass","pass_touchdown","interception","sack","rush_attempt","rush_touchdown","air_yards","receiving_yards","rushing_yards","passing_yards","yards_gained","fumble","fumble_lost"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    base_cols = [c for c in ["season","week"] if c in df.columns]
    if "posteam" not in df.columns:
        return pd.DataFrame()
    # Passing logs
    pass_df = pd.DataFrame()
    if "passer_player_name" in df.columns:
        pdf = df[df["passer_player_name"].notna() & (df["passer_player_name"].astype(str).str.len() > 1)].copy()
        if not pdf.empty:
            if "passing_yards" not in pdf.columns:
                pdf["passing_yards"] = np.where(pd.to_numeric(pdf.get("pass_attempt",0), errors="coerce").fillna(0).eq(1), pd.to_numeric(pdf.get("yards_gained",0), errors="coerce").fillna(0), 0)
            pass_df = _safe_group_sum(pdf, base_cols+["posteam","defteam","passer_player_name"], {
                "pass_attempt":"attempts", "complete_pass":"completions", "passing_yards":"passing_yards",
                "pass_touchdown":"passing_tds", "interception":"interceptions", "sack":"sacks"
            })
            if not pass_df.empty:
                pass_df = pass_df.rename(columns={"posteam":"team","defteam":"opp","passer_player_name":"player"})
                pass_df["position"] = "QB"
    # Rushing logs
    rush_df = pd.DataFrame()
    if "rusher_player_name" in df.columns:
        rdf = df[df["rusher_player_name"].notna() & (df["rusher_player_name"].astype(str).str.len() > 1)].copy()
        if not rdf.empty:
            if "rushing_yards" not in rdf.columns:
                rdf["rushing_yards"] = np.where(pd.to_numeric(rdf.get("rush_attempt",0), errors="coerce").fillna(0).eq(1), pd.to_numeric(rdf.get("yards_gained",0), errors="coerce").fillna(0), 0)
            rush_df = _safe_group_sum(rdf, base_cols+["posteam","defteam","rusher_player_name"], {
                "rush_attempt":"carries", "rushing_yards":"rushing_yards", "rush_touchdown":"rushing_tds", "fumble":"fumbles", "fumble_lost":"fumbles_lost"
            })
            if not rush_df.empty:
                rush_df = rush_df.rename(columns={"posteam":"team","defteam":"opp","rusher_player_name":"player"})
                rush_df["position"] = "RB"
    # Receiving logs
    rec_df = pd.DataFrame()
    if "receiver_player_name" in df.columns:
        cdf = df[df["receiver_player_name"].notna() & (df["receiver_player_name"].astype(str).str.len() > 1)].copy()
        if not cdf.empty:
            if "receiving_yards" not in cdf.columns:
                cdf["receiving_yards"] = np.where(pd.to_numeric(cdf.get("complete_pass",0), errors="coerce").fillna(0).eq(1), pd.to_numeric(cdf.get("yards_gained",0), errors="coerce").fillna(0), 0)
            cdf["targets_src"] = np.where(pd.to_numeric(cdf.get("pass_attempt",0), errors="coerce").fillna(0).eq(1), 1, 0)
            rec_df = _safe_group_sum(cdf, base_cols+["posteam","defteam","receiver_player_name"], {
                "targets_src":"targets", "complete_pass":"receptions", "receiving_yards":"receiving_yards",
                "pass_touchdown":"receiving_tds", "air_yards":"air_yards", "fumble":"fumbles", "fumble_lost":"fumbles_lost"
            })
            if not rec_df.empty:
                rec_df = rec_df.rename(columns={"posteam":"team","defteam":"opp","receiver_player_name":"player"})
                rec_df["position"] = "REC"
    keys = [c for c in ["season","week","team","opp","player"] if c in df.columns or c in ["team","opp","player"]]
    frames = [x for x in [pass_df, rush_df, rec_df] if x is not None and not x.empty]
    if not frames:
        return pd.DataFrame()
    # Outer merge all roles for dual-threat players; keep the most specific useful position.
    out = frames[0]
    for nxt in frames[1:]:
        out = out.merge(nxt.drop(columns=["position"], errors="ignore"), on=[c for c in ["season","week","team","opp","player"] if c in out.columns and c in nxt.columns], how="outer")
        if "position" not in out.columns:
            out["position"] = ""
    numeric_cols = [c for c in out.columns if c not in ["season","week","team","opp","player","position"]]
    out[numeric_cols] = out[numeric_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
    # Improve position labels: QBs have attempts; receivers have targets/receptions; rush-only remains RB.
    out["position"] = np.where(out.get("attempts",0).fillna(0) > 0, "QB", out.get("position", ""))
    out["position"] = np.where((out.get("targets",0).fillna(0) > 0) & (out["position"].astype(str).isin(["", "RB"])), "WR/TE", out["position"])
    out["position"] = out["position"].replace({"REC":"WR/TE", "":"RB"})
    if "fantasy_points_ppr" not in out.columns:
        out["fantasy_points_ppr"] = (
            out.get("passing_yards",0)*0.04 + out.get("passing_tds",0)*4 - out.get("interceptions",0)*1 +
            out.get("rushing_yards",0)*0.1 + out.get("rushing_tds",0)*6 +
            out.get("receptions",0)*1 + out.get("receiving_yards",0)*0.1 + out.get("receiving_tds",0)*6 -
            out.get("fumbles_lost",0)*2
        ).round(3)
    if "fantasy_points" not in out.columns:
        out["fantasy_points"] = (out["fantasy_points_ppr"] - out.get("receptions",0)).round(3)
    out = out.sort_values([c for c in ["season","week","team","player"] if c in out.columns]).reset_index(drop=True)
    return out

def _build_schedules_from_pbp(pbp, season=NFL_LAST_SEASON):
    """Fallback schedule builder from PBP when schedules.csv fails."""
    if pbp is None or pbp.empty or "game_id" not in pbp.columns:
        return pd.DataFrame()
    df = pbp.copy()
    if "season" in df.columns:
        df = df[df["season"].astype(str) == str(int(season))].copy()
    if "week" in df.columns:
        df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
    cols = [c for c in ["game_id","season","week","home_team","away_team","gameday","game_date","roof","surface"] if c in df.columns]
    if not {"game_id","home_team","away_team"}.issubset(set(cols)):
        return pd.DataFrame()
    sched = df[cols].drop_duplicates("game_id").copy()
    return sched

def _save_position_specific_phase6_tables(summary):
    """Cleaner previews and model inputs: no more generic all-zero tables."""
    try:
        if summary is None or summary.empty or "position" not in summary.columns:
            return
        work = summary.copy()
        pos = work["position"].astype(str).str.upper()
        qb_cols = [c for c in ["player","team","position","games_played","pass_attempts_pg","completions_pg","passing_yards_pg","passing_tds_pg","interceptions_pg","rush_attempts_pg","rushing_yards_pg","snap_share","red_zone_pass_attempts","fantasy_points_pg"] if c in work.columns]
        rb_cols = [c for c in ["player","team","position","games_played","rush_attempts_pg","rushing_yards_pg","rushing_tds","targets_pg","receptions_pg","receiving_yards_pg","carries_share","snap_share","red_zone_carries","goal_line_touches","fantasy_points_pg"] if c in work.columns]
        rec_cols = [c for c in ["player","team","position","games_played","targets_pg","receptions_pg","receiving_yards_pg","receiving_tds","target_share","air_yards_share","air_yards_pg","snap_share","route_participation","red_zone_targets","goal_line_touches","fantasy_points_pg"] if c in work.columns]
        if qb_cols:
            qbs = work[(pos == "QB") & (pd.to_numeric(work.get("pass_attempts_pg",0), errors="coerce").fillna(0) > 0)][qb_cols]
            qbs.to_csv(PHASE6_DIR / "qb_summary_last_season.csv", index=False)
        if rb_cols:
            rbs = work[(pd.to_numeric(work.get("rush_attempts_pg",0), errors="coerce").fillna(0) > 0) & ~pos.eq("QB")][rb_cols]
            rbs.to_csv(PHASE6_DIR / "rb_summary_last_season.csv", index=False)
        if rec_cols:
            recs = work[pd.to_numeric(work.get("targets_pg",0), errors="coerce").fillna(0) > 0][rec_cols]
            recs.to_csv(PHASE6_DIR / "receiver_te_summary_last_season.csv", index=False)
    except Exception as e:
        request_log("PHASE6_POSITION_TABLES", "ERROR", e)

def _first_present(df, names):
    for n in names:
        if n in df.columns:
            return n
    return None

def _build_pbp_context(pbp):
    """Create advanced team, defense, red-zone, OT, penalty, fumble, and trench context."""
    empty = pd.DataFrame()
    if pbp is None or pbp.empty:
        return {}, empty, empty, empty, empty, empty
    df = pbp.copy()
    for c in ["pass_attempt","rush_attempt","penalty","fumble_lost","fumble","sack","qb_hit","touchdown","epa","success","yards_gained","air_yards","complete_pass"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    if "down" in df.columns:
        df["down_num"] = pd.to_numeric(df["down"], errors="coerce")
    else:
        df["down_num"] = np.nan
    if "yardline_100" in df.columns:
        df["yardline_100_num"] = pd.to_numeric(df["yardline_100"], errors="coerce")
    else:
        df["yardline_100_num"] = np.nan
    if "qtr" in df.columns:
        df["qtr_num"] = pd.to_numeric(df["qtr"], errors="coerce")
    else:
        df["qtr_num"] = np.nan

    team_context = {}
    team_rows = []
    if "posteam" in df.columns:
        for team, g in df.dropna(subset=["posteam"]).groupby("posteam"):
            plays_mask = ((g.get("pass_attempt",0)==1) | (g.get("rush_attempt",0)==1)) if "pass_attempt" in g.columns and "rush_attempt" in g.columns else pd.Series(True, index=g.index)
            gp = max(1, g["game_id"].nunique() if "game_id" in g.columns else g["week"].nunique() if "week" in g.columns else 17)
            plays = max(1, int(plays_mask.sum()))
            pass_att = float(g.loc[plays_mask, "pass_attempt"].sum()) if "pass_attempt" in g.columns else 0.0
            rush_att = float(g.loc[plays_mask, "rush_attempt"].sum()) if "rush_attempt" in g.columns else 0.0
            early = g[g["down_num"].isin([1,2])] if "down_num" in g.columns else g.iloc[0:0]
            rz = g[g["yardline_100_num"].between(1,20, inclusive="both")] if "yardline_100_num" in g.columns else g.iloc[0:0]
            gl = g[g["yardline_100_num"].between(1,5, inclusive="both")] if "yardline_100_num" in g.columns else g.iloc[0:0]
            row = {
                "team": str(team),
                "pbp_plays_pg": round(plays/gp,2),
                "pbp_pass_rate": round(100*pass_att/max(1,pass_att+rush_att),2),
                "pbp_rush_rate": round(100*rush_att/max(1,pass_att+rush_att),2),
                "epa_per_play": round(float(g.loc[plays_mask,"epa"].mean()) if "epa" in g.columns and plays else 0,4),
                "success_rate": round(100*float(g.loc[plays_mask,"success"].mean()) if "success" in g.columns and plays else 0,2),
                "early_down_pass_rate": round(100*float(early.get("pass_attempt", pd.Series(dtype=float)).sum())/max(1,float(early.get("pass_attempt", pd.Series(dtype=float)).sum())+float(early.get("rush_attempt", pd.Series(dtype=float)).sum())),2) if not early.empty else 0,
                "early_down_success_rate": round(100*float(early.get("success", pd.Series(dtype=float)).mean()),2) if not early.empty and "success" in early.columns else 0,
                "red_zone_pass_rate": round(100*float(rz.get("pass_attempt", pd.Series(dtype=float)).sum())/max(1,float(rz.get("pass_attempt", pd.Series(dtype=float)).sum())+float(rz.get("rush_attempt", pd.Series(dtype=float)).sum())),2) if not rz.empty else 0,
                "goal_line_rush_rate": round(100*float(gl.get("rush_attempt", pd.Series(dtype=float)).sum())/max(1,float(gl.get("pass_attempt", pd.Series(dtype=float)).sum())+float(gl.get("rush_attempt", pd.Series(dtype=float)).sum())),2) if not gl.empty else 0,
                "penalties_pg": round(float(g.get("penalty", pd.Series(dtype=float)).sum())/gp,2) if "penalty" in g.columns else 0,
                "fumbles_pg": round(float(g.get("fumble", pd.Series(dtype=float)).sum())/gp,2) if "fumble" in g.columns else 0,
                "sacks_allowed_pg": round(float(g.get("sack", pd.Series(dtype=float)).sum())/gp,2) if "sack" in g.columns else 0,
                "qb_hits_allowed_pg": round(float(g.get("qb_hit", pd.Series(dtype=float)).sum())/gp,2) if "qb_hit" in g.columns else 0,
                "explosive_pass_rate": round(100*float(((g.get("pass_attempt",0)==1) & (g.get("yards_gained",0)>=20)).mean()),2) if "yards_gained" in g.columns and "pass_attempt" in g.columns else 0,
                "explosive_rush_rate": round(100*float(((g.get("rush_attempt",0)==1) & (g.get("yards_gained",0)>=10)).mean()),2) if "yards_gained" in g.columns and "rush_attempt" in g.columns else 0,
                "ot_games": int(g[g["qtr_num"]>=5]["game_id"].nunique()) if "game_id" in g.columns and "qtr_num" in g.columns else 0,
                "ot_rate": round(100*int(g[g["qtr_num"]>=5]["game_id"].nunique())/gp,2) if "game_id" in g.columns and "qtr_num" in g.columns else 0,
            }
            # Team identity labels for app notes/cards.
            row["team_identity"] = "PASS-FIRST" if row["pbp_pass_rate"] >= 59 else "RUN-FIRST" if row["pbp_rush_rate"] >= 45 else "BALANCED"
            row["coach_pace_proxy"] = "FAST" if row["pbp_plays_pg"] >= 64 else "SLOW" if row["pbp_plays_pg"] <= 58 else "NEUTRAL"
            team_rows.append(row)
            team_context[str(team)] = {k:v for k,v in row.items() if k != "team"}
    team_adv = pd.DataFrame(team_rows)

    # Defensive allowed/team front context.
    def_rows = []
    if "defteam" in df.columns:
        for team, g in df.dropna(subset=["defteam"]).groupby("defteam"):
            gp = max(1, g["game_id"].nunique() if "game_id" in g.columns else g["week"].nunique() if "week" in g.columns else 17)
            pass_mask = g.get("pass_attempt", pd.Series(0,index=g.index)) == 1
            rush_mask = g.get("rush_attempt", pd.Series(0,index=g.index)) == 1
            row = {
                "team": str(team),
                "def_epa_allowed_per_play": round(float(g.get("epa", pd.Series(dtype=float)).mean()),4) if "epa" in g.columns else 0,
                "def_success_allowed_rate": round(100*float(g.get("success", pd.Series(dtype=float)).mean()),2) if "success" in g.columns else 0,
                "def_sacks_pg": round(float(g.get("sack", pd.Series(dtype=float)).sum())/gp,2) if "sack" in g.columns else 0,
                "def_qb_hits_pg": round(float(g.get("qb_hit", pd.Series(dtype=float)).sum())/gp,2) if "qb_hit" in g.columns else 0,
                "def_fumbles_forced_pg": round(float(g.get("fumble", pd.Series(dtype=float)).sum())/gp,2) if "fumble" in g.columns else 0,
                "explosive_pass_allowed_rate": round(100*float(((pass_mask) & (g.get("yards_gained",0)>=20)).mean()),2) if "yards_gained" in g.columns else 0,
                "explosive_rush_allowed_rate": round(100*float(((rush_mask) & (g.get("yards_gained",0)>=10)).mean()),2) if "yards_gained" in g.columns else 0,
            }
            def_rows.append(row)
    defense_adv = pd.DataFrame(def_rows)
    if not defense_adv.empty:
        for col, asc, out in [
            ("def_epa_allowed_per_play", True, "def_epa_rank"),
            ("def_sacks_pg", False, "def_pass_rush_rank"),
            ("explosive_rush_allowed_rate", True, "def_explosive_run_rank"),
            ("explosive_pass_allowed_rate", True, "def_explosive_pass_rank"),
        ]:
            if col in defense_adv.columns:
                defense_adv[out] = defense_adv[col].rank(method="min", ascending=asc).astype(int)

    # Red-zone/goal-line player usage.
    rz_rows = []
    rz = df[df["yardline_100_num"].between(1,20, inclusive="both")] if "yardline_100_num" in df.columns else pd.DataFrame()
    if not rz.empty:
        player_sources = [
            ("rusher_player_name", "red_zone_carries"),
            ("receiver_player_name", "red_zone_targets"),
            ("passer_player_name", "red_zone_pass_attempts"),
        ]
        team_totals = {}
        for team, tg in rz.groupby("posteam") if "posteam" in rz.columns else []:
            team_totals[str(team)] = max(1, len(tg))
        tmp = {}
        for col, stat in player_sources:
            if col not in rz.columns:
                continue
            for (team, player), g in rz.dropna(subset=[col]).groupby(["posteam", col]) if "posteam" in rz.columns else []:
                key=(str(team), str(player))
                tmp.setdefault(key, {"team":str(team), "player":str(player), "red_zone_carries":0, "red_zone_targets":0, "red_zone_pass_attempts":0, "goal_line_touches":0})
                tmp[key][stat] += len(g)
                gl = g[g["yardline_100_num"].between(1,5, inclusive="both")]
                if stat in ["red_zone_carries", "red_zone_targets"]:
                    tmp[key]["goal_line_touches"] += len(gl)
        for (team, player), row in tmp.items():
            row["red_zone_touch_share"] = round(100*(row.get("red_zone_carries",0)+row.get("red_zone_targets",0))/team_totals.get(team,1),2)
            rz_rows.append(row)
    red_zone = pd.DataFrame(rz_rows)

    # OT context as its own table.
    ot = team_adv[["team","ot_games","ot_rate"]].copy() if not team_adv.empty and "ot_rate" in team_adv.columns else pd.DataFrame()

    # Trench context proxy from PBP.
    trench = pd.DataFrame()
    if not team_adv.empty:
        cols = [c for c in ["team","sacks_allowed_pg","qb_hits_allowed_pg","explosive_rush_rate"] if c in team_adv.columns]
        trench = team_adv[cols].copy()
        if "sacks_allowed_pg" in trench.columns:
            trench["ol_pass_pro_rank"] = trench["sacks_allowed_pg"].rank(method="min", ascending=True).astype(int)
        if "qb_hits_allowed_pg" in trench.columns:
            trench["pressure_allowed_rank"] = trench["qb_hits_allowed_pg"].rank(method="min", ascending=True).astype(int)
        if "explosive_rush_rate" in trench.columns:
            trench["ol_run_block_proxy_rank"] = trench["explosive_rush_rate"].rank(method="min", ascending=False).astype(int)
    return team_context, team_adv, defense_adv, red_zone, ot, trench

def build_phase6_nfl_database(season=NFL_LAST_SEASON, force_refresh=False):
    """Build and persist a full last-season NFL database.

    Default behavior: if the saved Phase 6 database already exists, reuse it and do not pull
    again. Press Force Rebuild in the UI only when you intentionally want to refresh/recreate
    last-season files.
    """
    season = int(season)
    if _phase6_existing_database_ready() and not force_refresh:
        manifest = load_json(PHASE6_MANIFEST_FILE, {})
        return {"season": season, "status": "USING_SAVED_DATABASE", "message": "Saved Phase 6 files found, so no download was needed.", **manifest}

    # Pull raw sources. Player-weekly is preferred, but if it fails, build player logs
    # from play-by-play so the database does not get stuck at player_week_rows = 0.
    weekly = fetch_nflverse_player_weekly_stats(season, force_refresh=force_refresh)
    schedules = fetch_nflverse_schedules(season, force_refresh=force_refresh)
    snaps = fetch_nflverse_snap_counts(season, force_refresh=force_refresh)
    pbp = fetch_nflverse_pbp(season, force_refresh=force_refresh)

    weekly_source = "nflverse_player_stats"
    if weekly.empty and not pbp.empty:
        weekly = _build_player_weekly_from_pbp(pbp, season)
        weekly_source = "pbp_fallback_player_logs" if not weekly.empty else "missing"
        if not weekly.empty:
            weekly.to_csv(PHASE6_RAW_DIR / f"player_weekly_from_pbp_{season}.csv", index=False)
            request_log("PHASE6_PBP_FALLBACK", "BUILT_PLAYER_WEEKLY", f"rows={len(weekly)}")

    schedule_source = "nflverse_schedules"
    if schedules.empty and not pbp.empty:
        schedules = _build_schedules_from_pbp(pbp, season)
        schedule_source = "pbp_fallback_schedule" if not schedules.empty else "missing"
        if not schedules.empty:
            schedules.to_csv(PHASE6_RAW_DIR / f"schedules_from_pbp_{season}.csv", index=False)
            request_log("PHASE6_PBP_FALLBACK", "BUILT_SCHEDULES", f"rows={len(schedules)}")

    diag = {
        "season": season,
        "status": "STARTED",
        "player_week_rows": int(len(weekly)),
        "player_week_source": weekly_source,
        "schedule_rows": int(len(schedules)),
        "schedule_source": schedule_source,
        "snap_rows": int(len(snaps)),
        "pbp_rows": int(len(pbp)),
        "cache_dir": str(PHASE6_RAW_DIR),
    }
    if weekly.empty:
        # Never delete existing working files if a new pull fails.
        if _phase6_existing_database_ready():
            manifest = load_json(PHASE6_MANIFEST_FILE, {})
            diag.update({"status": "PULL_FAILED_USING_SAVED_DATABASE", "message": "Player weekly data did not download, but saved Phase 6 files are still being used.", **manifest})
            return diag
        diag["status"] = "NO_PLAYER_WEEKLY_OR_PBP_DATA"
        diag["message"] = "Neither weekly player stats nor play-by-play fallback produced logs. Check request_log.json and try Force Refresh."
        return diag

    logs = weekly.copy()
    rename_candidates = {
        "player_display_name": "player", "recent_team": "team", "opponent_team": "opp",
        "position": "position", "week": "week", "season": "season"
    }
    for old, new in rename_candidates.items():
        if old in logs.columns and new not in logs.columns:
            logs[new] = logs[old]
    for c in ["player", "team", "opp", "position"]:
        if c not in logs.columns:
            logs[c] = ""

    numeric_cols = [
        "attempts","completions","passing_yards","passing_tds","interceptions","sacks",
        "carries","rushing_yards","rushing_tds","targets","receptions","receiving_yards",
        "receiving_tds","air_yards","fantasy_points","fantasy_points_ppr","fumbles","fumbles_lost"
    ]
    logs = _clean_numeric(logs, numeric_cols)

    # Add snap data when available.
    if not snaps.empty:
        s = snaps.copy()
        if "player_display_name" in s.columns and "player" not in s.columns:
            s["player"] = s["player_display_name"]
        elif "pfr_player_name" in s.columns and "player" not in s.columns:
            s["player"] = s["pfr_player_name"]
        snap_cols = [c for c in ["season","week","team","player","offense_snaps","offense_pct","defense_snaps","defense_pct","st_snaps","st_pct"] if c in s.columns]
        s = s[snap_cols].copy()
        join_cols = [c for c in ["season","week","team","player"] if c in logs.columns and c in s.columns]
        if len(join_cols) >= 3:
            logs = logs.merge(s.drop_duplicates(join_cols), on=join_cols, how="left", suffixes=("","_snap"))

    # Build player summaries.
    group_cols = ["player","team","position"]
    sum_cols = _phase6_sum_cols(logs, numeric_cols + ["offense_snaps","defense_snaps","st_snaps"])
    summary = logs.groupby(group_cols, dropna=False)[sum_cols].sum(numeric_only=True).reset_index() if sum_cols else logs[group_cols].drop_duplicates()
    games = logs.groupby(group_cols, dropna=False)["week"].nunique().reset_index(name="games_played") if "week" in logs.columns else summary[group_cols].assign(games_played=17)
    summary = summary.merge(games, on=group_cols, how="left")
    gp = summary["games_played"].replace(0, np.nan)
    per_game_map = {
        "attempts": "pass_attempts_pg", "completions": "completions_pg", "passing_yards": "passing_yards_pg",
        "passing_tds": "passing_tds_pg", "interceptions": "interceptions_pg", "carries": "rush_attempts_pg",
        "rushing_yards": "rushing_yards_pg", "targets": "targets_pg", "receptions": "receptions_pg",
        "receiving_yards": "receiving_yards_pg", "air_yards": "air_yards_pg", "fantasy_points_ppr": "fantasy_points_pg",
        "fantasy_points": "fantasy_points_std_pg"
    }
    for src, dst in per_game_map.items():
        if src in summary.columns:
            summary[dst] = (summary[src] / gp).round(3).fillna(0)

    if "targets" in summary.columns:
        team_targets = summary.groupby("team")["targets"].transform("sum").replace(0, np.nan)
        summary["target_share"] = ((summary["targets"] / team_targets) * 100).round(2).fillna(0)
    if "air_yards" in summary.columns:
        team_air = summary.groupby("team")["air_yards"].transform("sum").replace(0, np.nan)
        summary["air_yards_share"] = ((summary["air_yards"] / team_air) * 100).round(2).fillna(0)
    if "carries" in summary.columns:
        team_carries = summary.groupby("team")["carries"].transform("sum").replace(0, np.nan)
        summary["carries_share"] = ((summary["carries"] / team_carries) * 100).round(2).fillna(0)
    if "offense_snaps" in summary.columns:
        team_max_snap = summary.groupby("team")["offense_snaps"].transform("max").replace(0, np.nan)
        summary["snap_share"] = ((summary["offense_snaps"] / team_max_snap) * 100).round(2).fillna(0)
    else:
        summary["snap_share"] = 0
    pos = summary.get("position", pd.Series([""]*len(summary))).astype(str).str.upper()
    summary["route_participation"] = np.where(pos.isin(["WR","TE"]), np.minimum(98, summary["snap_share"].fillna(0) + 6), summary["snap_share"].fillna(0))

    # PBP advanced context, red-zone, OT, trench.
    pbp_team_context, team_adv, defense_adv, red_zone, ot, trench = _build_pbp_context(pbp)
    if not red_zone.empty:
        rz_small = red_zone[[c for c in ["team","player","red_zone_touch_share","red_zone_carries","red_zone_targets","red_zone_pass_attempts","goal_line_touches"] if c in red_zone.columns]].copy()
        summary = summary.merge(rz_small, on=["team","player"], how="left")
    if "red_zone_touch_share" not in summary.columns:
        summary["red_zone_touch_share"] = 0
    summary["red_zone_touch_share"] = pd.to_numeric(summary["red_zone_touch_share"], errors="coerce").fillna(0)

    # Team offense context from weekly if PBP is unavailable; prefer PBP fields when present.
    team_rows = []
    for team, g in logs.groupby("team"):
        if not str(team).strip():
            continue
        games_team = max(1, g["week"].nunique() if "week" in g.columns else 17)
        pass_att = safe_float(g["attempts"].sum()) if "attempts" in g.columns else 0
        rush_att = safe_float(g["carries"].sum()) if "carries" in g.columns else 0
        plays = (pass_att or 0) + (rush_att or 0)
        team_rows.append({
            "team": str(team),
            "plays_pg": round(plays/games_team,2),
            "pass_rate": round(100*(pass_att or 0)/max(1,plays),2),
            "rush_rate": round(100*(rush_att or 0)/max(1,plays),2),
            "pass_attempts_pg": round((pass_att or 0)/games_team,2),
            "rush_attempts_pg": round((rush_att or 0)/games_team,2),
        })
    team_df = pd.DataFrame(team_rows)
    if not team_adv.empty:
        team_df = team_df.merge(team_adv, on="team", how="outer") if not team_df.empty else team_adv.copy()
        # fill main projection fields with richer PBP names when available
        for src, dst in [("pbp_plays_pg","plays_pg"),("pbp_pass_rate","pass_rate"),("pbp_rush_rate","rush_rate")]:
            if src in team_df.columns:
                team_df[dst] = pd.to_numeric(team_df.get(dst), errors="coerce").fillna(pd.to_numeric(team_df[src], errors="coerce"))

    # Defense ranks allowed by opponent from weekly.
    def_rows = []
    if "opp" in logs.columns and str(logs["opp"].fillna("").sum()).strip():
        for opp, g in logs.groupby("opp"):
            games_opp = max(1, g["week"].nunique() if "week" in g.columns else 17)
            def_rows.append({
                "team": str(opp),
                "pass_yards_allowed_pg": round((g["passing_yards"].sum() if "passing_yards" in g.columns else 0)/games_opp,2),
                "rush_yards_allowed_pg": round((g["rushing_yards"].sum() if "rushing_yards" in g.columns else 0)/games_opp,2),
                "rec_yards_allowed_pg": round((g["receiving_yards"].sum() if "receiving_yards" in g.columns else 0)/games_opp,2),
                "receptions_allowed_pg": round((g["receptions"].sum() if "receptions" in g.columns else 0)/games_opp,2),
            })
    defense = pd.DataFrame(def_rows)
    if not defense.empty:
        defense["def_pass_rank"] = defense["pass_yards_allowed_pg"].rank(method="min", ascending=True).astype(int)
        defense["def_run_rank"] = defense["rush_yards_allowed_pg"].rank(method="min", ascending=True).astype(int)
        defense["def_role_rank"] = defense["rec_yards_allowed_pg"].rank(method="min", ascending=True).astype(int)
    if not defense_adv.empty:
        defense = defense.merge(defense_adv, on="team", how="outer") if not defense.empty else defense_adv.copy()

    team_context = {}
    for _, r in team_df.iterrows() if not team_df.empty else []:
        team = str(r.get("team"))
        d = {k: (None if pd.isna(v) else float(v) if isinstance(v, (np.floating, float)) else int(v) if isinstance(v, (np.integer, int)) else v) for k,v in r.items() if k != "team"}
        if not defense.empty and team in set(defense["team"].astype(str)):
            dr = defense[defense["team"].astype(str) == team].iloc[0].to_dict()
            for k,v in dr.items():
                if k != "team" and pd.notna(v):
                    d[k] = int(v) if isinstance(v,(np.integer,int)) else float(v) if isinstance(v,(np.floating,float)) else v
        if team in pbp_team_context:
            d.update(pbp_team_context[team])
        team_context[team] = d

    # Travel/stadium context from schedules.
    travel_rows = []
    if not schedules.empty:
        for _, r in schedules.iterrows():
            away = r.get("away_team")
            home = r.get("home_team")
            if not away or not home:
                continue
            env = STADIUM_ENV.get(str(home), {})
            travel_rows.append({
                "season": season,
                "week": r.get("week"),
                "matchup": f"{away} @ {home}",
                "away_team": away,
                "home_team": home,
                "travel_miles": great_circle_miles(away, home),
                "stadium": env.get("stadium", ""),
                "crowd": env.get("crowd", ""),
                "noise": env.get("noise", 1.0),
                "roof": env.get("roof", ""),
                "surface": env.get("surface", ""),
                "altitude": env.get("altitude", 0),
                "game_date": r.get("gameday") or r.get("game_date") or r.get("game_id"),
            })
    travel = pd.DataFrame(travel_rows)

    # Save Phase 6 files and app-ready hooks.
    logs.to_csv(PHASE6_PLAYER_LOG_FILE, index=False)
    summary.to_csv(PHASE6_PLAYER_SUMMARY_FILE, index=False)
    if not defense.empty:
        defense.to_csv(PHASE6_DEFENSE_RANK_FILE, index=False)
    if not travel.empty:
        travel.to_csv(PHASE6_TRAVEL_FILE, index=False)
    if not team_adv.empty:
        team_adv.to_csv(PHASE6_TEAM_ADVANCED_FILE, index=False)
    if not trench.empty:
        trench.to_csv(PHASE6_TRENCH_FILE, index=False)
    if not red_zone.empty:
        red_zone.to_csv(PHASE6_RED_ZONE_FILE, index=False)
    if not ot.empty:
        ot.to_csv(PHASE6_OT_FILE, index=False)

    app_usage_cols = [c for c in [
        "player","team","position","snap_share","route_participation","target_share","air_yards_share",
        "red_zone_touch_share","red_zone_carries","red_zone_targets","goal_line_touches",
        "targets_pg","receptions_pg","rush_attempts_pg","carries_share",
        "pass_attempts_pg","passing_yards_pg","rushing_yards_pg","receiving_yards_pg","fantasy_points_pg"
    ] if c in summary.columns]
    if app_usage_cols:
        # App-ready usage file should not be dominated by rows with all zeros.
        usage = summary[app_usage_cols].copy()
        usage_numeric = usage.select_dtypes(include=[np.number]).columns.tolist()
        if usage_numeric:
            usage = usage[usage[usage_numeric].abs().sum(axis=1) > 0].copy()
        usage.to_csv(USAGE_FILE, index=False)
    save_json(TEAM_CONTEXT_FILE, team_context)
    save_json(PHASE6_TEAM_CONTEXT_FILE, team_context)
    _save_position_specific_phase6_tables(summary)

    manifest = {
        "built_at": now_iso(),
        "season": season,
        "player_log_file": str(PHASE6_PLAYER_LOG_FILE),
        "player_summary_file": str(PHASE6_PLAYER_SUMMARY_FILE),
        "usage_file": str(USAGE_FILE),
        "team_context_file": str(TEAM_CONTEXT_FILE),
        "defense_rank_file": str(PHASE6_DEFENSE_RANK_FILE),
        "travel_file": str(PHASE6_TRAVEL_FILE),
        "team_advanced_file": str(PHASE6_TEAM_ADVANCED_FILE),
        "trench_file": str(PHASE6_TRENCH_FILE),
        "red_zone_file": str(PHASE6_RED_ZONE_FILE),
        "overtime_file": str(PHASE6_OT_FILE),
        "players": int(summary["player"].nunique()) if "player" in summary.columns else int(len(summary)),
        "teams": int(len(team_context)),
        "player_week_rows": int(len(logs)),
        "schedule_rows": int(len(schedules)),
        "snap_rows": int(len(snaps)),
        "pbp_rows": int(len(pbp)),
        "player_week_source": weekly_source,
        "schedule_source": schedule_source,
    }
    save_json(PHASE6_MANIFEST_FILE, manifest)
    diag.update({"status": "BUILT_AND_SAVED", **manifest})
    return diag



# ---------- Phase 6 Builder 3.0: persistent GitHub-style database layer ----------
# This layer intentionally mirrors the MLB workflow: pull/build once, save the resulting
# files, then reuse local/GitHub-committed files on every app start. It also lets you
# hard-input a completed phase6_nfl_database folder into GitHub later without changing code.
PHASE6_REQUIRED_FILES = [
    "nfl_player_summary_last_season.csv",
    "nfl_team_context_last_season.json",
]
PHASE6_OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE", "FB", "K"}

# Keep the v2.2 builder as a fallback, then override build_phase6_nfl_database below.
_phase6_v22_build = build_phase6_nfl_database


def _phase6_candidate_database_dirs():
    """Places the app will look for a saved Phase 6 database.

    This supports both workflows:
    1) App-built database in STORAGE_DIR/nfl_engine/phase6_nfl_database.
    2) GitHub hard-input database committed into ./phase6_nfl_database or
       ./nfl_engine/phase6_nfl_database.
    """
    cwd = Path.cwd()
    candidates = [
        PHASE6_DIR,
        cwd / "phase6_nfl_database",
        cwd / "nfl_engine" / "phase6_nfl_database",
        Path("phase6_nfl_database"),
        Path("nfl_engine") / "phase6_nfl_database",
    ]
    out = []
    seen = set()
    for d in candidates:
        try:
            r = d.resolve()
        except Exception:
            r = d
        if str(r) not in seen:
            seen.add(str(r)); out.append(d)
    return out


def _phase6_file_has_rows(path, min_rows=1):
    try:
        if not Path(path).exists() or Path(path).stat().st_size <= 50:
            return False
        if str(path).lower().endswith(".json"):
            data = load_json(path, {})
            return bool(data) and len(data) >= min_rows
        df = pd.read_csv(path, nrows=min_rows + 2)
        return len(df) >= min_rows
    except Exception:
        return False


def _phase6_database_quality(db_dir):
    """Score whether a database is actually useful, not just present.

    A useful DB should have a non-empty summary, app usage hooks, and team context.
    Player logs are preferred, but not required if summary+usage exist from a committed
    GitHub database. The manifest reports all counts for transparency.
    """
    db_dir = Path(db_dir)
    summary_path = db_dir / "nfl_player_summary_last_season.csv"
    logs_path = db_dir / "nfl_player_logs_last_season.csv"
    team_context_path = db_dir / "nfl_team_context_last_season.json"
    if not _phase6_file_has_rows(summary_path, 5):
        return False, {"reason": "missing_or_empty_player_summary"}
    try:
        summary = pd.read_csv(summary_path)
    except Exception as e:
        return False, {"reason": f"summary_read_error: {e}"}
    if summary.empty or "player" not in summary.columns:
        return False, {"reason": "bad_player_summary_schema"}
    offensive_rows = summary.copy()
    if "position" in offensive_rows.columns:
        offensive_rows = offensive_rows[offensive_rows["position"].astype(str).str.upper().isin(PHASE6_OFFENSIVE_POSITIONS)]
    numeric_cols = [c for c in offensive_rows.select_dtypes(include=[np.number]).columns if c not in ["season"]]
    non_zero_rows = 0
    if numeric_cols:
        non_zero_rows = int((offensive_rows[numeric_cols].abs().sum(axis=1) > 0).sum())
    team_context = load_json(team_context_path, {}) if team_context_path.exists() else {}
    quality = {
        "summary_rows": int(len(summary)),
        "offensive_rows": int(len(offensive_rows)),
        "non_zero_offensive_rows": non_zero_rows,
        "player_log_rows": int(len(pd.read_csv(logs_path, usecols=[0])) if logs_path.exists() and logs_path.stat().st_size > 50 else 0),
        "team_context_teams": int(len(team_context)) if isinstance(team_context, dict) else 0,
    }
    # Be practical: early/preseason database can still be useful if summary has real rows.
    ok = quality["non_zero_offensive_rows"] >= 25 and quality["team_context_teams"] >= 16
    if not ok:
        quality["reason"] = "database_present_but_low_quality_or_mostly_zero"
    return ok, quality


def _phase6_install_database_from_dir(src_dir):
    """Copy a committed/local database into the active STORAGE_DIR database folder."""
    src_dir = Path(src_dir)
    if not src_dir.exists():
        return False, {"reason": "source_dir_missing", "source": str(src_dir)}
    try:
        PHASE6_DIR.mkdir(parents=True, exist_ok=True)
        copied = []
        for file in src_dir.glob("*"):
            if file.is_file() and file.suffix.lower() in [".csv", ".json", ".parquet"]:
                target = PHASE6_DIR / file.name
                if file.resolve() != target.resolve():
                    target.write_bytes(file.read_bytes())
                    copied.append(file.name)
        # Copy app hooks if they are bundled there.
        usage_src = src_dir / "nfl_player_usage.csv"
        team_src = src_dir / "nfl_team_context.json"
        if usage_src.exists():
            USAGE_FILE.write_bytes(usage_src.read_bytes()); copied.append("nfl_player_usage.csv -> app hook")
        if team_src.exists():
            TEAM_CONTEXT_FILE.write_bytes(team_src.read_bytes()); copied.append("nfl_team_context.json -> app hook")
        # If only last-season names exist, install hooks from them.
        summary = PHASE6_DIR / "nfl_player_summary_last_season.csv"
        team_last = PHASE6_DIR / "nfl_team_context_last_season.json"
        if summary.exists() and not USAGE_FILE.exists():
            try:
                df = pd.read_csv(summary)
                app_usage_cols = [c for c in [
                    "player","team","position","snap_share","route_participation","target_share","air_yards_share",
                    "red_zone_touch_share","red_zone_carries","red_zone_targets","goal_line_touches",
                    "targets_pg","receptions_pg","rush_attempts_pg","carries_share",
                    "pass_attempts_pg","passing_yards_pg","rushing_yards_pg","receiving_yards_pg","fantasy_points_pg"
                ] if c in df.columns]
                if app_usage_cols:
                    df[app_usage_cols].to_csv(USAGE_FILE, index=False)
            except Exception as e:
                request_log("PHASE6_INSTALL_USAGE", "ERROR", e)
        if team_last.exists() and not TEAM_CONTEXT_FILE.exists():
            TEAM_CONTEXT_FILE.write_bytes(team_last.read_bytes())
        _save_position_specific_phase6_tables(pd.read_csv(summary) if summary.exists() else pd.DataFrame())
        ok, quality = _phase6_database_quality(PHASE6_DIR)
        return ok, {"source": str(src_dir), "copied": copied, **quality}
    except Exception as e:
        return False, {"reason": f"install_error: {e}", "source": str(src_dir)}


def _phase6_try_use_bundled_database():
    """Use a GitHub-hard-input database folder if present."""
    for d in _phase6_candidate_database_dirs():
        # Skip active dir here; active dir is checked separately.
        try:
            if d.resolve() == PHASE6_DIR.resolve():
                continue
        except Exception:
            pass
        ok, quality = _phase6_database_quality(d)
        if ok:
            installed, install_info = _phase6_install_database_from_dir(d)
            if installed:
                manifest = {
                    "built_at": now_iso(),
                    "status": "USING_GITHUB_HARD_INPUT_DATABASE",
                    "source_database_dir": str(d),
                    **install_info,
                }
                save_json(PHASE6_MANIFEST_FILE, manifest)
                return manifest
    return None


def _phase6_export_database_zip():
    """Create a zip the user can commit to GitHub or store as backup."""
    import zipfile
    PHASE6_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = PHASE6_DIR / "phase6_nfl_database_export.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in PHASE6_DIR.glob("*"):
            if file.is_file() and file.name != zip_path.name:
                z.write(file, arcname=file.name)
        if USAGE_FILE.exists():
            z.write(USAGE_FILE, arcname="nfl_player_usage.csv")
        if TEAM_CONTEXT_FILE.exists():
            z.write(TEAM_CONTEXT_FILE, arcname="nfl_team_context.json")
    return zip_path


def build_phase6_nfl_database(season=NFL_LAST_SEASON, force_refresh=False):
    """Phase 6 Builder 3.0.

    Behavior:
    - If saved local DB is good, use it and do not download.
    - If a DB folder is hard-input/committed to GitHub, install/use it.
    - Otherwise download/build once, save, and reuse forever until Force Refresh.
    """
    season = int(season)
    if not force_refresh:
        ok, quality = _phase6_database_quality(PHASE6_DIR)
        if ok:
            manifest = load_json(PHASE6_MANIFEST_FILE, {})
            return {"season": season, "status": "USING_SAVED_LOCAL_DATABASE", "message": "Saved Phase 6 files found. No download was needed.", **quality, **manifest}
        bundled = _phase6_try_use_bundled_database()
        if bundled:
            return {"season": season, "status": "USING_GITHUB_HARD_INPUT_DATABASE", "message": "Found a committed Phase 6 database folder and installed it into the active app database.", **bundled}

    # Build from web using v2.2 pipeline. If selected season fails, try previous season as a fallback
    # so the model never goes in blind while a new/current season file is unavailable.
    diag = _phase6_v22_build(season, force_refresh=force_refresh)
    ok, quality = _phase6_database_quality(PHASE6_DIR)
    if ok:
        diag.update({"status": "BUILT_AND_SAVED_PHASE6_V3", **quality})
        save_json(PHASE6_MANIFEST_FILE, diag)
        return diag
    if force_refresh and season > 1999:
        prev = season - 1
        request_log("PHASE6_V3", "TRY_PREVIOUS_SEASON", f"{season} quality failed; trying {prev}")
        diag2 = _phase6_v22_build(prev, force_refresh=True)
        ok2, quality2 = _phase6_database_quality(PHASE6_DIR)
        if ok2:
            diag2.update({"status": "BUILT_PREVIOUS_SEASON_AND_SAVED_PHASE6_V3", "requested_season": season, "built_season": prev, **quality2})
            save_json(PHASE6_MANIFEST_FILE, diag2)
            return diag2
    diag.update({"status": "PHASE6_BUILD_FAILED_OR_LOW_QUALITY", **quality})
    return diag


# ---------- optional real NFL data loaders ----------
def _read_optional_csv(path):
    try:
        if Path(path).exists():
            df=pd.read_csv(path)
            df.columns=[str(c).strip() for c in df.columns]
            return df
    except Exception as e:
        request_log(path, "CSV_LOAD_ERROR", e)
    return pd.DataFrame()

def load_usage_bank():
    df=_read_optional_csv(USAGE_FILE)
    if df.empty:
        return {}
    bank={}
    for _,r in df.iterrows():
        d={k:r.get(k) for k in df.columns}
        key=norm(d.get("player"))
        if key:
            bank[key]=d
    return bank

def load_team_context():
    data=load_json(TEAM_CONTEXT_FILE,{})
    return data if isinstance(data,dict) else {}

def load_injury_bank():
    data=load_json(INJURY_FILE,{})
    return data if isinstance(data,dict) else {}

def merge_nfl_context(row):
    """Attach real usage/team/injury context when local files exist. Missing data stays neutral."""
    row=dict(row or {})
    usage=load_usage_bank().get(norm(row.get("player")), {})
    for k,v in usage.items():
        if k and k not in row or row.get(k) in [None, ""]:
            row[k]=v
    teams=load_team_context()
    team=str(row.get("team") or "")
    opp=str(row.get("opp") or "")
    team_ctx=teams.get(team,{}) if isinstance(teams.get(team,{}),dict) else {}
    opp_ctx=teams.get(opp,{}) if isinstance(teams.get(opp,{}),dict) else {}
    # Team offense / pace / Vegas / coach-style context.  These are NFL-specific
    # equivalents of the MLB environment and lineup context layers.
    team_keys = [
        "pace","pass_rate","rush_rate","plays_pg","spread","game_total","team_total",
        "weather_risk","pbp_plays_pg","pbp_pass_rate","pbp_rush_rate",
        "epa_per_play","success_rate","early_down_pass_rate","early_down_success_rate",
        "red_zone_pass_rate","goal_line_rush_rate","penalties_pg","fumbles_pg",
        "sacks_allowed_pg","qb_hits_allowed_pg","explosive_pass_rate","explosive_rush_rate",
        "ot_rate","team_identity","coach_pace_proxy","seconds_per_play","no_huddle_rate"
    ]
    for k in team_keys:
        if row.get(k) in [None, ""] and team_ctx.get(k) not in [None, ""]:
            row[k]=team_ctx.get(k)

    # Opponent defensive context.  Prefix advanced fields with opp_ so we never
    # accidentally confuse offense and defense.
    opp_keys = [
        "def_pass_rank","def_run_rank","def_slot_rank","def_te_rank","def_rb_rec_rank",
        "def_role_rank","coverage_grade","pressure_rate","def_pressure_rate",
        "def_epa_allowed_per_play","def_success_allowed_rate","def_sacks_pg",
        "def_qb_hits_pg","def_fumbles_forced_pg","explosive_pass_allowed_rate",
        "explosive_rush_allowed_rate","def_explosive_pass_rank","def_explosive_run_rank",
        "def_pressure_rank","def_run_stop_rank"
    ]
    for k in opp_keys:
        if opp_ctx.get(k) not in [None, ""]:
            row.setdefault(k, opp_ctx.get(k))
            row.setdefault("opp_"+k, opp_ctx.get(k))
    injuries=load_injury_bank()
    inj=injuries.get(norm(row.get("player"))) or injuries.get(str(row.get("player") or ""))
    if inj and row.get("injury_status") in [None, ""]:
        row["injury_status"] = inj.get("status") if isinstance(inj,dict) else inj
    return row

def apply_real_usage_to_role(row, role):
    role=dict(role or {})
    mapping={
        "snap_share":"snap", "route_participation":"route", "target_share":"target",
        "carries_share":"carry", "red_zone_touch_share":"rz", "air_yards_share":"air",
        "pressure_rate":"pressure", "pace":"pace"
    }
    for src,dst in mapping.items():
        v=safe_float(row.get(src))
        if v is not None:
            role[dst]=float(clamp(v,0,100))
    ol=safe_float(row.get("ol_rank"))
    if ol is not None:
        # Lower rank is better; convert to 0-100 protection score.
        role["ol"]=float(clamp(74 - (ol-1)*1.5, 22, 78))
    return role

def usage_data_quality(row, prop):
    needed=ROLE_SAFETY_MINIMUMS.get(prop,{})
    have=0; total=max(1,len(needed))
    flags=[]
    for k,min_v in needed.items():
        v=safe_float(row.get(k))
        if v is not None:
            have+=1
            if v < min_v:
                flags.append(f"{k} below safe mark ({v:g} < {min_v:g})")
        else:
            flags.append(f"missing {k}")
    q=int(clamp(45 + (have/total)*45, 0, 100))
    # Strong bonus if we have core advanced fields.
    bonus=sum(1 for k in ["air_yards_share","red_zone_touch_share","spread","game_total","def_role_rank","coverage_grade"] if safe_float(row.get(k)) is not None)
    q=int(clamp(q + min(10, bonus*2), 0, 100))
    return q, flags[:5]

def opportunity_engine(row, role, prop):
    """Estimate the opportunity behind the prop before translating to yards/stats.

    This is the NFL equivalent of MLB volume logic: for props, opportunity is usually
    more predictive than raw yards.  The function is intentionally small and capped so
    it improves projections without overpowering live lines.
    """
    notes=[]
    pos=str(row.get("position") or "").upper()
    plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
    rush_rate=safe_float(row.get("pbp_rush_rate"), safe_float(row.get("rush_rate"), 44)) or 44
    spread=safe_float(row.get("spread"), 0) or 0
    total=safe_float(row.get("game_total"), 44) or 44

    snap=safe_float(row.get("snap_share"), role.get("snap",70)) or role.get("snap",70)
    route=safe_float(row.get("route_participation"), role.get("route",60)) or role.get("route",60)
    target=safe_float(row.get("target_share"), role.get("target",15)) or role.get("target",15)
    carry=safe_float(row.get("carries_share"), role.get("carry",10)) or role.get("carry",10)
    rz=safe_float(row.get("red_zone_touch_share"), role.get("rz",10)) or role.get("rz",10)

    # Simple expected opportunity estimates shown on cards/debug tables.
    dropbacks = plays * pass_rate/100.0
    rush_team = plays * rush_rate/100.0
    expected = {
        "plays_pg": round(plays,2),
        "dropbacks_pg": round(dropbacks,2),
        "team_rushes_pg": round(rush_team,2),
        "routes_pg": round(dropbacks * route/100.0,2),
        "targets_pg_est": round(dropbacks * target/100.0,2),
        "carries_pg_est": round(rush_team * carry/100.0,2),
        "rz_usage": round(rz,2),
        "snap_share": round(snap,2),
    }

    factor=1.0
    if prop in ["Passing Yards","Passing TDs","Pass Attempts","Completions","Interceptions"]:
        factor *= clamp(1 + (dropbacks-34)*0.006, 0.90, 1.10)
        if pass_rate >= 61: notes.append("Opportunity boost: pass-first team profile")
        elif pass_rate <= 51: notes.append("Opportunity tax: low pass-rate profile")
    elif prop in ["Receiving Yards","Receptions","Longest Reception"]:
        factor *= clamp(1 + (expected["routes_pg"]-25)*0.004, 0.90, 1.10)
        factor *= clamp(1 + (expected["targets_pg_est"]-5.5)*0.018, 0.88, 1.12)
        if target >= 24: notes.append("Opportunity boost: elite target share")
        elif target < 13: notes.append("Opportunity warning: thin target share")
    elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        factor *= clamp(1 + (expected["carries_pg_est"]-12)*0.018, 0.86, 1.14)
        if carry >= 55: notes.append("Opportunity boost: workhorse carry share")
        elif carry < 30: notes.append("Opportunity warning: committee rushing role")
    elif prop in ["Fantasy Points","Anytime TD"]:
        blended = (snap/100)*0.40 + (target/30)*0.25 + (carry/65)*0.20 + (rz/35)*0.15
        factor *= clamp(0.88 + blended*0.24, 0.86, 1.14)
    elif prop in ["Kicking Points","Field Goals Made"]:
        factor *= clamp(1 + (total-44)*0.008, 0.90, 1.10)

    # Pre-adjustment for likely trailing/favorite scripts.  Game script simulator below
    # handles the larger branch logic; this just reflects base opportunity.
    if spread > 5.5 and prop in ["Passing Yards","Receiving Yards","Receptions","Pass Attempts","Completions"]:
        factor *= 1.025; notes.append("Opportunity boost: projected trailing pass volume")
    if spread < -7.5 and prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        factor *= 1.025; notes.append("Opportunity boost: favorite rushing script")

    return {"factor": clamp(factor,0.82,1.18), "expected": expected, "notes": notes}

def pace_engine(row, prop):
    """Team pace / total-play context."""
    factor=1.0; notes=[]; risk="LOW"
    plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), None))
    seconds=safe_float(row.get("seconds_per_play"))
    pace_label=str(row.get("coach_pace_proxy") or "").upper()
    no_huddle=safe_float(row.get("no_huddle_rate"))
    if plays is not None:
        if plays >= 65: factor*=1.035; notes.append("Pace boost: high play-volume offense")
        elif plays <= 57: factor*=0.955; risk="MED"; notes.append("Pace tax: low play-volume offense")
    if seconds is not None:
        if seconds <= 26: factor*=1.018; notes.append("Pace boost: fast seconds/play")
        elif seconds >= 31: factor*=0.982; notes.append("Pace tax: slow seconds/play")
    if pace_label == "FAST": factor*=1.015; notes.append("Coach pace proxy: FAST")
    elif pace_label == "SLOW": factor*=0.985; notes.append("Coach pace proxy: SLOW")
    if no_huddle is not None and no_huddle >= 12:
        factor*=1.01; notes.append("No-huddle pace nudge")
    return clamp(factor,0.93,1.07), risk, notes

def vegas_environment_engine(row, prop):
    """Spread, total and implied team-volume context."""
    factor=1.0; notes=[]; risk="LOW"
    spread=safe_float(row.get("spread"))
    total=safe_float(row.get("game_total"))
    team_total=safe_float(row.get("team_total"))
    if total is not None:
        if total >= 49 and prop not in ["Interceptions"]:
            factor*=1.025; notes.append("Vegas boost: high total")
        elif total <= 39 and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Fantasy Points","Pass Attempts","Completions"]:
            factor*=0.955; risk="HIGH"; notes.append("Vegas tax: low total")
    if team_total is not None:
        if team_total >= 27 and prop in ["Passing TDs","Anytime TD","Fantasy Points","Kicking Points","Field Goals Made"]:
            factor*=1.025; notes.append("Team-total scoring boost")
        elif team_total <= 18.5 and prop in ["Passing TDs","Anytime TD","Fantasy Points"]:
            factor*=0.94; risk="HIGH"; notes.append("Low team-total scoring tax")
    if spread is not None and abs(spread) <= 3:
        factor*=1.01; notes.append("Close spread: full-game volume stability")
    return clamp(factor,0.90,1.08), risk, notes

def game_script_simulator(row, prop):
    """Small three-branch game-script model: close, trailing, blowout/favorite.

    This is not a moneyline model; it is a volume model for props. It nudges
    attempts/targets/carries based on likely script without changing raw database data.
    """
    spread=safe_float(row.get("spread"), 0) or 0
    total=safe_float(row.get("game_total"), 44) or 44
    pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"),56)) or 56
    factor=1.0; notes=[]; risk="LOW"
    close_weight=clamp(0.62 - abs(spread)*0.025, 0.28, 0.70)
    trailing_weight=clamp(0.19 + max(spread,0)*0.035, 0.12, 0.48)
    leading_weight=clamp(1.0 - close_weight - trailing_weight, 0.10, 0.50)
    branch={"close":round(close_weight,3),"trailing":round(trailing_weight,3),"leading":round(leading_weight,3)}

    if prop in ["Passing Yards","Receiving Yards","Receptions","Pass Attempts","Completions","Longest Reception"]:
        branch_factor = close_weight*1.00 + trailing_weight*1.07 + leading_weight*0.94
        factor *= branch_factor
        if trailing_weight >= 0.34: notes.append("Game script boost: trailing pass-volume branch")
        if leading_weight >= 0.36: notes.append("Game script tax: leading/blowout pass-volume branch")
    elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        branch_factor = close_weight*1.00 + trailing_weight*0.90 + leading_weight*1.07
        factor *= branch_factor
        if leading_weight >= 0.36: notes.append("Game script boost: favorite/lead rushing branch")
        if trailing_weight >= 0.34: notes.append("Game script tax: trailing rush-volume branch")
    elif prop in ["Passing TDs","Anytime TD","Fantasy Points"]:
        factor *= close_weight*1.00 + trailing_weight*1.02 + leading_weight*0.99

    if abs(spread) >= 8.5:
        risk="HIGH"; notes.append("High blowout-risk branch")
    elif abs(spread) >= 6.5:
        risk="MED"; notes.append("Moderate blowout-risk branch")
    if total >= 50 and prop not in ["Interceptions"]:
        factor*=1.01; notes.append("Shootout environment nudge")
    return clamp(factor,0.86,1.12), risk, notes, branch

def blowout_risk_engine(row, prop):
    spread=safe_float(row.get("spread"), 0) or 0
    factor=1.0; notes=[]; risk="LOW"
    blowout_prob=clamp((abs(spread)-3.0)/12.0, 0.04, 0.45)
    if abs(spread) >= 7.5:
        risk="HIGH"
        if spread < 0 and prop in ["Passing Yards","Receiving Yards","Receptions","Pass Attempts","Completions","Longest Reception"]:
            factor*=0.965; notes.append("Blowout tax: favorite may reduce late passing")
        if spread < 0 and prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
            factor*=1.025; notes.append("Blowout boost: favorite late rushing volume")
        if spread > 0 and prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
            factor*=0.945; notes.append("Blowout tax: underdog rushing volume risk")
        if spread > 0 and prop in ["Passing Yards","Receiving Yards","Receptions","Pass Attempts","Completions"]:
            factor*=1.015; notes.append("Blowout boost: underdog catch-up passing")
    return clamp(factor,0.90,1.07), risk, notes, round(blowout_prob,3)

def defensive_matchup_factor(row, prop):
    factor=1.0; notes=[]; risk="LOW"
    rank=safe_float(row.get("def_role_rank"))
    cov=safe_float(row.get("coverage_grade"))
    pass_rank=safe_float(row.get("def_pass_rank"))
    run_rank=safe_float(row.get("def_run_rank"))
    def_epa=safe_float(row.get("opp_def_epa_allowed_per_play"), safe_float(row.get("def_epa_allowed_per_play")))
    def_success=safe_float(row.get("opp_def_success_allowed_rate"), safe_float(row.get("def_success_allowed_rate")))
    pressure_rank=safe_float(row.get("opp_def_pressure_rank"), safe_float(row.get("def_pressure_rank")))
    def_sacks=safe_float(row.get("opp_def_sacks_pg"), safe_float(row.get("def_sacks_pg")))
    explosive_pass=safe_float(row.get("opp_explosive_pass_allowed_rate"), safe_float(row.get("explosive_pass_allowed_rate")))
    explosive_rush=safe_float(row.get("opp_explosive_rush_allowed_rate"), safe_float(row.get("explosive_rush_allowed_rate")))

    if prop in ["Receiving Yards","Receptions","Passing Yards","Passing TDs","Pass Attempts","Completions","Longest Reception"]:
        if rank is not None:
            if rank <= 8: factor*=0.94; risk="HIGH"; notes.append("Tough defensive role matchup")
            elif rank >= 24: factor*=1.045; notes.append("Weak defensive role matchup")
        if cov is not None:
            if cov >= 75: factor*=0.965; risk="HIGH"; notes.append("Strong coverage grade tax")
            elif cov <= 45: factor*=1.025; notes.append("Coverage weakness boost")
        if pass_rank is not None:
            if pass_rank <= 8: factor*=0.975; notes.append("Top pass defense tax")
            elif pass_rank >= 24: factor*=1.02; notes.append("Bottom pass defense boost")
        if pressure_rank is not None and pressure_rank <= 8:
            factor*=0.972; risk="HIGH"; notes.append("Elite opponent pass-rush pressure tax")
        if def_sacks is not None and def_sacks >= 3.0:
            factor*=0.985; notes.append("High sack-rate opponent tax")
        if explosive_pass is not None:
            if explosive_pass >= 9: factor*=1.018; notes.append("Explosive pass allowance boost")
            elif explosive_pass <= 5: factor*=0.988; notes.append("Explosive pass prevention tax")
    if prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        if run_rank is not None:
            if run_rank <= 8: factor*=0.94; risk="HIGH"; notes.append("Top run defense tax")
            elif run_rank >= 24: factor*=1.04; notes.append("Weak run defense boost")
        if explosive_rush is not None:
            if explosive_rush >= 8: factor*=1.02; notes.append("Explosive run allowance boost")
            elif explosive_rush <= 4: factor*=0.985; notes.append("Explosive run prevention tax")
    if def_epa is not None:
        if def_epa <= -0.06: factor*=0.985; notes.append("Defense efficiency tax")
        elif def_epa >= 0.06: factor*=1.015; notes.append("Defense efficiency boost")
    if def_success is not None and def_success >= 48 and prop not in ["Interceptions"]:
        factor*=1.01; notes.append("High success allowed boost")
    return clamp(factor,0.86,1.12), risk, notes

def game_environment_factor(row, prop):
    factor=1.0; notes=[]; risk="LOW"
    spread=safe_float(row.get("spread"))
    total=safe_float(row.get("game_total"))
    pace=safe_float(row.get("pace"))
    pass_rate=safe_float(row.get("pass_rate"))
    weather=str(row.get("weather_risk") or "").upper()
    if pace is not None:
        if pace >= 56: factor*=1.025; notes.append("Fast pace boost")
        elif pace <= 48: factor*=0.975; notes.append("Slow pace tax")
    if pass_rate is not None:
        if prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Pass Attempts","Completions","Longest Reception"]:
            factor*=clamp(1 + (pass_rate-56)*0.004, 0.94, 1.06)
        if prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
            factor*=clamp(1 - (pass_rate-56)*0.003, 0.94, 1.05)
    if total is not None:
        if total >= 48 and prop not in ["Interceptions"]: factor*=1.025; notes.append("High total environment")
        elif total <= 39 and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Fantasy Points"]:
            factor*=0.955; risk="HIGH"; notes.append("Low total offensive environment")
    if spread is not None and abs(spread) >= 8.5:
        risk="HIGH"
        if spread < 0 and prop in ["Passing Yards","Receiving Yards","Receptions"]:
            factor*=0.955; notes.append("Favorite blowout pass-volume branch")
        if spread > 0 and prop == "Rushing Yards":
            factor*=0.94; notes.append("Underdog negative rush-script branch")
    if weather in ["HIGH", "SEVERE", "WIND", "RAIN", "SNOW"]:
        risk="HIGH"
        if prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Pass Attempts","Completions","Longest Reception"]:
            factor*=0.92; notes.append("Weather collapse passing tax")
        elif prop in ["Rushing Yards","Rush Attempts","Longest Rush","Kicking Points","Field Goals Made"]:
            factor*=1.018; notes.append("Bad weather rush/kicking-volume nudge")
    return clamp(factor,0.82,1.12), risk, notes

def simulation_branch_rates(row, prop, injury_risk, game_script_risk):
    collapse=0.10; ceiling=0.07
    if injury_risk == "HIGH": collapse += 0.09
    if injury_risk == "EXTREME": collapse += 0.22
    if game_script_risk == "HIGH": collapse += 0.06
    if str(row.get("weather_risk") or "").upper() in ["HIGH","SEVERE","WIND","RAIN","SNOW"] and prop in ["Passing Yards","Receiving Yards","Receptions"]:
        collapse += 0.08; ceiling -= 0.02
    if safe_float(row.get("game_total"), 44) and safe_float(row.get("game_total"), 44) >= 49:
        ceiling += 0.025
    return clamp(collapse,0.05,0.42), clamp(ceiling,0.02,0.16)

# ---------- projection engine ----------
def player_role_defaults(position, prop):
    pos=(position or "").upper()
    role={"snap":72,"route":60,"target":18,"carry":8,"rz":12,"air":45,"pressure":22,"ol":50,"def":50,"pace":50}
    if pos=="QB": role.update({"snap":100,"route":0,"target":0,"carry":10,"rz":18,"air":0,"pressure":27,"ol":52,"pace":52})
    elif pos=="RB": role.update({"snap":63,"route":42,"target":11,"carry":58,"rz":24,"air":8,"pace":50})
    elif pos in ["WR"]: role.update({"snap":82,"route":86,"target":24,"carry":2,"rz":18,"air":92,"pace":51})
    elif pos in ["TE"]: role.update({"snap":76,"route":72,"target":17,"carry":0,"rz":20,"air":46,"pace":50})
    return role

def environment_for(row):
    team=row.get("team",""); opp=row.get("opp",""); home_away=(row.get("home_away") or "").upper()
    home_team = team if home_away=="HOME" else (opp if opp else team)
    env=STADIUM_ENV.get(home_team, {"stadium":"Unknown Stadium","crowd":"MODERATE","noise":1.0,"surface":"Unknown","roof":"Unknown","altitude":0})
    return env

def apply_environment(base, row, prop):
    env=environment_for(row)
    factor=1.0
    notes=[]
    away=(row.get("home_away") or "").upper()=="AWAY"
    if away and env["crowd"] in ["LOUD","EXTREME"] and prop in ["Passing Yards","Passing TDs","Interceptions","Pass Attempts","Completions"]:
        factor*=env.get("noise",1.0); notes.append(f"Road crowd noise: {env['crowd']}")
    if env.get("roof") in ["Dome","Retractable"] and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Pass Attempts","Completions","Longest Reception"]:
        factor*=1.025; notes.append("Dome/retractable roof pass boost")
    if env.get("altitude",0) >= 4000:
        factor*=1.008; notes.append("Altitude fatigue/pace nudge")
    return base*factor, notes, env

def usage_adjustment(role, prop):
    if prop=="Passing Yards": return 0.82 + role["pace"]/250 + role["ol"]/450 - role["pressure"]/700
    if prop=="Passing TDs": return 0.82 + role["rz"]/150 + role["ol"]/600
    if prop=="Interceptions": return 0.75 + role["pressure"]/120 + max(0,50-role["ol"])/200
    if prop=="Rushing Yards": return 0.70 + role["carry"]/120 + role["snap"]/500
    if prop=="Receiving Yards": return 0.65 + role["route"]/145 + role["target"]/180 + role["air"]/650
    if prop=="Receptions": return 0.70 + role["route"]/180 + role["target"]/115
    if prop=="Anytime TD": return 0.65 + role["rz"]/80 + role["snap"]/700
    if prop=="Pass Attempts": return 0.76 + role["pace"]/180 - max(0, role.get("carry", 0)-55)/500
    if prop=="Completions": return 0.74 + role["pace"]/210 + role["ol"]/650 - role["pressure"]/850
    if prop=="Rush Attempts": return 0.66 + role["carry"]/95 + role["snap"]/650
    if prop=="Longest Reception": return 0.64 + role["route"]/240 + role["air"]/170 + role["target"]/420
    if prop=="Longest Rush": return 0.70 + role["carry"]/150 + role["snap"]/900
    if prop=="Kicking Points": return 0.80 + role["pace"]/310
    if prop=="Field Goals Made": return 0.78 + role["pace"]/360
    if prop=="Tackles + Assists": return 0.80 + role["snap"]/360 + max(0,55-role.get("pace",50))/700
    if prop=="Sacks": return 0.70 + role["pressure"]/95 + max(0, role.get("snap",70)-55)/900
    return 1.0

def learning_scale(player, prop):
    data=load_json(LEARN_FILE,{})
    return safe_float(data.get(f"{norm(player)}|{prop}",1.0),1.0) or 1.0

def role_risk_adjustments(row, role, prop):
    """Small NFL version of MLB run-damage/leash logic: opportunity first, talent second."""
    risk_factor=1.0
    injury_risk="LOW"
    script_risk="LOW"
    notes=[]
    pos=str(row.get("position") or "").upper()

    # Manual/optional fields are supported for later real data feeds. Missing values stay neutral.
    snap=safe_float(row.get("snap_share"), role.get("snap")) or role.get("snap",70)
    route=safe_float(row.get("route_participation"), role.get("route")) or role.get("route",60)
    target=safe_float(row.get("target_share"), role.get("target")) or role.get("target",15)
    carry=safe_float(row.get("carry_share"), role.get("carry")) or role.get("carry",5)
    spread=safe_float(row.get("spread"))
    total=safe_float(row.get("game_total"))
    injury=str(row.get("injury_status") or "").upper()

    if "OUT" in injury or "DOUBTFUL" in injury:
        risk_factor*=0.70; injury_risk="EXTREME"; notes.append("Injury status blocks official play")
    elif "QUESTION" in injury or "LIMIT" in injury:
        risk_factor*=0.88; injury_risk="HIGH"; notes.append("Questionable/limited role risk")

    if prop in ["Receiving Yards","Receptions","Longest Reception"] and route < 65:
        risk_factor*=0.92; notes.append("Route participation below safe threshold")
    if prop in ["Rushing Yards","Rush Attempts","Longest Rush"] and carry < 38:
        risk_factor*=0.90; notes.append("Carry share below safe threshold")
    if prop in ["Passing Yards","Passing TDs","Pass Attempts","Completions"] and role.get("pressure",0) >= 32:
        risk_factor*=0.96; notes.append("Pass-rush pressure tax")

    if spread is not None and abs(spread) >= 7.5:
        script_risk="HIGH"
        if prop in ["Passing Yards","Receiving Yards","Receptions","Pass Attempts","Completions","Longest Reception"] and spread < -7.5:
            risk_factor*=0.96; notes.append("Blowout/low pass-volume risk")
        elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"] and spread > 7.5:
            risk_factor*=0.96; notes.append("Negative game-script rush risk")
    if total is not None and total <= 39 and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Fantasy Points"]:
        risk_factor*=0.97; notes.append("Low game-total environment tax")

    return clamp(risk_factor,0.70,1.05), injury_risk, script_risk, notes

def simulate_prop_distribution(base, sigma, prop, sims, seed, collapse_prob=0.115, ceiling_prob=0.075):
    rng=np.random.default_rng(seed)
    base=max(0.001, safe_float(base,0.001) or 0.001)
    sigma=max(0.05, safe_float(sigma,1) or 1)

    # NFL outcomes are asymmetric: normal core + role/game-script collapse branch.
    if prop in ["Passing TDs","Interceptions","Anytime TD","Field Goals Made","Sacks"]:
        raw=rng.normal(base, sigma, sims)
        sim=np.clip(raw,0,None)
    else:
        core=rng.normal(base, sigma, sims)
        collapse_mask=rng.random(sims) < collapse_prob
        ceiling_mask=rng.random(sims) < ceiling_prob
        core[collapse_mask] *= rng.uniform(0.35,0.72,collapse_mask.sum())
        core[ceiling_mask] *= rng.uniform(1.12,1.38,ceiling_mask.sum())
        sim=np.clip(core,0,None)
    return sim



def _prop_target_columns(prop):
    """Map NFL prop markets to possible actual stat columns in Phase 6 / graded logs."""
    mapping = {
        "Passing Yards": ["passing_yards", "pass_yds", "passing_yards_pg"],
        "Passing TDs": ["passing_tds", "pass_tds", "passing_tds_pg"],
        "Interceptions": ["interceptions", "ints", "interceptions_pg"],
        "Pass Attempts": ["attempts", "pass_attempts", "pass_attempts_pg"],
        "Completions": ["completions", "completions_pg"],
        "Rushing Yards": ["rushing_yards", "rush_yds", "rushing_yards_pg"],
        "Rush Attempts": ["carries", "rush_attempts", "rush_attempts_pg"],
        "Receiving Yards": ["receiving_yards", "rec_yds", "receiving_yards_pg"],
        "Receptions": ["receptions", "receptions_pg"],
        "Fantasy Points": ["fantasy_points_ppr", "fantasy_points", "fantasy_points_pg"],
        "Anytime TD": ["touchdowns", "receiving_tds", "rushing_tds", "passing_tds"],
        "Longest Reception": ["longest_reception", "longest_rec"],
        "Longest Rush": ["longest_rush"],
        "Kicking Points": ["kicking_points"],
        "Field Goals Made": ["fg_made", "field_goals_made"],
    }
    return mapping.get(str(prop), [])

XGB_FEATURE_KEYS = [
    "projection", "line", "edge", "fair_prob", "data_score", "stability_score", "usage_quality",
    "opportunity_score", "pace_factor", "vegas_factor", "game_script_factor", "matchup_factor",
    "blowout_prob", "collapse_prob", "ceiling_prob", "snap_share", "route_participation",
    "target_share", "air_yards_share", "red_zone_touch_share", "targets_pg", "receptions_pg",
    "rush_attempts_pg", "carries_share", "pass_attempts_pg", "spread", "game_total", "team_total",
    "plays_pg", "pbp_plays_pg", "pass_rate", "rush_rate", "def_pass_rank", "def_run_rank",
    "def_role_rank", "pressure_rate", "coverage_grade", "travel_miles", "rest_days",
]


def _numeric_feature(row, key, default=0.0):
    v = safe_float((row or {}).get(key))
    return default if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else float(v)


def _xgb_feature_vector(row):
    return [_numeric_feature(row, k, 0.0) for k in XGB_FEATURE_KEYS]


def _graded_training_rows(prop=None):
    rows = load_json(RESULT_LOG, [])
    out = []
    for r in rows:
        if safe_float(r.get("actual")) is None or safe_float(r.get("projection")) is None:
            continue
        if prop and r.get("prop") != prop:
            continue
        out.append(r)
    return out


def xgboost_assist_projection(row, rule_projection):
    """Optional post-grade XGBoost assist.

    This intentionally does NOT replace Monte Carlo.  The rules/opportunity engine produces
    the first projection, XGBoost learns from your graded results after enough samples, then
    Monte Carlo converts the final blended projection into probabilities.
    """
    enabled = bool(st.session_state.get("xgb_assist_enabled", False))
    if not enabled:
        return float(rule_projection), {"enabled": False, "status": "OFF", "blend": 0.0}

    prop = row.get("prop")
    same_prop = _graded_training_rows(prop)
    all_rows = _graded_training_rows(None)
    train_rows = same_prop if len(same_prop) >= 35 else all_rows
    min_rows = int(st.session_state.get("xgb_min_rows", 50) or 50)
    if len(train_rows) < min_rows:
        return float(rule_projection), {
            "enabled": True,
            "status": f"WARMUP {len(train_rows)}/{min_rows} graded rows",
            "blend": 0.0,
            "sample_count": len(train_rows),
        }

    try:
        X = np.array([_xgb_feature_vector(r) for r in train_rows], dtype=float)
        y = np.array([safe_float(r.get("actual"), safe_float(r.get("projection"), 0)) or 0 for r in train_rows], dtype=float)
        if len(y) < min_rows or np.nanstd(y) <= 0:
            return float(rule_projection), {"enabled": True, "status": "NO_VARIANCE", "blend": 0.0, "sample_count": len(train_rows)}
        try:
            from xgboost import XGBRegressor
            model = XGBRegressor(
                n_estimators=120,
                max_depth=3,
                learning_rate=0.05,
                subsample=0.85,
                colsample_bytree=0.85,
                objective="reg:squarederror",
                random_state=42,
                n_jobs=1,
            )
            model_name = "XGBoost"
        except Exception:
            from sklearn.ensemble import GradientBoostingRegressor
            model = GradientBoostingRegressor(n_estimators=120, max_depth=3, learning_rate=0.05, random_state=42)
            model_name = "GBR fallback"
        model.fit(X, y)
        pred = float(model.predict(np.array([_xgb_feature_vector(row)], dtype=float))[0])
        if not np.isfinite(pred):
            raise ValueError("non-finite ML prediction")
        rule = float(rule_projection)
        # Keep the assist as an assist.  It cannot yank the projection wildly away from the main engine.
        pred = clamp(pred, rule * 0.72, rule * 1.28) if rule > 0 else max(0, pred)
        max_blend = safe_float(st.session_state.get("xgb_blend_weight"), 0.22) or 0.22
        confidence_blend = clamp((len(train_rows) - min_rows) / 250.0, 0.08, max_blend)
        blended = (rule * (1 - confidence_blend)) + (pred * confidence_blend)
        return float(blended), {
            "enabled": True,
            "status": "ACTIVE",
            "model": model_name,
            "rule_projection": round(rule, 3),
            "xgb_projection": round(pred, 3),
            "blend": round(confidence_blend, 3),
            "sample_count": len(train_rows),
            "same_prop_samples": len(same_prop),
        }
    except Exception as e:
        return float(rule_projection), {"enabled": True, "status": f"ERROR: {str(e)[:90]}", "blend": 0.0, "sample_count": len(train_rows)}


def _phase6_consistency_score(player, prop):
    """Historical consistency from saved weekly logs when available."""
    try:
        if not PHASE6_PLAYER_LOG_FILE.exists():
            return 70, "Consistency neutral: no weekly log file loaded"
        cols = ["player", "player_display_name", "position", "week"] + _prop_target_columns(prop)
        # Read only available columns if possible; fall back to full small-ish CSV.
        sample = pd.read_csv(PHASE6_PLAYER_LOG_FILE, nrows=1)
        usecols = [c for c in cols if c in sample.columns]
        df = pd.read_csv(PHASE6_PLAYER_LOG_FILE, usecols=usecols if usecols else None)
        name_col = "player" if "player" in df.columns else "player_display_name" if "player_display_name" in df.columns else None
        if not name_col:
            return 70, "Consistency neutral: no player name column"
        target = next((c for c in _prop_target_columns(prop) if c in df.columns), None)
        if not target:
            return 70, "Consistency neutral: no stat column for prop"
        g = df[df[name_col].map(norm) == norm(player)].copy()
        vals = pd.to_numeric(g[target], errors="coerce").dropna()
        vals = vals[vals >= 0]
        if len(vals) < 5:
            return 70, f"Consistency warming up: {len(vals)} games"
        mean = float(vals.mean())
        std = float(vals.std())
        cv = std / max(1.0, mean)
        score = int(clamp(100 - cv * 48, 30, 96))
        return score, f"Historical consistency score {score} from {len(vals)} games"
    except Exception as e:
        return 70, f"Consistency neutral: {str(e)[:60]}"


def advanced_context_engine(row, prop):
    """Adds the extra football context layer: EP/drive, rest/travel, refs, importance,
    player consistency, and feature validation.
    """
    factor=1.0; notes=[]; risk="LOW"; ctx={}
    total=safe_float(row.get("game_total"))
    team_total=safe_float(row.get("team_total"))
    if team_total is None and total is not None:
        spread=safe_float(row.get("spread"), 0) or 0
        # Approx implied points.  Negative spread means favorite.
        team_total = (total / 2.0) - (spread / 2.0)
    if team_total is not None:
        drives=safe_float(row.get("projected_drives"), 10.4) or 10.4
        ep_drive=team_total / max(7.5, drives)
        ctx["expected_points_per_drive"] = round(ep_drive,3)
        if ep_drive >= 2.45 and prop in ["Passing TDs","Fantasy Points","Anytime TD","Kicking Points","Field Goals Made","Receiving Yards"]:
            factor*=1.018; notes.append("EP/drive boost: strong scoring environment")
        elif ep_drive <= 1.75 and prop in ["Passing TDs","Fantasy Points","Anytime TD","Receiving Yards","Rushing Yards"]:
            factor*=0.975; risk="MED"; notes.append("EP/drive tax: weak scoring environment")

    rest=safe_float(row.get("rest_days"))
    travel=safe_float(row.get("travel_miles"))
    if rest is not None:
        ctx["rest_days"] = rest
        if rest <= 4:
            factor*=0.985; risk="MED"; notes.append("Short-week fatigue tax")
        elif rest >= 10:
            factor*=1.008; notes.append("Extra-rest preparation nudge")
    if travel is not None:
        ctx["travel_miles"] = travel
        if travel >= 2200:
            factor*=0.99; notes.append("Long-travel fatigue tax")
        elif travel <= 350 and travel > 0:
            factor*=1.003; notes.append("Short-travel stability nudge")

    ref_pen=safe_float(row.get("ref_penalty_rate"), safe_float(row.get("ref_flags_pg"), None))
    if ref_pen is not None:
        ctx["ref_penalty_rate"] = ref_pen
        if ref_pen >= 15:
            risk="MED"; factor*=0.994; notes.append("Referee crew: high penalty/drive volatility")
        elif ref_pen <= 10:
            factor*=1.004; notes.append("Referee crew: low-flag pace stability")

    importance=str(row.get("game_importance") or row.get("motivation") or "").upper()
    starters_rest=str(row.get("starters_rest_risk") or "").upper()
    if any(x in importance for x in ["HIGH","PLAYOFF","DIVISION","MUST"]):
        factor*=1.006; notes.append("Game importance nudge: starters/volume more secure")
    if any(x in starters_rest for x in ["HIGH","REST","LIMIT"]):
        factor*=0.935; risk="HIGH"; notes.append("Starter rest / limited role risk")

    consistency, consistency_note = _phase6_consistency_score(row.get("player"), prop)
    ctx["consistency_score"] = consistency
    if consistency <= 48:
        risk="MED"; notes.append(consistency_note + " — volatility tax")
    elif consistency >= 82:
        notes.append(consistency_note + " — stable profile")

    # Feature validation: tells you when the projection is leaning on defaults.
    required_by_prop = {
        "Passing Yards": ["pass_attempts_pg","pass_rate","game_total","def_pass_rank"],
        "Passing TDs": ["pass_attempts_pg","red_zone_pass_rate","team_total","def_pass_rank"],
        "Interceptions": ["pass_attempts_pg","pressure_rate","def_pressure_rate"],
        "Rushing Yards": ["rush_attempts_pg","carries_share","rush_rate","def_run_rank"],
        "Rush Attempts": ["rush_attempts_pg","carries_share","spread","rush_rate"],
        "Receiving Yards": ["route_participation","target_share","air_yards_share","def_role_rank"],
        "Receptions": ["route_participation","target_share","def_role_rank"],
        "Longest Reception": ["air_yards_share","route_participation","explosive_pass_rate"],
        "Fantasy Points": ["snap_share","target_share","red_zone_touch_share","team_total"],
        "Anytime TD": ["red_zone_touch_share","team_total","goal_line_rush_rate"],
    }
    req = required_by_prop.get(prop, [])
    missing = [k for k in req if safe_float(row.get(k)) is None and row.get(k) in [None, ""]]
    ctx["missing_features"] = missing
    if missing:
        notes.append("Feature validation: missing " + ", ".join(missing[:4]))
        factor*=clamp(1 - 0.006*len(missing), 0.965, 1.0)
    return clamp(factor,0.90,1.06), risk, notes, ctx


def _prop_target_columns(prop):
    mapping = {
        "Passing Yards": ["passing_yards", "pass_yds", "passing_yards_pg"],
        "Passing TDs": ["passing_tds", "pass_tds", "passing_tds_pg"],
        "Interceptions": ["interceptions", "ints", "interceptions_pg"],
        "Rushing Yards": ["rushing_yards", "rush_yds", "rushing_yards_pg"],
        "Rush Attempts": ["carries", "rush_attempts", "rush_attempts_pg"],
        "Receiving Yards": ["receiving_yards", "rec_yds", "receiving_yards_pg"],
        "Receptions": ["receptions", "receptions_pg"],
        "Longest Reception": ["longest_rec", "receiving_yards", "receiving_yards_pg"],
        "Longest Rush": ["longest_rush", "rushing_yards", "rushing_yards_pg"],
        "Fantasy Points": ["fantasy_points_ppr", "fantasy_points", "fantasy_points_pg"],
        "Anytime TD": ["receiving_tds", "rushing_tds", "passing_tds"],
        "Kicking Points": ["kicking_points"],
        "Field Goals Made": ["fg_made"],
        "Pass Attempts": ["attempts", "pass_attempts_pg"],
        "Completions": ["completions", "completions_pg"],
    }
    return mapping.get(str(prop), [])

def _historical_player_values(player, prop, max_games=18):
    """Read saved Phase 6 weekly logs and return recent values for the matching prop.
    This is intentionally defensive so a missing column never breaks the board.
    """
    try:
        if not PHASE6_PLAYER_LOG_FILE.exists():
            return []
        sample = pd.read_csv(PHASE6_PLAYER_LOG_FILE, nrows=1)
        possible_name_cols = [c for c in ["player", "player_display_name", "player_name"] if c in sample.columns]
        stat_cols = [c for c in _prop_target_columns(prop) if c in sample.columns]
        base_cols = possible_name_cols + [c for c in ["week", "season"] if c in sample.columns] + stat_cols
        if not possible_name_cols or not stat_cols:
            return []
        df = pd.read_csv(PHASE6_PLAYER_LOG_FILE, usecols=list(dict.fromkeys(base_cols)))
        name_col = possible_name_cols[0]
        g = df[df[name_col].map(norm) == norm(player)].copy()
        if g.empty:
            return []
        if "week" in g.columns:
            g = g.sort_values("week")
        # Some props are composites, especially TD-style markets.
        vals = None
        if prop == "Anytime TD":
            td_cols = [c for c in ["receiving_tds", "rushing_tds"] if c in g.columns]
            if td_cols:
                vals = g[td_cols].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
        if vals is None:
            stat_col = stat_cols[0]
            vals = pd.to_numeric(g[stat_col], errors="coerce")
        vals = vals.dropna()
        vals = vals[vals >= 0]
        return [float(x) for x in vals.tail(max_games).tolist()]
    except Exception:
        return []

def bayesian_markov_poisson_engine(row, prop, rule_projection):
    """Optional advanced simulation assist.

    Layers added here:
    - Bayesian update: blends the rules projection with the player's own historical game logs.
    - Markov game-state proxy: close/trailing/leading game scripts adjust volume by prop type.
    - Poisson scoring-event nudge: touchdown/kicking/interception style props use scoring-event context.
    - Elo/efficiency matchup proxy: team strength and opponent defense can apply small bounded nudges.

    It is deliberately bounded. It should help calibration, not overpower the core engine.
    """
    enabled = bool(st.session_state.get("advanced_sim_assist_enabled", True))
    if not enabled:
        return float(rule_projection), {"enabled": False, "status": "OFF", "blend": 0.0, "notes": []}

    projection = float(rule_projection or 0.0)
    notes=[]; ctx={}; factor=1.0

    # Bayesian update from saved weekly logs.
    hist = _historical_player_values(row.get("player"), prop, max_games=18)
    min_games = int(st.session_state.get("bayes_min_games", 5) or 5)
    if len(hist) >= min_games:
        hist_mean = float(np.mean(hist))
        recent_mean = float(np.mean(hist[-5:])) if len(hist) >= 5 else hist_mean
        hist_std = float(np.std(hist)) if len(hist) > 1 else 0.0
        # More games + lower variance = higher confidence, capped so it remains an assist.
        variance_conf = 1.0 / (1.0 + (hist_std / max(1.0, hist_mean)))
        sample_conf = clamp(len(hist) / 18.0, 0.20, 1.0)
        bayes_weight = clamp(0.10 + 0.18 * variance_conf * sample_conf, 0.08, 0.30)
        bayes_mean = (hist_mean * 0.65) + (recent_mean * 0.35)
        bounded_mean = clamp(bayes_mean, projection * 0.70, projection * 1.30) if projection > 0 else bayes_mean
        projection = (projection * (1 - bayes_weight)) + (bounded_mean * bayes_weight)
        ctx.update({"historical_games": len(hist), "historical_mean": round(hist_mean,3), "recent5_mean": round(recent_mean,3), "bayes_weight": round(bayes_weight,3)})
        notes.append(f"Bayesian log update active: {len(hist)} games, weight {bayes_weight:.2f}")
    else:
        ctx["historical_games"] = len(hist)
        notes.append(f"Bayesian log update warming up: {len(hist)}/{min_games} games")

    # Markov game-state proxy using spread/total/pace: leading, close, trailing state distribution.
    spread = safe_float(row.get("spread"), 0.0) or 0.0
    total = safe_float(row.get("game_total"), 44.0) or 44.0
    pace = safe_float(row.get("pace"), 50.0) or 50.0
    lead_prob = clamp(0.34 + (-spread)*0.025, 0.12, 0.72)
    trail_prob = clamp(0.34 + (spread)*0.025, 0.12, 0.72)
    close_prob = clamp(1.0 - abs(lead_prob - 0.34) - abs(trail_prob - 0.34), 0.18, 0.58)
    # normalize
    z = max(0.01, lead_prob + trail_prob + close_prob)
    lead_prob, trail_prob, close_prob = lead_prob/z, trail_prob/z, close_prob/z
    ctx["markov_state_probs"] = {"leading": round(lead_prob,3), "close": round(close_prob,3), "trailing": round(trail_prob,3)}
    markov_factor = 1.0
    if prop in ["Passing Yards","Pass Attempts","Completions","Receiving Yards","Receptions","Longest Reception"]:
        markov_factor *= (1 + trail_prob*0.035 - lead_prob*0.026)
    elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        markov_factor *= (1 + lead_prob*0.032 - trail_prob*0.025)
    elif prop in ["Interceptions"]:
        markov_factor *= (1 + trail_prob*0.030)
    elif prop in ["Kicking Points", "Field Goals Made"]:
        markov_factor *= (1 + close_prob*0.020)
    if pace >= 56:
        markov_factor *= 1.006
    elif pace <= 47:
        markov_factor *= 0.994
    factor *= clamp(markov_factor, 0.94, 1.06)
    notes.append("Markov game-state volume model applied")

    # Poisson event-rate nudge for discrete scoring-like markets.
    if prop in ["Passing TDs", "Anytime TD", "Interceptions", "Field Goals Made", "Kicking Points"]:
        team_total = safe_float(row.get("team_total"))
        if team_total is None:
            team_total = (total/2.0) - (spread/2.0)
        drives = safe_float(row.get("projected_drives"), 10.4) or 10.4
        lam_score = clamp((team_total or 21.0) / max(1.0, drives), 0.9, 3.8)
        ctx["poisson_lambda_score_drive"] = round(lam_score,3)
        if prop in ["Passing TDs", "Anytime TD"]:
            factor *= clamp(0.965 + lam_score*0.018, 0.96, 1.055)
        elif prop in ["Field Goals Made", "Kicking Points"]:
            # Kicker volume likes scoring environment but also close/stalled drives.
            factor *= clamp(0.975 + lam_score*0.011 + close_prob*0.012, 0.965, 1.045)
        elif prop == "Interceptions":
            factor *= clamp(0.99 + trail_prob*0.025, 0.985, 1.04)
        notes.append("Poisson event-rate nudge applied")

    # Elo/efficiency proxy. If exact Elo is supplied, use it; otherwise infer from ranks/spread.
    team_elo = safe_float(row.get("team_elo"))
    opp_elo = safe_float(row.get("opp_elo"))
    if team_elo is not None and opp_elo is not None:
        elo_diff = clamp((team_elo - opp_elo) / 400.0, -0.22, 0.22)
    else:
        # Negative spread means stronger/favorite.
        elo_diff = clamp((-spread) / 24.0, -0.20, 0.20)
    ctx["elo_efficiency_diff"] = round(elo_diff,3)
    if prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Fantasy Points","Anytime TD"]:
        factor *= clamp(1 + elo_diff*0.035, 0.985, 1.018)
    elif prop in ["Rushing Yards","Rush Attempts"]:
        factor *= clamp(1 + elo_diff*0.025, 0.988, 1.016)
    notes.append("Elo/efficiency matchup proxy applied")

    final_projection = projection * clamp(factor, 0.90, 1.10)
    return float(final_projection), {"enabled": True, "status": "ACTIVE", "blend": round(abs(final_projection - float(rule_projection or 0.0)) / max(1.0, float(rule_projection or 1.0)),3), "factor": round(factor,3), "context": ctx, "notes": notes}


def ensemble_ml_assist_projection(row, rule_projection):
    """Optional Random Forest / Neural-style assist after enough graded rows.
    This is separate from XGBoost and stays bounded. It only turns on when the user has grades.
    """
    enabled = bool(st.session_state.get("ensemble_ml_assist_enabled", False))
    if not enabled:
        return float(rule_projection), {"enabled": False, "status": "OFF", "blend": 0.0}
    prop = row.get("prop")
    same_prop = _graded_training_rows(prop)
    all_rows = _graded_training_rows(None)
    train_rows = same_prop if len(same_prop) >= 45 else all_rows
    min_rows = int(st.session_state.get("ensemble_min_rows", 75) or 75)
    if len(train_rows) < min_rows:
        return float(rule_projection), {"enabled": True, "status": f"WARMUP {len(train_rows)}/{min_rows} graded rows", "blend": 0.0, "sample_count": len(train_rows)}
    try:
        from sklearn.ensemble import RandomForestRegressor, ExtraTreesRegressor
        X = np.array([_xgb_feature_vector(r) for r in train_rows], dtype=float)
        y = np.array([safe_float(r.get("actual"), safe_float(r.get("projection"), 0)) or 0 for r in train_rows], dtype=float)
        if len(y) < min_rows or np.nanstd(y) <= 0:
            return float(rule_projection), {"enabled": True, "status": "NO_VARIANCE", "blend": 0.0, "sample_count": len(train_rows)}
        rf = RandomForestRegressor(n_estimators=160, max_depth=6, min_samples_leaf=3, random_state=77, n_jobs=1)
        et = ExtraTreesRegressor(n_estimators=160, max_depth=6, min_samples_leaf=3, random_state=88, n_jobs=1)
        rf.fit(X, y); et.fit(X, y)
        xrow = np.array([_xgb_feature_vector(row)], dtype=float)
        pred = float((rf.predict(xrow)[0] * 0.55) + (et.predict(xrow)[0] * 0.45))
        rule = float(rule_projection or 0.0)
        pred = clamp(pred, rule * 0.76, rule * 1.24) if rule > 0 else max(0, pred)
        max_blend = safe_float(st.session_state.get("ensemble_blend_weight"), 0.16) or 0.16
        blend = clamp((len(train_rows) - min_rows) / 350.0, 0.04, max_blend)
        blended = (rule * (1 - blend)) + (pred * blend)
        return float(blended), {"enabled": True, "status": "ACTIVE", "model": "RF/ExtraTrees", "ensemble_projection": round(pred,3), "rule_projection": round(rule,3), "blend": round(blend,3), "sample_count": len(train_rows), "same_prop_samples": len(same_prop)}
    except Exception as e:
        return float(rule_projection), {"enabled": True, "status": f"ERROR: {str(e)[:90]}", "blend": 0.0, "sample_count": len(train_rows)}

def project_row(row, sims=12000):
    row=merge_nfl_context(row)
    prop=row.get("prop","Receiving Yards")
    cfg=PROP_CONFIG.get(prop, PROP_CONFIG["Receiving Yards"])
    role=player_role_defaults(row.get("position"),prop)
    role=apply_real_usage_to_role(row, role)
    usage_quality, usage_flags = usage_data_quality(row, prop)
    base=cfg["base"]*usage_adjustment(role,prop)
    base, env_notes, env=apply_environment(base,row,prop)

    opportunity = opportunity_engine(row, role, prop)
    pace_factor, pace_risk, pace_notes = pace_engine(row, prop)
    defense_factor, defense_risk, defense_notes = defensive_matchup_factor(row, prop)
    game_factor, game_env_risk, game_notes = game_environment_factor(row, prop)
    vegas_factor, vegas_risk, vegas_notes = vegas_environment_engine(row, prop)
    script_factor, script_risk, script_notes, script_branches = game_script_simulator(row, prop)
    blowout_factor, blowout_risk, blowout_notes, blowout_prob = blowout_risk_engine(row, prop)
    advanced_factor, advanced_risk, advanced_notes, advanced_context = advanced_context_engine(row, prop)
    role_factor, injury_risk, game_script_risk, risk_notes = role_risk_adjustments(row, role, prop)
    if defense_risk == "HIGH" or game_env_risk == "HIGH" or script_risk == "HIGH" or blowout_risk == "HIGH" or vegas_risk == "HIGH" or advanced_risk == "HIGH":
        game_script_risk="HIGH"
    base*=role_factor*defense_factor*game_factor*opportunity["factor"]*pace_factor*vegas_factor*script_factor*blowout_factor*advanced_factor
    learn=learning_scale(row.get("player"),prop)
    cal_scale, cal_note=calibration_scale(row.get("player"),prop)
    base*=learn*cal_scale
    line=safe_float(row.get("line"))

    # Real line anchoring stays, but demo rows cannot become official plays.
    if line is not None and row.get("source")!="DEMO":
        base=base*0.62 + line*0.38

    xgb_info = {"enabled": bool(st.session_state.get("xgb_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("xgb_assist_enabled", False):
        base, xgb_info = xgboost_assist_projection({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    bayes_markov_info = {"enabled": bool(st.session_state.get("advanced_sim_assist_enabled", True)), "status": "OFF"}
    if st.session_state.get("advanced_sim_assist_enabled", True):
        base, bayes_markov_info = bayesian_markov_poisson_engine({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "usage_quality": usage_quality, "data_score": 70}, prop, base)

    ensemble_info = {"enabled": bool(st.session_state.get("ensemble_ml_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("ensemble_ml_assist_enabled", False):
        base, ensemble_info = ensemble_ml_assist_projection({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    sigma=cfg["sigma"]
    if injury_risk in ["HIGH","EXTREME"]: sigma*=1.12
    if game_script_risk=="HIGH": sigma*=1.08
    if usage_quality < 72: sigma*=1.07
    if (advanced_context or {}).get("consistency_score", 70) <= 48: sigma*=1.06
    collapse_prob, ceiling_prob = simulation_branch_rates(row, prop, injury_risk, game_script_risk)
    collapse_prob = clamp(collapse_prob + (blowout_prob*0.12 if prop in ["Passing Yards","Receiving Yards","Receptions","Rushing Yards","Rush Attempts"] else 0), 0.05, 0.46)
    if script_risk == "HIGH":
        sigma *= 1.04
    seed=stable_projection_seed(row.get("player","x"), prop, line, row.get("team",""), row.get("opp",""), row.get("source",""))
    sim=simulate_prop_distribution(base, sigma, prop, sims, seed, collapse_prob, ceiling_prob)

    mean=float(np.mean(sim)); p50=float(np.percentile(sim,50)); p75=float(np.percentile(sim,75)); p90=float(np.percentile(sim,90)); p10=float(np.percentile(sim,10))
    if line is None:
        prob=None; side="NO LINE"; edge=None; ev=None; kelly=0.0
    else:
        over=float(np.mean(sim>line)); under=1-over
        side="OVER" if over>=under else "UNDER"
        prob=max(over,under)
        edge=mean-line
        ev=expected_value(prob, safe_float(row.get("odds"), -110) or -110)
        kelly=kelly_fraction(prob, safe_float(row.get("odds"), -110) or -110)

    upside_gap=p90-(line if line is not None else p50)
    if upside_gap>cfg["sigma"]*0.95: upside="ELITE"
    elif upside_gap>cfg["sigma"]*0.55: upside="GOOD"
    else: upside="NORMAL"

    vol=(p90-p10)/max(1,mean)
    volatility="HIGH" if vol>.9 else "MED" if vol>.55 else "LOW"
    stability=projection_stability_score(p10,p90,mean,prop)

    score=58
    if prob: score+=int((prob-.50)*110)
    score+=8 if upside in ["ELITE","GOOD"] else 0
    score-=NFL_VOLATILITY_TAX_HIGH if volatility=="HIGH" else NFL_VOLATILITY_TAX_MED if volatility=="MED" else 0
    score+=8 if row.get("source")!="DEMO" else -18
    score+=int((stability-60)*0.15)
    score+=int((usage_quality-70)*0.20)
    if injury_risk=="HIGH": score-=14
    if injury_risk=="EXTREME": score-=32
    if game_script_risk=="HIGH": score-=5
    score=int(clamp(score,0,99))

    line_delta=update_clv_snapshot(row.get("player"), prop, row.get("source"), line) if line is not None else None
    true_line_delta=track_line_delta(row.get("player"), prop, row.get("source"), line) if line is not None else None

    notes=[]+env_notes+opportunity.get("notes",[])+pace_notes+risk_notes+defense_notes+game_notes+vegas_notes+script_notes+blowout_notes+advanced_notes
    if usage_flags:
        notes.extend(["Usage data: "+x for x in usage_flags[:3]])
    if cal_scale != 1.0: notes.append(cal_note)
    elif row.get("source")!="DEMO": notes.append(cal_note)
    if xgb_info.get("enabled"):
        notes.append(f"XGBoost Assist: {xgb_info.get('status')}" + (f" · blend {xgb_info.get('blend')}" if xgb_info.get('blend') else ""))
    if bayes_markov_info.get("enabled"):
        notes.append(f"Bayesian/Markov Assist: {bayes_markov_info.get('status')}" + (f" · factor {bayes_markov_info.get('factor')}" if bayes_markov_info.get('factor') else ""))
        notes.extend((bayes_markov_info.get("notes") or [])[:3])
    if ensemble_info.get("enabled"):
        notes.append(f"Ensemble ML Assist: {ensemble_info.get('status')}" + (f" · blend {ensemble_info.get('blend')}" if ensemble_info.get('blend') else ""))
    if row.get("source")=="DEMO": notes.append("Demo row until live NFL props are available")

    out={**row,"projection":round(mean,2),"edge":None if edge is None else round(edge,2),"pick":side,"fair_prob":None if prob is None else round(prob,3),"ev":None if ev is None else round(ev,4),"kelly":round(kelly,4),"p10":round(p10,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"pure_upside":upside,"volatility":volatility,"stability_score":stability,"usage_quality":usage_quality,"opportunity_score":round(opportunity.get("factor",1.0)*100,1),"expected_opportunity":opportunity.get("expected",{}),"pace_factor":round(pace_factor,3),"vegas_factor":round(vegas_factor,3),"advanced_factor":round(advanced_factor,3),"advanced_context":advanced_context,"xgb_assist":xgb_info,"bayes_markov_assist":bayes_markov_info,"ensemble_ml_assist":ensemble_info,"game_script_factor":round(script_factor,3),"game_script_branches":script_branches,"blowout_prob":blowout_prob,"matchup_factor":round(defense_factor,3),"collapse_prob":round(collapse_prob,3),"ceiling_prob":round(ceiling_prob,3),"data_score":score,"injury_risk":injury_risk,"game_script_risk":game_script_risk,"defense_risk":defense_risk,"line_delta":line_delta,"true_line_delta":true_line_delta,"role":role,"env":env,"notes":notes,"sim_samples":sims}
    signal, action_tier, rejections = build_signal(out)
    out["signal"]=signal; out["action_tier"]=action_tier; out["official_rejections"]=rejections; out["bettable"]=action_tier=="BET"
    return out

def alt_ladder(p):
    line=safe_float(p.get("line")); prop=p.get("prop")
    if line is None: return pd.DataFrame()
    step=10 if "Yards" in prop else 5 if prop in ["Pass Attempts","Completions"] else 2 if prop in ["Rush Attempts","Longest Reception","Longest Rush","Tackles + Assists","Kicking Points"] else 1 if prop in ["Receptions","Field Goals Made"] else 0.5
    levels=[line-step,line,line+step,line+2*step,line+3*step]
    rows=[]
    mean=p["projection"]; sigma=PROP_CONFIG.get(prop,{}).get("sigma",10)
    rng=np.random.default_rng(42); sim=np.clip(rng.normal(mean,sigma,12000),0,None)
    for lvl in levels:
        rows.append({"Alt Line":round(lvl,1),"Over Hit %":round(float(np.mean(sim>lvl))*100,1),"Under Hit %":round(float(np.mean(sim<lvl))*100,1),"Use":"Main" if abs(lvl-line)<0.01 else ("Ladder" if lvl>line else "Safer")})
    return pd.DataFrame(rows)

# ---------- logging / grading ----------
def _json_safe(v):
    """Convert numpy/pandas objects so full slate snapshots always save cleanly."""
    try:
        if pd.isna(v) and not isinstance(v, (dict, list, tuple)):
            return None
    except Exception:
        pass
    if isinstance(v, dict):
        return {str(k): _json_safe(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v

def _clean_snapshot_row(row):
    keep = dict(row or {})
    # Keep the model output and context, but avoid any future massive objects.
    keep.pop("sim", None)
    keep.pop("samples", None)
    return _json_safe(keep)

def _snapshot_slate_id(label):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"NFL_{str(label).upper()}_{stamp}"

def save_snapshot(path, rows, label, scope="ALL", source_note="manual_button"):
    """Save a complete slate/board snapshot, grouped by slate_id.

    This is the NFL version of the MLB Save Official Before / After workflow:
    one click saves the whole board, not just a single player prop.
    """
    old=load_json(path,[])
    stamp=now_iso()
    slate_id=_snapshot_slate_id(label)
    saved=[]
    for r in rows:
        if scope == "LIVE_ONLY" and r.get("source") == "DEMO":
            continue
        if scope == "OFFICIAL_ONLY" and not r.get("bettable"):
            continue
        rr=_clean_snapshot_row(r)
        rr.update({
            "snapshot_type":label,
            "saved_at":stamp,
            "slate_id":slate_id,
            "snapshot_scope":scope,
            "source_note":source_note,
        })
        saved.append(rr)
    old.extend(saved)
    save_json(path, old[-12000:])
    return len(saved), slate_id

def _snapshot_groups(path, label=None):
    rows=load_json(path,[])
    groups={}
    for r in rows:
        if label and str(r.get("snapshot_type")) != str(label):
            continue
        key=r.get("slate_id") or r.get("saved_at") or "UNKNOWN"
        groups.setdefault(key,[]).append(r)
    out=[]
    for key, vals in groups.items():
        ts=vals[0].get("saved_at", key)
        srcs=sorted(set(str(v.get("source","")) for v in vals))
        out.append({"key":key,"label":f"{ts} · {len(vals)} rows · {', '.join([x for x in srcs if x])}","rows":vals})
    return sorted(out, key=lambda x: str(x["label"]), reverse=True)

def update_learning_from_result(player, prop, projected, actual):
    data=load_json(LEARN_FILE,{})
    key=f"{norm(player)}|{prop}"
    cur=safe_float(data.get(key,1.0),1.0) or 1.0
    proj=safe_float(projected); act=safe_float(actual)
    if proj and act is not None:
        err=clamp((act-proj)/max(1,proj),-.25,.25)
        data[key]=round(clamp(cur*(1+0.05*err),0.90,1.10),4)
        save_json(LEARN_FILE,data)
    return data.get(key,cur)

def grade_rows_and_learn(rows_to_grade, actual_values, grade_note="bulk_grade"):
    """Grade many saved props at once and update the learning file for each graded row."""
    results=load_json(RESULT_LOG,[])
    graded=[]
    for r, actual in zip(rows_to_grade, actual_values):
        actual=safe_float(actual)
        if actual is None:
            continue
        line=safe_float(r.get("line")); pick=str(r.get("pick") or "").upper(); win=None
        if line is not None:
            if pick == "OVER": win = actual > line
            elif pick == "UNDER": win = actual < line
        scale=update_learning_from_result(r.get("player"), r.get("prop"), r.get("projection"), actual)
        out=_clean_snapshot_row(r)
        out.update({
            "actual":actual,
            "win":win,
            "graded_at":now_iso(),
            "new_learning_scale":scale,
            "grade_note":grade_note,
            "projection_error":None if safe_float(r.get("projection")) is None else round(actual - safe_float(r.get("projection")), 3),
            "line_result_margin":None if line is None else round(actual - line, 3),
        })
        results.append(out); graded.append(out)
    save_json(RESULT_LOG, results[-12000:])
    return graded

def build_learning_summary_df(results):
    if not results:
        return pd.DataFrame()
    rdf=pd.DataFrame(results)
    if rdf.empty:
        return pd.DataFrame()
    for c in ["projection","actual","line"]:
        if c in rdf.columns:
            rdf[c]=pd.to_numeric(rdf[c], errors="coerce")
    if "win" in rdf.columns:
        rdf["win_num"] = rdf["win"].apply(lambda x: 1 if x is True else 0 if x is False else np.nan)
    else:
        rdf["win_num"] = np.nan
    if "projection_error" not in rdf.columns:
        rdf["projection_error"] = rdf.get("actual", np.nan) - rdf.get("projection", np.nan)
    grp_cols=[c for c in ["prop","player"] if c in rdf.columns]
    if not grp_cols:
        return pd.DataFrame()
    summ=rdf.groupby(grp_cols, dropna=False).agg(
        graded=("actual","count"),
        hit_rate=("win_num","mean"),
        avg_projection_error=("projection_error","mean"),
        avg_abs_error=("projection_error", lambda x: float(np.nanmean(np.abs(x))) if len(x) else np.nan),
    ).reset_index()
    summ["hit_rate"]=(summ["hit_rate"]*100).round(1)
    summ["avg_projection_error"]=summ["avg_projection_error"].round(3)
    summ["avg_abs_error"]=summ["avg_abs_error"].round(3)
    return summ.sort_values(["graded","avg_abs_error"], ascending=[False, True])

# ---------- UI ----------
POSITION_TAB_PROPS = {
    "QBs": ["All", "Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions", "Rushing Yards"],
    "RBs": ["All", "Rushing Yards", "Rush Attempts", "Receiving Yards", "Receptions", "Fantasy Points", "Anytime TD", "Longest Rush"],
    "Receivers": ["All", "Receiving Yards", "Receptions", "Longest Reception", "Fantasy Points", "Anytime TD"],
}

QB_POSITIONS = {"QB"}
RB_POSITIONS = {"RB", "FB"}
RECEIVER_POSITIONS = {"WR", "TE"}


def _pos(p):
    return str((p or {}).get("position") or "").upper().strip()


def _is_qb(p):
    return _pos(p) in QB_POSITIONS


def _is_rb(p):
    return _pos(p) in RB_POSITIONS


def _is_receiver(p):
    return _pos(p) in RECEIVER_POSITIONS


def _filter_position_market(rows, position_name, market="All", receiver_pos="All"):
    if position_name == "QBs":
        out = [p for p in rows if _is_qb(p)]
    elif position_name == "RBs":
        out = [p for p in rows if _is_rb(p)]
    elif position_name == "Receivers":
        out = [p for p in rows if _is_receiver(p)]
        if receiver_pos in ["WR", "TE"]:
            out = [p for p in out if _pos(p) == receiver_pos]
    else:
        out = list(rows)
    if market and market != "All":
        out = [p for p in out if p.get("prop") == market]
    return out


def _render_prop_table(rows, title="Board"):
    if not rows:
        st.warning("No props available for this view.")
        return
    view = pd.DataFrame(rows)
    show_cols=["player","position","team","matchup","prop","line","projection","edge","pick","fair_prob","ev","kelly","signal","action_tier","opportunity_score","xgb_status","advanced_factor","game_script_factor","matchup_factor","blowout_prob","pure_upside","volatility","stability_score","data_score","line_delta","source"]
    st.dataframe(view[[c for c in show_cols if c in view.columns]], use_container_width=True, hide_index=True)


def _render_player_cards(rows, limit=None, header=None):
    if header:
        st.markdown(f"<div class='section-title-pro'>{header}</div>", unsafe_allow_html=True)
    shown = rows if limit is None else rows[:limit]
    if not shown:
        st.warning("No player cards available in this view.")
        return
    for i,p in enumerate(shown):
        badge_class="good-badge" if p.get("pick")=="OVER" else "red-badge" if p.get("pick")=="UNDER" else "yellow-badge"
        fair = '' if p.get('fair_prob') is None else str(round(p.get('fair_prob')*100,1))+'%'
        st.markdown(f"""
        <div class='pick-card'>
          <div class='player-name'>{p['player']} <span class='small-muted'>({p.get('position','')} · {p.get('team','')})</span></div>
          <span class='badge'>{p.get('prop')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge {badge_class}'>{p.get('signal')}</span><span class='badge yellow-badge'>Upside {p.get('pure_upside')}</span><span class='badge'>Vol {p.get('volatility')}</span>
          <div class='kpi-strip'>
            <div class='metric-card'><div class='kpi-label'>Line</div><div class='kpi-value'>{p.get('line')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Projection</div><div class='kpi-value'>{p.get('projection')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Edge</div><div class='kpi-value'>{p.get('edge')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Fair Prob</div><div class='kpi-value'>{fair}</div></div>
            <div class='metric-card'><div class='kpi-label'>P75</div><div class='kpi-value'>{p.get('p75')}</div></div>
            <div class='metric-card'><div class='kpi-label'>P90 Ceiling</div><div class='kpi-value'>{p.get('p90')}</div></div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        with st.expander(f"View More — {p['player']} {p['prop']}"):
            c1,c2,c3=st.columns(3)
            with c1:
                st.subheader("Usage")
                role=p.get("role", {})
                st.write(f"Snap Share: **{role.get('snap','')}%**")
                st.write(f"Route Participation: **{role.get('route','')}%**")
                st.write(f"Target Share: **{role.get('target','')}%**")
                st.write(f"Carry Share: **{role.get('carry','')}%**")
                st.write(f"Red-Zone Usage: **{role.get('rz','')}%**")
                opp_ctx=p.get("expected_opportunity", {}) or {}
                if opp_ctx:
                    st.write("---")
                    st.write(f"Expected Plays: **{opp_ctx.get('plays_pg','')}**")
                    st.write(f"Dropbacks: **{opp_ctx.get('dropbacks_pg','')}**")
                    st.write(f"Routes: **{opp_ctx.get('routes_pg','')}**")
                    st.write(f"Targets Est: **{opp_ctx.get('targets_pg_est','')}**")
                    st.write(f"Carries Est: **{opp_ctx.get('carries_pg_est','')}**")
            with c2:
                st.subheader("Environment")
                env=p.get("env", {})
                st.write(f"Stadium: **{env.get('stadium','')}**")
                st.write(f"Crowd Noise: **{env.get('crowd','')}**")
                st.write(f"Roof: **{env.get('roof','')}**")
                st.write(f"Surface: **{env.get('surface','')}**")
                st.write(f"Altitude: **{env.get('altitude','')} ft**")
            with c3:
                st.subheader("Risk Notes")
                for n in p.get("notes",[]): st.write("- "+str(n))
                st.write(f"Data Score: **{p.get('data_score')}/99**")
                st.write(f"Opportunity Score: **{p.get('opportunity_score')}**")
                st.write(f"Matchup Factor: **{p.get('matchup_factor')}**")
                st.write(f"Game Script Factor: **{p.get('game_script_factor')}**")
                st.write(f"Blowout Prob: **{p.get('blowout_prob')}**")
                st.write(f"Advanced Factor: **{p.get('advanced_factor')}**")
                if p.get('xgb_assist'):
                    st.write(f"XGBoost Assist: **{(p.get('xgb_assist') or {}).get('status','OFF')}**")
                    st.write(f"Bayesian/Markov Assist: **{(p.get('bayes_markov_assist') or {}).get('status','OFF')}**")
                    st.write(f"Ensemble ML Assist: **{(p.get('ensemble_ml_assist') or {}).get('status','OFF')}**")
                    with st.expander("XGBoost / Bayesian / advanced context", expanded=False):
                        st.json({"xgb_assist": p.get('xgb_assist'), "advanced_context": p.get('advanced_context')})
                st.write(f"Stability Score: **{p.get('stability_score')} /100**")
                st.write(f"Action Tier: **{p.get('action_tier')}**")
                if p.get('game_script_branches'):
                    st.write("Game Script Branches:")
                    st.json(p.get('game_script_branches'))
                rejects=p.get('official_rejections') or []
                if rejects:
                    st.write("Official Filter Rejections:")
                    for rr in rejects: st.write("- "+str(rr))
                st.write(f"CLV Line Delta: **{p.get('line_delta')}**")
                st.write(f"Source: **{p.get('source')}**")
            st.subheader("Alt Ladder")
            st.dataframe(alt_ladder(p), use_container_width=True, hide_index=True)


def _render_position_board(rows, position_name):
    st.markdown(f"<div class='section-title-pro'>{position_name} Prop Board</div>", unsafe_allow_html=True)
    if position_name == "Receivers":
        c1, c2 = st.columns([1, 1])
        with c1:
            receiver_pos = st.selectbox("Receiver type", ["All", "WR", "TE"], index=0, key="receiver_position_filter")
        with c2:
            market = st.selectbox("Receiver market", POSITION_TAB_PROPS[position_name], index=0, key="receiver_market_filter")
        filtered = _filter_position_market(rows, position_name, market, receiver_pos)
    else:
        market = st.selectbox(f"{position_name} market", POSITION_TAB_PROPS[position_name], index=0, key=f"{position_name}_market_filter")
        filtered = _filter_position_market(rows, position_name, market)
    _render_prop_table(filtered, f"{position_name} board")
    _render_player_cards(filtered, header=None)


def _render_phase6_admin():
    st.markdown("### Admin: Phase 6 Database")
    st.caption("Hidden admin/setup area. The main app loads saved Phase 6 files automatically.")
    season_to_build = st.number_input("Last season to build", min_value=1999, max_value=2030, value=NFL_LAST_SEASON, step=1, key="phase6_admin_season")
    existing_ready = _phase6_existing_database_ready()
    st.metric("Saved database", "READY" if existing_ready else "NOT BUILT")
    if PHASE6_MANIFEST_FILE.exists():
        with st.expander("Last saved build", expanded=False):
            st.json(load_json(PHASE6_MANIFEST_FILE, {}))
    if st.button("Use Saved / Build If Missing", use_container_width=True, key="phase6_use_saved_sidebar"):
        diag = build_phase6_nfl_database(int(season_to_build), force_refresh=False)
        if diag.get("status") in ["BUILT_AND_SAVED", "USING_SAVED_DATABASE", "PULL_FAILED_USING_SAVED_DATABASE", "USING_SAVED_LOCAL_DATABASE", "USING_GITHUB_HARD_INPUT_DATABASE", "BUILT_AND_SAVED_PHASE6_V3"]:
            st.success(f"Phase 6 ready: {diag.get('status')}.")
        else:
            st.warning(f"Phase 6 database not built: {diag.get('status')}")
        st.json(diag)
    if st.button("Force Rebuild / Refresh From Web", use_container_width=True, key="phase6_force_sidebar"):
        diag = build_phase6_nfl_database(int(season_to_build), force_refresh=True)
        if diag.get("status") in ["BUILT_AND_SAVED", "PULL_FAILED_USING_SAVED_DATABASE", "BUILT_AND_SAVED_PHASE6_V3", "BUILT_PREVIOUS_SEASON_AND_SAVED_PHASE6_V3"]:
            st.success(f"Phase 6 refreshed/saved: {diag.get('status')}.")
        else:
            st.warning(f"Phase 6 database not built: {diag.get('status')}")
        st.json(diag)
    zip_path = PHASE6_DIR / "phase6_nfl_database_export.zip"
    if st.button("Export Saved Database ZIP", use_container_width=True, key="phase6_export_sidebar"):
        try:
            zip_path = _phase6_export_database_zip()
            st.success(f"Export created: {zip_path.name}")
        except Exception as e:
            st.error(f"Export failed: {e}")
    if zip_path.exists():
        try:
            st.download_button("⬇️ Download Complete Phase 6 Database ZIP", data=zip_path.read_bytes(), file_name="phase6_nfl_database_export.zip", mime="application/zip", use_container_width=True, key="phase6_download_zip_sidebar")
        except Exception as e:
            st.warning(f"ZIP exists but could not be prepared for download: {e}")
    if USAGE_FILE.exists():
        try:
            st.download_button("⬇️ Download Player Usage CSV", data=USAGE_FILE.read_bytes(), file_name="nfl_player_usage.csv", mime="text/csv", use_container_width=True, key="phase6_download_usage_sidebar")
        except Exception:
            pass
    if TEAM_CONTEXT_FILE.exists():
        try:
            st.download_button("⬇️ Download Team Context JSON", data=TEAM_CONTEXT_FILE.read_bytes(), file_name="nfl_team_context.json", mime="application/json", use_container_width=True, key="phase6_download_team_sidebar")
        except Exception:
            pass


st.markdown(f"""
<div class='hero-panel'>
  <div class='big-title'>NFL Prop Engine</div>
  <div class='sub-title'>Clean player cards · projections · pure upside · stadium/noise · weather-ready · CLV · save before/after · grading</div>
  <span class='badge'>{APP_VERSION}</span><span class='badge good-badge'>MLB framework converted to NFL structure</span>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("Controls")
    source_mode=st.radio("Prop Source", ["Live Underdog only", "Live Underdog first, demo fallback", "Demo board only"], index=0)

    if "board_pull_id" not in st.session_state:
        st.session_state["board_pull_id"] = 0
    last_board_meta = load_last_pulled_board()
    if st.button("🔄 Refresh / Pull Underdog Board Lines", use_container_width=True):
        st.session_state["board_pull_id"] = int(st.session_state.get("board_pull_id", 0)) + 1
        try:
            fetch_underdog_nfl_props.clear()
            fetch_underdog_nfl_moneylines.clear()
            safe_get_json.clear()
        except Exception:
            pass
        request_log("MANUAL_PULL_BOARD", "REQUESTED", f"pull_id={st.session_state['board_pull_id']}")
        st.rerun()
    st.caption(f"Last saved board pull: {last_board_meta.get('pulled_at') or 'None'} · Rows: {last_board_meta.get('row_count', 0)}")
    use_saved_board_if_blank = st.checkbox("Use last pulled board if live pull is blank", value=False, help="Useful if Underdog temporarily fails after you already pulled a board. Demo mode is still separate.")

    # Prop filtering is now handled by the main QBs / RBs / Receivers tabs.
    # Keep every supported market active in the engine and hide the old sidebar multiselect.
    prop_filter=list(PROP_CONFIG.keys())
    st.session_state["xgb_assist_enabled"] = st.toggle("XGBoost Assist after grading", value=bool(st.session_state.get("xgb_assist_enabled", False)), help="Uses your graded results to assist the main projection before Monte Carlo. Stays neutral until enough grades exist.")
    if st.session_state.get("xgb_assist_enabled", False):
        st.session_state["xgb_min_rows"] = st.slider("XGB min graded rows", 25, 250, int(st.session_state.get("xgb_min_rows", 50)), 5)
        st.session_state["xgb_blend_weight"] = st.slider("XGB max blend", 0.05, 0.40, float(st.session_state.get("xgb_blend_weight", 0.22)), 0.01)
    st.session_state["advanced_sim_assist_enabled"] = st.toggle("Bayesian / Markov / Poisson Assist", value=bool(st.session_state.get("advanced_sim_assist_enabled", True)), help="Adds bounded Bayesian historical-log updating, Markov game-state volume, Poisson event-rate nudges, and Elo/efficiency matchup context before Monte Carlo.")
    if st.session_state.get("advanced_sim_assist_enabled", True):
        st.session_state["bayes_min_games"] = st.slider("Bayesian min player games", 3, 12, int(st.session_state.get("bayes_min_games", 5)), 1)
    st.session_state["ensemble_ml_assist_enabled"] = st.toggle("Random Forest / Tree Ensemble Assist after grading", value=bool(st.session_state.get("ensemble_ml_assist_enabled", False)), help="Optional ML assist from graded results. Stays neutral until enough graded props exist.")
    if st.session_state.get("ensemble_ml_assist_enabled", False):
        st.session_state["ensemble_min_rows"] = st.slider("Ensemble min graded rows", 50, 400, int(st.session_state.get("ensemble_min_rows", 75)), 5)
        st.session_state["ensemble_blend_weight"] = st.slider("Ensemble max blend", 0.04, 0.30, float(st.session_state.get("ensemble_blend_weight", 0.16)), 0.01)
    min_score=st.slider("Minimum Data Score",0,99,0)
    show_all=st.checkbox("Show all player cards", True)
    st.divider()
    st.caption("API keys can be added in Streamlit secrets or Railway variables later.")
    show_feed_debug=st.checkbox("Show Underdog feed debug", False)
    with st.expander("Admin: Phase 6 Database", expanded=False):
        _render_phase6_admin()
    st.code("STORAGE_DIR=nfl_engine", language="bash")

pull_id = int(st.session_state.get("board_pull_id", 0))
live=[] if source_mode=="Demo board only" else fetch_underdog_nfl_props(pull_id)
moneylines=[] if source_mode=="Demo board only" else fetch_underdog_nfl_moneylines(pull_id)
if live:
    save_last_pulled_board(live, moneylines)
elif source_mode != "Demo board only" and use_saved_board_if_blank:
    cached_board = load_last_pulled_board()
    cached_money = load_last_pulled_moneylines()
    live = cached_board.get("rows", []) or []
    moneylines = cached_money.get("rows", []) or []
    if live:
        request_log("UNDERDOG_BOARD_CACHE", "USING_LAST_PULLED", f"rows={len(live)} pulled_at={cached_board.get('pulled_at')}")
raw = live if live else ([] if source_mode=="Live Underdog only" else DEMO_BOARD)
projected=[project_row(r) for r in raw if r.get("prop") in prop_filter]
for _p in projected:
    _x=_p.get("xgb_assist") or {}
    _p["xgb_status"] = _x.get("status", "OFF")
projected=[p for p in projected if p.get("data_score",0)>=min_score]

df=pd.DataFrame(projected)
real_count=sum(1 for p in projected if p.get("source")!="DEMO")
best_edges=[p for p in projected if p.get("action_tier")=="BET"]

st.markdown("<div class='kpi-strip'>"+
    f"<div class='kpi-box'><div class='kpi-label'>Player Cards</div><div class='kpi-value'>{len(projected)}</div><div class='kpi-sub'>shown on board</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Live Lines</div><div class='kpi-value'>{real_count}</div><div class='kpi-sub'>{'Underdog detected' if real_count else 'live only / no rows' if source_mode=='Live Underdog only' else 'demo fallback active'}</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Best Edges</div><div class='kpi-value'>{len(best_edges)}</div><div class='kpi-sub'>prob/edge filtered</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Before Saves</div><div class='kpi-value'>{len(load_json(PICK_LOG,[]))}</div><div class='kpi-sub'>official snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>After Saves</div><div class='kpi-value'>{len(load_json(AFTER_LOG,[]))}</div><div class='kpi-sub'>closing snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Graded</div><div class='kpi-value'>{len(load_json(RESULT_LOG,[]))}</div><div class='kpi-sub'>learning rows</div></div>"+
    "</div>", unsafe_allow_html=True)

if live:
    cached_meta = load_last_pulled_board()
    st.success(f"🟢 Live/loaded Underdog NFL props detected: {len(live)} rows. Last board pull: {cached_meta.get('pulled_at') or 'current refresh'}.")
elif source_mode == "Live Underdog only":
    st.warning("No live Underdog NFL rows were detected. Click 🔄 Refresh / Pull Underdog Board Lines in the sidebar when props are posted.")
else:
    st.info("Demo/testing mode is active. These rows are for UI testing only and should not be treated as real plays.")

if 'show_feed_debug' in globals() and show_feed_debug:
    req_log=load_json(REQUEST_LOG,[])
    st.caption("Latest Underdog/API request log")
    st.dataframe(pd.DataFrame(req_log[-25:]), use_container_width=True, hide_index=True)

tabs=st.tabs(["Today / Weekly Board", "QBs", "RBs", "Receivers", "Best Edges", "Player Cards", "Alt-Line Ladder", "Correlation Builder", "Save + Grade", "Learning Dashboard", "Money Line"])

with tabs[0]:
    st.markdown("<div class='section-title-pro'>NFL Board</div>", unsafe_allow_html=True)
    _render_prop_table(projected, "NFL Board")

with tabs[1]:
    _render_position_board(projected, "QBs")

with tabs[2]:
    _render_position_board(projected, "RBs")

with tabs[3]:
    _render_position_board(projected, "Receivers")

with tabs[4]:
    st.markdown("<div class='section-title-pro'>Best Edges + Official Filter</div>", unsafe_allow_html=True)
    filt_rows=[]
    for p in projected:
        filt_rows.append({
            "Player": p.get("player"), "Pos": p.get("position"), "Prop": p.get("prop"), "Pick": p.get("pick"),
            "Signal": p.get("signal"), "Tier": p.get("action_tier"), "Line": p.get("line"),
            "Proj": p.get("projection"), "Edge": p.get("edge"), "Fair Prob %": None if p.get("fair_prob") is None else round(p.get("fair_prob")*100,1),
            "EV %": None if p.get("ev") is None else round(p.get("ev")*100,1), "Kelly %": round((p.get("kelly") or 0)*100,2),
            "Data": p.get("data_score"), "Stability": p.get("stability_score"), "Vol": p.get("volatility"),
            "Rejected Why": "; ".join(p.get("official_rejections") or [])
        })
    if filt_rows:
        st.dataframe(pd.DataFrame(filt_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No props loaded.")
    edges=sorted(best_edges, key=lambda x: (x.get("fair_prob") or 0, x.get("data_score") or 0, abs(x.get("edge") or 0)), reverse=True)
    st.markdown("<div class='section-title-pro'>Best Edge Cards</div>", unsafe_allow_html=True)
    if not edges: st.warning("No strong edge cards yet. During live-only/no-line mode this is normal.")
    for p in edges[:30]:
        st.markdown(f"""
        <div class='pick-card'><div class='player-name'>{p['player']} — {p['prop']}</div>
        <span class='badge'>{p.get('team','')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge good-badge'>{p.get('signal')}</span><span class='badge yellow-badge'>Pure Upside: {p['pure_upside']}</span>
        <div class='kpi-strip'>
        <div class='metric-card'><div class='kpi-label'>Line</div><div class='kpi-value'>{p.get('line')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Projection</div><div class='kpi-value'>{p.get('projection')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Edge</div><div class='kpi-value'>{p.get('edge')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Fair Prob</div><div class='kpi-value'>{round((p.get('fair_prob') or 0)*100,1)}%</div></div>
        <div class='metric-card'><div class='kpi-label'>Ceiling P90</div><div class='kpi-value'>{p.get('p90')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Score</div><div class='kpi-value'>{p.get('data_score')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Stability</div><div class='kpi-value'>{p.get('stability_score')}</div></div>
        </div></div>""", unsafe_allow_html=True)

with tabs[5]:
    _render_player_cards(projected, header="Clickable Player Cards")

with tabs[6]:
    st.markdown("<div class='section-title-pro'>Alt-Line Ladder</div>", unsafe_allow_html=True)
    names=[f"{p['player']} — {p['prop']}" for p in projected]
    if names:
        choice=st.selectbox("Choose Player Prop", names)
        p=projected[names.index(choice)]
        st.dataframe(alt_ladder(p), use_container_width=True, hide_index=True)
    else: st.warning("No props to ladder.")

with tabs[7]:
    st.markdown("<div class='section-title-pro'>Correlation Builder</div>", unsafe_allow_html=True)
    st.write("Use this to avoid bad parlays and find positive stacks.")
    if df.empty: st.warning("No player cards loaded.")
    else:
        labels=[f"{p['player']} — {p['prop']}" for p in projected]
        left=st.selectbox("Leg 1", labels, key="corr1")
        right=st.selectbox("Leg 2", labels, key="corr2")
        p1=projected[labels.index(left)]
        p2=projected[labels.index(right)]
        corr="Neutral"
        if p1.get("matchup")==p2.get("matchup"):
            if "Passing" in p1["prop"] and p2["prop"] in ["Receiving Yards","Receptions","Anytime TD","Longest Reception"]: corr="Positive QB stack"
            elif p1["team"]==p2["team"] and p1["prop"]==p2["prop"]: corr="Possible target/usage conflict"
            elif p1["team"]!=p2["team"] and any(x in p1["prop"] for x in ["Passing","Receiving"]) and any(x in p2["prop"] for x in ["Passing","Receiving"]): corr="Positive game-script shootout"
        st.success(f"Correlation Read: {corr}")

with tabs[8]:
    st.markdown("<div class='section-title-pro'>Save Full Board / After / Bulk Grade</div>", unsafe_allow_html=True)
    st.write("This now works like the MLB workflow: save the whole pulled board/slate in one click, then bulk-grade it later.")

    source_warning = any(p.get("source") == "DEMO" for p in projected)
    if source_warning:
        st.warning("Demo rows are currently on the board. Use Live Underdog only for real official saves once NFL props are posted.")

    c1,c2,c3=st.columns(3)
    with c1:
        before_scope=st.selectbox("Before save scope", ["ALL", "LIVE_ONLY", "OFFICIAL_ONLY"], index=0, help="ALL saves the full visible board. LIVE_ONLY excludes demo rows. OFFICIAL_ONLY saves only bettable rows.")
        if st.button("💾 Save OFFICIAL BEFORE — Full Board", use_container_width=True):
            n, slate_id = save_snapshot(PICK_LOG, projected, "BEFORE", scope=before_scope, source_note="full_board_before")
            st.success(f"Saved {n} BEFORE rows · Slate ID: {slate_id}")
    with c2:
        after_scope=st.selectbox("After save scope", ["ALL", "LIVE_ONLY", "OFFICIAL_ONLY"], index=0, help="Use this before grading if you want a closing snapshot of the same board.")
        if st.button("📌 Save AFTER / Closing — Full Board", use_container_width=True):
            n, slate_id = save_snapshot(AFTER_LOG, projected, "AFTER", scope=after_scope, source_note="full_board_after")
            st.success(f"Saved {n} AFTER rows · Slate ID: {slate_id}")
    with c3:
        st.metric("Current Board Rows", len(projected))
        st.metric("Bettable Rows", sum(1 for p in projected if p.get("bettable")))
        st.metric("Live Rows", sum(1 for p in projected if p.get("source") != "DEMO"))

    st.divider()
    st.subheader("Clear Board Logs")
    st.caption("Use this when you saved demo/test slates and want a clean board. This does NOT delete the Phase 6 historical database.")
    clear_col1, clear_col2, clear_col3 = st.columns([1.2, 1.2, 2])
    with clear_col1:
        clear_learning_flag = st.checkbox("Also clear learning/calibration", value=False, help="Leave off unless you want to reset learned prop calibration too.")
    with clear_col2:
        clear_line_flag = st.checkbox("Also clear CLV/line history", value=False, help="Leave off unless you want to reset saved line movement history.")
    with clear_col3:
        confirm_clear = st.text_input("Type CLEAR to confirm", value="", placeholder="CLEAR")
        if st.button("🧹 Clear Board Logs", use_container_width=True):
            if confirm_clear.strip().upper() != "CLEAR":
                st.warning("Type CLEAR first so logs are not wiped by accident.")
            else:
                cleared = clear_board_logs(clear_learning=clear_learning_flag, clear_line_history=clear_line_flag)
                st.success("Cleared: " + ", ".join(cleared) if cleared else "Nothing cleared")
                st.info("Phase 6 database was preserved.")

    st.divider()
    st.subheader("Bulk Grade Saved BEFORE Slate")
    before_groups=_snapshot_groups(PICK_LOG, "BEFORE")
    if not before_groups:
        st.info("No BEFORE slates saved yet. Save the full board first, then come back here to grade it.")
    else:
        choice=st.selectbox("Choose saved BEFORE slate", before_groups, format_func=lambda x: x["label"])
        saved_rows=choice["rows"]
        grade_rows=[]
        for idx,r in enumerate(saved_rows):
            grade_rows.append({
                "idx": idx,
                "Player": r.get("player"),
                "Team": r.get("team"),
                "Prop": r.get("prop"),
                "Line": r.get("line"),
                "Pick": r.get("pick"),
                "Signal": r.get("signal"),
                "Projection": r.get("projection"),
                "Actual": None,
            })
        gdf=pd.DataFrame(grade_rows)
        st.caption("Enter actual results for as many rows as you want. Blank Actual rows will be skipped.")
        edited=st.data_editor(
            gdf,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            column_config={"Actual": st.column_config.NumberColumn("Actual", step=0.5)},
            disabled=["idx","Player","Team","Prop","Line","Pick","Signal","Projection"],
            key="bulk_grade_editor",
        )
        col_a,col_b=st.columns([1,2])
        with col_a:
            if st.button("✅ Grade Entered Rows + Learn", use_container_width=True):
                actuals=[]; rows_for_grade=[]
                for _,er in edited.iterrows():
                    actual=safe_float(er.get("Actual"))
                    if actual is not None:
                        rows_for_grade.append(saved_rows[int(er.get("idx"))])
                        actuals.append(actual)
                graded=grade_rows_and_learn(rows_for_grade, actuals, grade_note=f"bulk_grade_{choice['key']}")
                if graded:
                    wins=[g.get("win") for g in graded if g.get("win") is not None]
                    hit = round(100*np.mean(wins),1) if wins else "N/A"
                    st.success(f"Graded {len(graded)} rows · Hit Rate: {hit}% · Learning updated")
                else:
                    st.warning("No Actual values entered.")
        with col_b:
            st.info("You can still grade one or two props manually, but the main workflow is now full-board save → bulk grade → learning update, like MLB.")

    st.divider()
    st.subheader("Single Prop Quick Grade")
    if projected:
        g_choice=st.selectbox("Prop to grade", [f"{p['player']} — {p['prop']}" for p in projected])
        g=projected[[f"{p['player']} — {p['prop']}" for p in projected].index(g_choice)]
        actual=st.number_input("Actual result", min_value=0.0, step=0.5)
        if st.button("Submit Single Grade + Learn"):
            graded=grade_rows_and_learn([g], [actual], grade_note="single_quick_grade")
            win=graded[0].get("win") if graded else None
            scale=graded[0].get("new_learning_scale") if graded else None
            st.success(f"Graded. Result: {'WIN' if win else 'LOSS' if win is False else 'NO LINE'} · New learning scale: {scale}")

with tabs[9]:
    st.markdown("<div class='section-title-pro'>Learning Dashboard + Calibration</div>", unsafe_allow_html=True)
    results=load_json(RESULT_LOG,[]); learn=load_json(LEARN_FILE,{})
    if results:
        rdf=pd.DataFrame(results)
        total=len(rdf)
        graded_actual = rdf["actual"].notna().sum() if "actual" in rdf.columns else total
        win_series = rdf["win"].dropna() if "win" in rdf.columns else pd.Series(dtype=float)
        hit_rate = f"{round(win_series.mean()*100,1)}%" if len(win_series) else "N/A"
        avg_err = "N/A"
        if "projection_error" in rdf.columns:
            err=pd.to_numeric(rdf["projection_error"], errors="coerce").dropna()
            if len(err): avg_err=round(float(err.mean()),3)
        k1,k2,k3,k4=st.columns(4)
        k1.metric("Graded Props", graded_actual)
        k2.metric("Hit Rate", hit_rate)
        k3.metric("Avg Projection Bias", avg_err)
        k4.metric("Learning Keys", len(learn))

        st.subheader("Calibration by Prop + Player")
        summ=build_learning_summary_df(results)
        if not summ.empty:
            st.dataframe(summ.head(200), use_container_width=True, hide_index=True)
        st.subheader("Recent Graded Rows")
        show_cols=[c for c in ["graded_at","player","team","prop","line","pick","projection","actual","projection_error","win","new_learning_scale","slate_id","grade_note"] if c in rdf.columns]
        st.dataframe(rdf[show_cols].tail(200), use_container_width=True, hide_index=True)
    else:
        st.info("No graded NFL props yet. Once you bulk-grade a saved slate, calibration and learning will populate here.")
    if learn:
        with st.expander("Raw Learning Scale JSON"):
            st.json(learn)

with tabs[10]:
    st.markdown("<div class='section-title-pro'>Underdog Money Line</div>", unsafe_allow_html=True)
    st.write("This tab scans Underdog for NFL moneyline/winner markets when they are posted. It will not create fake moneylines if Underdog does not expose them yet.")
    if moneylines:
        st.success(f"Live Underdog moneyline-style rows detected: {len(moneylines)}")
        st.dataframe(pd.DataFrame(moneylines), use_container_width=True, hide_index=True)
    else:
        st.warning("No Underdog NFL moneyline rows detected right now. Player props can still load normally; this tab will populate automatically if Underdog posts moneyline/winner markets in the scanned feed.")
        st.caption("Tip: most DFS-style Underdog feeds focus on player props. If moneylines are not offered there, keep this tab as a monitor and use sportsbook odds APIs later for true moneyline pricing.")
