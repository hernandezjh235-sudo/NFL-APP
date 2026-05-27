
# -*- coding: utf-8 -*-
"""
NFL PROP ENGINE — Railway / Streamlit ready
Built from the MLB engine structure: clean UI, player cards, projections, pure upside,
alt ladder, CLV, before/after save, grading, learning dashboard.

This app is safe to run before NFL props are live. It attempts live Underdog lines first;
when no NFL prop feed is available, it shows clearly labeled preseason/demo examples so
the UI and workflow can be tested without confusing them as real bets.
"""

import os, json, math, time, difflib, unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

APP_VERSION = "NFL v1.4 — FULL PROP ECOSYSTEM + UNDERDOG MONEY LINE TAB"
LOCAL_DIR = Path(os.getenv("STORAGE_DIR", "nfl_engine"))
LOCAL_DIR.mkdir(parents=True, exist_ok=True)

PICK_LOG = LOCAL_DIR / "nfl_before_snapshots.json"
AFTER_LOG = LOCAL_DIR / "nfl_after_snapshots.json"
RESULT_LOG = LOCAL_DIR / "nfl_results.json"
LEARN_FILE = LOCAL_DIR / "nfl_learning.json"
CLV_FILE = LOCAL_DIR / "nfl_clv_tracker.json"
LINE_HISTORY_FILE = LOCAL_DIR / "nfl_line_history.json"
REQUEST_LOG = LOCAL_DIR / "request_log.json"
USAGE_FILE = LOCAL_DIR / "nfl_player_usage.csv"
TEAM_CONTEXT_FILE = LOCAL_DIR / "nfl_team_context.json"
INJURY_FILE = LOCAL_DIR / "nfl_injuries.json"

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
    reasons=official_rejection_reasons(p)
    side=p.get("pick","PASS")
    prob=safe_float(p.get("fair_prob"),0) or 0
    score=safe_float(p.get("data_score"),0) or 0
    edge_abs=abs(safe_float(p.get("edge"),0) or 0)
    elite=(not reasons and prob>=MIN_NFL_ELITE_PROB and score>=MIN_NFL_ELITE_SCORE and edge_abs>=edge_requirement(p.get("prop"))*1.35)
    if elite: return f"🔥 ELITE WATCH {side}", "BET", reasons
    if not reasons: return f"✅ STRONG WATCH {side}", "BET", reasons
    return f"🚫 PASS — {side}", "PASS", reasons

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
def fetch_underdog_nfl_props():
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
def fetch_underdog_nfl_moneylines():
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
    for k in ["pace","pass_rate","plays_pg","spread","game_total","weather_risk"]:
        if row.get(k) in [None, ""] and team_ctx.get(k) not in [None, ""]:
            row[k]=team_ctx.get(k)
    for k in ["def_pass_rank","def_run_rank","def_slot_rank","def_te_rank","def_rb_rec_rank","pressure_rate","coverage_grade"]:
        if row.get(k) in [None, ""] and opp_ctx.get(k) not in [None, ""]:
            row[k]=opp_ctx.get(k)
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

def defensive_matchup_factor(row, prop):
    factor=1.0; notes=[]; risk="LOW"
    rank=safe_float(row.get("def_role_rank"))
    cov=safe_float(row.get("coverage_grade"))
    pass_rank=safe_float(row.get("def_pass_rank"))
    run_rank=safe_float(row.get("def_run_rank"))
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
    if prop in ["Rushing Yards","Rush Attempts","Longest Rush"] and run_rank is not None:
        if run_rank <= 8: factor*=0.94; risk="HIGH"; notes.append("Top run defense tax")
        elif run_rank >= 24: factor*=1.04; notes.append("Weak run defense boost")
    return clamp(factor,0.88,1.10), risk, notes

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

