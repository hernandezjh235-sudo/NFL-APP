
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

APP_VERSION = "NFL v1.0 — CLEAN PROP ENGINE + PURE UPSIDE"
LOCAL_DIR = Path(os.getenv("STORAGE_DIR", "nfl_engine"))
LOCAL_DIR.mkdir(parents=True, exist_ok=True)

PICK_LOG = LOCAL_DIR / "nfl_before_snapshots.json"
AFTER_LOG = LOCAL_DIR / "nfl_after_snapshots.json"
RESULT_LOG = LOCAL_DIR / "nfl_results.json"
LEARN_FILE = LOCAL_DIR / "nfl_learning.json"
CLV_FILE = LOCAL_DIR / "nfl_clv_tracker.json"
LINE_HISTORY_FILE = LOCAL_DIR / "nfl_line_history.json"
REQUEST_LOG = LOCAL_DIR / "request_log.json"

UNDERDOG_URLS = [
    "https://api.underdogfantasy.com/beta/v6/over_under_lines",
    "https://api.underdogfantasy.com/beta/v5/over_under_lines",
    "https://api.underdogfantasy.com/v1/over_under_lines",
]

PROP_CONFIG = {
    "Passing Yards": {"stat": "pass_yds", "sigma": 42, "base": 235, "volume_key": "pass_attempts"},
    "Passing TDs": {"stat": "pass_tds", "sigma": 0.85, "base": 1.55, "volume_key": "pass_attempts"},
    "Interceptions": {"stat": "interceptions", "sigma": 0.65, "base": 0.72, "volume_key": "pass_attempts"},
    "Rushing Yards": {"stat": "rush_yds", "sigma": 24, "base": 49, "volume_key": "carries"},
    "Receiving Yards": {"stat": "rec_yds", "sigma": 27, "base": 52, "volume_key": "routes"},
    "Receptions": {"stat": "receptions", "sigma": 1.9, "base": 4.3, "volume_key": "targets"},
    "Fantasy Points": {"stat": "fantasy_pts", "sigma": 6.5, "base": 14.2, "volume_key": "usage"},
    "Anytime TD": {"stat": "anytime_td", "sigma": 0.28, "base": 0.34, "volume_key": "red_zone"},
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
    {"player":"Patrick Mahomes", "team":"KC", "opp":"LAC", "home_away":"HOME", "position":"QB", "prop":"Passing Yards", "line":285.5, "source":"DEMO", "matchup":"LAC @ KC"},
    {"player":"Josh Allen", "team":"BUF", "opp":"NYJ", "home_away":"HOME", "position":"QB", "prop":"Rushing Yards", "line":39.5, "source":"DEMO", "matchup":"NYJ @ BUF"},
    {"player":"Justin Jefferson", "team":"MIN", "opp":"GB", "home_away":"HOME", "position":"WR", "prop":"Receiving Yards", "line":89.5, "source":"DEMO", "matchup":"GB @ MIN"},
    {"player":"Christian McCaffrey", "team":"SF", "opp":"SEA", "home_away":"AWAY", "position":"RB", "prop":"Rushing Yards", "line":74.5, "source":"DEMO", "matchup":"SF @ SEA"},
    {"player":"Travis Kelce", "team":"KC", "opp":"LAC", "home_away":"HOME", "position":"TE", "prop":"Receptions", "line":5.5, "source":"DEMO", "matchup":"LAC @ KC"},
    {"player":"Amon-Ra St. Brown", "team":"DET", "opp":"CHI", "home_away":"HOME", "position":"WR", "prop":"Receptions", "line":6.5, "source":"DEMO", "matchup":"CHI @ DET"},
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
def looks_nfl(item):
    blob=json.dumps(item).lower()
    return any(x in blob for x in [" nfl", "football", "nfl_", "national football", "passing yards", "receiving yards", "rushing yards", "receptions"])

def prop_name_from_blob(blob):
    b=blob.lower()
    mapping=[("passing yards","Passing Yards"),("pass yards","Passing Yards"),("rushing yards","Rushing Yards"),("rush yards","Rushing Yards"),("receiving yards","Receiving Yards"),("receptions","Receptions"),("fantasy points","Fantasy Points"),("passing tds","Passing TDs"),("interceptions","Interceptions"),("anytime td","Anytime TD")]
    for key,val in mapping:
        if key in b: return val
    return None

def flatten(obj):
    out=[]
    if isinstance(obj,dict):
        out.append(obj)
        for v in obj.values(): out.extend(flatten(v))
    elif isinstance(obj,list):
        for x in obj: out.extend(flatten(x))
    return out

@st.cache_data(ttl=240, show_spinner=False)
def fetch_underdog_nfl_props():
    rows=[]
    for url in UNDERDOG_URLS:
        data=safe_get_json(url)
        if not data: continue
        objects=flatten(data)
        players={}
        for o in objects:
            name=o.get("first_name") and o.get("last_name") and f"{o.get('first_name')} {o.get('last_name')}"
            if name and o.get("id"): players[str(o.get("id"))]=name
        for o in objects:
            blob=json.dumps(o)
            if not looks_nfl(o): continue
            prop=prop_name_from_blob(blob)
            if not prop: continue
            line=None
            for k in ["stat_value","line","value","over_under","threshold"]:
                if safe_float(o.get(k)) is not None: line=safe_float(o.get(k)); break
            if line is None: continue
            player=o.get("player_name") or o.get("title") or o.get("name")
            if not player:
                pid=o.get("player_id") or o.get("over_under",{}).get("appearance_stat",{}).get("appearance",{}).get("player_id") if isinstance(o.get("over_under"),dict) else None
                player=players.get(str(pid), "Unknown Player")
            rows.append({"player":str(player),"team":o.get("team_abbr") or o.get("team") or "NFL","opp":"","home_away":"","position":o.get("position") or "","prop":prop,"line":line,"source":"Underdog","matchup":o.get("matchup") or ""})
    # dedupe
    seen=set(); clean=[]
    for r in rows:
        key=(norm(r["player"]),r["prop"],r["line"])
        if key not in seen:
            seen.add(key); clean.append(r)
    return clean[:300]

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
    if away and env["crowd"] in ["LOUD","EXTREME"] and prop in ["Passing Yards","Passing TDs","Interceptions"]:
        factor*=env.get("noise",1.0); notes.append(f"Road crowd noise: {env['crowd']}")
    if env.get("roof") in ["Dome","Retractable"] and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs"]:
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
    return 1.0

def learning_scale(player, prop):
    data=load_json(LEARN_FILE,{})
    return safe_float(data.get(f"{norm(player)}|{prop}",1.0),1.0) or 1.0

def project_row(row, sims=10000):
    prop=row.get("prop","Receiving Yards")
    cfg=PROP_CONFIG.get(prop, PROP_CONFIG["Receiving Yards"])
    role=player_role_defaults(row.get("position"),prop)
    base=cfg["base"]*usage_adjustment(role,prop)
    base, env_notes, env=apply_environment(base,row,prop)
    base*=learning_scale(row.get("player"),prop)
    line=safe_float(row.get("line"))
    # Anchor slightly toward real market line when available to avoid wild preseason estimates.
    if line is not None and row.get("source")!="DEMO":
        base=base*0.55 + line*0.45
    sigma=cfg["sigma"]
    if prop in ["Passing TDs","Interceptions","Anytime TD"]:
        raw=np.random.default_rng(abs(hash(row.get('player','x')+prop))%(2**32)).normal(base, sigma, sims)
        sim=np.clip(raw,0,None)
    else:
        raw=np.random.default_rng(abs(hash(row.get('player','x')+prop))%(2**32)).normal(base, sigma, sims)
        sim=np.clip(raw,0,None)
    mean=float(np.mean(sim)); p50=float(np.percentile(sim,50)); p75=float(np.percentile(sim,75)); p90=float(np.percentile(sim,90)); p10=float(np.percentile(sim,10))
    if line is None: prob=None; side="NO LINE"; edge=None
    else:
        over=float(np.mean(sim>line)); under=1-over
        side="OVER" if over>=under else "UNDER"; prob=max(over,under); edge=mean-line
    upside_gap=p90-(line if line is not None else p50)
    if upside_gap>cfg["sigma"]*0.95: upside="ELITE"
    elif upside_gap>cfg["sigma"]*0.55: upside="GOOD"
    else: upside="NORMAL"
    vol=(p90-p10)/max(1,mean)
    volatility="HIGH" if vol>.9 else "MED" if vol>.55 else "LOW"
    score=50
    if prob: score+=int((prob-.50)*100)
    score+=8 if upside in ["ELITE","GOOD"] else 0
    score-=8 if volatility=="HIGH" else 0
    if row.get("source")!="DEMO": score+=8
    score=int(clamp(score,0,99))
    notes=[]+env_notes
    if row.get("source")=="DEMO": notes.append("Demo row until live NFL props are available")
    return {**row,"projection":round(mean,2),"edge":None if edge is None else round(edge,2),"pick":side,"fair_prob":None if prob is None else round(prob,3),"p10":round(p10,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"pure_upside":upside,"volatility":volatility,"data_score":score,"role":role,"env":env,"notes":notes,"sim_samples":sims}

def alt_ladder(p):
    line=safe_float(p.get("line")); prop=p.get("prop")
    if line is None: return pd.DataFrame()
    step=10 if "Yards" in prop else 1 if prop in ["Receptions"] else 0.5
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
    source_mode=st.radio("Prop Source", ["Live first, demo fallback", "Demo board only"], index=0)
    prop_filter=st.multiselect("Prop Types", list(PROP_CONFIG.keys()), default=list(PROP_CONFIG.keys()))
    min_score=st.slider("Minimum Data Score",0,99,0)
    show_all=st.checkbox("Show all player cards", True)
    st.divider()
    st.caption("API keys can be added in Streamlit secrets or Railway variables later.")
    st.code("STORAGE_DIR=nfl_engine", language="bash")

live=[] if source_mode=="Demo board only" else fetch_underdog_nfl_props()
raw = live if live else DEMO_BOARD
projected=[project_row(r) for r in raw if r.get("prop") in prop_filter]
projected=[p for p in projected if p.get("data_score",0)>=min_score]

df=pd.DataFrame(projected)
real_count=sum(1 for p in projected if p.get("source")!="DEMO")
best_edges=[p for p in projected if p.get("fair_prob") and p.get("fair_prob")>=0.58 and abs(p.get("edge") or 0)>0]

st.markdown("<div class='kpi-strip'>"+
    f"<div class='kpi-box'><div class='kpi-label'>Player Cards</div><div class='kpi-value'>{len(projected)}</div><div class='kpi-sub'>shown on board</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Live Lines</div><div class='kpi-value'>{real_count}</div><div class='kpi-sub'>{'Underdog detected' if real_count else 'demo fallback active'}</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Best Edges</div><div class='kpi-value'>{len(best_edges)}</div><div class='kpi-sub'>prob/edge filtered</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Before Saves</div><div class='kpi-value'>{len(load_json(PICK_LOG,[]))}</div><div class='kpi-sub'>official snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>After Saves</div><div class='kpi-value'>{len(load_json(AFTER_LOG,[]))}</div><div class='kpi-sub'>closing snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Graded</div><div class='kpi-value'>{len(load_json(RESULT_LOG,[]))}</div><div class='kpi-sub'>learning rows</div></div>"+
    "</div>", unsafe_allow_html=True)

if not live:
    st.info("No live NFL prop feed was detected right now, so the app is showing clearly labeled DEMO cards. Once NFL props are live, switch to Live first and it will intake real lines when available.")

tabs=st.tabs(["Today / Weekly Board", "Best Edges", "Player Cards", "Alt-Line Ladder", "Correlation Builder", "Save + Grade", "Learning Dashboard", "System Notes"])

with tabs[0]:
    st.markdown("<div class='section-title-pro'>NFL Board</div>", unsafe_allow_html=True)
    if df.empty: st.warning("No props available with current filters.")
    else:
        show_cols=["player","position","team","matchup","prop","line","projection","edge","pick","fair_prob","pure_upside","volatility","data_score","source"]
        st.dataframe(df[[c for c in show_cols if c in df.columns]], use_container_width=True, hide_index=True)

with tabs[1]:
    st.markdown("<div class='section-title-pro'>Best Edges</div>", unsafe_allow_html=True)
    edges=sorted(best_edges, key=lambda x: (x.get("fair_prob") or 0, abs(x.get("edge") or 0)), reverse=True)
    if not edges: st.warning("No strong edge cards yet. During preseason/demo mode this is normal.")
    for p in edges[:30]:
        st.markdown(f"""
        <div class='pick-card'><div class='player-name'>{p['player']} — {p['prop']}</div>
        <span class='badge'>{p.get('team','')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge good-badge'>{p['pick']}</span><span class='badge yellow-badge'>Pure Upside: {p['pure_upside']}</span>
        <div class='kpi-strip'>
        <div class='metric-card'><div class='kpi-label'>Line</div><div class='kpi-value'>{p.get('line')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Projection</div><div class='kpi-value'>{p.get('projection')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Edge</div><div class='kpi-value'>{p.get('edge')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Fair Prob</div><div class='kpi-value'>{round((p.get('fair_prob') or 0)*100,1)}%</div></div>
        <div class='metric-card'><div class='kpi-label'>Ceiling P90</div><div class='kpi-value'>{p.get('p90')}</div></div>
        <div class='metric-card'><div class='kpi-label'>Score</div><div class='kpi-value'>{p.get('data_score')}</div></div>
        </div></div>""", unsafe_allow_html=True)

with tabs[2]:
    st.markdown("<div class='section-title-pro'>Clickable Player Cards</div>", unsafe_allow_html=True)
    for i,p in enumerate(projected):
        badge_class="good-badge" if p.get("pick")=="OVER" else "red-badge" if p.get("pick")=="UNDER" else "yellow-badge"
        st.markdown(f"""
        <div class='pick-card'>
          <div class='player-name'>{p['player']} <span class='small-muted'>({p.get('position','')} · {p.get('team','')})</span></div>
          <span class='badge'>{p.get('prop')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge {badge_class}'>{p.get('pick')}</span><span class='badge yellow-badge'>Upside {p.get('pure_upside')}</span><span class='badge'>Vol {p.get('volatility')}</span>
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
            if "Passing" in p1["prop"] and p2["prop"] in ["Receiving Yards","Receptions","Anytime TD"]: corr="Positive QB stack"
            elif p1["team"]==p2["team"] and p1["prop"]==p2["prop"]: corr="Possible target/usage conflict"
            elif p1["team"]!=p2["team"] and any(x in p1["prop"] for x in ["Passing","Receiving"]) and any(x in p2["prop"] for x in ["Passing","Receiving"]): corr="Positive game-script shootout"
        st.success(f"Correlation Read: {corr}")

with tabs[5]:
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

with tabs[6]:
    st.markdown("<div class='section-title-pro'>Learning Dashboard</div>", unsafe_allow_html=True)
    results=load_json(RESULT_LOG,[]); learn=load_json(LEARN_FILE,{})
    if results:
        rdf=pd.DataFrame(results)
        st.metric("Graded Props",len(rdf))
        if "win" in rdf.columns: st.metric("Hit Rate", f"{round(rdf['win'].dropna().mean()*100,1)}%" if len(rdf['win'].dropna()) else "N/A")
        st.dataframe(rdf.tail(100), use_container_width=True)
    else: st.info("No graded NFL props yet. Once you grade results, this dashboard will populate.")
    if learn: st.json(learn)

with tabs[7]:
    st.markdown("<div class='section-title-pro'>System Notes</div>", unsafe_allow_html=True)
    st.write("Built-in NFL modules included in this starter:")
    st.write("- Snap share / route participation / target share / carry share")
    st.write("- OL vs pass-rush style pressure risk")
    st.write("- Red-zone usage and TD equity")
    st.write("- Stadium/home-away layer: crowd noise, dome/outdoor, surface, altitude")
    st.write("- Pure upside simulation: P10/P50/P75/P90")
    st.write("- Alt-line ladder and correlation builder")
    st.write("- Before/after snapshots, CLV-ready logs, final grading, learning scale")
    st.warning("Preseason note: demo rows are for testing UI/workflow only. Treat real betting signals only when source is not DEMO and live lines are active.")