def project_row(row, sims=12000):
    row=merge_nfl_context(row)
    prop=row.get("prop","Receiving Yards")
    cfg=PROP_CONFIG.get(prop, PROP_CONFIG["Receiving Yards"])
    role=player_role_defaults(row.get("position"),prop)
    role=apply_real_usage_to_role(row, role)
    usage_quality, usage_flags = usage_data_quality(row, prop)
    base=cfg["base"]*usage_adjustment(role,prop)
    base, env_notes, env=apply_environment(base,row,prop)
    defense_factor, defense_risk, defense_notes = defensive_matchup_factor(row, prop)
    game_factor, game_env_risk, game_notes = game_environment_factor(row, prop)
    role_factor, injury_risk, game_script_risk, risk_notes = role_risk_adjustments(row, role, prop)
    if defense_risk == "HIGH" or game_env_risk == "HIGH":
        game_script_risk="HIGH"
    base*=role_factor*defense_factor*game_factor
    learn=learning_scale(row.get("player"),prop)
    cal_scale, cal_note=calibration_scale(row.get("player"),prop)
    base*=learn*cal_scale
    line=safe_float(row.get("line"))

    # Real line anchoring stays, but demo rows cannot become official plays.
    if line is not None and row.get("source")!="DEMO":
        base=base*0.62 + line*0.38

    sigma=cfg["sigma"]
    if injury_risk in ["HIGH","EXTREME"]: sigma*=1.12
    if game_script_risk=="HIGH": sigma*=1.08
    if usage_quality < 72: sigma*=1.07
    collapse_prob, ceiling_prob = simulation_branch_rates(row, prop, injury_risk, game_script_risk)
    seed=abs(hash(str(row.get('player','x'))+prop+str(line)))%(2**32)
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

    notes=[]+env_notes+risk_notes+defense_notes+game_notes
    if usage_flags:
        notes.extend(["Usage data: "+x for x in usage_flags[:3]])
    if cal_scale != 1.0: notes.append(cal_note)
    elif row.get("source")!="DEMO": notes.append(cal_note)
    if row.get("source")=="DEMO": notes.append("Demo row until live NFL props are available")

    out={**row,"projection":round(mean,2),"edge":None if edge is None else round(edge,2),"pick":side,"fair_prob":None if prob is None else round(prob,3),"ev":None if ev is None else round(ev,4),"kelly":round(kelly,4),"p10":round(p10,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"pure_upside":upside,"volatility":volatility,"stability_score":stability,"usage_quality":usage_quality,"collapse_prob":round(collapse_prob,3),"ceiling_prob":round(ceiling_prob,3),"data_score":score,"injury_risk":injury_risk,"game_script_risk":game_script_risk,"defense_risk":defense_risk,"line_delta":line_delta,"true_line_delta":true_line_delta,"role":role,"env":env,"notes":notes,"sim_samples":sims}
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
def save_snapshot(path, rows, label):
    old=load_json(path,[])
    stamp=now_iso()
    for r in rows:
        old.append({**r,"snapshot_type":label,"saved_at":stamp})
    save_json(path, old[-5000:])
    return len(rows)

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

# ---------- UI ----------
st.markdown(f"""
<div class='hero-panel'>
  <div class='big-title'>NFL Prop Engine</div>
  <div class='sub-title'>Clean player cards · projections · pure upside · stadium/noise · weather-ready · CLV · save before/after · grading</div>
  <span class='badge'>{APP_VERSION}</span><span class='badge good-badge'>MLB framework converted to NFL structure</span>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("Controls")
    source_mode=st.radio("Prop Source", ["Live Underdog first, demo fallback", "Live Underdog only", "Demo board only"], index=0)
    prop_filter=st.multiselect("Prop Types", list(PROP_CONFIG.keys()), default=list(PROP_CONFIG.keys()))
    min_score=st.slider("Minimum Data Score",0,99,0)
    show_all=st.checkbox("Show all player cards", True)
    st.divider()
    st.caption("API keys can be added in Streamlit secrets or Railway variables later.")
    show_feed_debug=st.checkbox("Show Underdog feed debug", False)
    st.code("STORAGE_DIR=nfl_engine", language="bash")

live=[] if source_mode=="Demo board only" else fetch_underdog_nfl_props()
moneylines=[] if source_mode=="Demo board only" else fetch_underdog_nfl_moneylines()
raw = live if live else ([] if source_mode=="Live Underdog only" else DEMO_BOARD)
projected=[project_row(r) for r in raw if r.get("prop") in prop_filter]
projected=[p for p in projected if p.get("data_score",0)>=min_score]

df=pd.DataFrame(projected)
real_count=sum(1 for p in projected if p.get("source")!="DEMO")
best_edges=[p for p in projected if p.get("action_tier")=="BET"]

st.markdown("<div class='kpi-strip'>"+
    f"<div class='kpi-box'><div class='kpi-label'>Player Cards</div><div class='kpi-value'>{len(projected)}</div><div class='kpi-sub'>shown on board</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Live Lines</div><div class='kpi-value'>{real_count}</div><div class='kpi-sub'>{'Underdog detected' if real_count else 'demo fallback active'}</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Best Edges</div><div class='kpi-value'>{len(best_edges)}</div><div class='kpi-sub'>prob/edge filtered</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Before Saves</div><div class='kpi-value'>{len(load_json(PICK_LOG,[]))}</div><div class='kpi-sub'>official snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>After Saves</div><div class='kpi-value'>{len(load_json(AFTER_LOG,[]))}</div><div class='kpi-sub'>closing snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Graded</div><div class='kpi-value'>{len(load_json(RESULT_LOG,[]))}</div><div class='kpi-sub'>learning rows</div></div>"+
    "</div>", unsafe_allow_html=True)

if live:
    st.success(f"Live Underdog NFL props detected: {len(live)} rows. Demo mode is OFF for this refresh.")
elif source_mode == "Live Underdog only":
    st.warning("No live Underdog NFL rows were detected. Live-only mode is showing an empty board instead of demo lines.")
else:
    st.info("No live NFL prop feed was detected right now, so the app is showing clearly labeled DEMO cards. Once Underdog NFL props are live, this app will automatically use real Underdog lines first.")

if 'show_feed_debug' in globals() and show_feed_debug:
    req_log=load_json(REQUEST_LOG,[])
    st.caption("Latest Underdog/API request log")
    st.dataframe(pd.DataFrame(req_log[-25:]), use_container_width=True, hide_index=True)

tabs=st.tabs(["Today / Weekly Board", "Best Edges", "Player Cards", "Alt-Line Ladder", "Correlation Builder", "Official Filter", "Save + Grade", "Learning Dashboard", "System Notes", "Money Line"])

with tabs[0]:
    st.markdown("<div class='section-title-pro'>NFL Board</div>", unsafe_allow_html=True)
    if df.empty: st.warning("No props available with current filters.")
    else:
        show_cols=["player","position","team","matchup","prop","line","projection","edge","pick","fair_prob","ev","kelly","signal","action_tier","pure_upside","volatility","stability_score","data_score","line_delta","source"]
        st.dataframe(df[[c for c in show_cols if c in df.columns]], use_container_width=True, hide_index=True)

with tabs[1]:
    st.markdown("<div class='section-title-pro'>Best Edges</div>", unsafe_allow_html=True)
    edges=sorted(best_edges, key=lambda x: (x.get("fair_prob") or 0, x.get("data_score") or 0, abs(x.get("edge") or 0)), reverse=True)
    if not edges: st.warning("No strong edge cards yet. During preseason/demo mode this is normal.")
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

with tabs[2]:
    st.markdown("<div class='section-title-pro'>Clickable Player Cards</div>", unsafe_allow_html=True)
    for i,p in enumerate(projected):
        badge_class="good-badge" if p.get("pick")=="OVER" else "red-badge" if p.get("pick")=="UNDER" else "yellow-badge"
        st.markdown(f"""
        <div class='pick-card'>
          <div class='player-name'>{p['player']} <span class='small-muted'>({p.get('position','')} · {p.get('team','')})</span></div>
          <span class='badge'>{p.get('prop')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge {badge_class}'>{p.get('signal')}</span><span class='badge yellow-badge'>Upside {p.get('pure_upside')}</span><span class='badge'>Vol {p.get('volatility')}</span>
          <div class='kpi-strip'>
            <div class='metric-card'><div class='kpi-label'>Line</div><div class='kpi-value'>{p.get('line')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Projection</div><div class='kpi-value'>{p.get('projection')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Edge</div><div class='kpi-value'>{p.get('edge')}</div></div>
            <div class='metric-card'><div class='kpi-label'>Fair Prob</div><div class='kpi-value'>{'' if p.get('fair_prob') is None else str(round(p.get('fair_prob')*100,1))+'%'}</div></div>
            <div class='metric-card'><div class='kpi-label'>P75</div><div class='kpi-value'>{p.get('p75')}</div></div>
            <div class='metric-card'><div class='kpi-label'>P90 Ceiling</div><div class='kpi-value'>{p.get('p90')}</div></div>
          </div>
        </div>
        """, unsafe_allow_html=True)
        with st.expander(f"View More — {p['player']} {p['prop']}"):
            c1,c2,c3=st.columns(3)
            with c1:
                st.subheader("Usage")
                role=p["role"]
                st.write(f"Snap Share: **{role['snap']}%**")
                st.write(f"Route Participation: **{role['route']}%**")
                st.write(f"Target Share: **{role['target']}%**")
                st.write(f"Carry Share: **{role['carry']}%**")
                st.write(f"Red-Zone Usage: **{role['rz']}%**")
            with c2:
                st.subheader("Environment")
                env=p["env"]
                st.write(f"Stadium: **{env['stadium']}**")
                st.write(f"Crowd Noise: **{env['crowd']}**")
                st.write(f"Roof: **{env['roof']}**")
                st.write(f"Surface: **{env['surface']}**")
                st.write(f"Altitude: **{env['altitude']} ft**")
            with c3:
                st.subheader("Risk Notes")
                for n in p.get("notes",[]): st.write("- "+n)
                st.write(f"Data Score: **{p['data_score']}/99**")
                st.write(f"Stability Score: **{p.get('stability_score')} /100**")
                st.write(f"Action Tier: **{p.get('action_tier')}**")
                rejects=p.get('official_rejections') or []
                if rejects:
                    st.write("Official Filter Rejections:")
                    for rr in rejects: st.write("- "+str(rr))
                st.write(f"CLV Line Delta: **{p.get('line_delta')}**")
                st.write(f"Source: **{p['source']}**")
            st.subheader("Alt Ladder")
            st.dataframe(alt_ladder(p), use_container_width=True, hide_index=True)

with tabs[3]:
    st.markdown("<div class='section-title-pro'>Alt-Line Ladder</div>", unsafe_allow_html=True)
    names=[f"{p['player']} — {p['prop']}" for p in projected]
    if names:
        choice=st.selectbox("Choose Player Prop", names)
        p=projected[names.index(choice)]
        st.dataframe(alt_ladder(p), use_container_width=True, hide_index=True)
    else: st.warning("No props to ladder.")

with tabs[4]:
    st.markdown("<div class='section-title-pro'>Correlation Builder</div>", unsafe_allow_html=True)
    st.write("Use this to avoid bad parlays and find positive stacks.")
    if df.empty: st.warning("No player cards loaded.")
    else:
        left=st.selectbox("Leg 1", [f"{p['player']} — {p['prop']}" for p in projected], key="corr1")
        right=st.selectbox("Leg 2", [f"{p['player']} — {p['prop']}" for p in projected], key="corr2")
        p1=projected[[f"{p['player']} — {p['prop']}" for p in projected].index(left)]
        p2=projected[[f"{p['player']} — {p['prop']}" for p in projected].index(right)]
        corr="Neutral"
        if p1.get("matchup")==p2.get("matchup"):
            if "Passing" in p1["prop"] and p2["prop"] in ["Receiving Yards","Receptions","Anytime TD","Longest Reception"]: corr="Positive QB stack"
            elif p1["team"]==p2["team"] and p1["prop"]==p2["prop"]: corr="Possible target/usage conflict"
            elif p1["team"]!=p2["team"] and any(x in p1["prop"] for x in ["Passing","Receiving"]) and any(x in p2["prop"] for x in ["Passing","Receiving"]): corr="Positive game-script shootout"
        st.success(f"Correlation Read: {corr}")

with tabs[5]:
    st.markdown("<div class='section-title-pro'>Official Play Filter 2.0 — NFL</div>", unsafe_allow_html=True)
    st.write("This mirrors the MLB app: it filters plays instead of forcing picks.")
    filt_rows=[]
    for p in projected:
        filt_rows.append({
            "Player": p.get("player"), "Prop": p.get("prop"), "Pick": p.get("pick"),
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

with tabs[6]:
    st.markdown("<div class='section-title-pro'>Save Before / After / Final Grade</div>", unsafe_allow_html=True)
    c1,c2,c3=st.columns(3)
    with c1:
        if st.button("Save BEFORE Snapshot", use_container_width=True): st.success(f"Saved {save_snapshot(PICK_LOG, projected, 'BEFORE')} before rows")
    with c2:
        if st.button("Save AFTER Snapshot", use_container_width=True): st.success(f"Saved {save_snapshot(AFTER_LOG, projected, 'AFTER')} after rows")
    with c3:
        st.write("Final grading below")
    st.divider()
    if projected:
        g_choice=st.selectbox("Prop to grade", [f"{p['player']} — {p['prop']}" for p in projected])
        g=projected[[f"{p['player']} — {p['prop']}" for p in projected].index(g_choice)]
        actual=st.number_input("Actual result", min_value=0.0, step=0.5)
        if st.button("Submit Final Grade + Learn"):
            line=safe_float(g.get("line")); pick=g.get("pick"); win=None
            if line is not None:
                win = actual > line if pick=="OVER" else actual < line if pick=="UNDER" else None
            scale=update_learning_from_result(g["player"],g["prop"],g["projection"],actual)
            rows=load_json(RESULT_LOG,[]); rows.append({**g,"actual":actual,"win":win,"graded_at":now_iso(),"new_learning_scale":scale}); save_json(RESULT_LOG,rows[-5000:])
            st.success(f"Graded. Result: {'WIN' if win else 'LOSS' if win is False else 'NO LINE'} · New learning scale: {scale}")

with tabs[7]:
    st.markdown("<div class='section-title-pro'>Learning Dashboard</div>", unsafe_allow_html=True)
    results=load_json(RESULT_LOG,[]); learn=load_json(LEARN_FILE,{})
    if results:
        rdf=pd.DataFrame(results)
        st.metric("Graded Props",len(rdf))
        if "win" in rdf.columns: st.metric("Hit Rate", f"{round(rdf['win'].dropna().mean()*100,1)}%" if len(rdf['win'].dropna()) else "N/A")
        st.dataframe(rdf.tail(100), use_container_width=True)
    else: st.info("No graded NFL props yet. Once you grade results, this dashboard will populate.")
    if learn: st.json(learn)

with tabs[8]:
    st.markdown("<div class='section-title-pro'>System Notes</div>", unsafe_allow_html=True)
    st.write("Built-in NFL modules included in this starter:")
    st.write("- Real snap %, route participation, target share, carries, air-yard share, red-zone usage")
    st.write("- Optional CSV/JSON data hooks: nfl_player_usage.csv, nfl_team_context.json, nfl_injuries.json")
    st.write("- OL vs pass-rush, pressure rate, coverage grade, defensive role matchup, pass/run defense ranking")
    st.write("- Vegas total/spread, pace, pass rate, blowout branches, weather collapse games")
    st.write("- Injury exits and asymmetric collapse/ceiling simulation branches")
    st.write("- Stadium/home-away layer: crowd noise, dome/outdoor, surface, altitude")
    st.write("- Pure upside simulation: P10/P50/P75/P90")
    st.write("- Alt-line ladder and correlation builder")
    st.write("- Before/after snapshots, CLV-ready logs, final grading, learning scale")
    st.write("- Expanded props: pass attempts, completions, rush attempts, longest reception/rush, kicking, tackles, sacks")
    st.write("- MLB-style Official Play Filter 2.0: probability, edge, data score, volatility, stability, and role-risk gates")
    st.write("- True calibration warmup from graded results with capped projection shifts")
    st.write("- Asymmetric NFL simulation with collapse/ceiling branches instead of clean normal-only outcomes")
    st.warning("Preseason note: demo rows are for testing UI/workflow only. Real Underdog NFL rows automatically override demo rows when live. Use Live Underdog only to verify the feed without fallback noise.")

with tabs[9]:
    st.markdown("<div class='section-title-pro'>Underdog Money Line</div>", unsafe_allow_html=True)
    st.write("This tab scans Underdog for NFL moneyline/winner markets when they are posted. It will not create fake moneylines if Underdog does not expose them yet.")
    if moneylines:
        st.success(f"Live Underdog moneyline-style rows detected: {len(moneylines)}")
        st.dataframe(pd.DataFrame(moneylines), use_container_width=True, hide_index=True)
    else:
        st.warning("No Underdog NFL moneyline rows detected right now. Player props can still load normally; this tab will populate automatically if Underdog posts moneyline/winner markets in the scanned feed.")
        st.caption("Tip: most DFS-style Underdog feeds focus on player props. If moneylines are not offered there, keep this tab as a monitor and use sportsbook odds APIs later for true moneyline pricing.")
