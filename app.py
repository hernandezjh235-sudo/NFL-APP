
# -*- coding: utf-8 -*-
"""
NFL PROP ENGINE — Railway / Streamlit ready
Built from the MLB engine structure: clean UI, player cards, projections, pure upside,
alt ladder, CLV, before/after save, grading, learning dashboard.

This app is safe to run before NFL props are live. It attempts live Underdog lines first;
when no NFL prop feed is available, it shows clearly labeled preseason/demo examples so
the UI and workflow can be tested without confusing them as real bets.
"""

import os, json, math, time, difflib, unicodedata, hashlib, re, io, zipfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

APP_VERSION = "FULL-MARKETS-NFL v5.0 — ACCURACY + MOBILE + PERFORMANCE"
MODEL_VERSION = "nfl-prop-engine-v5.0.0"
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
DEPTH_CHART_FILE = LOCAL_DIR / "nfl_depth_chart.csv"
WEATHER_FILE = LOCAL_DIR / "nfl_weather_context.json"
MARKET_CONTEXT_FILE = LOCAL_DIR / "nfl_market_context.csv"
CURRENT_USAGE_FILE = LOCAL_DIR / "nfl_current_player_usage.csv"
CURRENT_TEAM_CONTEXT_FILE = LOCAL_DIR / "nfl_current_team_context.json"
TRAVEL_CONTEXT_FILE = LOCAL_DIR / "nfl_travel_context.csv"
MATCHUP_CONTEXT_FILE = LOCAL_DIR / "nfl_matchup_context.csv"
QB_CONTEXT_FILE = LOCAL_DIR / "nfl_qb_context.csv"
DEF_INJURY_FILE = LOCAL_DIR / "nfl_defensive_injuries.json"
SPLITS_CONTEXT_FILE = LOCAL_DIR / "nfl_player_splits.csv"
PERSONNEL_CONTEXT_FILE = LOCAL_DIR / "nfl_personnel_matchups.csv"
API_CONFIG_FILE = LOCAL_DIR / "nfl_api_config.json"
FINAL_INACTIVES_FILE = LOCAL_DIR / "nfl_final_inactives.json"
MANUAL_OVERRIDE_FILE = LOCAL_DIR / "nfl_manual_overrides.json"

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
NFL_CURRENT_SEASON = int(os.getenv("NFL_CURRENT_SEASON", str(max(NFL_LAST_SEASON + 1, datetime.now().year))))

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
    "Passing Yards": ["passing yards", "pass yards", "pass yds", "qb passing yards", "pass yard", "passing yard", "passyards"],
    "Passing TDs": ["passing tds", "passing touchdowns", "pass tds", "pass touchdowns", "pass td", "passing td", "pass touchdowns", "passing touchdowns", "td passes"],
    "Interceptions": ["interceptions", "passing interceptions", "ints", "qb interceptions", "interception", "int"],
    "Rushing Yards": ["rushing yards", "rush yards", "rush yds", "rush yard", "rushing yard"],
    "Receiving Yards": ["receiving yards", "rec yards", "receiving yds", "rec yds", "receiving yard", "rec yard"],
    "Receptions": ["receptions", "rec", "catches"],
    "Fantasy Points": ["fantasy points", "fantasy score"],
    "Anytime TD": ["anytime td", "anytime touchdown", "td scorer", "touchdown scorer", "rush + rec tds", "rush rec tds", "rush + receiving tds", "rush receiving touchdowns", "rush + rec touchdowns", "rushing + receiving tds"],
    "Pass Attempts": ["pass attempts", "passing attempts", "attempted passes", "qb attempts"],
    "Completions": ["completions", "passing completions", "completed passes"],
    "Rush Attempts": ["rush attempts", "rushing attempts", "carries", "rushing attempts +", "rush att", "rushing att", "carrie"],
    "Longest Reception": ["longest reception", "longest catch", "long reception"],
    "Longest Rush": ["longest rush", "longest carry", "long rush"],
    "Kicking Points": ["kicking points", "kicker points"],
    "Field Goals Made": ["field goals made", "fg made", "made field goals"],
    "Tackles + Assists": ["tackles + assists", "tackles and assists", "combined tackles", "tackles assists"],
    "Sacks": ["sacks", "player sacks", "defensive sacks"],
}
NFL_SPORT_TERMS = ["nfl", "football", "national football", "nfl_", "american football"]
NON_NFL_BLOCK_TERMS = ["mlb", "baseball", "nba", "wnba", "basketball", "nhl", "hockey", "soccer", "tennis", "golf", "mma", "ufc"]

# Full single-game NFL market support. Feed validation, realistic line ranges, and
# position checks below prevent season totals or unrelated sports from entering the board.
ACTIVE_NFL_MARKETS = {
    "Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions",
    "Rushing Yards", "Rush Attempts", "Receiving Yards", "Receptions",
    "Fantasy Points", "Anytime TD", "Longest Reception", "Longest Rush",
    "Kicking Points", "Field Goals Made", "Tackles + Assists", "Sacks",
}
ACTIVE_NFL_MARKET_ORDER = [
    "Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions",
    "Receiving Yards", "Receptions", "Longest Reception",
    "Rushing Yards", "Rush Attempts", "Longest Rush",
    "Fantasy Points", "Anytime TD",
    "Kicking Points", "Field Goals Made", "Tackles + Assists", "Sacks",
]

# Current NFL team abbreviations. These are used to reject malformed feed rows such as
# team="NFL", matchup="@ NFL", and ISO timestamps accidentally parsed as games.
NFL_TEAM_ABBRS = {
    "ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB",
    "HOU","IND","JAX","KC","LV","LAC","LAR","MIA","MIN","NE","NO","NYG","NYJ",
    "PHI","PIT","SEA","SF","TB","TEN","WSH"
}
NFL_TEAM_ALIASES = {
    "JAC":"JAX", "WAS":"WSH", "OAK":"LV", "SD":"LAC", "STL":"LAR",
}
NFL_TEAM_NAME_ALIASES = {
    "ARIZONA CARDINALS":"ARI", "ATLANTA FALCONS":"ATL", "BALTIMORE RAVENS":"BAL",
    "BUFFALO BILLS":"BUF", "CAROLINA PANTHERS":"CAR", "CHICAGO BEARS":"CHI",
    "CINCINNATI BENGALS":"CIN", "CLEVELAND BROWNS":"CLE", "DALLAS COWBOYS":"DAL",
    "DENVER BRONCOS":"DEN", "DETROIT LIONS":"DET", "GREEN BAY PACKERS":"GB",
    "HOUSTON TEXANS":"HOU", "INDIANAPOLIS COLTS":"IND", "JACKSONVILLE JAGUARS":"JAX",
    "KANSAS CITY CHIEFS":"KC", "LAS VEGAS RAIDERS":"LV", "LOS ANGELES CHARGERS":"LAC",
    "LOS ANGELES RAMS":"LAR", "MIAMI DOLPHINS":"MIA", "MINNESOTA VIKINGS":"MIN",
    "NEW ENGLAND PATRIOTS":"NE", "NEW ORLEANS SAINTS":"NO", "NEW YORK GIANTS":"NYG",
    "NEW YORK JETS":"NYJ", "PHILADELPHIA EAGLES":"PHI", "PITTSBURGH STEELERS":"PIT",
    "SAN FRANCISCO 49ERS":"SF", "SEATTLE SEAHAWKS":"SEA", "TAMPA BAY BUCCANEERS":"TB",
    "TENNESSEE TITANS":"TEN", "WASHINGTON COMMANDERS":"WSH",
}
ACTIVE_NFL_MARKET_LABELS = {
    "Passing Yards": "Pass Yards", "Passing TDs": "Pass TDs", "Interceptions": "Interceptions",
    "Pass Attempts": "Pass Attempts", "Completions": "Completions",
    "Receiving Yards": "Receiving Yards", "Receptions": "Receptions", "Longest Reception": "Longest Reception",
    "Rushing Yards": "Rushing Yards", "Rush Attempts": "Rush Attempts", "Longest Rush": "Longest Rush",
    "Fantasy Points": "Fantasy Points", "Anytime TD": "Anytime TD",
    "Kicking Points": "Kicking Points", "Field Goals Made": "Field Goals Made",
    "Tackles + Assists": "Tackles + Assists", "Sacks": "Sacks",
}
PROJECTION_EDGE_CAPS = {
    "Passing Yards": 34.0, "Passing TDs": 0.85, "Interceptions": 0.65,
    "Receiving Yards": 24.0, "Receptions": 2.1, "Longest Reception": 9.0,
    "Rushing Yards": 18.0, "Rush Attempts": 4.5, "Longest Rush": 8.0,
    "Pass Attempts": 7.0, "Completions": 5.5,
    "Fantasy Points": 7.0, "Anytime TD": 0.45,
    "Kicking Points": 4.0, "Field Goals Made": 1.25,
    "Tackles + Assists": 3.0, "Sacks": 0.65,
}


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

# Keep only markets explicitly enabled above. All 17 supported single-game markets
# now remain available to the parser and projection engine.
NFL_PROP_ALIASES = {k: v for k, v in NFL_PROP_ALIASES.items() if k in ACTIVE_NFL_MARKETS}
PROP_CONFIG = {k: v for k, v in PROP_CONFIG.items() if k in ACTIVE_NFL_MARKETS}


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

# Accuracy V5 gates. These are deliberately bounded so current-week context and
# calibration improve a projection without overpowering the core player model.
NFL_SMART_CALIBRATION_MIN_EXACT = 12
NFL_SMART_CALIBRATION_MIN_ROLE = 18
NFL_SMART_CALIBRATION_MIN_POSITION = 24
NFL_SMART_CALIBRATION_MIN_PROP = 32
NFL_MARKET_STALE_HOURS = 6
NFL_MARKET_HARD_STALE_HOURS = 24
NFL_FINAL_INACTIVES_WINDOW_HOURS = 3.0
NFL_TEAM_RECONCILE_MIN_SCALE = 0.72

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

STADIUM_ENV.update({
    "ARI": {"stadium":"State Farm Stadium", "crowd":"MODERATE", "noise":1.006, "surface":"Grass", "roof":"Retractable", "altitude":1070},
    "BAL": {"stadium":"M&T Bank Stadium", "crowd":"LOUD", "noise":0.976, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "CAR": {"stadium":"Bank of America Stadium", "crowd":"MODERATE", "noise":0.992, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "CIN": {"stadium":"Paycor Stadium", "crowd":"MODERATE", "noise":0.986, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "CLE": {"stadium":"Huntington Bank Field", "crowd":"LOUD", "noise":0.974, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "HOU": {"stadium":"NRG Stadium", "crowd":"MODERATE", "noise":1.008, "surface":"Turf", "roof":"Retractable", "altitude":0},
    "IND": {"stadium":"Lucas Oil Stadium", "crowd":"MODERATE", "noise":1.012, "surface":"Turf", "roof":"Retractable", "altitude":0},
    "JAX": {"stadium":"EverBank Stadium", "crowd":"MODERATE", "noise":0.994, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "LAC": {"stadium":"SoFi Stadium", "crowd":"MODERATE", "noise":1.014, "surface":"Turf", "roof":"Canopy", "altitude":0},
    "LAR": {"stadium":"SoFi Stadium", "crowd":"MODERATE", "noise":1.014, "surface":"Turf", "roof":"Canopy", "altitude":0},
    "LV": {"stadium":"Allegiant Stadium", "crowd":"MODERATE", "noise":1.016, "surface":"Grass", "roof":"Dome", "altitude":2000},
    "MIA": {"stadium":"Hard Rock Stadium", "crowd":"MODERATE", "noise":0.998, "surface":"Grass", "roof":"Canopy", "altitude":0},
    "NE": {"stadium":"Gillette Stadium", "crowd":"LOUD", "noise":0.978, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "NYG": {"stadium":"MetLife Stadium", "crowd":"MODERATE", "noise":0.982, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "NYJ": {"stadium":"MetLife Stadium", "crowd":"MODERATE", "noise":0.982, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "PIT": {"stadium":"Acrisure Stadium", "crowd":"LOUD", "noise":0.972, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "SF": {"stadium":"Levi's Stadium", "crowd":"MODERATE", "noise":0.996, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "TB": {"stadium":"Raymond James Stadium", "crowd":"MODERATE", "noise":0.996, "surface":"Grass", "roof":"Outdoor", "altitude":0},
    "TEN": {"stadium":"Nissan Stadium", "crowd":"MODERATE", "noise":0.986, "surface":"Turf", "roof":"Outdoor", "altitude":0},
    "WAS": {"stadium":"Northwest Stadium", "crowd":"MODERATE", "noise":0.988, "surface":"Grass", "roof":"Outdoor", "altitude":0},
})

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
DEMO_BOARD = [r for r in DEMO_BOARD if r.get("prop") in ACTIVE_NFL_MARKETS]

# Veteran / elite QB stability layer for Passing Yards.
# This does not create fake yards. It only adjusts stability/volatility and lightly
# protects proven QBs from extreme game-script penalties.
ELITE_QB_TIERS = {
    "patrick mahomes": "ELITE_VETERAN", "josh allen": "ELITE_VETERAN", "joe burrow": "ELITE_VETERAN",
    "lamar jackson": "ELITE_VETERAN", "justin herbert": "ELITE_VETERAN", "dak prescott": "ELITE_VETERAN",
    "matthew stafford": "ELITE_VETERAN", "jalen hurts": "ELITE_VETERAN", "cj stroud": "GREAT_STABLE",
    "tua tagovailoa": "GREAT_STABLE", "jared goff": "GREAT_STABLE", "brock purdy": "GREAT_STABLE",
    "kirk cousins": "VETERAN_STABLE", "aaron rodgers": "VETERAN_STABLE", "geno smith": "VETERAN_STABLE",
    "jayden daniels": "YOUNG_UPSIDE", "caleb williams": "YOUNG_UPSIDE", "drake maye": "YOUNG_UPSIDE",
    "bo nix": "YOUNG_UPSIDE", "anthony richardson": "VOLATILE_UPSIDE",
}

def qb_tier_context(player, position=""):
    if str(position or "").upper() != "QB":
        return {"tier": "NON_QB", "factor": 1.0, "sigma_factor": 1.0, "confidence_boost": 0, "note": ""}
    tier = ELITE_QB_TIERS.get(norm(player), "STANDARD_STARTER")
    cfg = {
        "ELITE_VETERAN": (1.012, 0.92, 8, "Elite/veteran QB stability boost"),
        "GREAT_STABLE": (1.006, 0.95, 5, "Great/stable QB profile"),
        "VETERAN_STABLE": (1.000, 0.96, 4, "Veteran QB stability"),
        "YOUNG_UPSIDE": (1.004, 1.04, 2, "Young/upside QB volatility"),
        "VOLATILE_UPSIDE": (1.000, 1.10, -2, "Volatile QB profile"),
        "STANDARD_STARTER": (1.000, 1.00, 0, "Standard QB profile"),
    }.get(tier, (1.0, 1.0, 0, "Standard QB profile"))
    return {"tier": tier, "factor": cfg[0], "sigma_factor": cfg[1], "confidence_boost": cfg[2], "note": cfg[3]}


st.set_page_config(page_title="NFL Prop Engine", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
/* OneWayPickz NFL Premium Theme — MLB UI DNA, different colorway */
.stApp{background:radial-gradient(circle at top,#2b124c 0%,#090712 42%,#020204 100%);color:#fff;}
.block-container{padding-top:1.0rem;max-width:1600px;}
h1,h2,h3{color:#fff}.small-muted{color:#c7bddb;font-size:13px}.big-title{font-size:42px;font-weight:950;letter-spacing:-1px;color:#fff}.sub-title{color:#d9d0ea;margin-top:-8px}.hero-panel{background:linear-gradient(135deg,rgba(56,18,98,.92),rgba(7,7,14,.98));border:1px solid rgba(239,199,89,.45);border-radius:26px;padding:22px;box-shadow:0 0 34px rgba(166,91,255,.20);margin-bottom:18px}.pick-card{background:linear-gradient(145deg,#100b1f,#08070d);border:1px solid rgba(239,199,89,.34);border-radius:22px;padding:18px;box-shadow:0 0 26px rgba(166,91,255,.16);margin-bottom:14px}.green-card{background:linear-gradient(145deg,#002016,#06130d);border:1px solid rgba(0,255,150,.42);border-radius:22px;padding:18px}.warn-card{background:linear-gradient(145deg,#2c2105,#100c00);border:1px solid rgba(239,199,89,.55);border-radius:22px;padding:18px}.player-name{font-size:22px;font-weight:950}.big-number{font-size:42px;font-weight:950;line-height:1.05}.badge{display:inline-block;padding:5px 10px;border-radius:999px;background:#20143a;border:1px solid rgba(239,199,89,.45);color:#fff0bc;font-weight:800;margin:3px 4px 3px 0}.good-badge{background:#002916;border-color:rgba(0,255,135,.55);color:#b5ffd9}.yellow-badge{background:#2b1d00;border-color:rgba(255,210,70,.60);color:#ffe2a1}.red-badge{background:#2b0000;border-color:rgba(255,75,75,.55);color:#ffc0c0}.kpi-strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:12px 0 18px 0}.kpi-box{background:linear-gradient(145deg,#100b1f,#08070d);border:1px solid rgba(239,199,89,.28);border-radius:18px;padding:14px;min-height:92px}.kpi-label{font-size:12px;color:#c7bddb;font-weight:850;text-transform:uppercase;letter-spacing:.04em}.kpi-value{font-size:26px;font-weight:950;margin-top:5px;color:#fff}.kpi-sub{font-size:12px;color:#d9d0ea;margin-top:4px}.metric-card{background:rgba(255,255,255,.045);border:1px solid rgba(239,199,89,.18);border-radius:16px;padding:12px}.trust-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:12px 0}.trust-box{background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.10);border-radius:14px;padding:10px}.trust-label{font-size:11px;color:#c7bddb;text-transform:uppercase;font-weight:850;letter-spacing:.04em}.trust-value{font-size:18px;color:#fff;font-weight:950;margin-top:3px}.progress-wrap{width:100%;height:12px;border-radius:99px;background:#030206;overflow:hidden;border:1px solid rgba(255,255,255,.09)}.progress-green{height:100%;border-radius:99px;background:linear-gradient(90deg,#00d66b,#46ff9a)}.progress-orange{height:100%;border-radius:99px;background:linear-gradient(90deg,#c47dff,#efc759)}.progress-red{height:100%;border-radius:99px;background:linear-gradient(90deg,#ff2d2d,#ff7272)}.section-title-pro{margin-top:20px;margin-bottom:10px;font-size:24px;font-weight:950;border-left:5px solid #efc759;padding-left:12px}.stTabs [data-baseweb="tab"]{color:#c7bddb;font-weight:850}.stTabs [aria-selected="true"]{color:#efc759!important;border-bottom:3px solid #efc759}.click-more{border-top:1px solid rgba(255,255,255,.12);padding-top:8px;margin-top:8px}.hr-soft{border-top:1px solid rgba(255,255,255,.12);margin:14px 0}.mobile-decision-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:10px 0}.mobile-info-card{background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:12px;min-height:78px}.mini-k-bars{display:flex;align-items:flex-end;gap:10px;min-height:76px;margin-top:4px;overflow-x:auto}.mini-k-bar-wrap{display:inline-flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:18px}.mini-k-bar{display:block;width:17px;background:#efc759;border-radius:3px;box-shadow:0 0 10px rgba(239,199,89,.20)}.mini-k-label{font-size:12px;color:#c7bddb;margin-top:3px}@media(max-width:1100px){.kpi-strip{grid-template-columns:repeat(2,minmax(0,1fr));}.trust-strip{grid-template-columns:repeat(2,minmax(0,1fr));}}@media(max-width:900px){.big-title{font-size:28px}.big-number{font-size:30px}.player-name{font-size:20px}.pick-card{padding:14px;border-radius:18px}.mobile-decision-grid{grid-template-columns:repeat(2,minmax(0,1fr));}.mobile-info-card{min-height:72px;}}
</style>
""", unsafe_allow_html=True)


# Additional responsive/mobile layout rules.
st.markdown("""
<style>
.block-container{max-width:1600px;padding-top:1rem;padding-bottom:2rem}
[data-testid="stSidebar"]{min-width:320px;max-width:380px}
[data-testid="stSidebar"] .block-container{padding-top:.75rem}
div[data-testid="stDataFrame"]{border-radius:14px;overflow:hidden}
.stButton>button{min-height:44px;border-radius:12px;font-weight:850}
.nfl-page-note{font-size:12px;color:#c7bddb;margin:-4px 0 10px}
@media(max-width:760px){
  .block-container{padding-left:.75rem;padding-right:.75rem;padding-top:.55rem}
  [data-testid="stSidebar"]{min-width:88vw!important;max-width:88vw!important}
  .hero-panel{padding:14px;border-radius:18px;margin-bottom:10px}
  .big-title{font-size:27px!important;letter-spacing:-.5px}
  .sub-title{font-size:12px;line-height:1.35;margin-top:2px}
  .section-title-pro{font-size:21px;margin-top:14px}
  .pick-card{padding:12px!important;border-radius:16px!important;margin-bottom:10px}
  .player-name{font-size:19px!important;overflow-wrap:anywhere}
  .badge{font-size:11px;padding:4px 8px;white-space:normal;overflow-wrap:anywhere}
  .kpi-strip{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:7px!important}
  .kpi-box{padding:10px;min-height:72px;border-radius:13px}
  .kpi-value{font-size:21px}
  .metric-card{padding:9px;min-width:0;overflow-wrap:anywhere}
  .trust-strip{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:7px!important}
  .mobile-decision-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:7px!important}
  div[data-baseweb="select"]>div{min-height:46px}
  .stNumberInput input,.stTextInput input,.stTextArea textarea{font-size:16px!important}
}
@media(max-width:420px){
  .kpi-strip{grid-template-columns:repeat(2,minmax(0,1fr))!important}
  .trust-strip{grid-template-columns:repeat(2,minmax(0,1fr))!important}
  .kpi-label{font-size:10px}.kpi-value{font-size:19px}.kpi-sub{font-size:10px}
}
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
_JSON_RUNTIME_CACHE = {}
_CSV_RUNTIME_CACHE = {}
_RECORD_BANK_RUNTIME_CACHE = {}
_ROW_BANK_RUNTIME_CACHE = {}

def _path_signature(path):
    try:
        p = Path(path)
        stat = p.stat()
        return (str(p.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        return (str(Path(path)), 0, 0)

def load_json(path, default):
    """Load JSON once per file version during a Streamlit rerun."""
    sig = _path_signature(path)
    cached = _JSON_RUNTIME_CACHE.get(str(Path(path)))
    if cached and cached.get("sig") == sig:
        return cached.get("data")
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            _JSON_RUNTIME_CACHE[str(p)] = {"sig": sig, "data": data}
            return data
    except Exception:
        pass
    return default

def save_json(path, data):
    """Atomic JSON save and runtime-cache refresh."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(p)
        _JSON_RUNTIME_CACHE[str(p)] = {"sig": _path_signature(p), "data": data}
        return True
    except Exception:
        return False

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


def _meter_class(score):
    score = safe_float(score, 0) or 0
    if score >= 80:
        return "progress-green"
    if score >= 60:
        return "progress-orange"
    return "progress-red"

def _score_width(score):
    return int(clamp(safe_float(score, 0) or 0, 0, 100))

def _mini_recent_bars_from_player(p):
    """MLB-card inspired mini bars. Uses any recent/game-log arrays if the NFL file has them."""
    vals = []
    for key in ["recent_results", "last_5", "game_log_values", "last_games", "recent_yards"]:
        raw = p.get(key) if isinstance(p, dict) else None
        if isinstance(raw, list):
            for x in raw[-5:]:
                if isinstance(x, dict):
                    v = safe_float(x.get("value") or x.get("yards") or x.get("actual") or x.get("result"))
                else:
                    v = safe_float(x)
                if v is not None:
                    vals.append(v)
            if vals:
                break
    if not vals:
        return ""
    mx = max(max(vals), 1)
    pieces = []
    for v in vals[-5:]:
        h = int(clamp((v / mx) * 58, 8, 62))
        pieces.append(f"<div class='mini-k-bar-wrap'><span class='mini-k-bar' style='height:{h}px'></span><span class='mini-k-label'>{v:g}</span></div>")
    return "<div class='mini-k-bars'>" + "".join(pieces) + "</div>"

def _market_compare_text(p):
    line=safe_float((p or {}).get("line"))
    consensus=safe_float((p or {}).get("market_consensus_line"), safe_float((p or {}).get("market_consensus"), safe_float((p or {}).get("market_best_line"))))
    open_line=safe_float((p or {}).get("market_open_line"))
    best=safe_float((p or {}).get("market_best_line"))
    parts=[]
    if line is not None: parts.append(f"UD {line:g}")
    if consensus is not None: parts.append(f"Cons {consensus:g}")
    if best is not None: parts.append(f"Best {best:g}")
    if open_line is not None: parts.append(f"Open {open_line:g}")
    return " | ".join(parts)

def _recent_form_text(p):
    prop=(p or {}).get("prop")
    if prop == "Passing Yards":
        last3=safe_float((p or {}).get("last3_passing_yards_pg"))
        last5=safe_float((p or {}).get("last5_passing_yards_pg"))
        season=safe_float((p or {}).get("current_passing_yards_pg"), safe_float((p or {}).get("passing_yards_pg")))
        att=safe_float((p or {}).get("last3_pass_attempts_pg"), safe_float((p or {}).get("current_pass_attempts_pg")))
        bits=[]
        if last3: bits.append(f"L3 {last3:g}")
        if last5: bits.append(f"L5 {last5:g}")
        if season: bits.append(f"Base {season:g}")
        if att: bits.append(f"Att {att:g}")
        return " | ".join(bits)
    if prop == "Receiving Yards":
        last3=safe_float((p or {}).get("last3_receiving_yards_pg"))
        last5=safe_float((p or {}).get("last5_receiving_yards_pg"))
        season=safe_float((p or {}).get("current_receiving_yards_pg"), safe_float((p or {}).get("receiving_yards_pg")))
        targets=safe_float((p or {}).get("last3_targets_pg"), safe_float((p or {}).get("current_targets_pg"), safe_float((p or {}).get("targets_pg"))))
        bits=[]
        if last3: bits.append(f"L3 {last3:g}")
        if last5: bits.append(f"L5 {last5:g}")
        if season: bits.append(f"Base {season:g}")
        if targets: bits.append(f"Tgt {targets:g}")
        return " | ".join(bits)
    if prop == "Rushing Yards":
        last3=safe_float((p or {}).get("last3_rushing_yards_pg"))
        last5=safe_float((p or {}).get("last5_rushing_yards_pg"))
        season=safe_float((p or {}).get("current_rushing_yards_pg"), safe_float((p or {}).get("rushing_yards_pg")))
        carries=safe_float((p or {}).get("last3_rush_attempts_pg"), safe_float((p or {}).get("current_rush_attempts_pg"), safe_float((p or {}).get("rush_attempts_pg"))))
        bits=[]
        if last3: bits.append(f"L3 {last3:g}")
        if last5: bits.append(f"L5 {last5:g}")
        if season: bits.append(f"Base {season:g}")
        if carries: bits.append(f"Car {carries:g}")
        return " | ".join(bits)
    return ""

def _parse_any_datetime(value):
    if value in [None, ""]:
        return None
    if isinstance(value, datetime):
        return value
    txt=str(value).strip()
    for suffix in ["Z", "z"]:
        if txt.endswith(suffix):
            txt=txt[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(txt).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"]:
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            continue
    return None

def _hours_old(value):
    dt=_parse_any_datetime(value)
    if not dt:
        return None
    return max(0.0, (datetime.now() - dt).total_seconds()/3600.0)

def context_staleness(row):
    row=row or {}
    checks=[
        ("market", row.get("market_updated_at") or row.get("market_pulled_at"), 4),
        ("weather", row.get("weather_updated_at") or row.get("weather_pulled_at") or row.get("weather_game_time"), 12),
        ("injury", row.get("injury_updated_at") or row.get("practice_updated_at"), 24),
        ("depth", row.get("depth_updated_at") or row.get("role_updated_at"), 24),
        ("team", row.get("team_context_updated_at") or row.get("current_team_updated_at"), 48),
    ]
    stale=[]
    ages={}
    for label, value, max_hours in checks:
        age=_hours_old(value)
        if age is not None:
            ages[label]=round(age,2)
            if age > max_hours:
                stale.append(f"{label} context stale ({age:.1f}h)")
    return {"stale": stale, "ages": ages}

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

_TRACKING_RUNTIME = {"clv": None, "line": None, "clv_dirty": False, "line_dirty": False}

def update_clv_snapshot(player_name, prop, source, line):
    """Update CLV in memory; the board build flushes once after every row finishes."""
    if line is None: return 0.0
    if _TRACKING_RUNTIME["clv"] is None:
        loaded = load_json(CLV_FILE,{})
        _TRACKING_RUNTIME["clv"] = loaded if isinstance(loaded, dict) else {}
    data=_TRACKING_RUNTIME["clv"]
    today=datetime.now().strftime("%Y-%m-%d")
    key=f"{today}|{norm(player_name)}|{prop}|{source}"
    line=float(line)
    old=data.get(key)
    if not old:
        data[key]={"player":player_name,"prop":prop,"source":source,"open_line":line,"latest_line":line,"last_updated":now_iso()}
        _TRACKING_RUNTIME["clv_dirty"] = True
        return 0.0
    open_line=safe_float(old.get("open_line"), line) or line
    old["latest_line"]=line; old["last_updated"]=now_iso(); data[key]=old
    _TRACKING_RUNTIME["clv_dirty"] = True
    return round(line-open_line,2)

def track_line_delta(player_name, prop, source, line):
    """Update line history in memory; write the complete file once per board build."""
    if line is None: return 0.0
    if _TRACKING_RUNTIME["line"] is None:
        loaded = load_json(LINE_HISTORY_FILE,{})
        _TRACKING_RUNTIME["line"] = loaded if isinstance(loaded, dict) else {}
    hist=_TRACKING_RUNTIME["line"]
    key=f"{norm(player_name)}|{prop}|{source}"
    rows=hist.get(key,[])
    new_line=safe_float(line)
    # Do not append duplicate observations on ordinary Streamlit widget reruns.
    if not rows or safe_float(rows[-1].get("line")) != new_line:
        rows.append({"t":now_iso(),"line":new_line})
        hist[key]=rows[-40:]
        _TRACKING_RUNTIME["line_dirty"] = True
    if len(hist.get(key,[])) < 2: return 0.0
    first=safe_float(hist[key][0].get("line")); last=safe_float(hist[key][-1].get("line"))
    return None if first is None or last is None else round(last-first,2)

def flush_tracking_state():
    if _TRACKING_RUNTIME.get("clv_dirty") and isinstance(_TRACKING_RUNTIME.get("clv"), dict):
        save_json(CLV_FILE, _TRACKING_RUNTIME["clv"])
        _TRACKING_RUNTIME["clv_dirty"] = False
    if _TRACKING_RUNTIME.get("line_dirty") and isinstance(_TRACKING_RUNTIME.get("line"), dict):
        save_json(LINE_HISTORY_FILE, _TRACKING_RUNTIME["line"])
        _TRACKING_RUNTIME["line_dirty"] = False

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

def calibration_readiness(prop=None):
    results=load_json(RESULT_LOG,[])
    rows=[]
    for r in results:
        if prop and r.get("prop") != prop:
            continue
        if r.get("actual") is not None and r.get("projection") is not None:
            rows.append(r)
    n=len(rows)
    if n >= 75:
        label="TRAINED"
    elif n >= 25:
        label="USABLE"
    else:
        label="WARMING"
    return {"label":label, "graded_rows":n, "min_rows":25, "target_rows":75}

def projection_role_bucket(row, role=None):
    """Stable role bucket used by calibration and walk-forward testing."""
    row=row or {}; role=role or row.get("role") or {}
    pos=str(row.get("position") or "").upper().replace("WR/TE","REC")
    depth=safe_float(row.get("depth_rank"))
    starter=str(row.get("starter") or "").upper()
    target=safe_float(row.get("target_share"), safe_float(role.get("target"),0)) or 0
    carry=safe_float(row.get("carries_share"), safe_float(role.get("carry"),0)) or 0
    route=safe_float(row.get("route_participation"), safe_float(role.get("route"),0)) or 0
    rush_pg=safe_float(row.get("current_rush_attempts_pg"), safe_float(row.get("rush_attempts_pg"),0)) or 0
    status=str(row.get("qb_status") or row.get("role") or "").upper()
    if pos == "QB":
        if depth and depth > 1 or "BACKUP" in status or starter in ["NO","FALSE","0"]:
            return "QB_BACKUP"
        if rush_pg >= 5:
            return "QB_MOBILE_STARTER"
        return "QB_STARTER"
    if pos == "RB":
        if carry >= 55 or rush_pg >= 16:
            return "RB_WORKHORSE"
        if target >= 12:
            return "RB_RECEIVING"
        return "RB_COMMITTEE"
    if pos in ["WR","REC"]:
        if target >= 24 and route >= 80:
            return "WR_ALPHA"
        if target >= 16 or route >= 72:
            return "WR_STARTER"
        return "WR_ROTATION"
    if pos == "TE":
        if target >= 18 and route >= 72:
            return "TE_FEATURED"
        if route >= 60:
            return "TE_STARTER"
        return "TE_ROTATION"
    if pos in ["K","PK"]:
        return "KICKER"
    if pos in ["LB","DB","DL","EDGE","DE","S"]:
        return "DEFENDER"
    return pos or "UNKNOWN"

def projection_data_quality_bucket(row, usage_quality=None):
    score=safe_float((row or {}).get("data_score"), safe_float(usage_quality, safe_float((row or {}).get("usage_quality"),0))) or 0
    if score >= 90: return "90+"
    if score >= 82: return "82-89"
    if score >= 70: return "70-81"
    return "<70"

def _robust_calibration_bias(rows):
    vals=[]
    for r in rows:
        proj=safe_float(r.get("projection")); act=safe_float(r.get("actual"))
        if proj is not None and proj > 0 and act is not None:
            vals.append(float(clamp((act-proj)/max(1.0,proj), -0.35, 0.35)))
    if not vals:
        return 0.0
    arr=np.asarray(vals,dtype=float)
    if len(arr) >= 8:
        lo,hi=np.percentile(arr,[10,90]); arr=arr[(arr>=lo)&(arr<=hi)]
    return float(np.median(arr)*0.55 + np.mean(arr)*0.45)

def smart_calibration_scale(row, role=None, usage_quality=None):
    """Hierarchical calibration: prop + position + role + data quality.

    It only activates after enough graded rows and shrinks every correction toward
    neutral. The most-specific qualified bucket wins; no same-game/future result is used.
    """
    results=load_json(RESULT_LOG,[])
    clean=[r for r in results if r.get("actual") is not None and r.get("projection") is not None]
    prop=str((row or {}).get("prop") or "")
    pos=str((row or {}).get("position") or "").upper()
    role_bucket=projection_role_bucket(row, role)
    data_bucket=projection_data_quality_bucket(row, usage_quality)
    levels=[
        ("prop+position+role+quality", NFL_SMART_CALIBRATION_MIN_EXACT,
         lambda r: str(r.get("prop") or "")==prop and str(r.get("position") or "").upper()==pos and (r.get("role_bucket") or projection_role_bucket(r))==role_bucket and (r.get("data_quality_bucket") or projection_data_quality_bucket(r))==data_bucket),
        ("prop+position+role", NFL_SMART_CALIBRATION_MIN_ROLE,
         lambda r: str(r.get("prop") or "")==prop and str(r.get("position") or "").upper()==pos and (r.get("role_bucket") or projection_role_bucket(r))==role_bucket),
        ("prop+position", NFL_SMART_CALIBRATION_MIN_POSITION,
         lambda r: str(r.get("prop") or "")==prop and str(r.get("position") or "").upper()==pos),
        ("prop", NFL_SMART_CALIBRATION_MIN_PROP,
         lambda r: str(r.get("prop") or "")==prop),
    ]
    for label, minimum, matcher in levels:
        sample=[r for r in clean if matcher(r)][-120:]
        if len(sample) < minimum:
            continue
        bias=_robust_calibration_bias(sample)
        shrink=len(sample)/(len(sample)+30.0)
        shift=clamp(bias*0.42*shrink, -NFL_CALIBRATION_MAX_SHIFT_PCT, NFL_CALIBRATION_MAX_SHIFT_PCT)
        scale=1.0+shift
        return float(scale), f"Smart calibration {label} x{scale:.3f} from {len(sample)} prior rows", {
            "active":True,"level":label,"samples":len(sample),"bias":round(bias,4),
            "scale":round(scale,4),"role_bucket":role_bucket,"data_quality_bucket":data_bucket,
        }
    return 1.0, f"Smart calibration warming ({len(clean)} total graded rows)", {
        "active":False,"level":"WARMING","samples":len(clean),"scale":1.0,
        "role_bucket":role_bucket,"data_quality_bucket":data_bucket,
    }

def projection_stability_score(p10, p90, mean, prop):
    width=(safe_float(p90,0) or 0) - (safe_float(p10,0) or 0)
    base_sigma=PROP_CONFIG.get(prop,{}).get("sigma", max(1, safe_float(mean,1) or 1))
    ratio=width/max(1,base_sigma*2.56)
    score=100 - max(0,(ratio-1.0)*38)
    return int(clamp(score,0,100))

def _hours_to_kickoff(row):
    row=row or {}
    for key in ["starts_at","start_time","event_time","scheduled_at","game_time","game_date"]:
        dt=_parse_any_datetime(row.get(key))
        if dt:
            return (dt-datetime.now()).total_seconds()/3600.0
    return None

def official_inactives_safety_gate(row, prop=None):
    """Fail-safe status gate for player, QB, role and game-day inactive uncertainty."""
    row=row or {}; prop=prop or row.get("prop")
    hard=[]; review=[]
    player_status=" ".join(str(row.get(k) or "") for k in ["final_inactive_status","inactive_status","injury_status","manual_override_status"]).upper()
    practice=str(row.get("practice_status") or "").upper()
    if any(x in player_status for x in ["INACTIVE"," OUT","OUT ","IR","PUP","SUSPENDED","NFI"]):
        hard.append("Official/player status: inactive or unavailable")
    elif "DOUBTFUL" in player_status:
        hard.append("Player listed doubtful")
    elif any(x in player_status for x in ["QUESTIONABLE","LIMITED"]):
        review.append("Questionable/limited player role")
    if any(x in practice for x in ["DNP","NO PRACTICE"]):
        review.append("No-practice status requires role confirmation")

    limited=safe_float(row.get("limited_snap_risk"),0) or 0
    expected_snap=safe_float(row.get("expected_snap_share"))
    if limited >= 0.45:
        hard.append("Limited-snap probability too high")
    elif limited >= 0.25:
        review.append("Meaningful limited-snap risk")
    if expected_snap is not None and expected_snap < 35 and prop not in ["Kicking Points","Field Goals Made"]:
        hard.append("Expected snap share too low")

    hours=_hours_to_kickoff(row)
    confirmed=row.get("final_inactives_confirmed")
    confirmed_bool=str(confirmed).upper() in ["TRUE","YES","1"]
    unconfirmed=str(confirmed).upper() in ["FALSE","NO","0"]
    require=bool(row.get("require_final_inactives")) or (hours is not None and -1 <= hours <= NFL_FINAL_INACTIVES_WINDOW_HOURS)
    if require and not confirmed_bool and row.get("source") != "DEMO":
        hard.append("Final inactive list not confirmed near kickoff")
    elif unconfirmed and hours is not None and hours <= 12:
        review.append("Final inactive confirmation pending")

    qb_status=" ".join(str(row.get(k) or "") for k in ["qb_status","qb_injury_status","qb_change_risk","manual_qb_status"]).upper()
    qb_sensitive=prop in ["Passing Yards","Passing TDs","Pass Attempts","Completions","Interceptions","Receiving Yards","Receptions","Longest Reception","Fantasy Points"]
    if qb_sensitive and any(x in qb_status for x in ["OUT","DOUBTFUL","INACTIVE"]):
        hard.append("Starting-QB availability blocks projection")
    elif qb_sensitive and any(x in qb_status for x in ["BACKUP","CHANGE","UNCERTAIN","HIGH"]):
        review.append("Starting-QB change/uncertainty")

    ol_out=max(safe_float(row.get("starting_ol_out"),0) or 0, safe_float(row.get("ol_starters_out"),0) or 0)
    if prop in ["Passing Yards","Pass Attempts","Completions","Rushing Yards","Rush Attempts"]:
        if ol_out >= 3: hard.append("Three or more starting offensive linemen out")
        elif ol_out >= 2: review.append("Multiple starting offensive linemen out")
    return {"status":"BLOCK" if hard else "REVIEW" if review else "CLEAR", "hard_blocks":hard, "review_blocks":review, "hours_to_kickoff":None if hours is None else round(hours,2), "confirmed":confirmed_bool}

def market_intelligence_engine(row, projection=None, line=None):
    """Opening/current/consensus movement and freshness layer."""
    row=row or {}; prop=str(row.get("prop") or "")
    line=safe_float(line, safe_float(row.get("line")))
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    open_line=safe_float(row.get("market_open_line"), safe_float(row.get("open_line")))
    best=safe_float(row.get("market_best_line"), safe_float(row.get("best_line")))
    move=safe_float(row.get("market_line_move"))
    if move is None and line is not None and open_line is not None:
        move=line-open_line
    books_raw=row.get("market_market_books", row.get("market_books", row.get("books")))
    if isinstance(books_raw,(list,tuple,set)): books=len(books_raw)
    elif safe_float(books_raw) is not None: books=int(safe_float(books_raw))
    elif isinstance(books_raw,str) and books_raw.strip(): books=len([x for x in re.split(r"[,|;/]",books_raw) if x.strip()])
    else: books=0
    updated=row.get("market_updated_at") or row.get("market_pulled_at") or row.get("market_time")
    age=_hours_old(updated)
    stale=age is not None and age > NFL_MARKET_STALE_HOURS
    hard_stale=age is not None and age > NFL_MARKET_HARD_STALE_HOURS
    threshold={"Passing Yards":18,"Receiving Yards":10,"Rushing Yards":10,"Receptions":1.4,"Pass Attempts":4.5,"Completions":3.5,"Rush Attempts":2.5,"Passing TDs":0.5,"Interceptions":0.4,"Anytime TD":0.2}.get(prop,8)
    disagreement=abs(line-consensus) if line is not None and consensus is not None else None
    sudden=abs(move) >= threshold*0.50 if move is not None else False
    against=False
    if projection is not None and line is not None and move is not None and abs(move)>0:
        model_dir=1 if projection>line else -1
        move_dir=1 if move>0 else -1
        against=model_dir != move_dir
    anchor_weight=0.0
    if consensus is not None and not stale:
        anchor_weight=0.20 if books >= 3 else 0.12 if books >= 2 else 0.06
    confidence_delta=0; notes=[]; blocks=[]
    if books >= 3 and not stale: confidence_delta+=2; notes.append(f"Fresh {books}-book consensus")
    elif books == 1: confidence_delta-=2; notes.append("Single-book market context")
    if stale: confidence_delta-=5; notes.append(f"Market context stale ({age:.1f}h)")
    if hard_stale: blocks.append("Market consensus older than 24 hours")
    if sudden:
        notes.append(f"Sudden line move {move:+.2f}")
        confidence_delta += -6 if against else 2
    if disagreement is not None and disagreement >= threshold:
        blocks.append("Underdog line materially disagrees with consensus")
    return {"status":"STALE" if stale else "FRESH" if consensus is not None else "NO_CONSENSUS", "consensus":consensus,"open_line":open_line,"best_line":best,"current_line":line,"line_move":move,"books":books,"age_hours":None if age is None else round(age,2),"stale":stale,"sudden_move":sudden,"move_against_model":against,"disagreement":None if disagreement is None else round(disagreement,3),"anchor_weight":anchor_weight,"confidence_delta":confidence_delta,"notes":notes,"blocks":blocks}

def projection_audit(row):
    """Compact context audit: Fresh / Partial / Stale plus hard-block flags."""
    row=row or {}
    layers={
        "current_player": bool(row.get("has_current_usage") or row.get("current_context_source")),
        "current_team": bool(row.get("has_current_team_context")),
        "injury": row.get("injury_status") not in [None, ""],
        "depth": bool(row.get("has_depth_chart_context")),
        "weather": safe_float(row.get("weather_pass_factor")) is not None or row.get("weather_risk") not in [None, ""],
        "market": bool(row.get("has_market_context")),
        "travel": bool(row.get("has_travel_context")),
        "matchup": bool(row.get("has_matchup_context")),
        "qb": bool(row.get("has_qb_context")) or row.get("qb_status") not in [None, ""],
        "def_inj": bool(row.get("has_defensive_injury_context")),
        "splits": bool(row.get("has_splits_context")),
        "personnel": bool(row.get("has_personnel_context")),
        "final_inactives": bool(row.get("has_final_inactives_context") or row.get("final_inactives_confirmed") is not None),
        "manual_override": bool(row.get("has_manual_override_context")),
        "phase6": row.get("model_match_status") == "MATCHED" or row.get("model_player_match") not in [None, ""],
    }
    score=sum(1 for v in layers.values() if v)
    label="Fresh" if score >= 5 and (layers["current_player"] or layers["phase6"]) and layers["market"] else "Partial" if score >= 3 else "Stale"
    blocks=[]
    injury=str(row.get("injury_status") or "").upper()
    practice=str(row.get("practice_status") or "").upper()
    if any(x in injury for x in ["OUT","DOUBTFUL","IR","PUP"]):
        blocks.append("Player injury status blocks play")
    if any(x in practice for x in ["DNP","NO PRACTICE"]):
        blocks.append("No-practice injury risk")
    final_status=str(row.get("final_inactive_status") or row.get("inactive_status") or "").upper()
    if any(x in final_status for x in ["OUT","INACTIVE","IR","PUP"]):
        blocks.append("Final inactive list blocks play")
    final_confirmed=row.get("final_inactives_confirmed")
    # The dedicated game-day safety gate below only blocks unconfirmed final
    # inactives near kickoff. Early-week projections remain reviewable.
    override_status=str(row.get("manual_override_status") or row.get("manual_news_status") or "").upper()
    if any(x in override_status for x in ["OUT","INACTIVE","NO PLAY","SCRATCH"]):
        blocks.append("Manual news override blocks play")
    if any(x in override_status for x in ["LIMIT","SNAP","WORKLOAD"]) and safe_float(row.get("manual_override_confidence"), 1.0) >= 0.65:
        blocks.append("Manual workload override requires review")
    if safe_float(row.get("limited_snap_risk"), 0) and safe_float(row.get("limited_snap_risk"), 0) >= 0.45:
        blocks.append("Limited snap-risk too high")
    if str(row.get("weather_risk") or "").upper() in ["SEVERE","WIND"] and row.get("prop") in ["Passing Yards","Receiving Yards"]:
        blocks.append("Severe passing weather")
    travel_diff=travel_difficulty_score(row)
    if travel_diff.get("label") == "HIGH" and label == "Stale":
        blocks.append("High travel difficulty with stale context")
    if str(row.get("qb_status") or row.get("qb_injury_status") or "").upper() in ["OUT","DOUBTFUL","BACKUP"] and row.get("prop") == "Receiving Yards":
        blocks.append("Receiver tied to backup/out QB")
    stale=context_staleness(row)
    blocks.extend(stale.get("stale") or [])
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    line=safe_float(row.get("line"))
    if consensus is not None and line is not None:
        threshold={
            "Passing Yards": 18,
            "Receiving Yards": 10,
            "Rushing Yards": 10,
            "Receptions": 1.4,
            "Pass Attempts": 4.5,
            "Completions": 3.5,
            "Rush Attempts": 2.5,
        }.get(row.get("prop"), 10)
        if abs(line-consensus) >= threshold:
            blocks.append("Line too far from market consensus")
        elif abs(line-consensus) >= threshold*0.60:
            support_layers=sum(1 for k in ["injury","depth","weather","market","travel","matchup","qb"] if layers.get(k))
            if support_layers < 4:
                blocks.append("Market disagreement lacks supporting context")
    if label == "Stale" and row.get("source") != "DEMO":
        blocks.append("Projection context stale")
    safety=official_inactives_safety_gate(row, row.get("prop"))
    blocks.extend(safety.get("hard_blocks") or [])
    blocks.extend(safety.get("review_blocks") or [])
    market_intel=market_intelligence_engine(row, line=line)
    blocks.extend(market_intel.get("blocks") or [])
    # Preserve order while removing duplicate wording from overlapping safety checks.
    blocks=list(dict.fromkeys(str(x) for x in blocks if x))
    return {"label":label, "score":score, "max_score":len(layers), "layers":layers, "hard_blocks":blocks, "inactives_gate":safety, "market_intelligence":market_intel}

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
    cal=p.get("calibration_status") or calibration_readiness(prop)
    if cal.get("label") == "WARMING" and prob < 0.66:
        reasons.append(f"Calibration sample warming up ({cal.get('graded_rows',0)}/{cal.get('min_rows',25)})")
    audit=p.get("projection_audit") or {}
    reasons.extend(audit.get("hard_blocks") or [])
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

# v10.9 live-board safety: only accept realistic single-game NFL prop lines.
# This blocks season-long totals like 3249.5 passing yards from being treated as a game prop line.
MARKET_LINE_RANGES = {
    "Passing Yards": (70.0, 430.0),
    "Passing TDs": (0.5, 5.5),
    "Interceptions": (0.5, 3.5),
    "Pass Attempts": (8.5, 58.5),
    "Completions": (4.5, 42.5),
    "Rushing Yards": (2.5, 175.0),
    "Rush Attempts": (0.5, 35.5),
    "Receiving Yards": (2.5, 190.0),
    "Receptions": (0.5, 14.5),
    "Fantasy Points": (1.0, 55.0),
    "Anytime TD": (0.05, 2.5),
    "Longest Reception": (3.5, 95.5),
    "Longest Rush": (2.5, 80.5),
    "Kicking Points": (0.5, 22.5),
    "Field Goals Made": (0.5, 5.5),
    "Tackles + Assists": (0.5, 18.5),
    "Sacks": (0.5, 3.5),
}

def _valid_market_line(prop, line):
    v = safe_float(line)
    if v is None:
        return False
    lo, hi = MARKET_LINE_RANGES.get(str(prop or ""), (0.01, 999.0))
    return lo <= float(v) <= hi

def _extract_line_value_for_prop(prop, *objs):
    """Extract the actual over/under line for a prop, not season/player stat totals.

    Underdog JSON can include both the betting line and related player stat values
    inside linked objects. For Passing Yards, values like 3249.5 are season totals
    and must be rejected; a valid game prop line should be around 70-430 yards.
    """
    preferred_keys = ["stat_value", "line", "over_under", "threshold", "target"]
    nested_paths = [
        ["over_under", "stat_value"], ["over_under", "line"], ["over_under", "value"],
        ["over_under_line", "stat_value"], ["over_under_line", "line"],
        ["option", "line"], ["projection", "line"],
    ]
    candidates = []
    for obj_index, o in enumerate(objs):
        if not isinstance(o, dict):
            continue
        typ = str(o.get("type") or "").lower()
        # Do not take appearance/player stat_value first; those are often season/stat totals.
        type_penalty = 10 if ("appearance_stat" in typ or "appearance-stat" in typ or typ == "stat") else 0
        for k in preferred_keys:
            if k in o and not isinstance(o.get(k), dict):
                fv = safe_float(o.get(k))
                if fv is not None:
                    candidates.append((type_penalty + obj_index, k, fv))
        for path in nested_paths:
            fv = safe_float(_deep_get(o, path))
            if fv is not None:
                candidates.append((type_penalty + obj_index, "/".join(path), fv))
    # Prefer valid market-range candidates.
    valid = [(rank, key, val) for rank, key, val in candidates if _valid_market_line(prop, val)]
    if valid:
        valid.sort(key=lambda x: (x[0], 0 if "stat_value" in x[1] or "line" in x[1] else 1))
        return float(valid[0][2])
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


def _normalize_nfl_team(value):
    raw = str(value or "").strip().upper().replace(".", "")
    if not raw or raw in {"NFL", "FOOTBALL", "UNKNOWN", "N/A", "NA"}:
        return ""
    raw = NFL_TEAM_ALIASES.get(raw, raw)
    if raw in NFL_TEAM_ABBRS:
        return raw
    return NFL_TEAM_NAME_ALIASES.get(raw, "")

def _looks_like_timestamp(value):
    s = str(value or "").strip()
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s))

def _teams_from_matchup_text(value):
    s = str(value or "").strip().upper()
    if not s or _looks_like_timestamp(s):
        return "", ""
    # Exact full-name aliases first.
    for full, abbr in NFL_TEAM_NAME_ALIASES.items():
        s = s.replace(full, abbr)
    tokens = re.findall(r"[A-Z]{2,3}", s)
    teams = []
    for token in tokens:
        team = _normalize_nfl_team(token)
        if team and team not in teams:
            teams.append(team)
    return (teams[0], teams[1]) if len(teams) >= 2 else ("", "")

def _canonical_matchup(value="", team="", opp="", home_away=""):
    first, second = _teams_from_matchup_text(value)
    if first and second and first != second:
        # Preserve @ orientation when the feed supplies it.
        sep = " @ " if "@" in str(value) else " vs "
        return f"{first}{sep}{second}"
    team = _normalize_nfl_team(team)
    opp = _normalize_nfl_team(opp)
    if team and opp and team != opp:
        ha = str(home_away or "").strip().upper()
        if ha in {"HOME", "H"}:
            return f"{opp} @ {team}"
        if ha in {"AWAY", "A"}:
            return f"{team} @ {opp}"
        return f"{team} vs {opp}"
    return ""

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
    # scheduled_at is deliberately excluded: it is a timestamp, not a matchup.
    for k in ["matchup", "game", "event_title", "display_title", "title"]:
        v = o.get(k)
        if isinstance(v, str) and len(v) <= 120:
            cleaned = _canonical_matchup(v)
            if cleaned:
                return cleaned
    home = _normalize_nfl_team(_deep_get(o, ["game", "home_team"]) or o.get("home_team") or o.get("home_team_abbr"))
    away = _normalize_nfl_team(_deep_get(o, ["game", "away_team"]) or o.get("away_team") or o.get("away_team_abbr"))
    if away and home and away != home:
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

# ---------- Underdog JSON:API / app-board parser v10.2 ----------
def _as_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return list(x.values()) if all(isinstance(v, dict) for v in x.values()) else [x]
    return []

def _rel_ids(o, *names):
    ids=[]
    if not isinstance(o, dict):
        return ids
    rel=o.get("relationships") or {}
    for name in names:
        # direct fields first
        for k in [name, f"{name}_id"]:
            v=o.get(k)
            if isinstance(v, (str,int)):
                ids.append(str(v))
            elif isinstance(v, dict):
                vv=v.get("id") or _deep_get(v,["data","id"])
                if vv is not None: ids.append(str(vv))
        r=rel.get(name) or rel.get(name.replace("_","-"))
        d=r.get("data") if isinstance(r, dict) else r
        if isinstance(d, list):
            for item in d:
                if isinstance(item, dict) and item.get("id") is not None:
                    ids.append(str(item.get("id")))
        elif isinstance(d, dict) and d.get("id") is not None:
            ids.append(str(d.get("id")))
    return [x for i,x in enumerate(ids) if x and x not in ids[:i]]

def _entity_maps_from_underdog(data):
    objects=flatten(data)
    by_id={}
    by_type={}
    for o in objects:
        if not isinstance(o, dict):
            continue
        oid=o.get("id")
        typ=str(o.get("type") or o.get("kind") or o.get("object") or "").lower()
        if oid is not None:
            by_id[str(oid)] = o
            if typ:
                by_type.setdefault(typ, {})[str(oid)] = o
    return objects, by_id, by_type

def _get_related(o, by_id, by_type, *names):
    for rid in _rel_ids(o, *names):
        if rid in by_id:
            return by_id[rid]
        for mp in by_type.values():
            if rid in mp:
                return mp[rid]
    return {}

def _pick_first_string(objs, keys):
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for k in keys:
            v=obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

def _underdog_player_meta(o):
    if not isinstance(o, dict):
        return {}
    first=o.get("first_name") or _deep_get(o,["player","first_name"]) or ""
    last=o.get("last_name") or _deep_get(o,["player","last_name"]) or ""
    full=o.get("player_name") or o.get("display_name") or o.get("full_name") or o.get("name") or o.get("title") or ""
    if first and last:
        full=f"{first} {last}"
    return {
        "player": str(full).strip(),
        "team": o.get("team_abbr") or o.get("team") or o.get("team_code") or _deep_get(o,["team","abbr"]) or _deep_get(o,["team","abbreviation"]) or "",
        "position": o.get("position") or o.get("position_abbr") or o.get("pos") or _deep_get(o,["player","position"]) or "",
    }

def _build_player_bank_v2(objects):
    bank={}
    for o in objects:
        if not isinstance(o, dict):
            continue
        typ=str(o.get("type") or "").lower()
        blob=_blob(o)
        if "player" not in typ and not any(k in o for k in ["first_name","last_name","player_name","display_name","full_name"]):
            continue
        meta=_underdog_player_meta(o)
        full=meta.get("player")
        oid=o.get("id") or o.get("player_id") or o.get("appearance_id")
        if full and oid:
            bank[str(oid)] = meta
    return bank

def _expand_initial_player_name(short_name, player_bank):
    short=str(short_name or "").strip()
    if not short:
        return short
    # J. Goff -> Jared Goff if player exists in bank
    parts=short.replace(".", ". ").split()
    if len(parts) >= 2 and parts[0].endswith("."):
        init=parts[0][0].lower()
        last=parts[-1].lower()
        matches=[]
        for meta in player_bank.values():
            full=str(meta.get("player") or "")
            fp=full.split()
            if len(fp)>=2 and fp[0].lower().startswith(init) and fp[-1].lower()==last:
                matches.append(full)
        if len(matches)==1:
            return matches[0]
    return short

def _player_from_title(title, prop, player_bank):
    title=str(title or "").strip()
    if not title:
        return ""
    low=title.lower()
    cut=len(title)
    aliases=NFL_PROP_ALIASES.get(prop, []) if prop else []
    for a in aliases:
        idx=low.find(str(a).lower())
        if idx > 0:
            cut=min(cut, idx)
    candidate=title[:cut].strip(" -–—•|:")
    if not candidate or len(candidate.split()) < 2:
        # Last fallback: remove common prop words from the right side.
        candidate=title
        for words in NFL_PROP_ALIASES.values():
            for w in sorted(words, key=len, reverse=True):
                candidate=candidate.replace(w, "").replace(w.title(), "")
        candidate=" ".join(candidate.split()).strip(" -–—•|:")
    return _expand_initial_player_name(candidate, player_bank)

def _extract_line_value_v2(prop, *objs):
    return _extract_line_value_for_prop(prop, *objs)

def _prop_from_objs(*objs):
    # Prefer explicit stat/display labels over whole blob so TD promos do not hijack markets.
    keys=["stat", "stat_type", "stat_type_name", "display_stat", "appearance_stat", "label", "title", "name", "description"]
    for o in objs:
        if not isinstance(o, dict):
            continue
        text=" ".join([str(o.get(k) or "") for k in keys])
        prop=prop_name_from_blob(text)
        if prop:
            return prop
    return prop_name_from_blob(" ".join(_blob(o) for o in objs if isinstance(o, dict)))

def _is_probably_nfl_underdog(*objs):
    b=" ".join(_blob(o) for o in objs if isinstance(o, dict))
    if any(x in b for x in ["nfl", "football", "national football", "nfl_player"]):
        return True
    # If the market is football-specific and the related player has football-ish position/team, accept it.
    if prop_name_from_blob(b):
        for o in objs:
            pos=str((o or {}).get("position") or (o or {}).get("pos") or "").upper() if isinstance(o, dict) else ""
            if pos in ["QB","RB","WR","TE","K"]:
                return True
        return True
    return False

def _extract_matchup_v2(*objs):
    for o in objs:
        m = _extract_matchup(o) if isinstance(o, dict) else ""
        if m:
            return m
    home = _normalize_nfl_team(_pick_first_string(objs,["home_team","home_team_abbr","home_team_code"]))
    away = _normalize_nfl_team(_pick_first_string(objs,["away_team","away_team_abbr","away_team_code"]))
    if away and home and away != home:
        return f"{away} @ {home}"
    return ""

def _extract_underdog_jsonapi_rows(data, source_url):
    objects, by_id, by_type = _entity_maps_from_underdog(data)
    player_bank = _build_player_bank_v2(objects)

    # Candidate line objects are betting-line objects only.
    # Do NOT treat appearance_stat/player stat rows as line candidates; those can hold
    # season totals such as 3249.5 passing yards.
    candidates=[]
    for o in objects:
        if not isinstance(o, dict):
            continue
        typ=str(o.get("type") or "").lower()
        if "appearance_stat" in typ or "appearance-stat" in typ:
            continue
        has_line=_extract_line_value(o) is not None
        has_ou=bool(_rel_ids(o,"over_under","over-under","overUnder")) or bool(o.get("over_under_id"))
        is_line_type=("over_under_line" in typ or "over-under-line" in typ or "overunderline" in typ)
        if has_line and (is_line_type or has_ou):
            candidates.append(o)

    rows=[]
    for line_obj in candidates:
        ou=_get_related(line_obj, by_id, by_type, "over_under", "over-under", "overUnder")
        app_stat=_get_related(ou, by_id, by_type, "appearance_stat", "appearance-stat", "appearanceStat") or _get_related(line_obj, by_id, by_type, "appearance_stat", "appearance-stat", "appearanceStat")
        appearance=_get_related(app_stat, by_id, by_type, "appearance") or _get_related(ou, by_id, by_type, "appearance") or _get_related(line_obj, by_id, by_type, "appearance")
        player=_get_related(appearance, by_id, by_type, "player") or _get_related(app_stat, by_id, by_type, "player") or _get_related(ou, by_id, by_type, "player")
        game=_get_related(appearance, by_id, by_type, "game", "match", "event") or _get_related(ou, by_id, by_type, "game", "match", "event")
        combo=[line_obj, ou, app_stat, appearance, player, game]
        prop=_prop_from_objs(*combo)
        if not prop or prop not in PROP_CONFIG:
            continue
        line=_extract_line_value_v2(prop, line_obj, ou, app_stat)
        if line is None or not _valid_market_line(prop, line):
            continue
        if not _is_probably_nfl_underdog(*combo):
            continue

        pmeta=_underdog_player_meta(player)
        player_name=pmeta.get("player")
        if not player_name:
            title=_pick_first_string(combo,["title", "display_title", "label", "name", "description"])
            player_name=_player_from_title(title, prop, player_bank)
        if not player_name or player_name.lower() in ["unknown player", "over", "under"]:
            continue

        team=pmeta.get("team") or _pick_first_string(combo,["team_abbr", "team", "team_code", "abbr"]) or "NFL"
        position=pmeta.get("position") or _pick_first_string(combo,["position", "position_abbr", "pos"])
        matchup=_extract_matchup_v2(*combo)
        rows.append({
            "player": str(player_name),
            "team": team or "NFL",
            "opp": _pick_first_string(combo,["opponent", "opp", "opponent_team"]),
            "home_away": _pick_first_string(combo,["home_away", "home_or_away"]),
            "position": position or "",
            "prop": prop,
            "line": line,
            "price": _extract_price(line_obj) or _extract_price(ou),
            "source":"Underdog",
            "source_url":source_url,
            "matchup":matchup,
            "underdog_id":str(line_obj.get("id") or ou.get("id") or ""),
        })
    return rows, {"objects": len(objects), "line_candidates": len(candidates), "player_bank": len(player_bank)}

@st.cache_data(ttl=120, show_spinner=False)
def fetch_underdog_nfl_props(cache_bust=0):
    """Pull live Underdog NFL props when available.

    v10.2: Uses both the old flat parser and a JSON:API relationship parser.
    This matters because current Underdog endpoints can return 20k+ objects where
    the line, player, market name, and game are split across related objects.
    """
    rows=[]
    endpoint_debug=[]
    for url in UNDERDOG_URLS:
        data=safe_get_json(url)
        if not data:
            endpoint_debug.append({"url":url,"status":"NO_DATA","rows":0})
            continue

        # New relationship-aware parser first.
        rel_rows=[]; rel_diag={}
        try:
            rel_rows, rel_diag = _extract_underdog_jsonapi_rows(data, url)
        except Exception as e:
            rel_diag={"relationship_parser_error": str(e)[:220]}
            rel_rows=[]
        rows.extend(rel_rows)

        # Old flat fallback catches endpoints where line/player/prop are nested together.
        objects=flatten(data)
        player_bank=_collect_player_bank(objects)
        flat_rows=[]
        for o in objects:
            if not isinstance(o, dict):
                continue
            blob=_blob(o)
            if not looks_nfl(o):
                continue
            prop=prop_name_from_blob(blob)
            if not prop or prop not in PROP_CONFIG:
                continue
            line=_extract_line_value_for_prop(prop, o)
            if line is None or not _valid_market_line(prop, line):
                continue
            player=_extract_player_from_obj(o, player_bank)
            if not player or player.lower() in ["unknown player", "over", "under"]:
                continue
            team, position = _extract_team_pos(o, player, player_bank)
            flat_rows.append({
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
        rows.extend(flat_rows)

        url_rows=len(rel_rows)+len(flat_rows)
        endpoint_debug.append({"url":url,"status":"OK","rows":url_rows,"relationship_rows":len(rel_rows),"flat_rows":len(flat_rows),**rel_diag})
        if url_rows > 0:
            break

    # Dedupe: keep first/newest endpoint version.
    seen=set(); clean=[]
    for r in rows:
        key=(norm(r.get("player")),r.get("prop"),safe_float(r.get("line")),r.get("matchup",""),str(r.get("underdog_id","")))
        key2=(norm(r.get("player")),r.get("prop"),safe_float(r.get("line")),r.get("matchup",""))
        if key not in seen and key2 not in seen:
            seen.add(key); seen.add(key2); clean.append(r)
    clean = _filter_live_board_to_phase6_model(clean)
    request_log("UNDERDOG_NFL_LIVE_PULL", "FOUND" if clean else "NO_NFL_ROWS", endpoint_debug)
    return clean[:1000]

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
    """Load the saved Underdog moneyline board cache safely.

    This prevents startup crashes when the moneyline cache has not been created yet
    or was cleared. It mirrors load_last_pulled_board().
    """
    data = load_json(MONEYLINE_CACHE_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return data
    return {"pulled_at": None, "source": "NONE", "row_count": 0, "rows": []}


def _canon_prop_label(value):
    """Normalize Underdog/manual market labels into the app's canonical prop names."""
    raw = str(value or "").strip()
    if not raw:
        return None
    b = norm(raw)
    # Highest-confidence direct mappings first. Underdog uses labels like Pass Yards,
    # Rush + Rec TDs, Rec Yards, INT, etc. These must match the app markets exactly.
    manual_map = {
        "pass yards": "Passing Yards", "passing yards": "Passing Yards", "pass yds": "Passing Yards", "qb pass yards": "Passing Yards",
        "pass tds": "Passing TDs", "passing tds": "Passing TDs", "pass td": "Passing TDs", "passing touchdowns": "Passing TDs",
        "interceptions": "Interceptions", "interception": "Interceptions", "ints": "Interceptions", "int": "Interceptions",
        "pass attempts": "Pass Attempts", "passing attempts": "Pass Attempts", "attempts": "Pass Attempts",
        "completions": "Completions", "passing completions": "Completions",
        "rush yards": "Rushing Yards", "rushing yards": "Rushing Yards", "rush yds": "Rushing Yards",
        "rush attempts": "Rush Attempts", "rushing attempts": "Rush Attempts", "carries": "Rush Attempts",
        "rec yards": "Receiving Yards", "receiving yards": "Receiving Yards", "receiving yds": "Receiving Yards",
        "receptions": "Receptions", "rec": "Receptions", "catches": "Receptions",
        "longest reception": "Longest Reception", "long reception": "Longest Reception",
        "longest rush": "Longest Rush", "long rush": "Longest Rush",
        "rush rec tds": "Anytime TD", "rush receiving tds": "Anytime TD", "rush rec touchdowns": "Anytime TD",
        "rush + rec tds": "Anytime TD", "rush + receiving tds": "Anytime TD", "anytime td": "Anytime TD",
        "fantasy points": "Fantasy Points", "fantasy score": "Fantasy Points",
    }
    if b in manual_map:
        return manual_map[b]
    # Some labels include extra symbols/words.
    if "rush" in b and "rec" in b and ("td" in b or "touchdown" in b):
        return "Anytime TD"
    if "pass" in b and "yard" in b:
        return "Passing Yards"
    if "pass" in b and ("td" in b or "touchdown" in b):
        return "Passing TDs"
    if "interception" in b or b == "int" or b == "ints":
        return "Interceptions"
    if "completion" in b:
        return "Completions"
    if "attempt" in b and "pass" in b:
        return "Pass Attempts"
    if "attempt" in b and ("rush" in b or "rushing" in b):
        return "Rush Attempts"
    if "rush" in b and "yard" in b:
        return "Rushing Yards"
    if ("rec" in b or "receiving" in b) and "yard" in b:
        return "Receiving Yards"
    if "reception" in b or b == "rec" or "catch" in b:
        return "Receptions"
    return prop_name_from_blob(b)


def _phase6_player_lookup():
    """Build a fuzzy player lookup from saved Phase 6/player usage files for manual imports.

    This lets pasted Underdog shorthand like 'J. Goff' resolve to 'Jared Goff' using
    team + last name where possible, so the projection engine can match historical stats.
    """
    rows = []
    for path in [USAGE_FILE, PHASE6_PLAYER_SUMMARY_FILE, PHASE6_PLAYER_LOG_FILE]:
        try:
            if Path(path).exists():
                df = pd.read_csv(path, usecols=lambda c: str(c).lower() in ["player", "team", "position", "recent_team", "player_display_name"])
                df.columns = [str(c).strip() for c in df.columns]
                if "player_display_name" in df.columns and "player" not in df.columns:
                    df["player"] = df["player_display_name"]
                if "recent_team" in df.columns and "team" not in df.columns:
                    df["team"] = df["recent_team"]
                rows.extend(df[[c for c in ["player", "team", "position"] if c in df.columns]].dropna(subset=["player"]).to_dict("records"))
        except Exception:
            continue
    exact, by_last = {}, {}
    for r in rows:
        player = str(r.get("player") or "").strip()
        if not player:
            continue
        team = str(r.get("team") or "").upper().strip()
        pos = str(r.get("position") or "").upper().strip()
        meta = {"player": player, "team": team, "position": pos}
        exact[norm(player)] = meta
        parts = norm(player).split()
        if parts:
            last = parts[-1]
            by_last.setdefault(last, []).append(meta)
    return {"exact": exact, "by_last": by_last}


def _infer_team_from_matchup(matchup, player_team=None):
    txt = str(matchup or "").upper()
    teams = re.findall(r"\b[A-Z]{2,3}\b", txt)
    known = set(TEAM_STADIUM_COORDS.keys())
    teams = [t for t in teams if t in known]
    team = str(player_team or "").upper().strip()
    opp = ""
    if team and team in teams:
        opps = [t for t in teams if t != team]
        opp = opps[0] if opps else ""
    return team, opp


def _resolve_manual_player(player, team=None, position=None):
    lookup = _phase6_player_lookup()
    raw = str(player or "").strip()
    if not raw:
        return raw, team or "NFL", position or ""
    n = norm(raw)
    meta = lookup["exact"].get(n)
    if not meta:
        parts = n.split()
        # Handle Underdog shorthand like J. Goff, P Mahomes, C McCaffrey.
        if len(parts) >= 2 and (len(parts[0]) <= 2 or parts[0].endswith(".")):
            last = parts[-1]
            candidates = lookup["by_last"].get(last, [])
            team_u = str(team or "").upper().strip()
            pos_u = str(position or "").upper().strip()
            if team_u:
                team_matches = [m for m in candidates if str(m.get("team", "")).upper() == team_u]
                if team_matches:
                    candidates = team_matches
            if pos_u:
                pos_matches = [m for m in candidates if str(m.get("position", "")).upper() == pos_u]
                if pos_matches:
                    candidates = pos_matches
            if candidates:
                meta = candidates[0]
    if meta:
        return meta.get("player") or raw, (team or meta.get("team") or "NFL"), (position or meta.get("position") or "")
    return raw, team or "NFL", position or ""


def _infer_position_from_prop_player(prop, player=None, team=None, position=None):
    pos = str(position or "").upper().strip()
    if pos:
        return pos
    _, _, pos = _resolve_manual_player(player, team, position)
    if pos:
        return pos
    if prop in ["Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions"]:
        return "QB"
    return ""




def _resolve_model_player_strict(player, team=None, position=None):
    """Return a Phase-6/model player match or None.

    This is stricter than the manual resolver: live Underdog rows must match a
    player already in our saved NFL model database. This prevents MLB/tennis/etc.
    rows from slipping into the NFL prop board just because the market label
    looked similar.
    """
    lookup = _phase6_player_lookup()
    if not lookup.get("exact") and not lookup.get("by_last"):
        # If the saved model database is not present, do not hard-block startup.
        return {"player": str(player or "").strip(), "team": team or "NFL", "position": position or "", "model_match": False, "model_filter_disabled": True}

    raw = str(player or "").strip()
    if not raw:
        return None
    n = norm(raw)
    team_u = str(team or "").upper().strip()
    pos_u = str(position or "").upper().strip()

    meta = lookup["exact"].get(n)

    if not meta:
        parts = n.split()
        # Handle initials from Underdog like J. Goff, P Mahomes, C McCaffrey.
        if len(parts) >= 2 and (len(parts[0]) <= 2 or parts[0].endswith(".")):
            last = parts[-1]
            candidates = list(lookup["by_last"].get(last, []))
            if team_u:
                tm = [m for m in candidates if str(m.get("team", "")).upper() == team_u]
                if tm:
                    candidates = tm
            if pos_u:
                pm = [m for m in candidates if str(m.get("position", "")).upper() == pos_u]
                if pm:
                    candidates = pm
            if len(candidates) == 1:
                meta = candidates[0]
            elif candidates:
                meta = candidates[0]

    if not meta:
        # Conservative fuzzy match only for near-exact full names.
        names = list(lookup["exact"].keys())
        close = difflib.get_close_matches(n, names, n=1, cutoff=0.94)
        if close:
            meta = lookup["exact"].get(close[0])

    if not meta:
        return None

    return {
        "player": meta.get("player") or raw,
        "team": meta.get("team") or team or "NFL",
        "position": meta.get("position") or position or "",
        "model_match": True,
        "model_filter_disabled": False,
    }


def _prop_allowed_for_model_position(prop, position):
    """Validate every enabled prop against realistic NFL position groups."""
    prop = str(prop or "")
    pos = str(position or "").upper().strip()
    if prop not in ACTIVE_NFL_MARKETS:
        return False

    qb_props = {"Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions"}
    receiving_props = {"Receiving Yards", "Receptions", "Longest Reception"}
    rushing_props = {"Rushing Yards", "Rush Attempts", "Longest Rush"}
    offense_flex_props = {"Fantasy Points", "Anytime TD"}
    kicking_props = {"Kicking Points", "Field Goals Made"}
    defense_props = {"Tackles + Assists", "Sacks"}

    if prop in qb_props:
        return pos == "QB"
    if prop in receiving_props:
        return pos in {"RB", "FB", "WR", "TE"}
    if prop in rushing_props:
        return pos in {"QB", "RB", "FB", "WR", "TE"}
    if prop in offense_flex_props:
        return pos in {"QB", "RB", "FB", "WR", "TE"}
    if prop in kicking_props:
        return pos in {"K", "PK"}
    if prop in defense_props:
        return pos in {
            "DE", "DT", "DL", "EDGE", "LB", "ILB", "OLB", "MLB",
            "CB", "DB", "S", "FS", "SS", "NT", "DEF"
        }
    return False


def _infer_position_from_prop(prop, position=""):
    """Fill only unambiguous missing positions; never guess RB/WR/TE roles."""
    pos = str(position or "").upper().strip()
    if pos:
        return pos
    if prop in {"Passing Yards", "Passing TDs", "Interceptions", "Pass Attempts", "Completions"}:
        return "QB"
    if prop in {"Kicking Points", "Field Goals Made"}:
        return "K"
    if prop in {"Tackles + Assists", "Sacks"}:
        return "DEF"
    return ""


def _filter_live_board_to_phase6_model(rows):
    """Normalize NFL rows, reject malformed league/timestamp records, and deduplicate safely."""
    if not rows:
        return []
    lookup = _phase6_player_lookup()
    model_filter_available = bool(lookup.get("exact") or lookup.get("by_last"))
    clean = []
    dropped = {"not_in_model": 0, "bad_position_market": 0, "bad_team": 0, "duplicate": 0}
    seen = set()
    for r in rows:
        row = dict(r or {})
        prop = _canon_prop_label(row.get("prop")) or row.get("prop")
        if prop not in ACTIVE_NFL_MARKETS or prop not in PROP_CONFIG:
            dropped["bad_position_market"] += 1
            continue
        line = safe_float(row.get("line"))
        if line is None or not _valid_market_line(prop, line):
            dropped["bad_position_market"] += 1
            continue
        row["line"] = float(line)

        meta = _resolve_model_player_strict(row.get("player"), row.get("team"), row.get("position")) if model_filter_available else None
        if meta:
            row["player"] = meta.get("player") or row.get("player")
            row["position"] = str(meta.get("position") or row.get("position") or "").upper().strip()
            row["team"] = meta.get("team") or row.get("team")
            row["model_match"] = True
        else:
            # Do not delete a valid live NFL line merely because the optional Phase 6
            # database is incomplete. The projection remains available but its data
            # score/audit will reflect partial context until the database is repaired.
            if model_filter_available:
                dropped["not_in_model"] += 1
            row["position"] = _infer_position_from_prop(prop, row.get("position"))
            row["model_match"] = False
            row["model_filter_disabled"] = True

        if not _prop_allowed_for_model_position(prop, row.get("position")):
            dropped["bad_position_market"] += 1
            continue

        team = _normalize_nfl_team(row.get("team"))
        matchup_team, matchup_opp = _teams_from_matchup_text(row.get("matchup"))
        if not team:
            team = matchup_team or matchup_opp
        if not team:
            dropped["bad_team"] += 1
            continue

        opp = _normalize_nfl_team(row.get("opp"))
        if not opp:
            if matchup_team and matchup_team != team:
                opp = matchup_team
            elif matchup_opp and matchup_opp != team:
                opp = matchup_opp
        row["team"] = team
        row["opp"] = opp
        row["prop"] = prop
        row["matchup"] = _canonical_matchup(row.get("matchup"), team, opp, row.get("home_away"))
        row["matchup_status"] = "VALID" if row["matchup"] else "OPPONENT_PENDING"

        event_key = str(row.get("event_id") or row.get("game_id") or row.get("match_id") or row.get("matchup") or team)
        key = (event_key, norm(row.get("player")), row.get("prop"), safe_float(row.get("line")))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        clean.append(row)

    request_log(
        "NFL_MODEL_BOARD_FILTER", "FILTERED",
        f"kept={len(clean)} partial_not_model={dropped['not_in_model']} "
        f"dropped_bad_market={dropped['bad_position_market']} dropped_bad_team={dropped['bad_team']} "
        f"duplicates={dropped['duplicate']}"
    )
    return clean


def _select_primary_market_lines(rows):
    """Keep one central/primary line per player, prop, and event to avoid projecting full alt ladders."""
    groups = {}
    for row in rows or []:
        r = dict(row or {})
        event = str(r.get("event_id") or r.get("game_id") or r.get("match_id") or r.get("matchup") or r.get("team") or "")
        key = (event, norm(r.get("player")), str(r.get("prop") or ""))
        groups.setdefault(key, []).append(r)
    selected = []
    for group in groups.values():
        valid = [r for r in group if safe_float(r.get("line")) is not None]
        if not valid:
            continue
        lines = sorted(float(r.get("line")) for r in valid)
        median = lines[len(lines)//2]
        # Prefer the central line; then prefer a neutral/default payout object.
        best = min(valid, key=lambda r: (abs(float(r.get("line"))-median), 0 if r.get("price") in [None, "", 1, 1.0] else 1, str(r.get("underdog_id") or "")))
        selected.append(best)
    return selected


def _projection_context_signature():
    paths = [
        USAGE_FILE, CURRENT_USAGE_FILE, TEAM_CONTEXT_FILE, CURRENT_TEAM_CONTEXT_FILE,
        INJURY_FILE, DEPTH_CHART_FILE, WEATHER_FILE, MARKET_CONTEXT_FILE,
        TRAVEL_CONTEXT_FILE, MATCHUP_CONTEXT_FILE, QB_CONTEXT_FILE, DEF_INJURY_FILE,
        FINAL_INACTIVES_FILE, MANUAL_OVERRIDE_FILE, LEARN_FILE, RESULT_LOG,
        PHASE6_PLAYER_LOG_FILE, PHASE6_PLAYER_SUMMARY_FILE, PHASE6_DEFENSE_RANK_FILE,
        PHASE6_TEAM_ADVANCED_FILE, PHASE6_TRENCH_FILE, PHASE6_RED_ZONE_FILE,
        PHASE6_OT_FILE, PHASE6_TRAVEL_FILE, PHASE6_TEAM_CONTEXT_FILE,
    ]
    return [_path_signature(p) for p in paths]


def _board_projection_cache_key(rows, primary_only):
    settings = {
        "model": MODEL_VERSION,
        "primary_only": bool(primary_only),
        "xgb": bool(st.session_state.get("xgb_assist_enabled", False)),
        "xgb_min": int(st.session_state.get("xgb_min_rows", 50) or 50),
        "xgb_blend": safe_float(st.session_state.get("xgb_blend_weight"), 0.22),
        "advanced": bool(st.session_state.get("advanced_sim_assist_enabled", True)),
        "bayes_min": int(st.session_state.get("bayes_min_games", 5) or 5),
        "ensemble": bool(st.session_state.get("ensemble_ml_assist_enabled", False)),
        "ensemble_min": int(st.session_state.get("ensemble_min_rows", 75) or 75),
        "ensemble_blend": safe_float(st.session_state.get("ensemble_blend_weight"), 0.16),
        "smart_calibration": bool(st.session_state.get("smart_calibration_enabled", True)),
        "team_volume_reconciliation": bool(st.session_state.get("team_volume_reconciliation_enabled", True)),
        "context": _projection_context_signature(),
    }
    payload = {"rows": rows, "settings": settings}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _manual_row_to_board_row(row, default_prop=None):
    # Flexible column names from CSV upload or pasted tables.
    get = lambda *keys: next((row.get(k) for k in keys if k in row and row.get(k) not in [None, "", np.nan]), None)
    player = get("player", "Player", "name", "Name", "athlete", "Athlete")
    prop = _canon_prop_label(get("prop", "Prop", "market", "Market", "stat", "Stat", "category", "Category") or default_prop)
    line = safe_float(get("line", "Line", "value", "Value", "projection", "Projection", "total", "Total"))
    matchup = str(get("matchup", "Matchup", "game", "Game") or "")
    team = str(get("team", "Team", "team_abbr", "Team Abbr") or "").upper().strip()
    opp = str(get("opp", "opponent", "Opponent") or "").upper().strip()
    pos = str(get("position", "Position", "pos", "Pos") or "").upper().strip()
    if not prop or prop not in PROP_CONFIG or not player or line is None or not _valid_market_line(prop, line):
        return None
    player, team, pos = _resolve_manual_player(player, team, pos)
    if not opp:
        _, opp = _infer_team_from_matchup(matchup, team)
    if not pos:
        pos = _infer_position_from_prop_player(prop, player, team, pos)
    home_away = ""
    if matchup and team:
        mu = matchup.upper()
        if f"@ {team}" in mu or mu.endswith(f"@{team}"):
            home_away = "HOME"
        elif f"{team} @" in mu:
            home_away = "AWAY"
    return {
        "player": str(player),
        "team": team or "NFL",
        "opp": opp,
        "home_away": home_away,
        "position": pos,
        "prop": prop,
        "line": float(line),
        "price": None,
        "source": "Manual Underdog",
        "source_url": "manual_import",
        "matchup": matchup,
        "underdog_id": "manual",
    }


def parse_manual_underdog_board(text="", uploaded_file=None):
    """Parse manual Underdog board input into app-ready rows.

    Accepted CSV columns: player, prop, line, team, opp, matchup, position.
    Also accepts copied/pasted board text grouped by market, e.g.:
    Pass Yards\nJ. Goff\n271.5\nDET vs NO\nD. Prescott\n266.5\nDAL @ NYG.
    """
    rows = []
    raw_text = text or ""
    if uploaded_file is not None:
        try:
            data = uploaded_file.read()
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            raw_text = data.decode("utf-8", errors="ignore")
        except Exception:
            raw_text = str(text or "")
    # Try CSV/table first.
    if raw_text.strip():
        try:
            if "," in raw_text.splitlines()[0] or "\t" in raw_text.splitlines()[0]:
                sep = "\t" if "\t" in raw_text.splitlines()[0] and "," not in raw_text.splitlines()[0] else ","
                df = pd.read_csv(io.StringIO(raw_text), sep=sep)
                df.columns = [str(c).strip() for c in df.columns]
                for _, rr in df.iterrows():
                    out = _manual_row_to_board_row(rr.to_dict())
                    if out:
                        rows.append(out)
        except Exception as e:
            request_log("MANUAL_BOARD_IMPORT", "CSV_PARSE_ERROR", e)
    if rows:
        return _dedupe_board_rows(rows)

    # Flexible pasted text parser.
    current_prop = None
    lines = [ln.strip() for ln in str(raw_text or "").splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        prop_guess = _canon_prop_label(ln)
        if prop_guess:
            current_prop = prop_guess
            i += 1
            continue
        # One-line format: J. Goff 271.5 DET vs NO
        m = re.match(r"^([A-Za-z]\.?\s*[A-Za-z'\-\.]+(?:\s+(?:Jr\.?|Sr\.?|II|III|IV))?|[A-Za-z'\-\.]+\s+[A-Za-z'\-\.]+)\s+([0-9]+(?:\.[0-9]+)?)\s*(.*)$", ln)
        if m and current_prop:
            player, line, rest = m.group(1).strip(), m.group(2), m.group(3).strip()
            out = _manual_row_to_board_row({"player": player, "line": line, "matchup": rest}, current_prop)
            if out:
                rows.append(out)
            i += 1
            continue
        # Multi-line format: player / line / matchup.
        if current_prop and i + 1 < len(lines) and safe_float(lines[i+1]) is not None:
            player = ln
            line = lines[i+1]
            matchup = lines[i+2] if i + 2 < len(lines) and _canon_prop_label(lines[i+2]) is None and safe_float(lines[i+2]) is None else ""
            out = _manual_row_to_board_row({"player": player, "line": line, "matchup": matchup}, current_prop)
            if out:
                rows.append(out)
            i += 3 if matchup else 2
            continue
        i += 1
    return _dedupe_board_rows(rows)


def _dedupe_board_rows(rows):
    seen, clean = set(), []
    for r in rows or []:
        key = (norm(r.get("player")), r.get("prop"), safe_float(r.get("line")), norm(r.get("matchup")))
        if key not in seen:
            seen.add(key); clean.append(r)
    return clean


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
    sig = _path_signature(path)
    key = str(Path(path))
    cached = _CSV_RUNTIME_CACHE.get(key)
    if cached and cached.get("sig") == sig:
        return cached.get("df")
    try:
        if Path(path).exists():
            df=pd.read_csv(path)
            df.columns=[str(c).strip() for c in df.columns]
            _CSV_RUNTIME_CACHE[key] = {"sig": sig, "df": df}
            return df
    except Exception as e:
        request_log(path, "CSV_LOAD_ERROR", e)
    return pd.DataFrame()

def load_usage_bank():
    return _records_by_player(USAGE_FILE)

def _records_by_player(path):
    sig = _path_signature(path)
    key = (str(Path(path)), sig)
    if key in _RECORD_BANK_RUNTIME_CACHE:
        return _RECORD_BANK_RUNTIME_CACHE[key]
    df=_read_optional_csv(path)
    if df.empty:
        _RECORD_BANK_RUNTIME_CACHE[key] = {}
        return {}
    bank={}
    for _, r in df.iterrows():
        d={k:r.get(k) for k in df.columns}
        player = d.get("player") or d.get("player_display_name") or d.get("name")
        pkey=norm(player)
        if pkey:
            bank[pkey]=d
    _RECORD_BANK_RUNTIME_CACHE[key] = bank
    return bank

def load_current_usage_bank():
    return _records_by_player(CURRENT_USAGE_FILE)

def load_depth_chart_bank():
    """Optional depth-chart/role file.

    Supported columns: player, team, position, depth_rank, role, starter, slot_role,
    handcuff, backup_qb, qb_change_risk, expected_routes, expected_targets,
    expected_attempts, expected_carries, role_note.
    """
    return _records_by_player(DEPTH_CHART_FILE)

def load_market_context_bank():
    """Optional market-consensus file keyed by player/prop/team.

    Supported columns: player, prop, team, consensus_line, best_line, open_line,
    close_line, market_prob_over, market_prob_under, market_books, line_move,
    no_vig_over, no_vig_under.
    """
    df=_read_optional_csv(MARKET_CONTEXT_FILE)
    if df.empty:
        return {}
    bank={}
    for _, r in df.iterrows():
        d={k:r.get(k) for k in df.columns}
        player=norm(d.get("player") or d.get("player_display_name") or d.get("name"))
        prop=str(d.get("prop") or d.get("market") or "").strip()
        team=str(d.get("team") or "").upper().strip()
        if player and prop:
            bank[(player, prop, team)] = d
            bank[(player, prop, "")] = d
    return bank

def _row_key_bank(path, key_fields):
    sig = _path_signature(path)
    cache_key = (str(Path(path)), tuple(tuple(x) for x in key_fields), sig)
    if cache_key in _ROW_BANK_RUNTIME_CACHE:
        return _ROW_BANK_RUNTIME_CACHE[cache_key]
    df=_read_optional_csv(path)
    if df.empty:
        _ROW_BANK_RUNTIME_CACHE[cache_key] = {}
        return {}
    bank={}
    for _, r in df.iterrows():
        d={k:r.get(k) for k in df.columns}
        for fields in key_fields:
            key=[]
            for f in fields:
                val=d.get(f)
                if f in ["player","name","player_display_name"]:
                    val=norm(val)
                elif f in ["team","opp","home_team","away_team","prop","matchup"]:
                    val=str(val or "").upper().strip()
                else:
                    val=str(val or "").strip()
                key.append(val)
            if all(x not in [None,""] for x in key):
                bank[tuple(key)] = d
    _ROW_BANK_RUNTIME_CACHE[cache_key] = bank
    return bank

def load_travel_context_bank():
    return _row_key_bank(TRAVEL_CONTEXT_FILE, [
        ("team","opp"),
        ("matchup",),
    ])

def load_matchup_context_bank():
    return _row_key_bank(MATCHUP_CONTEXT_FILE, [
        ("team","opp"),
        ("matchup",),
    ])

def load_qb_context_bank():
    return _row_key_bank(QB_CONTEXT_FILE, [
        ("team",),
        ("player",),
    ])

def load_defensive_injury_context():
    data=load_json(DEF_INJURY_FILE,{})
    return data if isinstance(data,dict) else {}

def load_final_inactives_context():
    """Optional official inactive confirmation file.

    Expected shape:
    {
      "confirmed_matchups": {"BUF @ KC": {"confirmed": true, "updated_at": "..."}},
      "teams": {"KC": {"confirmed": true, "updated_at": "..."}},
      "players": {"player name": {"status": "ACTIVE|INACTIVE", "note": "..."}}
    }
    Top-level player-name keys are also accepted for quick manual entry.
    """
    data=load_json(FINAL_INACTIVES_FILE,{})
    return data if isinstance(data,dict) else {}

def load_splits_context_bank():
    return _row_key_bank(SPLITS_CONTEXT_FILE, [
        ("player","prop"),
        ("player",),
    ])

def load_personnel_context_bank():
    return _row_key_bank(PERSONNEL_CONTEXT_FILE, [
        ("team","opp"),
        ("matchup",),
    ])

def load_api_config():
    data=load_json(API_CONFIG_FILE,{})
    return data if isinstance(data,dict) else {}

def load_manual_overrides():
    data=load_json(MANUAL_OVERRIDE_FILE,{})
    return data if isinstance(data,dict) else {}

def _lookup_pair_context(bank, row):
    team=str((row or {}).get("team") or "").upper().strip()
    opp=str((row or {}).get("opp") or "").upper().strip()
    matchup=str((row or {}).get("matchup") or "").upper().strip()
    return bank.get((team,opp)) or bank.get((matchup,)) or {}

def _lookup_qb_context(row):
    bank=load_qb_context_bank()
    team=str((row or {}).get("team") or "").upper().strip()
    player=norm((row or {}).get("player"))
    return bank.get((team,)) or bank.get((player,)) or {}

def _lookup_player_prop_context(bank, row):
    player=norm((row or {}).get("player"))
    prop=str((row or {}).get("prop") or "").strip()
    return bank.get((player,prop)) or bank.get((player,)) or {}

def _lookup_manual_override(row):
    data=load_manual_overrides()
    if not data:
        return {}
    player=norm((row or {}).get("player"))
    prop=str((row or {}).get("prop") or "").strip()
    team=str((row or {}).get("team") or "").upper().strip()
    matchup=str((row or {}).get("matchup") or "").upper().strip()
    out={}
    players=data.get("players") if isinstance(data.get("players"), dict) else {}
    for key in [player, str((row or {}).get("player") or "")]:
        ctx=players.get(key)
        if isinstance(ctx, dict):
            out.update(ctx)
    prop_overrides=data.get("player_props") if isinstance(data.get("player_props"), dict) else {}
    for key in [f"{player}|{prop}", f"{str((row or {}).get('player') or '')}|{prop}"]:
        ctx=prop_overrides.get(key)
        if isinstance(ctx, dict):
            out.update(ctx)
    teams=data.get("teams") if isinstance(data.get("teams"), dict) else {}
    if isinstance(teams.get(team), dict):
        for k,v in teams[team].items():
            out.setdefault(k, v)
    matchups=data.get("matchups") if isinstance(data.get("matchups"), dict) else {}
    if isinstance(matchups.get(matchup), dict):
        for k,v in matchups[matchup].items():
            out.setdefault(k, v)
    if not out:
        return {}
    normalized={}
    passthrough_fields=[
        "injury_status","practice_status","limited_snap_risk","expected_snap_share",
        "expected_routes","expected_targets","expected_attempts","expected_carries",
        "qb_status","qb_change_risk","weather_risk","weather_pass_factor",
        "game_total","spread","team_total","pass_rate","rush_rate","pace",
        "starters_rest_risk","game_importance","motivation",
    ]
    for k,v in out.items():
        if v not in [None, ""]:
            if k in passthrough_fields:
                normalized[k]=v
            else:
                normalized[f"manual_{k}" if not str(k).startswith("manual_") else k]=v
    normalized["has_manual_override_context"]=True
    normalized["manual_override_updated_at"]=out.get("updated_at") or out.get("time") or now_iso()
    if out.get("status") not in [None, ""]:
        normalized["manual_override_status"]=out.get("status")
    if out.get("confidence") not in [None, ""]:
        normalized["manual_override_confidence"]=out.get("confidence")
    return normalized

def _lookup_final_inactives(row):
    data=load_final_inactives_context()
    if not data:
        return {}
    out={}
    player_key=norm((row or {}).get("player"))
    team=str((row or {}).get("team") or "").upper().strip()
    matchup=str((row or {}).get("matchup") or "").upper().strip()

    players=data.get("players") if isinstance(data.get("players"), dict) else {}
    pctx=players.get(player_key) or players.get(str((row or {}).get("player") or "")) or data.get(player_key)
    if isinstance(pctx, dict):
        out["final_inactive_status"]=pctx.get("status", pctx.get("inactive_status"))
        out["final_inactive_note"]=pctx.get("note", pctx.get("inactive_note", ""))
        out["final_inactives_updated_at"]=pctx.get("updated_at") or pctx.get("time")
        out["has_final_inactives_context"]=True
    elif isinstance(pctx, str):
        out["final_inactive_status"]=pctx
        out["has_final_inactives_context"]=True

    confirmed=None; updated=None
    confirmed_matchups=data.get("confirmed_matchups") if isinstance(data.get("confirmed_matchups"), dict) else {}
    mctx=confirmed_matchups.get(matchup) or confirmed_matchups.get(matchup.upper())
    if isinstance(mctx, dict):
        confirmed=mctx.get("confirmed")
        updated=mctx.get("updated_at") or mctx.get("time")
    teams=data.get("teams") if isinstance(data.get("teams"), dict) else {}
    tctx=teams.get(team)
    if isinstance(tctx, dict):
        confirmed=tctx.get("confirmed", confirmed)
        updated=tctx.get("updated_at", updated)
    if confirmed is not None:
        out["final_inactives_confirmed"]=confirmed
        out["has_final_inactives_context"]=True
    if updated:
        out["final_inactives_updated_at"]=updated
    return out

def load_current_team_context():
    data=load_json(CURRENT_TEAM_CONTEXT_FILE,{})
    return data if isinstance(data,dict) else {}

def load_weather_context():
    """Optional matchup/team weather JSON.

    Accepted keys can be matchup strings ("DET @ GB"), home team abbreviations,
    or team|opp. Values can include wind_mph, gust_mph, precipitation_pct,
    temperature, roof, weather_risk, weather_note, game_time.
    """
    data=load_json(WEATHER_FILE,{})
    return data if isinstance(data,dict) else {}

def _weather_risk_from_detail(ctx):
    if not isinstance(ctx, dict):
        return "", 1.0, []
    notes=[]
    wind=safe_float(ctx.get("wind_mph"), safe_float(ctx.get("wind"), 0)) or 0
    gust=safe_float(ctx.get("gust_mph"), safe_float(ctx.get("gust"), 0)) or 0
    precip=safe_float(ctx.get("precipitation_pct"), safe_float(ctx.get("precip_pct"), 0)) or 0
    temp=safe_float(ctx.get("temperature"), safe_float(ctx.get("temp_f"), None))
    roof=str(ctx.get("roof") or "").upper()
    risk=str(ctx.get("weather_risk") or "").upper()
    pass_factor=1.0
    if roof in ["DOME", "CLOSED", "RETRACTABLE_CLOSED"]:
        return "LOW", 1.012, ["Weather: protected roof"]
    if wind >= 18 or gust >= 28:
        risk="WIND"; pass_factor*=0.91; notes.append("Weather: high wind passing tax")
    elif wind >= 13 or gust >= 22:
        risk=risk or "HIGH"; pass_factor*=0.955; notes.append("Weather: moderate wind tax")
    if precip >= 55:
        risk=risk or "RAIN"; pass_factor*=0.965; notes.append("Weather: precipitation risk")
    if temp is not None and temp <= 25:
        risk=risk or "COLD"; pass_factor*=0.982; notes.append("Weather: cold-game efficiency tax")
    note=ctx.get("weather_note") or ctx.get("summary")
    if note:
        notes.append(str(note)[:120])
    return risk or "LOW", clamp(pass_factor,0.86,1.03), notes

def _lookup_weather_for_row(row):
    weather=load_weather_context()
    if not weather:
        return {}
    team=str(row.get("team") or "").upper().strip()
    opp=str(row.get("opp") or "").upper().strip()
    matchup=str(row.get("matchup") or "").upper().strip()
    keys=[matchup, f"{team}|{opp}", f"{opp}|{team}", team, opp]
    for k in keys:
        if k and isinstance(weather.get(k), dict):
            return weather.get(k)
    return {}

@st.cache_data(ttl=21600, show_spinner=False)
def _current_season_context_bank(season=NFL_CURRENT_SEASON):
    """Build rolling current-season player/team context when nflverse data exists.

    In preseason this usually returns empty and the app falls back to saved Phase 6.
    Once regular-season weeks exist, it supplies last3/last5 rolling form.
    """
    ctx={"players":{}, "teams":{}, "source":"none", "rows":0}
    try:
        weekly=fetch_nflverse_player_weekly_stats(int(season), force_refresh=False)
        if weekly.empty:
            return ctx
        logs=weekly.copy()
        if "player_display_name" in logs.columns and "player" not in logs.columns:
            logs["player"]=logs["player_display_name"]
        if "recent_team" in logs.columns and "team" not in logs.columns:
            logs["team"]=logs["recent_team"]
        for c in ["attempts","completions","passing_yards","targets","receptions","receiving_yards","air_yards","carries","rushing_yards"]:
            if c not in logs.columns:
                logs[c]=0
            logs[c]=pd.to_numeric(logs[c], errors="coerce").fillna(0)
        if "week" in logs.columns:
            logs["week_num"]=pd.to_numeric(logs["week"], errors="coerce").fillna(0)
        else:
            logs["week_num"]=np.arange(len(logs))
        for (player, team), g in logs.groupby(["player","team"], dropna=False):
            player=str(player or "").strip()
            if not player:
                continue
            g=g.sort_values("week_num")
            gp=max(1, len(g))
            tail3=g.tail(3); tail5=g.tail(5)
            d={
                "player": player,
                "team": str(team or ""),
                "current_games": int(gp),
                "current_pass_attempts_pg": round(float(g["attempts"].sum())/gp,3),
                "current_completions_pg": round(float(g["completions"].sum())/gp,3),
                "current_passing_yards_pg": round(float(g["passing_yards"].sum())/gp,3),
                "current_targets_pg": round(float(g["targets"].sum())/gp,3),
                "current_receiving_yards_pg": round(float(g["receiving_yards"].sum())/gp,3),
                "current_receptions_pg": round(float(g["receptions"].sum())/gp,3),
                "current_rush_attempts_pg": round(float(g["carries"].sum())/gp,3),
                "current_rushing_yards_pg": round(float(g["rushing_yards"].sum())/gp,3),
                "last3_pass_attempts_pg": round(float(tail3["attempts"].mean()),3) if not tail3.empty else 0,
                "last3_completions_pg": round(float(tail3["completions"].mean()),3) if not tail3.empty else 0,
                "last3_passing_yards_pg": round(float(tail3["passing_yards"].mean()),3) if not tail3.empty else 0,
                "last3_targets_pg": round(float(tail3["targets"].mean()),3) if not tail3.empty else 0,
                "last3_receptions_pg": round(float(tail3["receptions"].mean()),3) if not tail3.empty else 0,
                "last3_receiving_yards_pg": round(float(tail3["receiving_yards"].mean()),3) if not tail3.empty else 0,
                "last3_rush_attempts_pg": round(float(tail3["carries"].mean()),3) if not tail3.empty else 0,
                "last3_rushing_yards_pg": round(float(tail3["rushing_yards"].mean()),3) if not tail3.empty else 0,
                "last5_pass_attempts_pg": round(float(tail5["attempts"].mean()),3) if not tail5.empty else 0,
                "last5_completions_pg": round(float(tail5["completions"].mean()),3) if not tail5.empty else 0,
                "last5_passing_yards_pg": round(float(tail5["passing_yards"].mean()),3) if not tail5.empty else 0,
                "last5_targets_pg": round(float(tail5["targets"].mean()),3) if not tail5.empty else 0,
                "last5_receptions_pg": round(float(tail5["receptions"].mean()),3) if not tail5.empty else 0,
                "last5_receiving_yards_pg": round(float(tail5["receiving_yards"].mean()),3) if not tail5.empty else 0,
                "last5_rush_attempts_pg": round(float(tail5["carries"].mean()),3) if not tail5.empty else 0,
                "last5_rushing_yards_pg": round(float(tail5["rushing_yards"].mean()),3) if not tail5.empty else 0,
                "current_context_source": f"nflverse_current_{season}",
            }
            ctx["players"][norm(player)]=d
        team_rows=[]
        for team, g in logs.groupby("team", dropna=False):
            team=str(team or "").strip()
            if not team:
                continue
            weeks=max(1, g["week_num"].nunique())
            pass_att=float(g["attempts"].sum())
            rush_att=float(g["carries"].sum())
            plays=pass_att+rush_att
            team_rows.append((team, {
                "current_plays_pg": round(plays/weeks,2),
                "current_pass_rate": round(100*pass_att/max(1,plays),2),
                "current_rush_rate": round(100*rush_att/max(1,plays),2),
            }))
        ctx["teams"]=dict(team_rows)
        ctx["rows"]=int(len(logs))
        ctx["source"]=f"nflverse_current_{season}"
    except Exception as e:
        request_log("CURRENT_SEASON_CONTEXT", "ERROR", str(e)[:220])
    return ctx


@st.cache_data(ttl=86400, show_spinner=False)
def _online_passing_yards_context_bank(season=NFL_LAST_SEASON):
    """Build an online/saved-data context bank for Passing Yards.

    Primary source is nflverse weekly player stats + saved Phase 6 files.  This is
    intentionally cached and persisted by the existing nflverse fetchers so the app
    can pull/build once, then reuse the same context like the MLB engine.
    """
    ctx = {"players": {}, "teams": {}, "defenses": {}, "source": "none", "rows": 0}
    try:
        df = pd.DataFrame()
        if Path(PHASE6_PLAYER_SUMMARY_FILE).exists():
            try:
                df = pd.read_csv(PHASE6_PLAYER_SUMMARY_FILE)
                ctx["source"] = "saved_phase6_player_summary"
            except Exception:
                df = pd.DataFrame()
        if df.empty:
            weekly = fetch_nflverse_player_weekly_stats(int(season), force_refresh=False)
            if not weekly.empty:
                logs = weekly.copy()
                if "player_display_name" in logs.columns and "player" not in logs.columns:
                    logs["player"] = logs["player_display_name"]
                if "recent_team" in logs.columns and "team" not in logs.columns:
                    logs["team"] = logs["recent_team"]
                if "position" not in logs.columns:
                    logs["position"] = ""
                for c in ["attempts","completions","passing_yards"]:
                    if c not in logs.columns:
                        logs[c] = 0
                    logs[c] = pd.to_numeric(logs[c], errors="coerce").fillna(0)
                gcols=["player","team","position"]
                df = logs.groupby(gcols, dropna=False)[["attempts","completions","passing_yards"]].sum().reset_index()
                games = logs.groupby(gcols, dropna=False)["week"].nunique().reset_index(name="games_played") if "week" in logs.columns else df[gcols].assign(games_played=17)
                df = df.merge(games, on=gcols, how="left")
                gp=df["games_played"].replace(0, np.nan)
                df["pass_attempts_pg"]=(df["attempts"]/gp).round(3)
                df["completions_pg"]=(df["completions"]/gp).round(3)
                df["passing_yards_pg"]=(df["passing_yards"]/gp).round(3)
                ctx["source"] = "online_nflverse_player_weekly"
        if not df.empty:
            ctx["rows"] = int(len(df))
            # Player QB context.
            for _, r in df.iterrows():
                pos=str(r.get("position") or "").upper()
                attempts=safe_float(r.get("pass_attempts_pg"), safe_float(r.get("attempts"), 0)) or 0
                ypg=safe_float(r.get("passing_yards_pg"), None)
                if ypg is None and safe_float(r.get("passing_yards"), None) is not None:
                    games=safe_float(r.get("games_played"), 17) or 17
                    ypg=(safe_float(r.get("passing_yards"), 0) or 0)/max(1,games)
                if attempts < 8 and pos != "QB":
                    continue
                player=str(r.get("player") or r.get("player_display_name") or "").strip()
                if not player:
                    continue
                team=str(r.get("team") or r.get("recent_team") or "").strip()
                comps=safe_float(r.get("completions_pg"), None)
                games=safe_float(r.get("games_played"), 17) or 17
                ypa=(safe_float(r.get("passing_yards"), 0) or 0) / max(1.0, safe_float(r.get("attempts"), 0) or 0)
                ctx["players"][norm(player)] = {
                    "player": player, "team": team, "position": "QB" if pos == "QB" or attempts >= 10 else pos,
                    "passing_yards_pg": round(float(ypg or 0),3),
                    "pass_attempts_pg": round(float(attempts or 0),3),
                    "completions_pg": None if comps is None else round(float(comps),3),
                    "yards_per_attempt": round(float(clamp(ypa if ypa else (ypg or 0)/max(1, attempts), 3.5, 10.5)),3),
                    "games_played": int(games),
                    "passing_context_source": ctx["source"],
                }
            # Team offense/pass rate from summary totals.
            if all(c in df.columns for c in ["team"]):
                tmp=df.copy()
                for c in ["attempts","passing_yards"]:
                    if c in tmp.columns:
                        tmp[c]=pd.to_numeric(tmp[c], errors="coerce").fillna(0)
                team_attempts = tmp.groupby("team")["attempts"].sum() if "attempts" in tmp.columns else None
                if team_attempts is not None:
                    for team, att in team_attempts.items():
                        if not team:
                            continue
                        ctx["teams"][str(team)] = {"team_pass_attempts_pg": round(float(att)/17.0,3)}
        # Team pass/rush rates and defensive ranks from saved context if available.
        teams = load_json(TEAM_CONTEXT_FILE,{})
        if isinstance(teams, dict):
            for team, data in teams.items():
                if not isinstance(data, dict):
                    continue
                ctx["teams"].setdefault(str(team), {}).update({k:v for k,v in data.items() if k in ["pass_rate","pbp_pass_rate","plays_pg","pbp_plays_pg","game_total","spread","team_total"] and v not in [None,""]})
                ctx["defenses"].setdefault(str(team), {}).update({k:v for k,v in data.items() if k in ["def_pass_rank","def_epa_allowed_per_play","def_success_allowed_rate","pressure_rate","def_pressure_rate","coverage_grade"] and v not in [None,""]})
        # If defense ranks file exists, use it too.
        if Path(PHASE6_DEFENSE_RANK_FILE).exists():
            try:
                ddf=pd.read_csv(PHASE6_DEFENSE_RANK_FILE)
                for _, r in ddf.iterrows():
                    team=str(r.get("team") or "").strip()
                    if not team:
                        continue
                    ctx["defenses"].setdefault(team,{})
                    for k in ["def_pass_rank","pass_yards_allowed_pg","def_role_rank"]:
                        if k in ddf.columns and r.get(k) not in [None,""]:
                            ctx["defenses"][team][k]=r.get(k)
            except Exception as e:
                request_log("PASS_YARDS_CONTEXT", "DEF_RANK_READ_ERROR", str(e)[:180])
        request_log("PASS_YARDS_CONTEXT", "READY", f"players={len(ctx['players'])} teams={len(ctx['teams'])} defenses={len(ctx['defenses'])} source={ctx['source']}")
    except Exception as e:
        request_log("PASS_YARDS_CONTEXT", "ERROR", str(e)[:240])
    return ctx


def _fuzzy_player_context(player, bank, team=None, min_score=0.88):
    key=norm(player)
    if key in bank:
        return bank[key]
    if not key or not bank:
        return {}
    best=None; best_score=0.0
    wanted_team=str(team or "").upper()
    for k, meta in bank.items():
        score=difflib.SequenceMatcher(None, key, k).ratio()
        # Strong boost for initial+last style or same last name.
        kp=key.split(); mp=k.split()
        if kp and mp and kp[-1] == mp[-1]:
            score=max(score, 0.86)
            if kp[0][:1] == mp[0][:1]:
                score=max(score, 0.94)
        if wanted_team and str(meta.get("team") or "").upper() == wanted_team:
            score += 0.03
        if score > best_score:
            best_score=score; best=meta
    return dict(best or {}) if best_score >= min_score else {}


def enrich_passing_yards_context(row):
    """Attach online/saved passing-yards context to an Underdog row.

    This is the final guard that makes the projection use football inputs first:
    QB last-season YPG, attempts/game, YPA, team pass rate/plays, opponent pass defense,
    spread/total, and stadium/weather when those fields are available.
    """
    row=dict(row or {})
    if row.get("prop") != "Passing Yards":
        return row
    bank=_online_passing_yards_context_bank(int(NFL_LAST_SEASON))
    pctx=_fuzzy_player_context(row.get("player"), bank.get("players",{}), team=row.get("team"))
    if pctx:
        for k,v in pctx.items():
            if row.get(k) in [None,"", "NFL"]:
                row[k]=v
        # Always let model DB fix missing/bad team/position from Underdog.
        if row.get("team") in [None,"", "NFL"] and pctx.get("team"):
            row["team"]=pctx.get("team")
        if row.get("position") in [None,""] and pctx.get("position"):
            row["position"]=pctx.get("position")
        row["model_player_match"] = pctx.get("player")
        row["model_match_status"] = "MATCHED"
    else:
        row["model_match_status"] = "NO_MODEL_MATCH"
    team=str(row.get("team") or "")
    opp=str(row.get("opp") or "")
    if team and team in bank.get("teams",{}):
        for k,v in bank["teams"][team].items():
            if row.get(k) in [None,""]:
                row[k]=v
    if opp and opp in bank.get("defenses",{}):
        for k,v in bank["defenses"][opp].items():
            if row.get(k) in [None,""]:
                row[k]=v
            if row.get("opp_"+k) in [None,""]:
                row["opp_"+k]=v
    row["passing_context_bank_source"] = bank.get("source")
    row["passing_context_players"] = len(bank.get("players",{}))
    return row


@st.cache_data(ttl=86400, show_spinner=False)
def _online_receiving_yards_context_bank(season=NFL_LAST_SEASON):
    """Build online/saved context for Receiving Yards (WR/TE/RB receiving).

    Uses Phase 6/nflverse saved data first, then online nflverse weekly stats.
    Inputs: receiving yards/game, targets/game, receptions/game, yards/target,
    team pass rate/plays, opponent receiving/pass-defense ranks, stadium/weather.
    """
    ctx = {"players": {}, "teams": {}, "defenses": {}, "source": "none", "rows": 0}
    try:
        df = pd.DataFrame()
        if Path(PHASE6_PLAYER_SUMMARY_FILE).exists():
            try:
                df = pd.read_csv(PHASE6_PLAYER_SUMMARY_FILE)
                ctx["source"] = "saved_phase6_player_summary"
            except Exception:
                df = pd.DataFrame()
        if df.empty:
            weekly = fetch_nflverse_player_weekly_stats(int(season), force_refresh=False)
            if not weekly.empty:
                logs = weekly.copy()
                if "player_display_name" in logs.columns and "player" not in logs.columns:
                    logs["player"] = logs["player_display_name"]
                if "recent_team" in logs.columns and "team" not in logs.columns:
                    logs["team"] = logs["recent_team"]
                if "position" not in logs.columns:
                    logs["position"] = ""
                for c in ["targets","receptions","receiving_yards","air_yards"]:
                    if c not in logs.columns:
                        logs[c] = 0
                    logs[c] = pd.to_numeric(logs[c], errors="coerce").fillna(0)
                gcols=["player","team","position"]
                df = logs.groupby(gcols, dropna=False)[["targets","receptions","receiving_yards","air_yards"]].sum().reset_index()
                games = logs.groupby(gcols, dropna=False)["week"].nunique().reset_index(name="games_played") if "week" in logs.columns else df[gcols].assign(games_played=17)
                df = df.merge(games, on=gcols, how="left")
                gp=df["games_played"].replace(0, np.nan)
                df["targets_pg"]=(df["targets"]/gp).round(3)
                df["receptions_pg"]=(df["receptions"]/gp).round(3)
                df["receiving_yards_pg"]=(df["receiving_yards"]/gp).round(3)
                df["air_yards_pg"]=(df["air_yards"]/gp).round(3)
                ctx["source"] = "online_nflverse_player_weekly"
        if not df.empty:
            ctx["rows"] = int(len(df))
            for _, r in df.iterrows():
                pos=str(r.get("position") or "").upper()
                if pos not in ["WR","TE","RB"]:
                    continue
                player=str(r.get("player") or r.get("player_display_name") or "").strip()
                if not player:
                    continue
                team=str(r.get("team") or r.get("recent_team") or "").strip()
                games=safe_float(r.get("games_played"), 17) or 17
                targets=safe_float(r.get("targets"), None)
                rec_yards=safe_float(r.get("receiving_yards"), None)
                targets_pg=safe_float(r.get("targets_pg"), None)
                if targets_pg is None and targets is not None:
                    targets_pg=targets/max(1,games)
                yards_pg=safe_float(r.get("receiving_yards_pg"), None)
                if yards_pg is None and rec_yards is not None:
                    yards_pg=rec_yards/max(1,games)
                receptions_pg=safe_float(r.get("receptions_pg"), None)
                air_pg=safe_float(r.get("air_yards_pg"), None)
                ypt=(rec_yards or 0)/max(1.0, targets or 0)
                if targets_pg is None or targets_pg < 1.0:
                    continue
                ctx["players"][norm(player)] = {
                    "player": player, "team": team, "position": pos,
                    "receiving_yards_pg": round(float(yards_pg or 0),3),
                    "targets_pg": round(float(targets_pg or 0),3),
                    "receptions_pg": None if receptions_pg is None else round(float(receptions_pg),3),
                    "air_yards_pg": None if air_pg is None else round(float(air_pg),3),
                    "yards_per_target": round(float(clamp(ypt if ypt else (yards_pg or 0)/max(1, targets_pg or 1), 3.0, 14.5)),3),
                    "games_played": int(games),
                    "receiving_context_source": ctx["source"],
                }
        teams = load_json(TEAM_CONTEXT_FILE,{})
        if isinstance(teams, dict):
            for team, data in teams.items():
                if not isinstance(data, dict):
                    continue
                ctx["teams"].setdefault(str(team), {}).update({k:v for k,v in data.items() if k in ["pass_rate","pbp_pass_rate","plays_pg","pbp_plays_pg","game_total","spread","team_total"] and v not in [None,""]})
                ctx["defenses"].setdefault(str(team), {}).update({k:v for k,v in data.items() if k in ["def_pass_rank","def_role_rank","def_te_rank","def_slot_rank","def_rb_rec_rank","coverage_grade","pressure_rate","def_pressure_rate"] and v not in [None,""]})
        if Path(PHASE6_DEFENSE_RANK_FILE).exists():
            try:
                ddf=pd.read_csv(PHASE6_DEFENSE_RANK_FILE)
                for _, r in ddf.iterrows():
                    team=str(r.get("team") or "").strip()
                    if not team:
                        continue
                    ctx["defenses"].setdefault(team,{})
                    for k in ["def_pass_rank","pass_yards_allowed_pg","rec_yards_allowed_pg","receptions_allowed_pg","def_role_rank"]:
                        if k in ddf.columns and r.get(k) not in [None,""]:
                            ctx["defenses"][team][k]=r.get(k)
            except Exception as e:
                request_log("REC_YARDS_CONTEXT", "DEF_RANK_READ_ERROR", str(e)[:180])
        request_log("REC_YARDS_CONTEXT", "READY", f"players={len(ctx['players'])} teams={len(ctx['teams'])} defenses={len(ctx['defenses'])} source={ctx['source']}")
    except Exception as e:
        request_log("REC_YARDS_CONTEXT", "ERROR", str(e)[:240])
    return ctx

def enrich_receiving_yards_context(row):
    """Attach receiving-yards context for WR/TE/RB lines."""
    row=dict(row or {})
    if row.get("prop") != "Receiving Yards":
        return row
    bank=_online_receiving_yards_context_bank(int(NFL_LAST_SEASON))
    pctx=_fuzzy_player_context(row.get("player"), bank.get("players",{}), team=row.get("team"), min_score=0.86)
    if pctx:
        for k,v in pctx.items():
            if row.get(k) in [None,"", "NFL"]:
                row[k]=v
        if row.get("team") in [None,"", "NFL"] and pctx.get("team"):
            row["team"]=pctx.get("team")
        if row.get("position") in [None,""] and pctx.get("position"):
            row["position"]=pctx.get("position")
        row["model_player_match"] = pctx.get("player")
        row["model_match_status"] = "MATCHED"
    else:
        row["model_match_status"] = "NO_MODEL_MATCH"
    team=str(row.get("team") or "")
    opp=str(row.get("opp") or "")
    if team and team in bank.get("teams",{}):
        for k,v in bank["teams"][team].items():
            if row.get(k) in [None,""]:
                row[k]=v
    if opp and opp in bank.get("defenses",{}):
        for k,v in bank["defenses"][opp].items():
            if row.get(k) in [None,""]:
                row[k]=v
            if row.get("opp_"+k) in [None,""]:
                row["opp_"+k]=v
    row["receiving_context_bank_source"] = bank.get("source")
    row["receiving_context_players"] = len(bank.get("players",{}))
    return row

def load_team_context():
    data=load_json(TEAM_CONTEXT_FILE,{})
    return data if isinstance(data,dict) else {}

def load_injury_bank():
    data=load_json(INJURY_FILE,{})
    return data if isinstance(data,dict) else {}

def merge_nfl_context(row):
    """Attach real usage/team/injury context when local files exist. Missing data stays neutral."""
    row=dict(row or {})
    usage_bank = load_usage_bank()
    current_usage_bank = load_current_usage_bank()
    usage=usage_bank.get(norm(row.get("player")), {})
    current_usage=current_usage_bank.get(norm(row.get("player")), {})
    # Passing/Receiving yards need model context even when live Underdog object only has player+line.
    if row.get("prop") == "Passing Yards":
        row = enrich_passing_yards_context(row)
        usage=usage or usage_bank.get(norm(row.get("model_player_match") or row.get("player")), {})
        current_usage=current_usage or current_usage_bank.get(norm(row.get("model_player_match") or row.get("player")), {})
    elif row.get("prop") == "Receiving Yards":
        row = enrich_receiving_yards_context(row)
        usage=usage or usage_bank.get(norm(row.get("model_player_match") or row.get("player")), {})
        current_usage=current_usage or current_usage_bank.get(norm(row.get("model_player_match") or row.get("player")), {})
    for k,v in usage.items():
        if not k:
            continue
        if k not in row or row.get(k) in [None, "", "NFL"]:
            row[k]=v
    # Current-season/weekly usage should override last-season Phase 6 when present.
    for k,v in current_usage.items():
        if not k or v in [None, ""]:
            continue
        row[k]=v
        if str(k).startswith("current_"):
            row["has_current_usage"] = True
    # Fix generic live-feed labels after usage/model context is attached.
    if row.get("team") in [None, "", "NFL"]:
        if current_usage.get("team") not in [None, ""]:
            row["team"] = current_usage.get("team")
        elif usage.get("team") not in [None, ""]:
            row["team"] = usage.get("team")
    if row.get("position") in [None, ""]:
        if current_usage.get("position") not in [None, ""]:
            row["position"] = current_usage.get("position")
        elif usage.get("position") not in [None, ""]:
            row["position"] = usage.get("position")

    # If current-season nflverse has games, blend its rolling values into the row.
    current_bank=_current_season_context_bank(int(NFL_CURRENT_SEASON))
    current_player=_fuzzy_player_context(row.get("player"), current_bank.get("players",{}), team=row.get("team"), min_score=0.88)
    if current_player:
        for k,v in current_player.items():
            if v not in [None, ""]:
                row[k]=v
        row["current_context_source"]=current_player.get("current_context_source")
    teams=load_team_context()
    current_teams=load_current_team_context()
    team=str(row.get("team") or "")
    opp=str(row.get("opp") or "")
    team_ctx=teams.get(team,{}) if isinstance(teams.get(team,{}),dict) else {}
    current_team_ctx=current_teams.get(team,{}) if isinstance(current_teams.get(team,{}),dict) else {}
    if not current_team_ctx and team in current_bank.get("teams",{}):
        current_team_ctx=current_bank["teams"].get(team,{})
    opp_ctx=teams.get(opp,{}) if isinstance(teams.get(opp,{}),dict) else {}
    # Team offense / pace / Vegas / coach-style context.  These are NFL-specific
    # equivalents of the MLB environment and lineup context layers.
    team_keys = [
        "pace","pass_rate","rush_rate","plays_pg","spread","game_total","team_total",
        "weather_risk","pbp_plays_pg","pbp_pass_rate","pbp_rush_rate",
        "epa_per_play","success_rate","early_down_pass_rate","early_down_success_rate",
        "red_zone_pass_rate","goal_line_rush_rate","penalties_pg","fumbles_pg",
        "sacks_allowed_pg","qb_hits_allowed_pg","explosive_pass_rate","explosive_rush_rate",
        "ot_rate","team_identity","coach_pace_proxy","seconds_per_play","no_huddle_rate",
        "offense_rank","off_pass_rank","off_run_rank","off_scoring_rank","off_pace_rank",
        "off_success_rank","off_epa_rank","off_explosive_pass_rank","off_explosive_run_rank",
        "ol_pass_pro_rank","ol_run_block_rank","wr_unit_rank","rb_unit_rank","qb_unit_rank"
    ]
    for k in team_keys:
        if row.get(k) in [None, ""] and team_ctx.get(k) not in [None, ""]:
            row[k]=team_ctx.get(k)
    for src, dst in [
        ("current_plays_pg", "plays_pg"),
        ("current_pass_rate", "pass_rate"),
        ("current_rush_rate", "rush_rate"),
        ("pbp_plays_pg", "pbp_plays_pg"),
        ("pbp_pass_rate", "pbp_pass_rate"),
        ("pbp_rush_rate", "pbp_rush_rate"),
    ]:
        if current_team_ctx.get(src) not in [None, ""]:
            row[dst]=current_team_ctx.get(src)
            row["has_current_team_context"] = True
    for k in team_keys:
        if current_team_ctx.get(k) not in [None, ""]:
            row[k]=current_team_ctx.get(k)
            row["has_current_team_context"] = True

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
    if isinstance(inj, dict):
        for k in ["practice_status","injury_note","body_part","limited_snap_risk","expected_snap_share"]:
            if inj.get(k) not in [None, ""]:
                row[k]=inj.get(k)

    final_inactives=_lookup_final_inactives(row)
    for k,v in final_inactives.items():
        if v not in [None, ""]:
            row[k]=v

    depth=load_depth_chart_bank().get(norm(row.get("player")), {})
    for k,v in depth.items():
        if v not in [None, ""]:
            row[k]=v
            row["has_depth_chart_context"] = True

    weather_ctx=_lookup_weather_for_row(row)
    if weather_ctx:
        risk, pass_factor, weather_notes = _weather_risk_from_detail(weather_ctx)
        for k,v in weather_ctx.items():
            if v not in [None, ""]:
                row[f"weather_{k}"]=v
        if risk and risk != "LOW":
            row["weather_risk"]=risk
        row["weather_pass_factor"]=pass_factor
        row["weather_notes"]=weather_notes

    market_bank=load_market_context_bank()
    market=market_bank.get((norm(row.get("player")), str(row.get("prop") or ""), team)) or market_bank.get((norm(row.get("player")), str(row.get("prop") or ""), ""))
    if market:
        for k,v in market.items():
            if v not in [None, ""]:
                row[f"market_{k}"]=v
        row["has_market_context"] = True

    travel_ctx=_lookup_pair_context(load_travel_context_bank(), row)
    if travel_ctx:
        for k,v in travel_ctx.items():
            if v not in [None, ""]:
                row[k]=v
                row["has_travel_context"] = True
    elif team and opp:
        miles=great_circle_miles(team, opp)
        if miles:
            row["travel_miles"]=miles
            row["has_travel_context"] = True

    matchup_ctx=_lookup_pair_context(load_matchup_context_bank(), row)
    for k,v in matchup_ctx.items():
        if v not in [None, ""]:
            row[k]=v
            row["has_matchup_context"] = True

    qb_ctx=_lookup_qb_context(row)
    for k,v in qb_ctx.items():
        if v not in [None, ""]:
            row[k if str(k).startswith("qb_") else f"qb_{k}"]=v
            row["has_qb_context"] = True

    def_inj=load_defensive_injury_context()
    opp_def=def_inj.get(opp) if isinstance(def_inj, dict) else None
    if isinstance(opp_def, dict):
        for k,v in opp_def.items():
            if v not in [None, ""]:
                row[f"opp_def_injury_{k}"]=v
                row["has_defensive_injury_context"] = True

    splits=_lookup_player_prop_context(load_splits_context_bank(), row)
    for k,v in splits.items():
        if v not in [None, ""]:
            row[f"split_{k}" if k not in ["player","prop","team"] else k]=v
            row["has_splits_context"] = True

    personnel=_lookup_pair_context(load_personnel_context_bank(), row)
    for k,v in personnel.items():
        if v not in [None, ""]:
            row[f"personnel_{k}" if k not in ["team","opp","matchup"] else k]=v
            row["has_personnel_context"] = True
    manual_override=_lookup_manual_override(row)
    for k,v in manual_override.items():
        if v not in [None, ""]:
            row[k]=v
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
    expected_snap=safe_float(row.get("expected_snap_share"))
    if expected_snap is not None:
        role["snap"]=float(clamp(expected_snap,0,100))
    expected_routes=safe_float(row.get("expected_routes"))
    if expected_routes is not None:
        dropbacks=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
        pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
        route_share=100*expected_routes/max(1.0, dropbacks*pass_rate/100.0)
        role["route"]=float(clamp(route_share,0,100))
    depth_rank=safe_float(row.get("depth_rank"))
    starter=str(row.get("starter") or "").upper()
    if depth_rank is not None and depth_rank >= 3 and starter not in ["YES","TRUE","1","STARTER"]:
        role["snap"]*=0.88
        role["route"]*=0.90
    ol=safe_float(row.get("ol_rank"))
    if ol is not None:
        # Lower rank is better; convert to 0-100 protection score.
        role["ol"]=float(clamp(74 - (ol-1)*1.5, 22, 78))
    return role

def _first_numeric(row, keys, default=None):
    for key in keys:
        v=safe_float((row or {}).get(key))
        if v is not None:
            return v
    return default

def _weighted_current_metric(row, season_keys, last5_keys, last3_keys):
    season=_first_numeric(row, season_keys)
    l5=_first_numeric(row, last5_keys)
    l3=_first_numeric(row, last3_keys)
    games=int(safe_float((row or {}).get("current_games"),0) or 0)
    vals=[]
    if season is not None: vals.append((season,0.50 if games>=5 else 0.65))
    if l5 is not None: vals.append((l5,0.30 if games>=5 else 0.20))
    if l3 is not None: vals.append((l3,0.20 if games>=5 else 0.15))
    if not vals: return None
    total=sum(w for _,w in vals)
    return sum(v*w for v,w in vals)/max(total,1e-9)

def current_week_role_engine(row, role, prop):
    """Blend season role with L5/L3 usage, depth chart, QB and OL changes."""
    row=row or {}; role=dict(role or {}); notes=[]; factor=1.0; risk="LOW"
    metrics={}
    metric_specs={
        "snap":(["current_snap_share","snap_share"],["last5_snap_share","l5_snap_share"],["last3_snap_share","l3_snap_share"]),
        "route":(["current_route_participation","route_participation"],["last5_route_participation","l5_route_participation"],["last3_route_participation","l3_route_participation"]),
        "target":(["current_target_share","target_share"],["last5_target_share","l5_target_share"],["last3_target_share","l3_target_share"]),
        "carry":(["current_carries_share","carries_share","carry_share"],["last5_carries_share","last5_carry_share"],["last3_carries_share","last3_carry_share"]),
        "rz":(["current_red_zone_touch_share","red_zone_touch_share"],["last5_red_zone_touch_share"],["last3_red_zone_touch_share"]),
    }
    for dst,(season_keys,l5_keys,l3_keys) in metric_specs.items():
        val=_weighted_current_metric(row,season_keys,l5_keys,l3_keys)
        if val is not None:
            old=safe_float(role.get(dst),val) or val
            role[dst]=float(clamp(val,0,100)); metrics[dst]=round(val,2)
            if abs(val-old)>=8: notes.append(f"Current-week {dst} role moved {val-old:+.1f} pts")

    volume_specs={
        "pass":(["current_pass_attempts_pg","pass_attempts_pg"],["last5_pass_attempts_pg"],["last3_pass_attempts_pg"]),
        "targets":(["current_targets_pg","targets_pg"],["last5_targets_pg"],["last3_targets_pg"]),
        "carries":(["current_rush_attempts_pg","rush_attempts_pg"],["last5_rush_attempts_pg"],["last3_rush_attempts_pg"]),
    }
    volumes={k:_weighted_current_metric(row,*spec) for k,spec in volume_specs.items()}
    metrics.update({k:round(v,2) for k,v in volumes.items() if v is not None})
    if prop in ["Passing Yards","Passing TDs","Interceptions","Pass Attempts","Completions"]:
        season=_first_numeric(row,["current_pass_attempts_pg","pass_attempts_pg"])
        if volumes.get("pass") is not None and season and season>0:
            factor*=clamp(volumes["pass"]/season,0.90,1.10)
    elif prop in ["Receiving Yards","Receptions","Longest Reception"]:
        season=_first_numeric(row,["current_targets_pg","targets_pg"])
        if volumes.get("targets") is not None and season and season>0:
            factor*=clamp(volumes["targets"]/season,0.88,1.12)
    elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        season=_first_numeric(row,["current_rush_attempts_pg","rush_attempts_pg"])
        if volumes.get("carries") is not None and season and season>0:
            factor*=clamp(volumes["carries"]/season,0.86,1.14)
    elif prop in ["Fantasy Points","Anytime TD"]:
        parts=[]
        for key,base_keys in [("targets",["current_targets_pg","targets_pg"]),("carries",["current_rush_attempts_pg","rush_attempts_pg"])]:
            base=_first_numeric(row,base_keys)
            if volumes.get(key) is not None and base and base>0: parts.append(clamp(volumes[key]/base,0.88,1.12))
        if parts: factor*=sum(parts)/len(parts)

    depth=safe_float(row.get("depth_rank")); prev_depth=safe_float(row.get("previous_depth_rank"))
    starter=str(row.get("starter") or "").upper()
    if depth is not None:
        if prev_depth is not None and depth < prev_depth: factor*=1.025; notes.append("Depth-chart promotion")
        if depth >= 3 and starter not in ["TRUE","YES","1","STARTER"]: factor*=0.90; risk="HIGH"; notes.append("Depth-chart demotion/rotation risk")
    qb_change=str(row.get("qb_change_risk") or row.get("qb_status") or "").upper()
    if prop in ["Receiving Yards","Receptions","Longest Reception","Fantasy Points"]:
        if any(x in qb_change for x in ["HIGH","BACKUP","CHANGE","UNCERTAIN"]): factor*=0.94; risk="HIGH"; notes.append("Starting-QB change tax")
    ol_out=max(safe_float(row.get("starting_ol_out"),0) or 0,safe_float(row.get("ol_starters_out"),0) or 0)
    if ol_out and prop in ["Passing Yards","Pass Attempts","Completions","Rushing Yards","Rush Attempts"]:
        factor*=clamp(1-0.025*ol_out,0.90,1.0); notes.append(f"Offensive-line availability tax ({int(ol_out)} starters out)")
        if ol_out>=2: risk="HIGH"
    limited=safe_float(row.get("limited_snap_risk"),0) or 0
    if limited>0: factor*=clamp(1-limited*0.18,0.84,1.0)
    role_bucket=projection_role_bucket(row,role)
    has_recent=any(_first_numeric(row,[k]) is not None for k in ["last3_pass_attempts_pg","last3_targets_pg","last3_rush_attempts_pg","last3_snap_share","last5_snap_share"])
    if has_recent: notes.append("L3/L5 current-week role blend active")
    return role, {"factor":float(clamp(factor,0.84,1.16)),"risk":risk,"notes":notes,"metrics":metrics,"role_bucket":role_bucket,"active":bool(metrics or notes)}

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
    bonus=sum(1 for k in ["air_yards_share","red_zone_touch_share","spread","game_total","def_role_rank","coverage_grade","has_current_usage","has_depth_chart_context","has_market_context"] if safe_float(row.get(k)) is not None or row.get(k) is True)
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

def offense_defense_rank_factor(row, prop):
    """Compare offensive rank vs opponent defensive rank.

    Rank convention: 1 is best. Positive edge means the offense has an advantage
    relative to the opponent defense. Effects are intentionally bounded and differ
    by prop type so QB/RB/WR are not all treated the same.
    """
    row=row or {}
    notes=[]; risk="LOW"; ctx={}
    def _rank(*keys):
        for k in keys:
            v=safe_float(row.get(k))
            if v is not None:
                return float(clamp(v, 1, 32))
        return None

    if prop in ["Passing Yards", "Pass Attempts", "Completions"]:
        off=_rank("off_pass_rank", "off_epa_rank", "offense_rank")
        deff=_rank("def_pass_rank", "opp_def_pass_rank")
        ol=_rank("ol_pass_pro_rank", "qb_pass_protection_rank")
        pressure=_rank("opp_def_pressure_rank", "def_pressure_rank")
        label="QB/pass offense vs pass defense"
        cap=(0.955, 1.055) if prop in ["Pass Attempts", "Completions"] else (0.94, 1.07)
    elif prop in ["Receiving Yards", "Receptions"]:
        off=_rank("off_pass_rank", "wr_unit_rank", "off_epa_rank", "offense_rank")
        deff=_rank("def_role_rank", "opp_def_role_rank", "def_pass_rank", "opp_def_pass_rank")
        ol=_rank("ol_pass_pro_rank", "qb_pass_protection_rank")
        pressure=_rank("opp_def_pressure_rank", "def_pressure_rank")
        label="receiver/pass offense vs coverage defense"
        cap=(0.945, 1.065) if prop == "Receiving Yards" else (0.955, 1.05)
    elif prop in ["Rushing Yards", "Rush Attempts"]:
        off=_rank("off_run_rank", "ol_run_block_rank", "offense_rank")
        deff=_rank("def_run_rank", "opp_def_run_rank", "def_run_stop_rank", "opp_def_run_stop_rank")
        ol=_rank("ol_run_block_rank", "run_block_rank")
        pressure=None
        label="rush offense vs run defense"
        cap=(0.94, 1.07) if prop == "Rushing Yards" else (0.955, 1.055)
    else:
        return 1.0, "LOW", [], {"active": False}

    if off is None or deff is None:
        return 1.0, "LOW", ["Offense-vs-defense rank missing"], {"active": False, "off_rank": off, "def_rank": deff, "label": label}

    # Lower offensive rank is better; higher defensive rank is easier. Large positive edge is good.
    edge=(deff - off)
    factor=1.0 + edge*0.0032
    if ol is not None and pressure is not None and prop in ["Passing Yards", "Pass Attempts", "Completions", "Receiving Yards", "Receptions"]:
        trench_edge=pressure - ol
        factor += trench_edge*0.0012
        ctx["pass_pro_vs_pressure_edge"]=round(trench_edge,2)
    factor=clamp(factor, cap[0], cap[1])
    if edge >= 12:
        notes.append(f"{label}: offense edge")
    elif edge <= -12:
        risk="MED"; notes.append(f"{label}: defense edge")
    else:
        notes.append(f"{label}: neutral")
    ctx.update({
        "active": True,
        "label": label,
        "off_rank": round(off,2),
        "def_rank": round(deff,2),
        "rank_edge": round(edge,2),
        "factor": round(factor,3),
        "prop": prop,
    })
    return factor, risk, notes, ctx

def game_environment_factor(row, prop):
    factor=1.0; notes=[]; risk="LOW"
    spread=safe_float(row.get("spread"))
    total=safe_float(row.get("game_total"))
    pace=safe_float(row.get("pace"))
    pass_rate=safe_float(row.get("pass_rate"))
    weather=str(row.get("weather_risk") or "").upper()
    weather_pass_factor=safe_float(row.get("weather_pass_factor"))
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
    if weather_pass_factor is not None and prop in ["Passing Yards","Receiving Yards","Receptions","Passing TDs","Pass Attempts","Completions","Longest Reception"]:
        factor*=clamp(weather_pass_factor,0.86,1.03)
        notes.extend(row.get("weather_notes") or [])
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
    practice=str(row.get("practice_status") or "").upper()
    depth_rank=safe_float(row.get("depth_rank"))
    role_note=str(row.get("role_note") or row.get("injury_note") or "")

    if "OUT" in injury or "DOUBTFUL" in injury:
        risk_factor*=0.70; injury_risk="EXTREME"; notes.append("Injury status blocks official play")
    elif "QUESTION" in injury or "LIMIT" in injury:
        risk_factor*=0.88; injury_risk="HIGH"; notes.append("Questionable/limited role risk")
    if any(x in practice for x in ["DNP", "NO PRACTICE", "LIMITED"]):
        risk_factor*=0.94; injury_risk="HIGH"; notes.append("Practice status role risk")
    limited_snap=safe_float(row.get("limited_snap_risk"))
    if limited_snap is not None and limited_snap >= 0.35:
        risk_factor*=0.92; injury_risk="HIGH"; notes.append("Limited snap-risk flag")
    if depth_rank is not None and depth_rank >= 3:
        risk_factor*=0.93; notes.append("Depth-chart role tax")
    if role_note:
        notes.append(str(role_note)[:120])

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

def simulate_prop_distribution(base, sigma, prop, sims, seed, collapse_prob=0.115, ceiling_prob=0.075, empirical_values=None):
    """Prop-specific NFL distributions with optional empirical game-log blending."""
    rng=np.random.default_rng(seed)
    base=max(0.001, safe_float(base,0.001) or 0.001)
    sigma=max(0.05, safe_float(sigma,1) or 1)
    count_props={"Passing TDs","Interceptions","Receptions","Pass Attempts","Completions","Rush Attempts","Field Goals Made","Tackles + Assists","Sacks"}
    yard_props={"Passing Yards","Receiving Yards","Rushing Yards","Longest Reception","Longest Rush","Fantasy Points","Kicking Points"}
    meta={"family":"normal","empirical_count":0,"empirical_weight":0.0}
    if prop == "Anytime TD":
        p=float(clamp(base,0.01,0.95)); sim=rng.binomial(1,p,sims).astype(float); meta["family"]="bernoulli"
    elif prop in count_props:
        variance=max(base+0.05, sigma*sigma)
        if variance <= base*1.08:
            sim=rng.poisson(base,sims).astype(float); meta["family"]="poisson"
        else:
            shape=max(0.25, base*base/max(0.05,variance-base))
            rate=rng.gamma(shape, base/shape, sims)
            sim=rng.poisson(rate).astype(float); meta.update({"family":"negative_binomial","shape":round(shape,3)})
    elif prop in yard_props:
        variance=sigma*sigma
        cv2=max(1e-6,variance/(base*base))
        log_s2=math.log1p(cv2); log_mu=math.log(base)-0.5*log_s2
        log_sample=rng.lognormal(log_mu,math.sqrt(log_s2),sims)
        gamma_shape=max(0.25,base*base/max(variance,1e-6)); gamma_scale=max(1e-6,variance/base)
        gamma_sample=rng.gamma(gamma_shape,gamma_scale,sims)
        choose=rng.random(sims)<0.58
        sim=np.where(choose,log_sample,gamma_sample)
        collapse_mask=rng.random(sims)<collapse_prob; ceiling_mask=rng.random(sims)<ceiling_prob
        sim[collapse_mask]*=rng.uniform(0.35,0.72,collapse_mask.sum())
        sim[ceiling_mask]*=rng.uniform(1.12,1.38,ceiling_mask.sum())
        meta.update({"family":"lognormal_gamma_mixture","gamma_shape":round(gamma_shape,3)})
    else:
        sim=np.clip(rng.normal(base,sigma,sims),0,None); meta["family"]="truncated_normal"

    empirical=[]
    for v in empirical_values or []:
        x=safe_float(v)
        if x is not None and x>=0: empirical.append(x)
    if len(empirical)>=5:
        arr=np.asarray(empirical[-17:],dtype=float); emp_mean=float(arr.mean())
        weight=float(clamp(0.25+0.035*len(arr),0.35,0.68))
        if prop == "Anytime TD":
            emp_p=float(np.clip(arr,0,1).mean())
            blended_p=float(clamp((1-weight)*base + weight*emp_p,0.01,0.95))
            sim=rng.binomial(1,blended_p,sims).astype(float)
            meta.update({"empirical_count":len(arr),"empirical_weight":round(weight,3),"empirical_mean":round(emp_p,3),"blended_probability":round(blended_p,3)})
        elif emp_mean>0:
            sampled=rng.choice(arr,size=sims,replace=True)*(base/emp_mean)
            sampled=np.clip(sampled+rng.normal(0,max(0.03,sigma*0.06),sims),0,None)
            mask=rng.random(sims)<weight; sim[mask]=sampled[mask]
            if prop in count_props: sim=np.rint(sim)
            meta.update({"empirical_count":len(arr),"empirical_weight":round(weight,3),"empirical_mean":round(emp_mean,3)})
    return np.clip(sim,0,None), meta



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

_EMPIRICAL_PROP_BANK_CACHE = {"sig":None,"bank":{}}

def _empirical_prop_bank():
    sig=_path_signature(PHASE6_PLAYER_LOG_FILE)
    if _EMPIRICAL_PROP_BANK_CACHE.get("sig")==sig:
        return _EMPIRICAL_PROP_BANK_CACHE.get("bank",{})
    df=_read_optional_csv(PHASE6_PLAYER_LOG_FILE)
    bank={}
    if not df.empty:
        player_col="player" if "player" in df.columns else "player_display_name" if "player_display_name" in df.columns else None
        if player_col:
            for player,g in df.groupby(player_col,dropna=False):
                pkey=norm(player)
                if not pkey: continue
                for prop in ACTIVE_NFL_MARKETS:
                    col=next((c for c in _prop_target_columns(prop) if c in g.columns),None)
                    if not col: continue
                    vals=pd.to_numeric(g[col],errors="coerce").dropna().tolist()
                    if len(vals)>=3: bank[(pkey,prop)]=[float(x) for x in vals[-20:]]
    _EMPIRICAL_PROP_BANK_CACHE.update({"sig":sig,"bank":bank})
    return bank

def empirical_values_for_row(row, prop):
    vals=[]
    for key in ["recent_results","game_log_values","last_games","recent_yards","prop_game_log"]:
        raw=(row or {}).get(key)
        if isinstance(raw,list):
            for item in raw:
                if isinstance(item,dict):
                    val=safe_float(item.get("actual"),safe_float(item.get("value"),safe_float(item.get("result"),safe_float(item.get("yards")))))
                else: val=safe_float(item)
                if val is not None: vals.append(val)
    if len(vals)>=5: return vals[-20:]
    return _empirical_prop_bank().get((norm((row or {}).get("model_player_match") or (row or {}).get("player")),prop),[])

XGB_FEATURE_KEYS = [
    "projection", "line", "edge", "fair_prob", "data_score", "stability_score", "usage_quality",
    "opportunity_score", "pace_factor", "vegas_factor", "game_script_factor", "matchup_factor",
    "blowout_prob", "collapse_prob", "ceiling_prob", "snap_share", "route_participation",
    "target_share", "air_yards_share", "red_zone_touch_share", "targets_pg", "receptions_pg",
    "rush_attempts_pg", "carries_share", "pass_attempts_pg", "spread", "game_total", "team_total",
    "plays_pg", "pbp_plays_pg", "pass_rate", "rush_rate", "def_pass_rank", "def_run_rank",
    "def_role_rank", "pressure_rate", "coverage_grade", "travel_miles", "rest_days",
    "opp_rest_days", "timezone_shift", "consecutive_road_games", "audit_score",
    "weather_pass_factor", "qb_pass_protection_rank", "opp_def_pressure_rank",
    "opp_def_injury_missing_cb_starters", "opp_def_injury_missing_safety_starters",
    "opp_def_injury_missing_edge_starters",
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

def travel_difficulty_score(row):
    row=row or {}
    miles=safe_float(row.get("travel_miles"), 0) or 0
    rest=safe_float(row.get("rest_days"))
    opp_rest=safe_float(row.get("opp_rest_days"))
    tz=safe_float(row.get("timezone_shift"), safe_float(row.get("time_zone_shift"), 0)) or 0
    consecutive_road=safe_float(row.get("consecutive_road_games"), 0) or 0
    international=str(row.get("international_game") or row.get("neutral_site") or "").upper() in ["TRUE","YES","1","INTERNATIONAL","LONDON","MUNICH","BRAZIL","MADRID"]
    early_body=str(row.get("body_clock_risk") or "").upper() in ["HIGH","WEST_TO_EAST_EARLY","EARLY"]
    altitude=safe_float((environment_for(row) or {}).get("altitude"), 0) or 0
    score=0
    notes=[]
    if miles >= 2200: score+=24; notes.append("Long travel distance")
    elif miles >= 1500: score+=16; notes.append("Moderate-long travel")
    elif miles >= 750: score+=8
    if rest is not None and rest <= 4: score+=18; notes.append("Short week")
    if rest is not None and opp_rest is not None and rest - opp_rest <= -2:
        score+=12; notes.append("Negative rest differential")
    if abs(tz) >= 3: score+=12; notes.append("Three-hour body-clock shift")
    elif abs(tz) >= 2: score+=8
    if consecutive_road >= 2: score+=10; notes.append("Second straight road game")
    if international: score+=16; notes.append("International/neutral-site travel")
    if early_body: score+=10; notes.append("Body-clock kickoff risk")
    if altitude >= 4000 and str(row.get("home_away") or "").upper() == "AWAY":
        score+=8; notes.append("Altitude road environment")
    label="HIGH" if score >= 42 else "MED" if score >= 22 else "LOW"
    return {"score": int(clamp(score,0,100)), "label": label, "notes": notes}

def split_personnel_factor(row, prop):
    row=row or {}
    factor=1.0; notes=[]; risk="LOW"; ctx={}
    env=environment_for(row) or {}
    roof=str(env.get("roof") or "").upper()
    surface=str(env.get("surface") or "").upper()
    home_away=str(row.get("home_away") or "").upper()
    if roof in ["DOME","RETRACTABLE","CANOPY"]:
        dome_split=safe_float(row.get("split_indoor_factor"), safe_float(row.get("split_dome_factor")))
        if dome_split is not None and prop in ["Passing Yards","Receiving Yards"]:
            f=clamp(dome_split,0.94,1.06); factor*=f; notes.append(f"Indoor/dome split x{f:.3f}")
    if surface:
        surf_key="split_turf_factor" if "TURF" in surface else "split_grass_factor"
        surf=safe_float(row.get(surf_key))
        if surf is not None:
            f=clamp(surf,0.95,1.05); factor*=f; notes.append(f"{surface.title()} split x{f:.3f}")
    ha_key="split_home_factor" if home_away == "HOME" else "split_away_factor"
    ha=safe_float(row.get(ha_key))
    if ha is not None:
        f=clamp(ha,0.95,1.05); factor*=f; notes.append(f"{home_away or 'site'} split x{f:.3f}")
    if prop in ["Passing Yards","Receiving Yards"]:
        if str(row.get("personnel_shadow_corner") or row.get("shadow_corner") or "").upper() in ["TRUE","YES","1"]:
            grade=safe_float(row.get("personnel_shadow_corner_grade"), safe_float(row.get("shadow_corner_grade"), 70)) or 70
            if grade >= 78:
                factor*=0.966; risk="MED"; notes.append("Elite shadow coverage tax")
            elif grade <= 55:
                factor*=1.014; notes.append("Weak shadow coverage boost")
        slot=safe_float(row.get("personnel_slot_weakness"), safe_float(row.get("slot_weakness")))
        te=safe_float(row.get("personnel_te_weakness"), safe_float(row.get("te_weakness")))
        rb_rec=safe_float(row.get("personnel_rb_receiving_weakness"), safe_float(row.get("rb_receiving_weakness")))
        pos=str(row.get("position") or "").upper()
        if pos == "WR" and slot is not None and slot >= 1:
            factor*=1.012; notes.append("Slot matchup weakness boost")
        if pos == "TE" and te is not None and te >= 1:
            factor*=1.014; notes.append("TE coverage weakness boost")
        if pos == "RB" and rb_rec is not None and rb_rec >= 1:
            factor*=1.012; notes.append("RB receiving matchup boost")
    ctx["split_personnel_factor"]=round(factor,3)
    return clamp(factor,0.90,1.08), risk, notes, ctx

def calibrated_sigma(prop, base_sigma, row, usage_quality, injury_risk, game_script_risk, advanced_context):
    sigma=safe_float(base_sigma, 1.0) or 1.0
    pos=str((row or {}).get("position") or "").upper()
    if prop == "Rushing Yards" and pos in ["RB","QB"]:
        sigma*=0.96 if safe_float((row or {}).get("rush_attempts_pg"), safe_float((row or {}).get("current_rush_attempts_pg"), 0)) and safe_float((row or {}).get("rush_attempts_pg"), safe_float((row or {}).get("current_rush_attempts_pg"), 0)) >= 15 else 1.05
    if prop == "Receiving Yards":
        route=safe_float((row or {}).get("route_participation"))
        target=safe_float((row or {}).get("target_share"))
        if route is not None and route >= 82 and target is not None and target >= 22:
            sigma*=0.96
        elif route is not None and route < 65:
            sigma*=1.08
    if str((row or {}).get("rookie_flag") or "").upper() in ["TRUE","YES","1"]:
        sigma*=1.06
    if str((row or {}).get("qb_change_risk") or (row or {}).get("qb_qb_change_risk") or "").upper() == "HIGH":
        sigma*=1.08
    if injury_risk in ["HIGH","EXTREME"]: sigma*=1.12
    if game_script_risk=="HIGH": sigma*=1.08
    if usage_quality < 72: sigma*=1.07
    if (advanced_context or {}).get("consistency_score", 70) <= 48: sigma*=1.06
    return sigma


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
    travel_diff=travel_difficulty_score(row)
    ctx["travel_difficulty"] = travel_diff
    if travel_diff["label"] == "HIGH":
        factor*=0.978; risk="MED"; notes.append("Travel difficulty: HIGH")
    elif travel_diff["label"] == "MED":
        factor*=0.99; notes.append("Travel difficulty: MED")
    notes.extend(travel_diff.get("notes", [])[:3])

    if str(row.get("divisional_game") or "").upper() in ["TRUE","YES","1"]:
        if prop in ["Passing Yards","Receiving Yards"]:
            factor*=0.992; notes.append("Divisional familiarity explosive-play tax")
    if str(row.get("rematch_game") or "").upper() in ["TRUE","YES","1"]:
        if prop in ["Passing Yards","Receiving Yards"]:
            factor*=0.994; notes.append("Same-season rematch familiarity tax")
    if str(row.get("pass_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]:
        if prop in ["Passing Yards","Receiving Yards"]:
            factor*=1.014; notes.append("Pass-funnel matchup boost")
    if str(row.get("run_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]:
        if prop in ["Passing Yards","Receiving Yards"]:
            factor*=0.992; notes.append("Run-funnel pass-volume tax")
    protection=safe_float(row.get("qb_pass_protection_rank"), safe_float(row.get("ol_pass_pro_rank"), safe_float(row.get("ol_rank"))))
    opp_pressure=safe_float(row.get("opp_def_pressure_rank"), safe_float(row.get("def_pressure_rank")))
    if protection is not None and opp_pressure is not None:
        ctx["protection_pressure_matchup"] = {"pass_pro_rank": protection, "opp_pressure_rank": opp_pressure}
        if protection >= 24 and opp_pressure <= 8 and prop in ["Passing Yards","Receiving Yards"]:
            factor*=0.974; risk="MED"; notes.append("Protection mismatch: weak OL vs strong pressure")
        elif protection <= 8 and opp_pressure >= 24 and prop in ["Passing Yards","Receiving Yards"]:
            factor*=1.012; notes.append("Protection edge: strong OL vs weak pressure")

    qb_status=str(row.get("qb_status") or row.get("qb_injury_status") or "").upper()
    qb_change=str(row.get("qb_change_risk") or row.get("qb_qb_change_risk") or "").upper()
    if row.get("prop") == "Receiving Yards":
        if any(x in qb_status for x in ["OUT","DOUBTFUL","BACKUP"]) or qb_change in ["HIGH","BACKUP"]:
            factor*=0.925; risk="HIGH"; notes.append("Receiver QB dependency tax")
        elif any(x in qb_status for x in ["QUESTION","LIMIT"]):
            factor*=0.965; risk="MED"; notes.append("QB limitation receiver tax")
    missing_cb=safe_float(row.get("opp_def_injury_missing_cb_starters"), 0) or 0
    missing_safety=safe_float(row.get("opp_def_injury_missing_safety_starters"), 0) or 0
    missing_edge=safe_float(row.get("opp_def_injury_missing_edge_starters"), 0) or 0
    if prop in ["Passing Yards","Receiving Yards"] and (missing_cb + missing_safety) >= 1:
        factor*=1.012 + min(0.018, 0.006*(missing_cb+missing_safety)); notes.append("Opponent secondary injury boost")
    if prop == "Passing Yards" and missing_edge >= 1:
        factor*=1.008; notes.append("Opponent pass-rush injury boost")

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

    opp_recent=opponent_adjusted_recent_context(row, prop)
    ctx["opponent_adjusted_recent"] = opp_recent
    if opp_recent.get("active"):
        factor*=safe_float(opp_recent.get("factor"), 1.0) or 1.0
        notes.extend(opp_recent.get("notes") or [])

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

def opponent_adjusted_recent_context(row, prop):
    """Small recent-form layer adjusted for opponent difficulty.

    It uses saved weekly logs when available, then compares recent output to the
    player's baseline and opponent rank. The cap is intentionally tight because
    current usage, role, market, and matchup already have their own model layers.
    """
    hist=_historical_player_values((row or {}).get("player"), prop, max_games=8)
    if len(hist) < 3:
        return {"active": False, "factor": 1.0, "games": len(hist), "notes": [f"Opponent-adjusted form warming up: {len(hist)}/3 logs"]}
    recent=hist[-min(5, len(hist)):]
    recent_mean=float(np.mean(recent))
    baseline_keys={
        "Passing Yards": ["current_passing_yards_pg","last5_passing_yards_pg","passing_yards_pg"],
        "Receiving Yards": ["current_receiving_yards_pg","last5_receiving_yards_pg","receiving_yards_pg"],
        "Rushing Yards": ["current_rushing_yards_pg","last5_rushing_yards_pg","rushing_yards_pg"],
    }.get(prop, [])
    baseline=None
    for k in baseline_keys:
        baseline=safe_float((row or {}).get(k))
        if baseline and baseline > 0:
            break
    if not baseline:
        baseline=safe_float((row or {}).get("line"), recent_mean) or recent_mean
    recent_ratio=clamp(recent_mean/max(1.0, baseline), 0.84, 1.16)

    rank_key="def_pass_rank" if prop == "Passing Yards" else "def_run_rank" if prop == "Rushing Yards" else "def_role_rank"
    rank=safe_float((row or {}).get(rank_key), safe_float((row or {}).get("opp_"+rank_key)))
    opp_factor=1.0
    if rank is not None:
        # Rank 32 is weakest defense, rank 1 is toughest. Keep impact modest.
        opp_factor=clamp(1.0 + ((rank - 16.5) / 16.5) * 0.035, 0.955, 1.045)
    factor=clamp((recent_ratio * 0.62) + (opp_factor * 0.38), 0.94, 1.06)
    notes=[f"Opponent-adjusted recent form x{factor:.3f}: recent {recent_mean:.1f} vs base {baseline:.1f}"]
    if rank is not None:
        notes.append(f"Opponent rank context: {rank_key} {rank:g}")
    return {
        "active": True,
        "factor": round(factor, 3),
        "games": len(hist),
        "recent_mean": round(recent_mean, 2),
        "baseline": round(float(baseline), 2),
        "recent_ratio": round(recent_ratio, 3),
        "rank_key": rank_key,
        "rank": rank,
        "opponent_factor": round(opp_factor, 3),
        "notes": notes,
    }

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



def passing_yards_stat_projection(row, role, cfg):
    """Passing Yards projection built from QB history + opportunity + matchup.

    Uses the saved Phase 6/nflverse database when available:
    - last season passing yards per game
    - pass attempts per game
    - estimated yards per attempt
    - team pass rate / expected plays
    - opponent pass defense rank
    - spread / total / stadium/weather

    This replaces line-driven projection behavior for Passing Yards so the model
    projects from football inputs first, then only lightly checks the market line.
    """
    row = enrich_passing_yards_context(dict(row or {}))
    notes = []
    breakdown = {}

    player_ypg = safe_float(row.get("passing_yards_pg"))
    attempts_pg = safe_float(row.get("pass_attempts_pg"))
    completions_pg = safe_float(row.get("completions_pg"))
    team_pass_att = safe_float(row.get("team_pass_attempts_pg"), safe_float(row.get("pass_attempts_team_pg")))
    team_plays = safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    pass_rate = safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
    spread = safe_float(row.get("spread"), 0) or 0
    total = safe_float(row.get("game_total"), 44) or 44
    line = safe_float(row.get("line"))
    if row.get("model_match_status") == "NO_MODEL_MATCH":
        notes.append("NO MODEL MATCH: using capped fallback until QB exists in Phase 6/context bank")
    elif row.get("model_player_match"):
        notes.append(f"Model match: {row.get('model_player_match')} ({row.get('passing_context_bank_source')})")

    # Phase 6 player summary is the preferred base. Fallbacks keep the app usable
    # when the QB is missing a prior sample, but the card will show that clearly.
    if player_ypg is None or player_ypg <= 0:
        player_ypg = safe_float(row.get("avg_pass_yards_pg"), None)
    if attempts_pg is None or attempts_pg <= 0:
        attempts_pg = safe_float(row.get("attempts_pg"), None)
    if attempts_pg is None or attempts_pg <= 0:
        attempts_pg = team_pass_att if team_pass_att and team_pass_att > 0 else 33.5
        notes.append("QB pass attempts fallback used")
    if player_ypg is None or player_ypg <= 0:
        # Fallback is intentionally near market but not equal to market; this prevents
        # blind 300+ projections when historical data is missing.
        player_ypg = (line * 0.96) if line and _valid_market_line("Passing Yards", line) else cfg.get("base", 235)
        notes.append("QB pass yards fallback used")
    current_games=safe_float(row.get("current_games"), 0) or 0
    cur_ypg=safe_float(row.get("current_passing_yards_pg"))
    last3_ypg=safe_float(row.get("last3_passing_yards_pg"))
    cur_att=safe_float(row.get("current_pass_attempts_pg"))
    last3_att=safe_float(row.get("last3_pass_attempts_pg"))
    if current_games >= 2 and cur_ypg and cur_ypg > 0:
        recent_blend=0.22 if current_games < 5 else 0.34
        form_ypg=(cur_ypg*0.65 + (last3_ypg or cur_ypg)*0.35)
        player_ypg=(player_ypg*(1-recent_blend))+(form_ypg*recent_blend)
        notes.append(f"Current-season QB form blend active ({int(current_games)} games)")
    if current_games >= 2 and cur_att and cur_att > 0:
        recent_blend=0.24 if current_games < 5 else 0.36
        form_att=(cur_att*0.65 + (last3_att or cur_att)*0.35)
        attempts_pg=(attempts_pg*(1-recent_blend))+(form_att*recent_blend)

    ypa = safe_float(row.get("yards_per_attempt"), None)
    if ypa is None or ypa <= 0:
        ypa = player_ypg / max(1.0, attempts_pg)
    ypa = clamp(ypa, 5.0, 9.4)

    # Project attempts from team pace/pass rate and QB baseline attempts.
    pace_attempts = team_plays * pass_rate / 100.0
    expected_attempts = (attempts_pg * 0.62) + (pace_attempts * 0.38)

    # Game script: underdogs throw more; large favorites may lose late pass volume.
    script_attempt_factor = 1.0
    if spread >= 6:
        script_attempt_factor += 0.055
        notes.append("Passing volume boost: projected trailing script")
    elif spread >= 3:
        script_attempt_factor += 0.025
    elif spread <= -9:
        script_attempt_factor -= 0.060
        notes.append("Passing volume tax: blowout/favorite script")
    elif spread <= -5.5:
        script_attempt_factor -= 0.025

    total_factor = clamp(1 + (total - 44) * 0.006, 0.94, 1.07)
    pass_rate_factor = clamp(1 + (pass_rate - 56) * 0.0045, 0.93, 1.08)

    env = environment_for(row)
    stadium_factor = 1.0
    if env.get("roof") in ["Dome", "Retractable"]:
        stadium_factor *= 1.018
        notes.append("Dome/retractable roof passing nudge")
    if str(row.get("weather_risk") or "").upper() in ["HIGH", "SEVERE", "WIND", "RAIN", "SNOW"]:
        stadium_factor *= 0.925
        notes.append("Weather passing tax")
    weather_pass_factor=safe_float(row.get("weather_pass_factor"))
    if weather_pass_factor is not None:
        stadium_factor *= clamp(weather_pass_factor,0.86,1.03)
        notes.extend(row.get("weather_notes") or [])
    if (str(row.get("home_away") or "").upper() == "AWAY") and env.get("crowd") in ["LOUD", "EXTREME"]:
        stadium_factor *= 0.988
        notes.append("Road crowd communication tax")

    # Opponent pass defense: rank 1 is tough, rank 32 is weak.
    opp_rank = safe_float(row.get("def_pass_rank"), safe_float(row.get("opp_def_pass_rank")))
    if opp_rank is None:
        matchup_factor = 1.0
        notes.append("Opponent pass defense rank missing")
    else:
        matchup_factor = clamp(1 + (opp_rank - 16.5) * 0.0065, 0.90, 1.105)
        if opp_rank <= 8:
            notes.append("Top pass defense tax")
        elif opp_rank >= 25:
            notes.append("Weak pass defense boost")

    pressure = safe_float(row.get("pressure_rate"), safe_float(row.get("opp_def_pressure_rate")))
    pressure_factor = 1.0
    if pressure is not None:
        # Higher pressure suppresses efficiency more than attempts.
        pressure_factor = clamp(1 - (pressure - 24) * 0.0035, 0.94, 1.04)

    projected_attempts = expected_attempts * script_attempt_factor * pass_rate_factor
    attempt_model = projected_attempts * ypa * total_factor * stadium_factor * matchup_factor * pressure_factor
    history_model = player_ypg * total_factor * stadium_factor * matchup_factor * pressure_factor
    projection = (history_model * 0.55) + (attempt_model * 0.45)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Passing Yards", consensus):
        projection=(projection*0.84)+(consensus*0.16)
        notes.append("Market consensus blend active")

    # A final realism guard before market sanity. Passing yards game projections
    # generally should not be extreme unless the live line/team context justifies it.
    if line is not None and _valid_market_line("Passing Yards", line):
        projection = clamp(projection, line - 42, line + 42)
    projection = clamp(projection, 90, 390)

    breakdown = {
        "player_pass_ypg": round(player_ypg, 2),
        "pass_attempts_pg": round(attempts_pg, 2),
        "projected_attempts": round(projected_attempts, 2),
        "yards_per_attempt": round(ypa, 3),
        "attempt_model": round(attempt_model, 2),
        "history_model": round(history_model, 2),
        "team_pass_rate": round(pass_rate, 2),
        "team_plays_pg": round(team_plays, 2),
        "opponent_pass_def_rank": None if opp_rank is None else int(round(opp_rank)),
        "game_total": round(total, 2),
        "spread": round(spread, 2),
        "matchup_factor": round(matchup_factor, 3),
        "stadium_factor": round(stadium_factor, 3),
        "total_factor": round(total_factor, 3),
        "pressure_factor": round(pressure_factor, 3),
        "final_pre_market": round(projection, 2),
        "context_source": row.get("passing_context_bank_source"),
        "model_match_status": row.get("model_match_status"),
        "model_player_match": row.get("model_player_match"),
    }
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}


def receiving_yards_stat_projection(row, role, cfg):
    """Receiving Yards projection from receiver history + targets × yards/target.

    Works for WR + TE and RB receiving-yard markets. Uses last-season receiving
    yards/game, targets/game, yards/target, team pass rate/plays, opponent pass/role
    defense, spread/total, stadium/weather, then Monte Carlo runs downstream.
    """
    row = enrich_receiving_yards_context(dict(row or {}))
    notes=[]; breakdown={}
    line=safe_float(row.get("line"))
    rec_ypg=safe_float(row.get("receiving_yards_pg"))
    targets_pg=safe_float(row.get("targets_pg"))
    receptions_pg=safe_float(row.get("receptions_pg"))
    air_pg=safe_float(row.get("air_yards_pg"))
    ypt=safe_float(row.get("yards_per_target"))
    pos=str(row.get("position") or "").upper()
    team_plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
    spread=safe_float(row.get("spread"), 0) or 0
    total=safe_float(row.get("game_total"), 44) or 44
    target_share=safe_float(row.get("target_share"), None)
    route_part=safe_float(row.get("route_participation"), None)
    if row.get("model_match_status") == "NO_MODEL_MATCH":
        notes.append("NO MODEL MATCH: using capped receiving fallback until player exists in Phase 6/context bank")
    elif row.get("model_player_match"):
        notes.append(f"Model match: {row.get('model_player_match')} ({row.get('receiving_context_bank_source')})")
    if targets_pg is None or targets_pg <= 0:
        # Estimate from team pass volume and role when target data is missing.
        implied_team_attempts=team_plays * pass_rate/100.0
        if target_share is not None and target_share > 0:
            targets_pg = implied_team_attempts * (target_share/100.0)
            notes.append("Targets estimated from target share")
        else:
            targets_pg = 7.2 if pos=="WR" else 5.4 if pos=="TE" else 3.8
            notes.append("Targets fallback used")
    if rec_ypg is None or rec_ypg <= 0:
        rec_ypg = (line * 0.96) if line and _valid_market_line("Receiving Yards", line) else cfg.get("base", 52)
        notes.append("Receiving yards fallback used")
    current_games=safe_float(row.get("current_games"), 0) or 0
    cur_rec_ypg=safe_float(row.get("current_receiving_yards_pg"))
    last3_rec_ypg=safe_float(row.get("last3_receiving_yards_pg"))
    cur_targets=safe_float(row.get("current_targets_pg"))
    last3_targets=safe_float(row.get("last3_targets_pg"))
    if current_games >= 2 and cur_rec_ypg and cur_rec_ypg > 0:
        recent_blend=0.24 if current_games < 5 else 0.38
        form_ypg=(cur_rec_ypg*0.62 + (last3_rec_ypg or cur_rec_ypg)*0.38)
        rec_ypg=(rec_ypg*(1-recent_blend))+(form_ypg*recent_blend)
        notes.append(f"Current-season receiver form blend active ({int(current_games)} games)")
    if current_games >= 2 and cur_targets and cur_targets > 0:
        recent_blend=0.28 if current_games < 5 else 0.42
        form_targets=(cur_targets*0.62 + (last3_targets or cur_targets)*0.38)
        targets_pg=(targets_pg*(1-recent_blend))+(form_targets*recent_blend)
    if ypt is None or ypt <= 0:
        ypt = rec_ypg/max(1.0, targets_pg)
    ypt = clamp(ypt, 4.0 if pos=="RB" else 5.0, 12.8 if pos!="RB" else 10.5)
    implied_team_attempts=team_plays * pass_rate/100.0
    expected_targets=(targets_pg*0.68)
    if target_share is not None and target_share > 0:
        expected_targets += (implied_team_attempts * target_share/100.0)*0.32
    else:
        expected_targets += targets_pg*0.32
    if route_part is not None:
        expected_targets *= clamp(0.90 + (route_part/100.0)*0.14, 0.90, 1.04)
    # Game script and totals.
    script_factor=1.0
    if spread >= 6:
        script_factor += 0.050; notes.append("Receiving volume boost: projected trailing script")
    elif spread >= 3:
        script_factor += 0.025
    elif spread <= -9:
        script_factor -= 0.050; notes.append("Receiving volume tax: blowout/favorite script")
    elif spread <= -5.5:
        script_factor -= 0.022
    total_factor=clamp(1 + (total-44)*0.006, 0.94, 1.07)
    pass_rate_factor=clamp(1 + (pass_rate-56)*0.004, 0.94, 1.07)
    env=environment_for(row)
    stadium_factor=1.0
    if env.get("roof") in ["Dome","Retractable"]:
        stadium_factor*=1.014; notes.append("Dome/retractable roof receiving nudge")
    if str(row.get("weather_risk") or "").upper() in ["HIGH","SEVERE","WIND","RAIN","SNOW"]:
        stadium_factor*=0.93; notes.append("Weather receiving tax")
    weather_pass_factor=safe_float(row.get("weather_pass_factor"))
    if weather_pass_factor is not None:
        stadium_factor*=clamp(weather_pass_factor,0.86,1.03)
        notes.extend(row.get("weather_notes") or [])
    # Opponent role/pass defense: higher rank = easier.
    role_rank=safe_float(row.get("def_role_rank"), safe_float(row.get("opp_def_role_rank")))
    pass_rank=safe_float(row.get("def_pass_rank"), safe_float(row.get("opp_def_pass_rank")))
    opp_rank=role_rank if role_rank is not None else pass_rank
    if opp_rank is None:
        matchup_factor=1.0; notes.append("Opponent receiving/pass defense rank missing")
    else:
        matchup_factor=clamp(1 + (opp_rank-16.5)*0.0058, 0.90, 1.10)
        if opp_rank <= 8: notes.append("Top receiving/pass defense tax")
        elif opp_rank >= 25: notes.append("Weak receiving/pass defense boost")
    coverage=safe_float(row.get("coverage_grade"), safe_float(row.get("opp_coverage_grade")))
    coverage_factor=1.0
    if coverage is not None:
        coverage_factor=clamp(1 - (coverage-60)*0.0025, 0.94, 1.04)
    target_model=expected_targets * ypt * script_factor * pass_rate_factor * total_factor * stadium_factor * matchup_factor * coverage_factor
    history_model=rec_ypg * script_factor * total_factor * stadium_factor * matchup_factor * coverage_factor
    projection=(history_model*0.58)+(target_model*0.42)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Receiving Yards", consensus):
        projection=(projection*0.82)+(consensus*0.18)
        notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Receiving Yards", line):
        projection=clamp(projection, line-32, line+32)
    projection=clamp(projection, 4, 185)
    breakdown={
        "player_rec_ypg": round(rec_ypg,2),
        "targets_pg": round(targets_pg,2),
        "projected_targets": round(expected_targets,2),
        "yards_per_target": round(ypt,3),
        "target_model": round(target_model,2),
        "history_model": round(history_model,2),
        "team_pass_rate": round(pass_rate,2),
        "team_plays_pg": round(team_plays,2),
        "opponent_receiving_def_rank": None if opp_rank is None else int(round(opp_rank)),
        "game_total": round(total,2),
        "spread": round(spread,2),
        "matchup_factor": round(matchup_factor,3),
        "stadium_factor": round(stadium_factor,3),
        "total_factor": round(total_factor,3),
        "coverage_factor": round(coverage_factor,3),
        "final_pre_market": round(projection,2),
        "context_source": row.get("receiving_context_bank_source"),
        "model_match_status": row.get("model_match_status"),
        "model_player_match": row.get("model_player_match"),
    }
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def rushing_yards_stat_projection(row, role, cfg):
    """Rushing Yards projection from carries x yards/carry plus script/trench context."""
    row=dict(row or {})
    notes=[]; line=safe_float(row.get("line"))
    rush_ypg=safe_float(row.get("rushing_yards_pg"))
    carries_pg=safe_float(row.get("rush_attempts_pg"), safe_float(row.get("carries_pg")))
    current_games=safe_float(row.get("current_games"), 0) or 0
    cur_ypg=safe_float(row.get("current_rushing_yards_pg"))
    last3_ypg=safe_float(row.get("last3_rushing_yards_pg"))
    cur_carries=safe_float(row.get("current_rush_attempts_pg"))
    last3_carries=safe_float(row.get("last3_rush_attempts_pg"))
    team_plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    rush_rate=safe_float(row.get("pbp_rush_rate"), safe_float(row.get("rush_rate"), 44)) or 44
    carry_share=safe_float(row.get("carries_share"), role.get("carry", 40)) or role.get("carry", 40)
    spread=safe_float(row.get("spread"), 0) or 0
    total=safe_float(row.get("game_total"), 44) or 44
    if carries_pg is None or carries_pg <= 0:
        carries_pg=(team_plays*rush_rate/100.0)*(carry_share/100.0)
        notes.append("Carries estimated from team rush rate and carry share")
    if rush_ypg is None or rush_ypg <= 0:
        rush_ypg=(line*0.96) if line and _valid_market_line("Rushing Yards", line) else cfg.get("base",49)
        notes.append("Rushing yards fallback used")
    if current_games >= 2 and cur_ypg and cur_ypg > 0:
        blend=0.25 if current_games < 5 else 0.38
        form_ypg=(cur_ypg*0.62 + (last3_ypg or cur_ypg)*0.38)
        rush_ypg=(rush_ypg*(1-blend))+(form_ypg*blend)
        notes.append(f"Current-season RB form blend active ({int(current_games)} games)")
    if current_games >= 2 and cur_carries and cur_carries > 0:
        blend=0.30 if current_games < 5 else 0.44
        form_carries=(cur_carries*0.62 + (last3_carries or cur_carries)*0.38)
        carries_pg=(carries_pg*(1-blend))+(form_carries*blend)
    ypc=safe_float(row.get("yards_per_carry"))
    if ypc is None or ypc <= 0:
        ypc=rush_ypg/max(1.0,carries_pg)
    ypc=clamp(ypc,2.8,6.2)
    team_rushes=team_plays*rush_rate/100.0
    expected_carries=(carries_pg*0.70)+((team_rushes*carry_share/100.0)*0.30)
    script_factor=1.0
    if spread <= -6:
        script_factor+=0.055; notes.append("Rushing volume boost: favorite/lead script")
    elif spread >= 6:
        script_factor-=0.070; notes.append("Rushing volume tax: trailing script")
    elif abs(spread) <= 3:
        script_factor+=0.012; notes.append("Close game rushing stability")
    total_factor=clamp(1 + (total-44)*0.003,0.96,1.04)
    run_rank=safe_float(row.get("def_run_rank"), safe_float(row.get("opp_def_run_rank")))
    if run_rank is None:
        matchup_factor=1.0; notes.append("Opponent run defense rank missing")
    else:
        matchup_factor=clamp(1 + (run_rank-16.5)*0.006,0.90,1.10)
        if run_rank <= 8: notes.append("Top run defense tax")
        elif run_rank >= 25: notes.append("Weak run defense boost")
    run_block=safe_float(row.get("ol_run_block_proxy_rank"), safe_float(row.get("run_block_rank")))
    run_stop=safe_float(row.get("def_run_stop_rank"), safe_float(row.get("opp_def_run_stop_rank")))
    trench_factor=1.0
    if run_block is not None and run_stop is not None:
        if run_block <= 8 and run_stop >= 24:
            trench_factor*=1.018; notes.append("Run-blocking trench edge")
        elif run_block >= 24 and run_stop <= 8:
            trench_factor*=0.972; notes.append("Run-blocking trench mismatch")
    if str(row.get("run_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]:
        matchup_factor*=1.016; notes.append("Run-funnel matchup boost")
    if str(row.get("pass_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]:
        matchup_factor*=0.992; notes.append("Pass-funnel rushing volume tax")
    weather=str(row.get("weather_risk") or "").upper()
    weather_factor=1.0
    if weather in ["WIND","RAIN","SNOW","SEVERE"]:
        weather_factor*=1.01; notes.append("Weather rush-volume nudge")
    projection=(rush_ypg*0.54 + expected_carries*ypc*0.46)*script_factor*total_factor*matchup_factor*trench_factor*weather_factor
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Rushing Yards", consensus):
        projection=(projection*0.82)+(consensus*0.18); notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Rushing Yards", line):
        projection=clamp(projection,line-26,line+26)
    projection=clamp(projection,1,175)
    breakdown={
        "player_rush_ypg": round(rush_ypg,2),
        "rush_attempts_pg": round(carries_pg,2),
        "projected_carries": round(expected_carries,2),
        "yards_per_carry": round(ypc,3),
        "team_rush_rate": round(rush_rate,2),
        "team_plays_pg": round(team_plays,2),
        "opponent_run_def_rank": None if run_rank is None else int(round(run_rank)),
        "script_factor": round(script_factor,3),
        "matchup_factor": round(matchup_factor,3),
        "trench_factor": round(trench_factor,3),
        "weather_factor": round(weather_factor,3),
        "final_pre_market": round(projection,2),
    }
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def pass_attempts_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    att_pg=safe_float(row.get("current_pass_attempts_pg"), safe_float(row.get("last5_pass_attempts_pg"), safe_float(row.get("pass_attempts_pg"))))
    team_plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
    if att_pg is None or att_pg <= 0:
        att_pg=(line*0.96) if line and _valid_market_line("Pass Attempts", line) else cfg.get("base", 33.5)
        notes.append("Pass attempts fallback anchored to market/default")
    projected_dropbacks=team_plays*pass_rate/100.0
    spread=safe_float(row.get("spread"),0) or 0
    total=safe_float(row.get("game_total"),44) or 44
    script_factor=1.0 + clamp(spread, -10, 10)*0.012
    total_factor=clamp(1+(total-44)*0.006,0.94,1.06)
    pace_factor=clamp(team_plays/62.0,0.92,1.08)
    pressure=safe_float(row.get("opp_def_pressure_rank"), safe_float(row.get("def_pressure_rank")))
    pressure_factor=1.0
    if pressure is not None and pressure <= 8:
        pressure_factor*=0.985; notes.append("Strong pressure can reduce attempts/completion rhythm")
    projection=(att_pg*0.52 + projected_dropbacks*0.48)*script_factor*total_factor*pace_factor*pressure_factor
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Pass Attempts", consensus):
        projection=(projection*0.80)+(consensus*0.20); notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Pass Attempts", line):
        projection=clamp(projection,line-8,line+8)
    projection=clamp(projection,5,62)
    breakdown={"pass_attempts_pg":round(att_pg,2),"projected_dropbacks":round(projected_dropbacks,2),"team_pass_rate":round(pass_rate,2),"team_plays_pg":round(team_plays,2),"script_factor":round(script_factor,3),"total_factor":round(total_factor,3),"pace_factor":round(pace_factor,3),"pressure_factor":round(pressure_factor,3)}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def completions_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    comp_pg=safe_float(row.get("current_completions_pg"), safe_float(row.get("completions_pg")))
    attempts=safe_float(row.get("current_pass_attempts_pg"), safe_float(row.get("last5_pass_attempts_pg"), safe_float(row.get("pass_attempts_pg"))))
    if attempts is None or attempts <= 0:
        attempts=(safe_float(row.get("market_consensus_line")) or line or 33.5) * 1.45 if line else 33.5
    completion_rate=safe_float(row.get("completion_rate"), safe_float(row.get("qb_completion_rate")))
    if completion_rate is None:
        if comp_pg and attempts:
            completion_rate=100*comp_pg/max(1,attempts)
        else:
            completion_rate=64.0
    pass_att_base, pass_att_info=pass_attempts_stat_projection({**row, "prop":"Pass Attempts", "line":None}, role, {"base":attempts})
    pressure=safe_float(row.get("opp_def_pressure_rank"), safe_float(row.get("def_pressure_rank")))
    coverage=safe_float(row.get("coverage_grade"))
    rate_factor=1.0
    if pressure is not None and pressure <= 8:
        rate_factor*=0.982; notes.append("Pressure tax on completion rate")
    if coverage is not None and coverage >= 70:
        rate_factor*=0.986; notes.append("Strong coverage tax")
    weather=str(row.get("weather_risk") or "").upper()
    if weather in ["WIND","RAIN","SNOW","SEVERE"]:
        rate_factor*=0.972; notes.append("Weather completion tax")
    projection=pass_att_base*(completion_rate/100.0)*rate_factor
    if comp_pg and comp_pg > 0:
        projection=(projection*0.65)+(comp_pg*0.35)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Completions", consensus):
        projection=(projection*0.80)+(consensus*0.20); notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Completions", line):
        projection=clamp(projection,line-6,line+6)
    projection=clamp(projection,2,45)
    breakdown={"projected_attempts":round(pass_att_base,2),"completion_rate":round(completion_rate,2),"rate_factor":round(rate_factor,3),"completions_pg":None if comp_pg is None else round(comp_pg,2),"attempt_model":pass_att_info.get("breakdown",{})}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def receptions_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    rec_pg=safe_float(row.get("current_receptions_pg"), safe_float(row.get("last5_receptions_pg"), safe_float(row.get("receptions_pg"))))
    targets_pg=safe_float(row.get("current_targets_pg"), safe_float(row.get("last5_targets_pg"), safe_float(row.get("targets_pg"))))
    if targets_pg is None or targets_pg <= 0:
        targets_pg=(line*1.55) if line and _valid_market_line("Receptions", line) else 6.2
        notes.append("Targets fallback anchored to reception line/default")
    catch_rate=safe_float(row.get("catch_rate"), safe_float(row.get("reception_rate")))
    if catch_rate is None:
        catch_rate=100*rec_pg/max(1,targets_pg) if rec_pg and targets_pg else (69 if str(row.get("position") or "").upper()=="TE" else 64)
    route=safe_float(row.get("route_participation"), role.get("route",65)) or role.get("route",65)
    target_share=safe_float(row.get("target_share"), role.get("target",15)) or role.get("target",15)
    role_factor=clamp((route/75.0)*0.55 + (target_share/20.0)*0.45,0.78,1.20)
    qb_status=str(row.get("qb_status") or row.get("qb_injury_status") or "").upper()
    qb_factor=0.94 if any(x in qb_status for x in ["OUT","BACKUP","DOUBTFUL"]) else 0.975 if any(x in qb_status for x in ["QUESTION","LIMIT"]) else 1.0
    coverage=safe_float(row.get("coverage_grade"))
    matchup_factor=1.0
    if coverage is not None and coverage >= 70:
        matchup_factor*=0.976; notes.append("Strong coverage receptions tax")
    elif coverage is not None and coverage <= 45:
        matchup_factor*=1.012; notes.append("Coverage matchup boost")
    projection=targets_pg*(catch_rate/100.0)*role_factor*qb_factor*matchup_factor
    if rec_pg and rec_pg > 0:
        projection=(projection*0.62)+(rec_pg*0.38)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Receptions", consensus):
        projection=(projection*0.80)+(consensus*0.20); notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Receptions", line):
        projection=clamp(projection,line-2.5,line+2.5)
    projection=clamp(projection,0,16)
    breakdown={"receptions_pg":None if rec_pg is None else round(rec_pg,2),"targets_pg":round(targets_pg,2),"catch_rate":round(catch_rate,2),"role_factor":round(role_factor,3),"qb_factor":round(qb_factor,3),"matchup_factor":round(matchup_factor,3)}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def rush_attempts_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    carries_pg=safe_float(row.get("current_rush_attempts_pg"), safe_float(row.get("last5_rush_attempts_pg"), safe_float(row.get("rush_attempts_pg"))))
    if carries_pg is None or carries_pg <= 0:
        carries_pg=(line*0.96) if line and _valid_market_line("Rush Attempts", line) else cfg.get("base",13.5)
        notes.append("Rush attempts fallback anchored to market/default")
    team_plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    rush_rate=safe_float(row.get("pbp_rush_rate"), safe_float(row.get("rush_rate"), 44)) or 44
    carry_share=safe_float(row.get("carries_share"), role.get("carry",35)) or role.get("carry",35)
    expected_team_rushes=team_plays*rush_rate/100.0
    spread=safe_float(row.get("spread"),0) or 0
    script_factor=1.0 + clamp(-spread, -10, 10)*0.018
    blowout=safe_float(row.get("blowout_prob"),0) or 0
    if spread <= -7:
        script_factor*=1.02
    elif spread >= 7:
        script_factor*=0.94
    projection=(carries_pg*0.58 + expected_team_rushes*(carry_share/100.0)*0.42)*script_factor*(1+min(0.04, blowout*0.06))
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    if consensus is not None and _valid_market_line("Rush Attempts", consensus):
        projection=(projection*0.80)+(consensus*0.20); notes.append("Market consensus blend active")
    if line is not None and _valid_market_line("Rush Attempts", line):
        projection=clamp(projection,line-5,line+5)
    projection=clamp(projection,0,38)
    breakdown={"rush_attempts_pg":round(carries_pg,2),"expected_team_rushes":round(expected_team_rushes,2),"carry_share":round(carry_share,2),"script_factor":round(script_factor,3)}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def _market_line_sanity_projection(base, line, prop, source=None):
    """Prevent early/live NFL markets from producing unrealistic high edges.

    The model still uses opportunity, matchup, weather, game script, Bayesian/ML, and
    Monte Carlo, but after those layers it is re-anchored to the actual Underdog line
    and capped by market-specific realistic edge bands. This is important before we
    have enough graded NFL prop samples.
    """
    line = safe_float(line)
    base = safe_float(base, 0.0) or 0.0
    if line is None or prop not in ACTIVE_NFL_MARKETS or line <= 0:
        return base, {"active": False, "note": "No active market line cap"}
    # Stronger anchor for live/manual lines while NFL learning is still warming up.
    # Passing Yards has its own stat model, so keep it less market-forced.
    if prop == "Passing Yards":
        anchored = (base * 0.70) + (float(line) * 0.30)
    elif prop == "Receiving Yards":
        anchored = (base * 0.62) + (float(line) * 0.38)
    elif prop == "Rushing Yards":
        anchored = (base * 0.64) + (float(line) * 0.36)
    else:
        anchored = (base * 0.48) + (float(line) * 0.52)
    cap = PROJECTION_EDGE_CAPS.get(prop, max(8.0, float(line) * 0.18))
    capped = clamp(anchored, float(line) - cap, float(line) + cap)
    return float(capped), {
        "active": abs(capped - base) >= 0.01,
        "raw_before_cap": round(base, 3),
        "anchored": round(anchored, 3),
        "cap": cap,
        "note": f"{prop} line sanity anchor/cap active"
    }

def project_row(row, sims=12000):
    row=merge_nfl_context(row)
    prop=row.get("prop","Passing Yards")
    cfg=PROP_CONFIG.get(prop, PROP_CONFIG["Passing Yards"])
    role=player_role_defaults(row.get("position"),prop)
    role=apply_real_usage_to_role(row, role)
    role, current_week_role = current_week_role_engine(row, role, prop)
    usage_quality, usage_flags = usage_data_quality(row, prop)
    base=cfg["base"]*usage_adjustment(role,prop)
    base, env_notes, env=apply_environment(base,row,prop)
    pass_yards_model_info = {"active": False}
    receiving_yards_model_info = {"active": False}
    rushing_yards_model_info = {"active": False}
    pass_attempts_model_info = {"active": False}
    completions_model_info = {"active": False}
    receptions_model_info = {"active": False}
    rush_attempts_model_info = {"active": False}
    qb_tier_info = qb_tier_context(row.get("player"), row.get("position")) if prop == "Passing Yards" else {"tier":"N/A","factor":1.0,"sigma_factor":1.0,"confidence_boost":0,"note":""}
    if prop == "Passing Yards":
        base, pass_yards_model_info = passing_yards_stat_projection(row, role, cfg)
        base *= qb_tier_info.get("factor", 1.0)
        env_notes = env_notes + (pass_yards_model_info.get("notes") or [])
        if qb_tier_info.get("note"):
            env_notes.append(qb_tier_info.get("note"))
    elif prop == "Receiving Yards":
        base, receiving_yards_model_info = receiving_yards_stat_projection(row, role, cfg)
        env_notes = env_notes + (receiving_yards_model_info.get("notes") or [])
    elif prop == "Rushing Yards":
        base, rushing_yards_model_info = rushing_yards_stat_projection(row, role, cfg)
        env_notes = env_notes + (rushing_yards_model_info.get("notes") or [])
    elif prop == "Pass Attempts":
        base, pass_attempts_model_info = pass_attempts_stat_projection(row, role, cfg)
        env_notes = env_notes + (pass_attempts_model_info.get("notes") or [])
    elif prop == "Completions":
        base, completions_model_info = completions_stat_projection(row, role, cfg)
        env_notes = env_notes + (completions_model_info.get("notes") or [])
    elif prop == "Receptions":
        base, receptions_model_info = receptions_stat_projection(row, role, cfg)
        env_notes = env_notes + (receptions_model_info.get("notes") or [])
    elif prop == "Rush Attempts":
        base, rush_attempts_model_info = rush_attempts_stat_projection(row, role, cfg)
        env_notes = env_notes + (rush_attempts_model_info.get("notes") or [])

    # Stat-specific models already use recent form, so the role factor is bounded and
    # partially damped for those markets to prevent double counting.
    current_role_factor=safe_float(current_week_role.get("factor"),1.0) or 1.0
    if prop in ["Passing Yards","Receiving Yards","Rushing Yards","Pass Attempts","Completions","Receptions","Rush Attempts"]:
        base *= 1.0 + (current_role_factor-1.0)*0.65
    else:
        base *= current_role_factor
    opportunity = opportunity_engine(row, role, prop)
    pace_factor, pace_risk, pace_notes = pace_engine(row, prop)
    defense_factor, defense_risk, defense_notes = defensive_matchup_factor(row, prop)
    rank_factor, rank_risk, rank_notes, rank_context = offense_defense_rank_factor(row, prop)
    game_factor, game_env_risk, game_notes = game_environment_factor(row, prop)
    vegas_factor, vegas_risk, vegas_notes = vegas_environment_engine(row, prop)
    script_factor, script_risk, script_notes, script_branches = game_script_simulator(row, prop)
    blowout_factor, blowout_risk, blowout_notes, blowout_prob = blowout_risk_engine(row, prop)
    advanced_factor, advanced_risk, advanced_notes, advanced_context = advanced_context_engine(row, prop)
    split_factor, split_risk, split_notes, split_context = split_personnel_factor(row, prop)
    role_factor, injury_risk, game_script_risk, risk_notes = role_risk_adjustments(row, role, prop)
    if defense_risk == "HIGH" or rank_risk == "HIGH" or game_env_risk == "HIGH" or script_risk == "HIGH" or blowout_risk == "HIGH" or vegas_risk == "HIGH" or advanced_risk == "HIGH" or split_risk == "HIGH":
        game_script_risk="HIGH"
    if prop == "Passing Yards":
        # Passing yards model already includes QB history, attempts, pass rate, matchup,
        # spread/total, and stadium/weather. Keep only small generic risk modifiers.
        base*=clamp(role_factor,0.92,1.04)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.03)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.94,1.05)
    elif prop == "Receiving Yards":
        # Receiving yards model already includes player receiving history, targets, team pass rate, matchup,
        # spread/total, and stadium/weather. Keep generic modifiers small to avoid double counting.
        base*=clamp(role_factor,0.90,1.05)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.95,1.05)*clamp(blowout_factor,0.93,1.04)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.94,1.06)
    elif prop == "Rushing Yards":
        base*=clamp(role_factor,0.88,1.06)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.95,1.06)*clamp(blowout_factor,0.94,1.05)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.95,1.04)
    elif prop in ["Pass Attempts", "Completions"]:
        base*=clamp(role_factor,0.94,1.03)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.04)*clamp(advanced_factor,0.96,1.04)
    elif prop == "Receptions":
        base*=clamp(role_factor,0.91,1.05)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.04)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.96,1.04)
    elif prop == "Rush Attempts":
        base*=clamp(role_factor,0.90,1.05)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.05)*clamp(blowout_factor,0.94,1.05)*clamp(advanced_factor,0.97,1.03)
    else:
        base*=role_factor*defense_factor*rank_factor*game_factor*opportunity["factor"]*pace_factor*vegas_factor*script_factor*blowout_factor*advanced_factor*split_factor
    learn=learning_scale(row.get("player"),prop)
    if bool(st.session_state.get("smart_calibration_enabled", True)):
        cal_scale, cal_note, smart_calibration = smart_calibration_scale(row, role, usage_quality)
    else:
        cal_scale, cal_note = calibration_scale(row.get("player"),prop)
        smart_calibration={"active":cal_scale!=1.0,"level":"legacy_player_prop","scale":cal_scale,"role_bucket":projection_role_bucket(row,role),"data_quality_bucket":projection_data_quality_bucket(row,usage_quality)}
    cal_status=calibration_readiness(prop)
    base*=learn*cal_scale
    line=safe_float(row.get("line"))
    if line is not None and prop in ACTIVE_NFL_MARKETS and not _valid_market_line(prop, line):
        # Never project off a corrupted/season-long line. This row should normally
        # be filtered earlier, but this final guard prevents 3000+ yard cards.
        line = None
        row["line"] = None

    # Market reference is secondary to the raw model. Fresh multi-book consensus can
    # receive a small anchor; stale/single-book context never determines the projection.
    market_intelligence=market_intelligence_engine(row, projection=base, line=line)
    if line is not None and row.get("source")!="DEMO" and prop in ACTIVE_NFL_MARKETS:
        base_line_weight={"Passing Yards":0.18,"Receiving Yards":0.22,"Rushing Yards":0.22,"Receptions":0.27,"Pass Attempts":0.25,"Completions":0.25,"Rush Attempts":0.27}.get(prop,0.32)
        if market_intelligence.get("stale"): base_line_weight*=0.45
        consensus=safe_float(market_intelligence.get("consensus"))
        consensus_weight=safe_float(market_intelligence.get("anchor_weight"),0) or 0
        line_weight=max(0.0,base_line_weight-consensus_weight*0.55)
        base=base*(1-line_weight-consensus_weight) + line*line_weight + (consensus if consensus is not None else line)*consensus_weight

    xgb_info = {"enabled": bool(st.session_state.get("xgb_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("xgb_assist_enabled", False):
        base, xgb_info = xgboost_assist_projection({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    bayes_markov_info = {"enabled": bool(st.session_state.get("advanced_sim_assist_enabled", True)), "status": "OFF"}
    if st.session_state.get("advanced_sim_assist_enabled", True):
        base, bayes_markov_info = bayesian_markov_poisson_engine({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "usage_quality": usage_quality, "data_score": 70}, prop, base)

    ensemble_info = {"enabled": bool(st.session_state.get("ensemble_ml_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("ensemble_ml_assist_enabled", False):
        base, ensemble_info = ensemble_ml_assist_projection({**row, "projection": base, "line": line, "edge": None if line is None else base-line, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    base, line_sanity_info = _market_line_sanity_projection(base, line, prop, row.get("source"))

    sigma=cfg["sigma"]
    if prop == "Passing Yards":
        sigma *= safe_float(qb_tier_info.get("sigma_factor"), 1.0) or 1.0
    sigma=calibrated_sigma(prop, sigma, row, usage_quality, injury_risk, game_script_risk, advanced_context)
    collapse_prob, ceiling_prob = simulation_branch_rates(row, prop, injury_risk, game_script_risk)
    collapse_prob = clamp(collapse_prob + (blowout_prob*0.12 if prop in ["Passing Yards","Receiving Yards","Receptions","Rushing Yards","Rush Attempts"] else 0), 0.05, 0.46)
    if script_risk == "HIGH":
        sigma *= 1.04
    seed=stable_projection_seed(row.get("player","x"), prop, line, row.get("team",""), row.get("opp",""), row.get("source",""))
    empirical_values=empirical_values_for_row(row,prop)
    sim, distribution_meta=simulate_prop_distribution(base, sigma, prop, sims, seed, collapse_prob, ceiling_prob, empirical_values=empirical_values)

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
    if current_week_role.get("risk") == "HIGH": score-=7
    score += int(market_intelligence.get("confidence_delta",0) or 0)
    if prop == "Passing Yards":
        score += int(qb_tier_info.get("confidence_boost", 0) or 0)
    audit_preview=projection_audit(row)
    if audit_preview.get("label") == "Fresh":
        score+=4
    elif audit_preview.get("label") == "Stale":
        score-=12
    if audit_preview.get("hard_blocks"):
        score-=18
    score=int(clamp(score,0,99))

    line_delta=update_clv_snapshot(row.get("player"), prop, row.get("source"), line) if line is not None else None
    true_line_delta=track_line_delta(row.get("player"), prop, row.get("source"), line) if line is not None else None

    notes=[]+env_notes+opportunity.get("notes",[])+pace_notes+risk_notes+defense_notes+rank_notes+game_notes+vegas_notes+script_notes+blowout_notes+advanced_notes+split_notes+current_week_role.get("notes",[])+market_intelligence.get("notes",[])
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
    active_breakdown = {}
    if pass_yards_model_info.get("active"):
        b = pass_yards_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Passing Yards model: {b.get('projected_attempts')} att × {b.get('yards_per_attempt')} YPA | base {b.get('player_pass_ypg')} YPG")
        notes.append(f"QB tier: {qb_tier_info.get('tier')}")
    if receiving_yards_model_info.get("active"):
        b = receiving_yards_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Receiving Yards model: {b.get('projected_targets')} targets × {b.get('yards_per_target')} YPT | base {b.get('player_rec_ypg')} YPG")
    if rushing_yards_model_info.get("active"):
        b = rushing_yards_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Rushing Yards model: {b.get('projected_carries')} carries × {b.get('yards_per_carry')} YPC | base {b.get('player_rush_ypg')} YPG")
    if pass_attempts_model_info.get("active"):
        b = pass_attempts_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Pass Attempts model: {b.get('projected_dropbacks')} dropbacks | pass rate {b.get('team_pass_rate')}%")
    if completions_model_info.get("active"):
        b = completions_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Completions model: {b.get('projected_attempts')} att × {b.get('completion_rate')}%")
    if receptions_model_info.get("active"):
        b = receptions_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Receptions model: {b.get('targets_pg')} targets × {b.get('catch_rate')}% catch")
    if rush_attempts_model_info.get("active"):
        b = rush_attempts_model_info.get("breakdown", {})
        active_breakdown = b
        notes.append(f"Rush Attempts model: {b.get('expected_team_rushes')} team rushes × {b.get('carry_share')}% share")
    if line_sanity_info.get("active"):
        notes.append(line_sanity_info.get("note"))
    fresh_layers=[]
    if row.get("has_current_usage") or row.get("current_context_source"):
        fresh_layers.append("current player form/usage")
    if row.get("has_current_team_context"):
        fresh_layers.append("current team pace/context")
    if row.get("has_depth_chart_context"):
        fresh_layers.append("depth chart")
    if row.get("weather_pass_factor") is not None:
        fresh_layers.append("weather detail")
    if row.get("has_market_context"):
        fresh_layers.append("market consensus")
    if row.get("has_travel_context"):
        fresh_layers.append("travel/rest")
    if row.get("has_matchup_context"):
        fresh_layers.append("matchup style")
    if row.get("has_qb_context"):
        fresh_layers.append("QB dependency")
    if row.get("has_defensive_injury_context"):
        fresh_layers.append("defensive injuries")
    if row.get("has_final_inactives_context"):
        fresh_layers.append("final inactives")
    if row.get("has_manual_override_context"):
        fresh_layers.append("manual news override")
        if row.get("manual_note") or row.get("manual_override_status"):
            notes.append(f"Manual override: {row.get('manual_override_status') or ''} {row.get('manual_note') or ''}".strip())
    if fresh_layers:
        notes.append("Fresh context active: " + ", ".join(fresh_layers))
    else:
        notes.append("Fresh context missing: using saved Phase 6/default context")
    if row.get("source")=="DEMO": notes.append("Demo row until live NFL props are available")

    factor_stack={"role":round(role_factor,3),"current_week_role":round(current_role_factor,3),"game_env":round(game_factor,3),"defense":round(defense_factor,3),"offense_defense_rank":round(rank_factor,3),"opportunity":round(opportunity.get("factor",1.0),3),"pace":round(pace_factor,3),"vegas":round(vegas_factor,3),"script":round(script_factor,3),"blowout":round(blowout_factor,3),"advanced":round(advanced_factor,3),"splits_personnel":round(split_factor,3),"learning":round(learn,3),"calibration":round(cal_scale,3),"sigma":round(sigma,3),"line_sanity_active":bool(line_sanity_info.get("active"))}
    model_meta={"model_version":MODEL_VERSION,"app_version":APP_VERSION,"generated_at":now_iso(),"active_market_count":len(ACTIVE_NFL_MARKETS),"prop":prop,"source":row.get("source"),"context_layers":audit_preview.get("layers",{}),"staleness":context_staleness(row),"calibration_status":cal_status}
    out={**row,"projection":round(mean,2),"edge":None if edge is None else round(edge,2),"pick":side,"fair_prob":None if prob is None else round(prob,3),"ev":None if ev is None else round(ev,4),"kelly":round(kelly,4),"p10":round(p10,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"pure_upside":upside,"volatility":volatility,"stability_score":stability,"usage_quality":usage_quality,"opportunity_score":round(opportunity.get("factor",1.0)*100,1),"expected_opportunity":opportunity.get("expected",{}),"pace_factor":round(pace_factor,3),"vegas_factor":round(vegas_factor,3),"advanced_factor":round(advanced_factor,3),"split_personnel_factor":round(split_factor,3),"split_personnel_context":split_context,"advanced_context":advanced_context,"offense_defense_rank_context":rank_context,"offense_defense_rank_factor":round(rank_factor,3),"passing_yards_model":pass_yards_model_info,"receiving_yards_model":receiving_yards_model_info,"rushing_yards_model":rushing_yards_model_info,"pass_attempts_model":pass_attempts_model_info,"completions_model":completions_model_info,"receptions_model":receptions_model_info,"rush_attempts_model":rush_attempts_model_info,"qb_tier":qb_tier_info,"projection_breakdown":active_breakdown,"factor_stack":factor_stack,"model_meta":model_meta,"model_version":MODEL_VERSION,"calibration_status":cal_status,"smart_calibration":smart_calibration,"role_bucket":current_week_role.get("role_bucket") or projection_role_bucket(row,role),"data_quality_bucket":projection_data_quality_bucket(row,usage_quality),"current_week_role":current_week_role,"market_intelligence":market_intelligence,"distribution_meta":distribution_meta,"projection_audit":audit_preview,"audit_label":audit_preview.get("label"),"audit_score":audit_preview.get("score"),"xgb_assist":xgb_info,"bayes_markov_assist":bayes_markov_info,"ensemble_ml_assist":ensemble_info,"line_sanity":line_sanity_info,"game_script_factor":round(script_factor,3),"game_script_branches":script_branches,"blowout_prob":blowout_prob,"matchup_factor":round(defense_factor,3),"collapse_prob":round(collapse_prob,3),"ceiling_prob":round(ceiling_prob,3),"data_score":score,"injury_risk":injury_risk,"game_script_risk":game_script_risk,"defense_risk":defense_risk,"line_delta":line_delta,"true_line_delta":true_line_delta,"role":role,"env":env,"notes":notes,"sim_samples":sims}
    out["market_compare"]=_market_compare_text(out)
    out["recent_form"]=_recent_form_text(out)
    signal, action_tier, rejections = build_signal(out)
    out["signal"]=signal; out["action_tier"]=action_tier; out["official_rejections"]=rejections; out["bettable"]=action_tier=="BET"
    return out

def _resimulate_scaled_projection(p, scale, reason, sims=3000):
    p=dict(p or {}); old=safe_float(p.get("projection"),0) or 0
    scale=float(clamp(scale,NFL_TEAM_RECONCILE_MIN_SCALE,1.0))
    if old<=0 or scale>=0.999: return p
    prop=p.get("prop"); target=old*scale
    sigma=safe_float((p.get("factor_stack") or {}).get("sigma"), PROP_CONFIG.get(prop,{}).get("sigma",1)) or 1
    seed=stable_projection_seed(p.get("player","x"),prop,p.get("line"),p.get("team",""),p.get("opp",""),"team_reconcile")
    sim,meta=simulate_prop_distribution(target,sigma*max(0.82,math.sqrt(scale)),prop,sims,seed,safe_float(p.get("collapse_prob"),0.12) or 0.12,safe_float(p.get("ceiling_prob"),0.08) or 0.08,empirical_values=empirical_values_for_row(p,prop))
    mean=float(np.mean(sim)); p10,p50,p75,p90=[float(np.percentile(sim,q)) for q in [10,50,75,90]]
    line=safe_float(p.get("line"))
    if line is not None:
        over=float(np.mean(sim>line)); under=1-over; side="OVER" if over>=under else "UNDER"; prob=max(over,under)
        p.update({"edge":round(mean-line,2),"pick":side,"fair_prob":round(prob,3),"ev":round(expected_value(prob,safe_float(p.get("odds"),-110) or -110),4),"kelly":round(kelly_fraction(prob,safe_float(p.get("odds"),-110) or -110),4)})
    p.update({"projection":round(mean,2),"p10":round(p10,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"stability_score":projection_stability_score(p10,p90,mean,prop),"distribution_meta":{**(p.get("distribution_meta") or {}),**meta},"team_volume_reconciliation":{"active":True,"scale":round(scale,4),"before":round(old,2),"after":round(mean,2),"reason":reason}})
    p["data_score"]=int(clamp((safe_float(p.get("data_score"),70) or 70)-max(1,int((1-scale)*25)),0,99))
    notes=list(p.get("notes") or []); notes.append(f"Team-volume reconciliation: {reason} (x{scale:.3f})"); p["notes"]=notes
    signal,tier,rejections=build_signal(p); p.update({"signal":signal,"action_tier":tier,"official_rejections":rejections,"bettable":tier=="BET"})
    return p

def reconcile_team_projection_volume(rows):
    """Cap contradictory independent player totals to a realistic team workload.

    This only scales down impossible stacks; it never boosts a player just to fill a team total.
    """
    rows=[dict(r) for r in (rows or [])]
    if not rows: return rows
    scales={i:(1.0,[]) for i in range(len(rows))}
    groups={}
    for i,r in enumerate(rows):
        team=str(r.get("team") or "").upper(); matchup=str(r.get("matchup") or "")
        if team in NFL_TEAM_ABBRS: groups.setdefault((team,matchup),[]).append(i)
    for (team,matchup),idxs in groups.items():
        group=[rows[i] for i in idxs]
        def max_prop(prop):
            vals=[safe_float(r.get("projection")) for r in group if r.get("prop")==prop and safe_float(r.get("projection")) is not None]
            return max(vals) if vals else None
        pass_att=max_prop("Pass Attempts")
        completions=max_prop("Completions") or (pass_att*0.66 if pass_att else None)
        pass_yards=max_prop("Passing Yards")
        exemplar=group[0]
        expected=exemplar.get("expected_opportunity") or {}
        plays=safe_float(expected.get("plays_pg"),safe_float(exemplar.get("pbp_plays_pg"),safe_float(exemplar.get("plays_pg"),62))) or 62
        rush_rate=safe_float(exemplar.get("pbp_rush_rate"),safe_float(exemplar.get("rush_rate"),43)) or 43
        rush_budget=max(16.0,plays*rush_rate/100.0)

        # QB completions cannot exceed a realistic completion rate.
        if pass_att:
            for i in idxs:
                if rows[i].get("prop")=="Completions" and safe_float(rows[i].get("projection"),0)>pass_att*0.80:
                    s=(pass_att*0.80)/max(0.1,safe_float(rows[i].get("projection"),1)); scales[i]=(min(scales[i][0],s),scales[i][1]+["completions exceeded 80% of projected attempts"])
        constraints=[
            ("Receptions", completions*0.96 if completions else None, "listed receptions exceeded team completion budget"),
            ("Receiving Yards", pass_yards*0.98 if pass_yards else None, "listed receiving yards exceeded QB passing-yard budget"),
            ("Rush Attempts", rush_budget*0.96, "listed carries exceeded team rushing-play budget"),
            ("Rushing Yards", rush_budget*5.65, "listed rushing yards exceeded team rush-volume ceiling"),
        ]
        for prop,cap,reason in constraints:
            if cap is None: continue
            pidx=[i for i in idxs if rows[i].get("prop")==prop and safe_float(rows[i].get("projection")) is not None]
            total=sum(safe_float(rows[i].get("projection"),0) or 0 for i in pidx)
            if pidx and total>cap*1.02:
                s=float(clamp(cap/max(total,0.1),NFL_TEAM_RECONCILE_MIN_SCALE,1.0))
                for i in pidx: scales[i]=(min(scales[i][0],s),scales[i][1]+[reason])
        context={"team":team,"matchup":matchup,"pass_attempt_budget":None if pass_att is None else round(pass_att,2),"completion_budget":None if completions is None else round(completions,2),"passing_yard_budget":None if pass_yards is None else round(pass_yards,2),"rush_attempt_budget":round(rush_budget,2)}
        for i in idxs: rows[i]["team_volume_context"]=context
    for i,(scale,reasons) in scales.items():
        if scale<0.999: rows[i]=_resimulate_scaled_projection(rows[i],scale,"; ".join(dict.fromkeys(reasons)))
        else: rows[i].setdefault("team_volume_reconciliation",{"active":False,"scale":1.0})
    return rows

def alt_ladder(p):
    line=safe_float(p.get("line")); prop=p.get("prop")
    if line is None: return pd.DataFrame()
    step=10 if "Yards" in prop else 5 if prop in ["Pass Attempts","Completions"] else 2 if prop in ["Rush Attempts","Longest Reception","Longest Rush","Tackles + Assists","Kicking Points"] else 1 if prop in ["Receptions","Field Goals Made"] else 0.5
    levels=[line-step,line,line+step,line+2*step,line+3*step]
    rows=[]
    mean=p["projection"]; sigma=PROP_CONFIG.get(prop,{}).get("sigma",10)
    sim,_=simulate_prop_distribution(mean,sigma,prop,8000,42,safe_float(p.get("collapse_prob"),0.12) or 0.12,safe_float(p.get("ceiling_prob"),0.08) or 0.08,empirical_values=empirical_values_for_row(p,prop))
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

def grade_from_results_csv(uploaded_file, candidate_rows, grade_note="results_csv"):
    if uploaded_file is None:
        return []
    try:
        data=uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        rdf=pd.read_csv(io.BytesIO(data))
    except Exception as e:
        request_log("RESULT_IMPORT", "CSV_ERROR", e)
        return []
    if rdf.empty:
        return []
    rdf.columns=[str(c).strip() for c in rdf.columns]
    rows=[]; actuals=[]
    for _, rr in rdf.iterrows():
        player=norm(rr.get("player") or rr.get("Player") or rr.get("name"))
        prop=_canon_prop_label(rr.get("prop") or rr.get("Prop") or rr.get("market")) or str(rr.get("prop") or rr.get("Prop") or "")
        actual=safe_float(rr.get("actual"), safe_float(rr.get("Actual"), safe_float(rr.get("result"), safe_float(rr.get("Result")))))
        if not player or not prop or actual is None:
            continue
        match=None
        for cand in candidate_rows:
            if norm(cand.get("player")) == player and str(cand.get("prop")) == str(prop):
                match=cand
                break
        if match is None:
            close=[cand for cand in candidate_rows if str(cand.get("prop")) == str(prop) and difflib.SequenceMatcher(None, norm(cand.get("player")), player).ratio() >= 0.92]
            if close:
                match=close[0]
        if match:
            rows.append(match)
            actuals.append(actual)
    return grade_rows_and_learn(rows, actuals, grade_note=grade_note) if rows else []

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
    show_cols=["player","position","team","matchup","prop","line","projection","edge","pick","fair_prob","signal","action_tier","audit_label","market_compare","recent_form","data_score","stability_score","opportunity_score","ev","kelly","xgb_status","advanced_factor","game_script_factor","matchup_factor","blowout_prob","pure_upside","volatility","line_delta","source"]
    view = view[[c for c in show_cols if c in view.columns]]
    safe_key = re.sub(r"[^a-z0-9]+", "_", str(title).lower()).strip("_") or "board"
    default_size = int(st.session_state.get("nfl_table_page_size", 50) or 50)
    page_size = st.selectbox("Rows per page", [25, 50, 100], index=[25,50,100].index(default_size if default_size in [25,50,100] else 50), key=f"{safe_key}_table_size")
    pages = max(1, math.ceil(len(view) / page_size))
    page = st.number_input("Page", min_value=1, max_value=pages, value=min(int(st.session_state.get(f"{safe_key}_page", 1) or 1), pages), step=1, key=f"{safe_key}_page")
    lo=(int(page)-1)*page_size; hi=min(len(view), lo+page_size)
    st.caption(f"Showing {lo+1}-{hi} of {len(view)} rows")
    st.dataframe(view.iloc[lo:hi], use_container_width=True, hide_index=True, height=min(720, 42*(hi-lo+1)+38))

def _render_player_cards(rows, limit=None, header=None):
    if header:
        st.markdown(f"<div class='section-title-pro'>{header}</div>", unsafe_allow_html=True)
    if not rows:
        st.warning("No player cards available in this view.")
        return
    page_size = int(limit or st.session_state.get("nfl_card_page_size", 12) or 12)
    page_size = max(4, min(page_size, 50))
    safe_key = re.sub(r"[^a-z0-9]+", "_", str(header or (rows[0].get("prop") if rows else "cards")).lower()).strip("_") or "cards"
    pages = max(1, math.ceil(len(rows) / page_size))
    page = st.number_input("Card page", min_value=1, max_value=pages, value=min(int(st.session_state.get(f"{safe_key}_card_page", 1) or 1), pages), step=1, key=f"{safe_key}_card_page")
    lo=(int(page)-1)*page_size; hi=min(len(rows), lo+page_size)
    shown = rows[lo:hi]
    st.caption(f"Showing cards {lo+1}-{hi} of {len(rows)}. Only this page is rendered for speed.")
    for i,p in enumerate(shown):
        badge_class="good-badge" if p.get("pick")=="OVER" else "red-badge" if p.get("pick")=="UNDER" else "yellow-badge"
        fair = '' if p.get('fair_prob') is None else str(round(p.get('fair_prob')*100,1))+'%'
        data_w = _score_width(p.get('data_score'))
        stab_w = _score_width(p.get('stability_score'))
        opp_w = _score_width(p.get('opportunity_score'))
        val_w = _score_width((p.get('fair_prob') or 0) * 100)
        data_cls = _meter_class(p.get('data_score'))
        stab_cls = _meter_class(p.get('stability_score'))
        opp_cls = _meter_class(p.get('opportunity_score'))
        val_cls = _meter_class((p.get('fair_prob') or 0) * 100)
        mini_bars = _mini_recent_bars_from_player(p)
        audit_label = p.get("audit_label") or "Partial"
        market_txt = p.get("market_compare") or "Market: not loaded"
        recent_txt = p.get("recent_form") or "Recent form: not loaded"
        st.markdown(f"""
        <div class='pick-card'>
          <div class='player-name'>{p['player']} <span class='small-muted'>({p.get('position','')} · {p.get('team','')})</span></div>
          <span class='badge'>{p.get('prop')}</span><span class='badge'>{p.get('matchup','')}</span><span class='badge {badge_class}'>{p.get('signal')}</span><span class='badge yellow-badge'>Audit {audit_label}</span><span class='badge'>Upside {p.get('pure_upside')}</span><span class='badge'>Vol {p.get('volatility')}</span>
          <div class='small-muted'>{market_txt}</div>
          <div class='small-muted'>{recent_txt}</div>
          <div class='trust-strip'>
            <div class='trust-box'><div class='trust-label'>Data IQ</div><div class='trust-value'>{p.get('data_score')}</div><div class='progress-wrap'><div class='{data_cls}' style='width:{data_w}%'></div></div></div>
            <div class='trust-box'><div class='trust-label'>Stability</div><div class='trust-value'>{p.get('stability_score')}</div><div class='progress-wrap'><div class='{stab_cls}' style='width:{stab_w}%'></div></div></div>
            <div class='trust-box'><div class='trust-label'>Opportunity</div><div class='trust-value'>{p.get('opportunity_score')}</div><div class='progress-wrap'><div class='{opp_cls}' style='width:{opp_w}%'></div></div></div>
            <div class='trust-box'><div class='trust-label'>Fair Prob</div><div class='trust-value'>{fair or '—'}</div><div class='progress-wrap'><div class='{val_cls}' style='width:{val_w}%'></div></div></div>
          </div>
          {mini_bars}
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
                st.write(f"Offense/Defense Rank Factor: **{p.get('offense_defense_rank_factor')}**")
                st.write(f"Game Script Factor: **{p.get('game_script_factor')}**")
                st.write(f"Blowout Prob: **{p.get('blowout_prob')}**")
                st.write(f"Advanced Factor: **{p.get('advanced_factor')}**")
                audit=p.get("projection_audit") or {}
                st.write(f"Projection Audit: **{audit.get('label', p.get('audit_label',''))}** ({audit.get('score', p.get('audit_score',''))}/{audit.get('max_score', 11)})")
                hard_blocks=audit.get("hard_blocks") or []
                if hard_blocks:
                    st.write("Hard Blocks:")
                    for hb in hard_blocks: st.write("- "+str(hb))
                if p.get("market_compare"):
                    st.write(f"Market: **{p.get('market_compare')}**")
                if p.get("recent_form"):
                    st.write(f"Recent Form: **{p.get('recent_form')}**")
                if p.get("factor_stack"):
                    st.write("Factor Stack:")
                    st.json(p.get("factor_stack"))
                if p.get('xgb_assist'):
                    st.write(f"XGBoost Assist: **{(p.get('xgb_assist') or {}).get('status','OFF')}**")
                    st.write(f"Bayesian/Markov Assist: **{(p.get('bayes_markov_assist') or {}).get('status','OFF')}**")
                    st.write(f"Ensemble ML Assist: **{(p.get('ensemble_ml_assist') or {}).get('status','OFF')}**")
                    st.write("Advanced context:")
                    st.json({"xgb_assist": p.get('xgb_assist'), "bayes_markov_assist": p.get('bayes_markov_assist'), "ensemble_ml_assist": p.get('ensemble_ml_assist'), "line_sanity": p.get('line_sanity'), "projection_breakdown": p.get('projection_breakdown'), "qb_tier": p.get('qb_tier'), "offense_defense_rank_context": p.get('offense_defense_rank_context'), "advanced_context": p.get('advanced_context')})
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


PROJECTION_DATA_TEMPLATES = {
    "nfl_current_player_usage.csv": """player,team,position,snap_share,route_participation,target_share,air_yards_share,red_zone_touch_share,targets_pg,receptions_pg,pass_attempts_pg,completions_pg,receiving_yards_pg,passing_yards_pg,rush_attempts_pg,rushing_yards_pg,yards_per_carry,current_games,last5_targets_pg,last5_receptions_pg,last5_pass_attempts_pg,last5_completions_pg,last5_rush_attempts_pg
Patrick Mahomes,KC,QB,100,,,,,,,36.8,24.5,,265.4,,,,4,,,37.2,24.9,
Justin Jefferson,MIN,WR,91,94,29,39,20,9.7,6.4,,,96.2,,,,,4,10.2,6.8,,,
Christian McCaffrey,SF,RB,78,48,15,8,34,4.8,3.9,,,31.2,,18.2,86.4,4.75,4,5.1,4.0,,,18.6
""",
    "nfl_depth_chart.csv": """player,team,position,depth_rank,starter,role,slot_role,expected_routes,expected_targets,expected_attempts,expected_carries,qb_change_risk,role_note
Patrick Mahomes,KC,QB,1,TRUE,starter,,0,,37,,LOW,
Justin Jefferson,MIN,WR,1,TRUE,WR1,FALSE,36,10,,,LOW,
Christian McCaffrey,SF,RB,1,TRUE,RB1,FALSE,18,5,,18,LOW,
""",
    "nfl_market_context.csv": """player,team,prop,consensus_line,best_line,open_line,close_line,market_prob_over,market_prob_under,market_books,line_move
Patrick Mahomes,KC,Passing Yards,264.5,263.5,259.5,,0.51,0.49,5,5
Justin Jefferson,MIN,Receiving Yards,88.5,87.5,85.5,,0.52,0.48,5,3
""",
    "nfl_current_team_context.json": """{
  "KC": {
    "current_plays_pg": 64.2,
    "current_pass_rate": 61.5,
    "game_total": 48.5,
    "spread": -3.5,
    "team_total": 26.0,
    "seconds_per_play": 27.8,
    "no_huddle_rate": 8.4,
    "off_pass_rank": 4,
    "off_run_rank": 13,
    "off_scoring_rank": 5,
    "ol_pass_pro_rank": 7,
    "ol_run_block_rank": 12
  },
  "MIN": {
    "current_plays_pg": 62.8,
    "current_pass_rate": 59.2,
    "game_total": 46.5,
    "spread": 1.5,
    "team_total": 22.5,
    "off_pass_rank": 11,
    "off_run_rank": 19,
    "off_scoring_rank": 14,
    "wr_unit_rank": 3,
    "ol_pass_pro_rank": 12,
    "ol_run_block_rank": 20
  }
}
""",
    "nfl_injuries.json": """{
  "patrick mahomes": {
    "status": "ACTIVE",
    "practice_status": "FULL",
    "limited_snap_risk": 0.0,
    "expected_snap_share": 100,
    "injury_note": ""
  },
  "justin jefferson": {
    "status": "ACTIVE",
    "practice_status": "FULL",
    "limited_snap_risk": 0.0,
    "expected_snap_share": 91,
    "injury_note": ""
  }
}
""",
    "nfl_weather_context.json": """{
  "BUF @ KC": {
    "wind_mph": 8,
    "gust_mph": 13,
    "precipitation_pct": 15,
    "temperature": 42,
    "roof": "Outdoor",
    "weather_note": "Normal passing conditions"
  },
  "MIN": {
    "roof": "Dome",
    "weather_note": "Protected indoor game"
  }
}
""",
    "nfl_travel_context.csv": """team,opp,matchup,travel_miles,rest_days,opp_rest_days,timezone_shift,consecutive_road_games,international_game,neutral_site,body_clock_risk,divisional_game,rematch_game
KC,LAC,LAC @ KC,1350,7,7,0,0,FALSE,FALSE,LOW,TRUE,FALSE
MIN,GB,GB @ MIN,280,7,6,0,0,FALSE,FALSE,LOW,TRUE,FALSE
""",
    "nfl_matchup_context.csv": """team,opp,matchup,pass_funnel,run_funnel,slot_weakness,te_weakness,rb_receiving_weakness,shadow_corner,shadow_corner_grade,blitz_rate,man_rate,zone_rate,qb_pass_protection_rank,opp_def_pressure_rank,def_pass_rank,def_run_rank,def_role_rank,def_run_stop_rank
KC,LAC,LAC @ KC,TRUE,FALSE,FALSE,FALSE,FALSE,FALSE,,28,31,69,7,18,24,17,22,18
MIN,GB,GB @ MIN,FALSE,FALSE,TRUE,FALSE,FALSE,FALSE,,24,25,75,12,21,12,9,16,10
""",
    "nfl_qb_context.csv": """team,player,qb_status,qb_name,qb_change_risk,qb_injury_status,qb_blitz_grade,qb_pressure_grade,qb_deep_accuracy,qb_receiver_quality_note
KC,Patrick Mahomes,ACTIVE,Patrick Mahomes,LOW,ACTIVE,88,91,86,stable
MIN,J.J. McCarthy,ACTIVE,J.J. McCarthy,MED,ACTIVE,62,58,64,young qb volatility
""",
    "nfl_defensive_injuries.json": """{
  "LAC": {
    "missing_cb_starters": 0,
    "missing_safety_starters": 0,
    "missing_edge_starters": 0,
    "defense_injury_note": ""
  },
  "GB": {
    "missing_cb_starters": 1,
    "missing_safety_starters": 0,
    "missing_edge_starters": 0,
    "defense_injury_note": "CB1 questionable"
  }
}
""",
    "nfl_player_splits.csv": """player,prop,team,indoor_factor,dome_factor,turf_factor,grass_factor,home_factor,away_factor,rookie_flag,updated_at
Patrick Mahomes,Passing Yards,KC,1.02,1.02,1.00,0.99,1.01,0.99,FALSE,2026-09-01T10:00:00
Justin Jefferson,Receiving Yards,MIN,1.03,1.03,1.01,0.99,1.02,0.98,FALSE,2026-09-01T10:00:00
Christian McCaffrey,Rushing Yards,SF,1.00,1.00,1.01,1.00,1.01,0.99,FALSE,2026-09-01T10:00:00
""",
    "nfl_personnel_matchups.csv": """team,opp,matchup,shadow_corner,shadow_corner_grade,slot_weakness,te_weakness,rb_receiving_weakness,edge_rush_advantage,interior_run_stuffer_missing,updated_at
KC,LAC,LAC @ KC,FALSE,,0,0,0,0,0,2026-09-01T10:00:00
MIN,GB,GB @ MIN,FALSE,,1,0,0,0,0,2026-09-01T10:00:00
SF,SEA,SF @ SEA,FALSE,,0,0,0,0,1,2026-09-01T10:00:00
""",
    "nfl_final_inactives.json": """{
  "confirmed_matchups": {
    "LAC @ KC": {
      "confirmed": true,
      "updated_at": "2026-09-01T11:30:00",
      "source": "official_inactives"
    },
    "GB @ MIN": {
      "confirmed": false,
      "updated_at": "",
      "source": ""
    }
  },
  "teams": {
    "KC": {
      "confirmed": true,
      "updated_at": "2026-09-01T11:30:00"
    }
  },
  "players": {
    "patrick mahomes": {
      "status": "ACTIVE",
      "note": "",
      "updated_at": "2026-09-01T11:30:00"
    },
    "example inactive wr": {
      "status": "INACTIVE",
      "note": "officially inactive",
      "updated_at": "2026-09-01T11:30:00"
    }
  }
}
""",
    "nfl_manual_overrides.json": """{
  "players": {
    "example wr": {
      "status": "LIMITED_WORKLOAD",
      "limited_snap_risk": 0.55,
      "expected_snap_share": 62,
      "note": "coach said pitch count",
      "confidence": 0.75,
      "updated_at": "2026-09-01T10:45:00"
    }
  },
  "player_props": {
    "example rb|Rushing Yards": {
      "expected_carries": 10,
      "rush_rate": 41,
      "status": "WORKLOAD_LIMIT",
      "note": "committee expected",
      "confidence": 0.7,
      "updated_at": "2026-09-01T10:45:00"
    }
  },
  "teams": {
    "KC": {
      "pass_rate": 62,
      "pace": 54,
      "note": "normal starters expected",
      "updated_at": "2026-09-01T10:45:00"
    }
  },
  "matchups": {
    "LAC @ KC": {
      "weather_risk": "LOW",
      "game_importance": "HIGH",
      "note": "manual slate context reviewed",
      "updated_at": "2026-09-01T10:45:00"
    }
  }
}
""",
    "nfl_api_config.json": """{
  "odds_api_env": "ODDS_API_KEY",
  "weather_api_env": "WEATHER_API_KEY",
  "injury_source": "manual_or_api",
  "depth_chart_source": "manual_or_api",
  "endpoints": {
    "market": {"url": "", "api_key_env": "ODDS_API_KEY"},
    "weather": {"url": "", "api_key_env": "WEATHER_API_KEY"},
    "injuries": {"url": "", "api_key_env": ""},
    "depth": {"url": "", "api_key_env": ""},
    "final_inactives": {"url": "", "api_key_env": ""},
    "manual_overrides": {"url": "", "api_key_env": ""}
  },
  "targets": {
    "market": "nfl_market_context.csv",
    "weather": "nfl_weather_context.json",
    "injuries": "nfl_injuries.json",
    "depth": "nfl_depth_chart.csv",
    "final_inactives": "nfl_final_inactives.json",
    "manual_overrides": "nfl_manual_overrides.json"
  }
}
""",
}

def _context_file_status(path, kind):
    p=Path(path)
    out={"file": p.name, "exists": p.exists(), "rows": 0, "loaded": False, "detail": ""}
    if not p.exists():
        out["detail"]="missing"
        return out
    try:
        if kind == "csv":
            df=pd.read_csv(p)
            out["rows"]=int(len(df))
            out["loaded"]=True
            out["detail"]=f"{len(df)} rows"
        else:
            data=load_json(p,{})
            out["rows"]=len(data) if isinstance(data, dict) else 0
            out["loaded"]=isinstance(data, dict)
            out["detail"]=f"{out['rows']} keys"
    except Exception as e:
        out["detail"]=f"error: {str(e)[:80]}"
    return out

def _render_projection_data_admin():
    st.markdown("### Projection Data")
    st.caption("Fresh files here override last-season defaults without breaking startup.")
    status=[
        _context_file_status(USAGE_FILE, "csv"),
        _context_file_status(CURRENT_USAGE_FILE, "csv"),
        _context_file_status(CURRENT_TEAM_CONTEXT_FILE, "json"),
        _context_file_status(INJURY_FILE, "json"),
        _context_file_status(DEPTH_CHART_FILE, "csv"),
        _context_file_status(WEATHER_FILE, "json"),
        _context_file_status(MARKET_CONTEXT_FILE, "csv"),
        _context_file_status(TRAVEL_CONTEXT_FILE, "csv"),
        _context_file_status(MATCHUP_CONTEXT_FILE, "csv"),
        _context_file_status(QB_CONTEXT_FILE, "csv"),
        _context_file_status(DEF_INJURY_FILE, "json"),
        _context_file_status(SPLITS_CONTEXT_FILE, "csv"),
        _context_file_status(PERSONNEL_CONTEXT_FILE, "csv"),
        _context_file_status(FINAL_INACTIVES_FILE, "json"),
        _context_file_status(MANUAL_OVERRIDE_FILE, "json"),
        _context_file_status(API_CONFIG_FILE, "json"),
        _context_file_status(PHASE6_PLAYER_SUMMARY_FILE, "csv"),
        _context_file_status(PHASE6_DEFENSE_RANK_FILE, "csv"),
        _context_file_status(PHASE6_TEAM_ADVANCED_FILE, "csv"),
        _context_file_status(PHASE6_RED_ZONE_FILE, "csv"),
        _context_file_status(PHASE6_OT_FILE, "csv"),
    ]
    loaded=sum(1 for s in status if s.get("loaded"))
    st.metric("Fresh context files", f"{loaded}/{len(status)}")
    st.dataframe(pd.DataFrame(status)[["file","exists","rows","detail"]], use_container_width=True, hide_index=True)
    st.caption(f"Storage folder: {LOCAL_DIR}")
    if st.button("Create Starter Context Files", use_container_width=True, key="create_starter_context_files"):
        written=[]
        try:
            LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            for filename, payload in PROJECTION_DATA_TEMPLATES.items():
                target=LOCAL_DIR / filename
                target.write_text(payload, encoding="utf-8")
                written.append(filename)
            st.success("Created starter context files: " + ", ".join(written))
            st.rerun()
        except Exception as e:
            st.error(f"Could not create starter files: {e}")
    for filename, payload in PROJECTION_DATA_TEMPLATES.items():
        mime="application/json" if filename.endswith(".json") else "text/csv"
        st.download_button(
            f"Template: {filename}",
            data=payload.encode("utf-8"),
            file_name=filename,
            mime=mime,
            use_container_width=True,
            key=f"context_template_{filename}",
        )
    st.divider()
    st.caption("Upload completed context files here. Saved files are used on the next rerun.")
    upload_targets=[
        ("Last-season player usage", USAGE_FILE, ["csv"]),
        ("Current player usage", CURRENT_USAGE_FILE, ["csv"]),
        ("Current team context", CURRENT_TEAM_CONTEXT_FILE, ["json"]),
        ("Injuries", INJURY_FILE, ["json"]),
        ("Depth chart", DEPTH_CHART_FILE, ["csv"]),
        ("Weather", WEATHER_FILE, ["json"]),
        ("Market context", MARKET_CONTEXT_FILE, ["csv"]),
        ("Travel/rest context", TRAVEL_CONTEXT_FILE, ["csv"]),
        ("Matchup style context", MATCHUP_CONTEXT_FILE, ["csv"]),
        ("QB dependency context", QB_CONTEXT_FILE, ["csv"]),
        ("Defensive injuries", DEF_INJURY_FILE, ["json"]),
        ("Player splits", SPLITS_CONTEXT_FILE, ["csv"]),
        ("Personnel matchups", PERSONNEL_CONTEXT_FILE, ["csv"]),
        ("Final inactives", FINAL_INACTIVES_FILE, ["json"]),
        ("Manual news overrides", MANUAL_OVERRIDE_FILE, ["json"]),
        ("API config", API_CONFIG_FILE, ["json"]),
        ("Phase 6 player summary", PHASE6_PLAYER_SUMMARY_FILE, ["csv"]),
        ("Phase 6 defense ranks", PHASE6_DEFENSE_RANK_FILE, ["csv"]),
        ("Phase 6 team advanced", PHASE6_TEAM_ADVANCED_FILE, ["csv"]),
        ("Phase 6 red zone usage", PHASE6_RED_ZONE_FILE, ["csv"]),
        ("Phase 6 overtime context", PHASE6_OT_FILE, ["csv"]),
    ]
    target_by_filename={Path(target).name: (label, Path(target), types) for label, target, types in upload_targets}

    def _save_context_upload_bytes(filename, data):
        clean_name=Path(str(filename or "")).name
        if clean_name not in target_by_filename:
            return {"file": clean_name, "status": "SKIPPED", "detail": "filename not recognized"}
        label, target, types=target_by_filename[clean_name]
        try:
            suffix=target.suffix.lower()
            if suffix == ".json":
                json.loads(data.decode("utf-8"))
                rows="json ok"
            else:
                preview=pd.read_csv(io.BytesIO(data), nrows=5)
                rows=f"csv ok ({len(preview)} preview rows)"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return {"file": clean_name, "status": "SAVED", "target": target.name, "detail": rows}
        except Exception as e:
            return {"file": clean_name, "status": "ERROR", "target": target.name, "detail": str(e)[:120]}

    st.markdown("#### Bulk Auto-Route Upload")
    st.caption("Upload the ZIP I gave you, or select multiple CSV/JSON files. Files are routed by exact filename.")
    bulk_uploads=st.file_uploader(
        "Upload ZIP or multiple context files",
        type=["zip","csv","json"],
        accept_multiple_files=True,
        key="bulk_context_auto_route_upload",
    )
    if bulk_uploads and st.button("Save All Recognized Files", use_container_width=True, key="save_bulk_context_auto_route"):
        results=[]
        for uploaded in bulk_uploads:
            name=Path(uploaded.name).name
            data=uploaded.read()
            if name.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for member in z.infolist():
                            if member.is_dir():
                                continue
                            member_name=Path(member.filename).name
                            if not member_name:
                                continue
                            results.append(_save_context_upload_bytes(member_name, z.read(member)))
                except Exception as e:
                    results.append({"file": name, "status": "ERROR", "detail": f"zip read failed: {str(e)[:100]}"})
            else:
                results.append(_save_context_upload_bytes(name, data))
        rdf=pd.DataFrame(results)
        saved=int((rdf["status"]=="SAVED").sum()) if not rdf.empty and "status" in rdf.columns else 0
        if saved:
            st.success(f"Saved {saved} context files.")
        else:
            st.warning("No recognized files were saved. Make sure filenames match the templates.")
        st.dataframe(rdf, use_container_width=True, hide_index=True)
        st.rerun()

    st.divider()
    st.caption("Or upload one file at a time below. Saved files are used on the next rerun.")
    for label, target, types in upload_targets:
        uploaded=st.file_uploader(label, type=types, key=f"context_upload_{Path(target).name}")
        if uploaded is not None and st.button(f"Save {Path(target).name}", use_container_width=True, key=f"context_save_{Path(target).name}"):
            try:
                data=uploaded.read()
                if Path(target).suffix == ".json":
                    json.loads(data.decode("utf-8"))
                else:
                    pd.read_csv(io.BytesIO(data), nrows=5)
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(data)
                st.success(f"Saved {Path(target).name}")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save {Path(target).name}: {e}")
    st.divider()
    _render_api_automation_panel()
    st.divider()
    _render_manual_override_panel()


def _render_phase6_admin():
    st.markdown("### Admin: Phase 6 Database")
    st.caption("Builds or repairs the missing player summary, defense ranks, team advanced, red-zone, overtime, usage, and team-context files. Saved files load automatically after the build.")
    season_to_build = st.number_input("Last season to build", min_value=1999, max_value=2030, value=NFL_LAST_SEASON, step=1, key="phase6_admin_season")
    existing_ready = _phase6_existing_database_ready()
    st.metric("Saved database", "READY" if existing_ready else "NOT BUILT")
    if PHASE6_MANIFEST_FILE.exists() and st.checkbox("Show last saved Phase 6 build", value=False, key="show_phase6_last_saved_build"):
        st.json(load_json(PHASE6_MANIFEST_FILE, {}))
    if st.button("🛠️ Build / Repair Projection Database", use_container_width=True, key="phase6_use_saved_sidebar"):
        diag = build_phase6_nfl_database(int(season_to_build), force_refresh=False)
        if diag.get("status") in ["BUILT_AND_SAVED", "USING_SAVED_DATABASE", "PULL_FAILED_USING_SAVED_DATABASE", "USING_SAVED_LOCAL_DATABASE", "USING_GITHUB_HARD_INPUT_DATABASE", "BUILT_AND_SAVED_PHASE6_V3"]:
            st.success(f"Phase 6 ready: {diag.get('status')}.")
        else:
            st.warning(f"Phase 6 database not built: {diag.get('status')}")
        st.json(diag)
    if st.button("🌐 Force Refresh Projection Data", use_container_width=True, key="phase6_force_sidebar"):
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

def _render_slate_context(rows):
    if not rows:
        st.info("No slate rows loaded.")
        return
    game_map={}
    pending=0
    for p in rows:
        team=_normalize_nfl_team(p.get("team"))
        opp=_normalize_nfl_team(p.get("opp"))
        matchup=_canonical_matchup(p.get("matchup"), team, opp, p.get("home_away"))
        if not matchup:
            pending += 1
            continue
        game_map.setdefault(matchup, []).append(p)
    if pending:
        st.caption(f"{pending} valid player props are hidden from Slate Context until an opponent/game relationship is available. They remain on the projection board.")
    if not game_map:
        st.info("No fully validated two-team matchups are available yet.")
        return
    slate=[]
    for matchup, props in game_map.items():
        first=props[0]
        env=first.get("env") or {}
        audit_counts=pd.Series([x.get("audit_label","") for x in props]).value_counts().to_dict()
        hard_blocks=sum(len((x.get("projection_audit") or {}).get("hard_blocks") or []) for x in props)
        travel=travel_difficulty_score(first)
        slate.append({
            "matchup": matchup,
            "props": len(props),
            "teams": " / ".join(sorted(set(_normalize_nfl_team(x.get("team")) for x in props if _normalize_nfl_team(x.get("team"))))),
            "stadium": env.get("stadium","") or "Pending",
            "roof": env.get("roof","") or "Pending",
            "surface": env.get("surface","") or "Pending",
            "crowd": env.get("crowd","") or "Pending",
            "noise": env.get("noise","") or "Pending",
            "game_total": first.get("game_total"),
            "spread": first.get("spread"),
            "weather_risk": first.get("weather_risk"),
            "travel_difficulty": travel.get("label"),
            "travel_score": travel.get("score"),
            "rest_days": first.get("rest_days"),
            "opp_rest_days": first.get("opp_rest_days"),
            "travel_miles": first.get("travel_miles"),
            "fresh": audit_counts.get("Fresh",0),
            "partial": audit_counts.get("Partial",0),
            "stale": audit_counts.get("Stale",0),
            "hard_blocks": hard_blocks,
        })
    st.dataframe(pd.DataFrame(slate), use_container_width=True, hide_index=True, height=min(600, 42*(len(slate)+1)+38))

def _correlation_warning(p1, p2):
    warnings=[]
    if not p1 or not p2:
        return "Neutral"
    same_game=p1.get("matchup")==p2.get("matchup")
    same_team=p1.get("team")==p2.get("team")
    if same_game and same_team and p1.get("player") != p2.get("player"):
        if p1.get("prop") == "Passing Yards" and p2.get("prop") == "Receiving Yards":
            warnings.append("Positive QB/receiver stack")
        elif p1.get("prop") == p2.get("prop"):
            warnings.append("Same-team usage conflict")
    if same_game and not same_team:
        if p1.get("prop") in ["Passing Yards","Receiving Yards"] and p2.get("prop") in ["Passing Yards","Receiving Yards"]:
            warnings.append("Game-script/shootout correlation")
    if str(p1.get("weather_risk") or p2.get("weather_risk") or "").upper() in ["WIND","SEVERE","RAIN","SNOW"]:
        warnings.append("Shared weather risk")
    if p1.get("audit_label") == "Stale" or p2.get("audit_label") == "Stale":
        warnings.append("One leg has stale projection context")
    if p1.get("game_script_risk") == "HIGH" or p2.get("game_script_risk") == "HIGH":
        warnings.append("Shared game-script volatility")
    return "; ".join(warnings) if warnings else "Neutral"

def _result_time_value(row, fallback_index=0):
    for key in ["graded_at","saved_at","game_date","event_time","generated_at","date"]:
        dt=_parse_any_datetime((row or {}).get(key))
        if dt: return dt
    return datetime(2000,1,1)+timedelta(seconds=fallback_index)

def walk_forward_backtest(rows):
    """Leakage-safe calibration test: each row only uses results graded before it."""
    ordered=sorted([dict(r) for r in (rows or []) if safe_float(r.get("projection")) is not None and safe_float(r.get("actual")) is not None],key=lambda r:_result_time_value(r))
    prior=[]; output=[]
    for idx,r in enumerate(ordered):
        prop=str(r.get("prop") or ""); pos=str(r.get("position") or "").upper(); role=r.get("role_bucket") or projection_role_bucket(r); quality=r.get("data_quality_bucket") or projection_data_quality_bucket(r)
        levels=[
            ("exact",12,[x for x in prior if str(x.get("prop") or "")==prop and str(x.get("position") or "").upper()==pos and (x.get("role_bucket") or projection_role_bucket(x))==role and (x.get("data_quality_bucket") or projection_data_quality_bucket(x))==quality]),
            ("role",18,[x for x in prior if str(x.get("prop") or "")==prop and str(x.get("position") or "").upper()==pos and (x.get("role_bucket") or projection_role_bucket(x))==role]),
            ("position",24,[x for x in prior if str(x.get("prop") or "")==prop and str(x.get("position") or "").upper()==pos]),
            ("prop",32,[x for x in prior if str(x.get("prop") or "")==prop]),
        ]
        scale=1.0; level="warming"; n=0
        for label,minimum,sample in levels:
            if len(sample)>=minimum:
                bias=_robust_calibration_bias(sample[-120:]); shrink=len(sample)/(len(sample)+30.0); scale=1+clamp(bias*0.42*shrink,-NFL_CALIBRATION_MAX_SHIFT_PCT,NFL_CALIBRATION_MAX_SHIFT_PCT); level=label; n=len(sample); break
        proj=safe_float(r.get("projection")); actual=safe_float(r.get("actual")); line=safe_float(r.get("line")); cal_proj=proj*scale
        raw_hit=cal_hit=None
        if line is not None:
            actual_side="OVER" if actual>line else "UNDER" if actual<line else "PUSH"
            raw_side="OVER" if proj>line else "UNDER"; cal_side="OVER" if cal_proj>line else "UNDER"
            raw_hit=None if actual_side=="PUSH" else int(raw_side==actual_side); cal_hit=None if actual_side=="PUSH" else int(cal_side==actual_side)
        fair=safe_float(r.get("fair_prob")); brier=None
        if fair is not None and raw_hit is not None: brier=(fair-raw_hit)**2
        output.append({"date":_result_time_value(r,idx),"player":r.get("player"),"prop":prop,"position":pos,"role_bucket":role,"data_quality_bucket":quality,"projection":proj,"calibrated_projection":cal_proj,"actual":actual,"raw_abs_error":abs(actual-proj),"cal_abs_error":abs(actual-cal_proj),"raw_hit":raw_hit,"cal_hit":cal_hit,"brier":brier,"calibration_level":level,"prior_samples":n,"scale":scale})
        prior.append(r)
    return pd.DataFrame(output)

def _render_backtest_dashboard():
    rows=load_json(RESULT_LOG, [])
    graded=[r for r in rows if r.get("win") is not None]
    if not graded:
        st.info("No graded props yet. Save and grade slates to unlock backtesting.")
        return
    rdf=pd.DataFrame(graded)
    if "win" in rdf.columns:
        rdf["win_num"]=rdf["win"].astype(float)
    if "edge" in rdf.columns:
        rdf["edge_bucket"]=pd.cut(pd.to_numeric(rdf["edge"], errors="coerce").abs(), bins=[-0.01,5,10,18,999], labels=["0-5","5-10","10-18","18+"])
    if "data_score" in rdf.columns:
        rdf["data_bucket"]=pd.cut(pd.to_numeric(rdf["data_score"], errors="coerce"), bins=[-1,69,81,89,99], labels=["<70","70-81","82-89","90+"])
    c1,c2,c3=st.columns(3)
    c1.metric("Graded Props", len(rdf))
    c2.metric("Hit Rate", f"{rdf['win_num'].mean()*100:.1f}%" if "win_num" in rdf else "n/a")
    c3.metric("Avg Error", f"{pd.to_numeric(rdf.get('projection_error'), errors='coerce').mean():.2f}" if "projection_error" in rdf else "n/a")
    group_cols=[c for c in ["prop","edge_bucket","data_bucket","audit_label"] if c in rdf.columns]
    for col in group_cols:
        summary=rdf.groupby(col, dropna=False)["win_num"].agg(["count","mean"]).reset_index()
        summary["hit_rate_pct"]=(summary["mean"]*100).round(1)
        st.write(f"By {col}")
        st.dataframe(summary.drop(columns=["mean"]), use_container_width=True, hide_index=True)
    st.markdown("### Walk-forward accuracy (no future leakage)")
    wf=walk_forward_backtest(graded)
    if wf.empty:
        st.info("Not enough complete projection/actual rows for walk-forward testing.")
    else:
        w1,w2,w3,w4=st.columns(4)
        w1.metric("Raw MAE",f"{wf['raw_abs_error'].mean():.2f}")
        w2.metric("Calibrated MAE",f"{wf['cal_abs_error'].mean():.2f}",delta=f"{wf['raw_abs_error'].mean()-wf['cal_abs_error'].mean():+.2f} better")
        valid=wf.dropna(subset=['raw_hit','cal_hit'])
        w3.metric("Raw Hit Rate",f"{valid['raw_hit'].mean()*100:.1f}%" if not valid.empty else "n/a")
        w4.metric("Cal Hit Rate",f"{valid['cal_hit'].mean()*100:.1f}%" if not valid.empty else "n/a")
        cols=["prop","position","role_bucket","data_quality_bucket"]
        summary=wf.groupby(cols,dropna=False).agg(count=("actual","count"),raw_mae=("raw_abs_error","mean"),cal_mae=("cal_abs_error","mean"),raw_hit=("raw_hit","mean"),cal_hit=("cal_hit","mean")).reset_index()
        summary["raw_hit_pct"]=(summary["raw_hit"]*100).round(1); summary["cal_hit_pct"]=(summary["cal_hit"]*100).round(1)
        summary["mae_gain"]=(summary["raw_mae"]-summary["cal_mae"]).round(3)
        st.dataframe(summary.drop(columns=["raw_hit","cal_hit"]).sort_values(["count","mae_gain"],ascending=[False,False]),use_container_width=True,hide_index=True)
        brier=pd.to_numeric(wf["brier"],errors="coerce").dropna()
        if len(brier): st.caption(f"Probability Brier score: {brier.mean():.4f} · lower is better.")

def _exposure_rows(rows):
    out=[]
    candidates=[r for r in rows if r.get("action_tier") in ["BET","LEAN"]]
    for key,label in [("team","Team"),("matchup","Game"),("qb_name","QB"),("weather_risk","Weather"),("source","Source")]:
        counts={}
        for r in candidates:
            v=r.get(key) or (r.get("qb_qb_name") if key == "qb_name" else None) or "UNKNOWN"
            counts[str(v)] = counts.get(str(v),0)+1
        for v,c in counts.items():
            if c >= 2:
                risk="HIGH" if c >= 4 else "MED"
                out.append({"type":label,"value":v,"legs":c,"risk":risk})
    return out

def _render_exposure_dashboard(rows):
    st.markdown("<div class='section-title-pro'>Portfolio Exposure</div>", unsafe_allow_html=True)
    exposure=_exposure_rows(rows)
    if exposure:
        st.dataframe(pd.DataFrame(exposure), use_container_width=True, hide_index=True)
    else:
        st.info("No concentrated exposure among BET/LEAN rows.")
    stack_rows=[]
    for r in rows:
        if r.get("action_tier") in ["BET","LEAN"]:
            stack_rows.append({
                "player":r.get("player"),"prop":r.get("prop"),"team":r.get("team"),"matchup":r.get("matchup"),
                "pick":r.get("pick"),"signal":r.get("signal"),"audit":r.get("audit_label"),
                "weather":r.get("weather_risk"),"qb":r.get("qb_name") or r.get("qb_qb_name"),
                "hard_blocks":"; ".join((r.get("projection_audit") or {}).get("hard_blocks") or [])
            })
    if stack_rows:
        st.write("Candidate exposure rows")
        st.dataframe(pd.DataFrame(stack_rows), use_container_width=True, hide_index=True)

def _render_closing_review(rows):
    st.markdown("<div class='section-title-pro'>Closing Review</div>", unsafe_allow_html=True)
    review=[]
    for r in rows:
        audit=r.get("projection_audit") or {}
        hard=audit.get("hard_blocks") or []
        cal=r.get("calibration_status") or {}
        review.append({
            "player":r.get("player"),"team":r.get("team"),"matchup":r.get("matchup"),"prop":r.get("prop"),
            "pick":r.get("pick"),"line":r.get("line"),"projection":r.get("projection"),"edge":r.get("edge"),
            "fair_prob":r.get("fair_prob"),"signal":r.get("signal"),"tier":r.get("action_tier"),
            "audit":r.get("audit_label"),"market":r.get("market_compare"),"recent":r.get("recent_form"),
            "final_inactives":r.get("final_inactive_status") or ("CONFIRMED" if str(r.get("final_inactives_confirmed")).upper() in ["TRUE","1","YES"] else "UNCONFIRMED" if str(r.get("final_inactives_confirmed")).upper() in ["FALSE","0","NO"] else ""),
            "calibration":f"{cal.get('label','')} {cal.get('graded_rows','')}/{cal.get('target_rows','')}".strip(),
            "model_version":r.get("model_version"),"hard_blocks":"; ".join(hard),
            "ready":bool(r.get("action_tier") == "BET" and not hard and r.get("source") != "DEMO"),
        })
    rdf=pd.DataFrame(review)
    if rdf.empty:
        st.info("No rows loaded.")
        return
    c1,c2,c3=st.columns(3)
    c1.metric("Ready", int(rdf["ready"].sum()))
    c2.metric("Blocked", int((rdf["hard_blocks"].astype(str).str.len()>0).sum()))
    c3.metric("Model", MODEL_VERSION)
    st.dataframe(rdf.sort_values(["ready","fair_prob"], ascending=[False,False]), use_container_width=True, hide_index=True)

def _current_week_context_from_nflverse(season=NFL_CURRENT_SEASON, force_refresh=True):
    """Save current-season nflverse player/team context into the app's live context files."""
    season=int(season)
    weekly=fetch_nflverse_player_weekly_stats(season, force_refresh=force_refresh)
    if weekly.empty:
        request_log("AUTO_CURRENT_CONTEXT", "NO_DATA", f"season={season}")
        return {"status":"NO_DATA", "season":season, "players":0, "teams":0}
    logs=weekly.copy()
    if "player_display_name" in logs.columns and "player" not in logs.columns:
        logs["player"]=logs["player_display_name"]
    if "recent_team" in logs.columns and "team" not in logs.columns:
        logs["team"]=logs["recent_team"]
    if "position" not in logs.columns:
        logs["position"]=""
    for c in ["attempts","completions","passing_yards","targets","receptions","receiving_yards","air_yards","carries","rushing_yards"]:
        if c not in logs.columns:
            logs[c]=0
        logs[c]=pd.to_numeric(logs[c], errors="coerce").fillna(0)
    logs["week_num"]=pd.to_numeric(logs["week"], errors="coerce").fillna(0) if "week" in logs.columns else np.arange(len(logs))
    # nflverse snap counts add the most important current-week role signal.
    snap_bank={}
    try:
        snaps=fetch_nflverse_snap_counts(season, force_refresh=force_refresh)
        if not snaps.empty:
            s=snaps.copy()
            pcol=next((c for c in ["player","player_name","pfr_player_name"] if c in s.columns),None)
            tcol=next((c for c in ["team","recent_team"] if c in s.columns),None)
            pctcol=next((c for c in ["offense_pct","offense_snap_pct","offense_percentage"] if c in s.columns),None)
            if pcol and tcol and pctcol:
                s["_pct"]=pd.to_numeric(s[pctcol],errors="coerce")
                s.loc[s["_pct"]<=1.01,"_pct"]*=100
                if "week" in s.columns: s["_week"]=pd.to_numeric(s["week"],errors="coerce").fillna(0)
                else: s["_week"]=np.arange(len(s))
                for (pl,tm),sg in s.groupby([pcol,tcol],dropna=False):
                    sg=sg.sort_values("_week"); vals=sg["_pct"].dropna()
                    if len(vals): snap_bank[(norm(pl),str(tm or ""))]={"snap_share":round(float(vals.mean()),2),"last3_snap_share":round(float(vals.tail(3).mean()),2),"last5_snap_share":round(float(vals.tail(5).mean()),2)}
    except Exception as e:
        request_log("AUTO_CURRENT_SNAPS","ERROR",str(e)[:180])
    player_rows=[]
    for (player, team, pos), g in logs.groupby(["player","team","position"], dropna=False):
        player=str(player or "").strip()
        if not player:
            continue
        g=g.sort_values("week_num")
        gp=max(1, len(g))
        tail3=g.tail(3); tail5=g.tail(5)
        targets=float(g["targets"].sum())
        air=float(g["air_yards"].sum())
        team_targets=logs[logs["team"].astype(str)==str(team)]["targets"].sum()
        snap_ctx=snap_bank.get((norm(player),str(team or "")),{})
        player_rows.append({
            "player":player,
            "team":str(team or ""),
            "position":str(pos or ""),
            "snap_share":snap_ctx.get("snap_share",""),
            "last3_snap_share":snap_ctx.get("last3_snap_share",""),
            "last5_snap_share":snap_ctx.get("last5_snap_share",""),
            "route_participation":"",
            "target_share":round(100*targets/max(1.0, float(team_targets)),2) if team_targets else "",
            "air_yards_share":"",
            "red_zone_touch_share":"",
            "targets_pg":round(targets/gp,3),
            "receptions_pg":round(float(g["receptions"].sum())/gp,3),
            "pass_attempts_pg":round(float(g["attempts"].sum())/gp,3),
            "completions_pg":round(float(g["completions"].sum())/gp,3),
            "receiving_yards_pg":round(float(g["receiving_yards"].sum())/gp,3),
            "passing_yards_pg":round(float(g["passing_yards"].sum())/gp,3),
            "rush_attempts_pg":round(float(g["carries"].sum())/gp,3),
            "rushing_yards_pg":round(float(g["rushing_yards"].sum())/gp,3),
            "yards_per_carry":round(float(g["rushing_yards"].sum())/max(1.0, float(g["carries"].sum())),3),
            "current_games":int(gp),
            "last3_pass_attempts_pg":round(float(tail3["attempts"].mean()),3) if not tail3.empty else "",
            "last3_passing_yards_pg":round(float(tail3["passing_yards"].mean()),3) if not tail3.empty else "",
            "last3_completions_pg":round(float(tail3["completions"].mean()),3) if not tail3.empty else "",
            "last3_targets_pg":round(float(tail3["targets"].mean()),3) if not tail3.empty else "",
            "last3_receptions_pg":round(float(tail3["receptions"].mean()),3) if not tail3.empty else "",
            "last3_rush_attempts_pg":round(float(tail3["carries"].mean()),3) if not tail3.empty else "",
            "last3_receiving_yards_pg":round(float(tail3["receiving_yards"].mean()),3) if not tail3.empty else "",
            "last3_rushing_yards_pg":round(float(tail3["rushing_yards"].mean()),3) if not tail3.empty else "",
            "last5_pass_attempts_pg":round(float(tail5["attempts"].mean()),3) if not tail5.empty else "",
            "last5_passing_yards_pg":round(float(tail5["passing_yards"].mean()),3) if not tail5.empty else "",
            "last5_completions_pg":round(float(tail5["completions"].mean()),3) if not tail5.empty else "",
            "last5_targets_pg":round(float(tail5["targets"].mean()),3) if not tail5.empty else "",
            "last5_receptions_pg":round(float(tail5["receptions"].mean()),3) if not tail5.empty else "",
            "last5_rush_attempts_pg":round(float(tail5["carries"].mean()),3) if not tail5.empty else "",
            "last5_receiving_yards_pg":round(float(tail5["receiving_yards"].mean()),3) if not tail5.empty else "",
            "last5_rushing_yards_pg":round(float(tail5["rushing_yards"].mean()),3) if not tail5.empty else "",
            "updated_at":now_iso(),
        })
    team_context={}
    for team, g in logs.groupby("team", dropna=False):
        team=str(team or "").strip()
        if not team:
            continue
        weeks=max(1, g["week_num"].nunique())
        pass_att=float(g["attempts"].sum())
        rush_att=float(g["carries"].sum())
        plays=pass_att+rush_att
        team_context[team]={
            "current_plays_pg":round(plays/weeks,2),
            "current_pass_rate":round(100*pass_att/max(1.0, plays),2),
            "current_rush_rate":round(100*rush_att/max(1.0, plays),2),
            "updated_at":now_iso(),
            "source":f"nflverse_player_weekly_{season}",
        }
    if player_rows:
        pd.DataFrame(player_rows).to_csv(CURRENT_USAGE_FILE, index=False)
    if team_context:
        save_json(CURRENT_TEAM_CONTEXT_FILE, team_context)
    request_log("AUTO_CURRENT_CONTEXT", "SAVED", f"season={season} players={len(player_rows)} teams={len(team_context)}")
    return {"status":"SAVED", "season":season, "players":len(player_rows), "teams":len(team_context), "files":[CURRENT_USAGE_FILE.name, CURRENT_TEAM_CONTEXT_FILE.name]}

def _vendor_endpoint_map(cfg):
    endpoints=cfg.get("endpoints") if isinstance(cfg.get("endpoints"), dict) else {}
    legacy={
        "market": cfg.get("market_url"),
        "weather": cfg.get("weather_url"),
        "injuries": cfg.get("injury_url"),
        "depth": cfg.get("depth_chart_url"),
        "final_inactives": cfg.get("final_inactives_url"),
        "manual_overrides": cfg.get("manual_overrides_url"),
    }
    for k,v in legacy.items():
        if v and k not in endpoints:
            endpoints[k]=v
    return endpoints

def _endpoint_target(name):
    return {
        "market": MARKET_CONTEXT_FILE,
        "weather": WEATHER_FILE,
        "injuries": INJURY_FILE,
        "depth": DEPTH_CHART_FILE,
        "final_inactives": FINAL_INACTIVES_FILE,
        "manual_overrides": MANUAL_OVERRIDE_FILE,
        "current_player_usage": CURRENT_USAGE_FILE,
        "current_team_context": CURRENT_TEAM_CONTEXT_FILE,
        "travel": TRAVEL_CONTEXT_FILE,
        "matchup": MATCHUP_CONTEXT_FILE,
        "qb": QB_CONTEXT_FILE,
        "defensive_injuries": DEF_INJURY_FILE,
        "splits": SPLITS_CONTEXT_FILE,
        "personnel": PERSONNEL_CONTEXT_FILE,
    }.get(str(name or ""))

def _download_context_endpoint(name, spec, cfg):
    target=_endpoint_target(name)
    if target is None:
        return {"name":name, "status":"SKIPPED", "detail":"unknown target"}
    if isinstance(spec, str):
        url=spec; api_env=""; headers={}
    elif isinstance(spec, dict):
        url=spec.get("url") or spec.get("endpoint")
        api_env=spec.get("api_key_env") or spec.get("key_env") or ""
        headers=dict(spec.get("headers") or {})
    else:
        return {"name":name, "status":"SKIPPED", "detail":"bad endpoint spec"}
    if not url:
        return {"name":name, "status":"SKIPPED", "detail":"missing url"}
    if api_env:
        key=get_secret(api_env, "")
        if key:
            headers.setdefault("Authorization", f"Bearer {key}")
            headers.setdefault("x-api-key", key)
    try:
        r=requests.get(url, headers=headers or {"User-Agent":"NFLPropEngine/1.0"}, timeout=18)
        if r.status_code != 200:
            request_log("AUTO_ENDPOINT", "HTTP_ERROR", f"{name} {r.status_code} {url}")
            return {"name":name, "status":"HTTP_ERROR", "detail":r.status_code}
        data=r.content
        suffix=target.suffix.lower()
        if suffix == ".json":
            parsed=json.loads(data.decode("utf-8"))
            save_json(target, parsed)
            rows=len(parsed) if isinstance(parsed, dict) else len(parsed) if isinstance(parsed, list) else 1
        else:
            df=pd.read_csv(io.BytesIO(data))
            df.to_csv(target, index=False)
            rows=len(df)
        request_log("AUTO_ENDPOINT", "SAVED", f"{name} -> {target.name} rows={rows}")
        return {"name":name, "status":"SAVED", "file":target.name, "rows":rows}
    except Exception as e:
        request_log("AUTO_ENDPOINT", "ERROR", f"{name}: {str(e)[:240]}")
        return {"name":name, "status":"ERROR", "detail":str(e)[:120]}

def auto_refresh_projection_context(refresh_current=True, refresh_underdog=True, refresh_endpoints=True, force_refresh=True):
    cfg=load_api_config()
    results=[]
    if refresh_underdog:
        try:
            fetch_underdog_nfl_props.clear()
            fetch_underdog_nfl_moneylines.clear()
            rows=fetch_underdog_nfl_props(int(time.time()))
            money_rows=fetch_underdog_nfl_moneylines(int(time.time()))
            board=save_last_pulled_board(rows, money_rows)
            rows=board.get("rows") or []
            if rows:
                market_rows=[]
                for r in rows:
                    market_rows.append({
                        "player":r.get("player"), "team":r.get("team"), "prop":r.get("prop"),
                        "consensus_line":r.get("line"), "best_line":r.get("line"),
                        "open_line":"", "close_line":"", "market_prob_over":"", "market_prob_under":"",
                        "market_books":1, "line_move":"", "updated_at":now_iso(), "source":"Underdog live board"
                    })
                pd.DataFrame(market_rows).to_csv(MARKET_CONTEXT_FILE, index=False)
            results.append({"name":"underdog_board", "status":"SAVED", "rows":len(rows), "file":BOARD_CACHE_FILE.name})
        except Exception as e:
            results.append({"name":"underdog_board", "status":"ERROR", "detail":str(e)[:120]})
    if refresh_current:
        results.append({"name":"nflverse_current_context", **_current_week_context_from_nflverse(NFL_CURRENT_SEASON, force_refresh=force_refresh)})
    if refresh_endpoints:
        for name,spec in _vendor_endpoint_map(cfg).items():
            results.append(_download_context_endpoint(name, spec, cfg))
    save_json(LOCAL_DIR / "nfl_auto_refresh_last_run.json", {"ran_at":now_iso(), "results":results})
    return results

def _render_api_automation_panel():
    st.markdown("### API Automation")
    cfg=load_api_config()
    st.caption("Refresh live board/cache, save current nflverse context, and pull optional vendor endpoints into local context files.")
    default_cfg={
        "odds_api_env": "ODDS_API_KEY",
        "weather_api_env": "WEATHER_API_KEY",
        "injury_source": "manual_or_api",
        "depth_chart_source": "manual_or_api",
        "endpoints": {
            "market": {"url": "", "api_key_env": "ODDS_API_KEY"},
            "weather": {"url": "", "api_key_env": "WEATHER_API_KEY"},
            "injuries": {"url": "", "api_key_env": ""},
            "depth": {"url": "", "api_key_env": ""},
            "final_inactives": {"url": "", "api_key_env": ""},
            "manual_overrides": {"url": "", "api_key_env": ""}
        },
        "targets": {
            "market": str(MARKET_CONTEXT_FILE),
            "weather": str(WEATHER_FILE),
            "injuries": str(INJURY_FILE),
            "depth": str(DEPTH_CHART_FILE),
            "final_inactives": str(FINAL_INACTIVES_FILE),
            "manual_overrides": str(MANUAL_OVERRIDE_FILE)
        }
    }
    st.json(cfg or default_cfg)
    c1,c2,c3=st.columns(3)
    with c1:
        run_underdog=st.checkbox("Refresh Underdog board", value=True, key="auto_refresh_underdog")
    with c2:
        run_current=st.checkbox("Build current nflverse files", value=True, key="auto_refresh_current")
    with c3:
        run_endpoints=st.checkbox("Pull configured endpoints", value=True, key="auto_refresh_endpoints")
    if st.button("Run Auto Refresh Now", use_container_width=True, key="run_auto_projection_refresh"):
        with st.spinner("Refreshing projection context..."):
            results=auto_refresh_projection_context(run_current, run_underdog, run_endpoints, force_refresh=True)
        st.success("Auto refresh finished.")
        st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
        st.rerun()
    last=load_json(LOCAL_DIR / "nfl_auto_refresh_last_run.json", {})
    if last and st.checkbox("Show last auto refresh", value=False, key="show_last_auto_refresh"):
        st.json(last)

def _render_manual_override_panel():
    st.markdown("### Manual News Override")
    st.caption("Fast Sunday override for workload, status, QB change, weather, or coach-news adjustments.")
    data=load_manual_overrides()
    player=st.text_input("Player override", value="", placeholder="Player name", key="manual_override_player")
    prop=st.selectbox("Optional prop", ["", *ACTIVE_NFL_MARKET_ORDER], index=0, key="manual_override_prop")
    status=st.selectbox("News status", ["ACTIVE", "LIMITED_WORKLOAD", "WORKLOAD_LIMIT", "QUESTIONABLE", "OUT", "INACTIVE", "BACKUP_QB_RISK"], index=0, key="manual_override_status_select")
    c1,c2=st.columns(2)
    with c1:
        snap=st.number_input("Expected snap %", min_value=0.0, max_value=100.0, value=0.0, step=1.0, key="manual_override_snap")
        carries=st.number_input("Expected carries", min_value=0.0, max_value=50.0, value=0.0, step=1.0, key="manual_override_carries")
    with c2:
        risk=st.number_input("Limited snap risk", min_value=0.0, max_value=1.0, value=0.0, step=0.05, key="manual_override_risk")
        routes=st.number_input("Expected routes", min_value=0.0, max_value=70.0, value=0.0, step=1.0, key="manual_override_routes")
    note=st.text_area("Override note", value="", placeholder="coach said pitch count, backup expected, weather worsening...", key="manual_override_note")
    confidence=st.slider("Confidence", min_value=0.0, max_value=1.0, value=0.75, step=0.05, key="manual_override_confidence")
    if st.button("Save Manual Override", use_container_width=True, key="save_manual_override_quick"):
        if not player.strip():
            st.warning("Add a player name first.")
        else:
            data=data if isinstance(data, dict) else {}
            payload={"status":status, "note":note, "confidence":confidence, "updated_at":now_iso()}
            if snap > 0:
                payload["expected_snap_share"]=snap
            if risk > 0:
                payload["limited_snap_risk"]=risk
            if carries > 0:
                payload["expected_carries"]=carries
            if routes > 0:
                payload["expected_routes"]=routes
            if prop:
                data.setdefault("player_props", {})[f"{norm(player)}|{prop}"]=payload
            else:
                data.setdefault("players", {})[norm(player)]=payload
            save_json(MANUAL_OVERRIDE_FILE, data)
            st.success(f"Saved override for {player}.")
            st.rerun()
    if data and st.checkbox("Show current manual overrides", value=False, key="show_current_manual_overrides"):
        st.json(data)


st.markdown(f"""
<div class='hero-panel'>
  <div class='big-title'>NFL Prop Engine</div>
  <div class='sub-title'>Clean player cards · MLB-style IQ cards · projections · pure upside · stadium/noise · weather-ready · CLV · full-board save/grade</div>
  <span class='badge'>{APP_VERSION}</span><span class='badge good-badge'>MLB framework converted to NFL structure</span>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("Controls")
    source_mode=st.radio("Prop Source", ["Live Underdog only", "Live Underdog first, demo fallback", "Demo board only"], index=0)

    if "board_pull_id" not in st.session_state:
        st.session_state["board_pull_id"] = 0
    if "manual_board_pull_requested" not in st.session_state:
        st.session_state["manual_board_pull_requested"] = False
    last_board_meta = load_last_pulled_board()
    if st.button("🔄 Refresh / Pull Underdog Board Lines", use_container_width=True):
        st.session_state["board_pull_id"] = int(st.session_state.get("board_pull_id", 0)) + 1
        st.session_state["manual_board_pull_requested"] = True
        st.session_state.pop("nfl_projection_cache_key", None)
        st.session_state.pop("nfl_projection_cache_rows", None)
        try:
            fetch_underdog_nfl_props.clear()
            fetch_underdog_nfl_moneylines.clear()
            safe_get_json.clear()
        except Exception:
            pass
        request_log("MANUAL_PULL_BOARD", "REQUESTED", f"pull_id={st.session_state['board_pull_id']}")
        st.rerun()
    st.caption(f"Last saved board pull: {last_board_meta.get('pulled_at') or 'None'} · Rows: {last_board_meta.get('row_count', 0)}")
    use_saved_board_if_blank = st.checkbox("Use last pulled/manual board if live pull is blank", value=True, help="Loads your last saved Underdog/manual board instantly and only hits Underdog when you press Refresh/Pull. This prevents slow startup.")
    auto_pull_on_load = st.checkbox("Auto-pull live board on app load", value=False, help="Leave OFF for Streamlit Cloud speed. Turn ON only if you want the app to call Underdog every page load.")

    with st.expander("Manual Underdog Board Import", expanded=False):
        st.caption("Use this when the live endpoint returns no rows. This build accepts all enabled NFL single-game markets, including yards, volume, touchdowns, fantasy, kicking, and defensive props.")
        manual_upload = st.file_uploader("Upload CSV/TXT board", type=["csv", "txt"], key="manual_board_upload")
        manual_text = st.text_area("Paste Underdog Pass Yards board text", height=150, placeholder="Pass Yards\nJ. Goff\n271.5\nDET vs NO\nD. Prescott\n266.5\nDAL @ NYG", key="manual_board_text")
        if st.button("📥 Load Manual Board Into App", use_container_width=True, key="load_manual_board_btn"):
            manual_rows = parse_manual_underdog_board(manual_text, manual_upload)
            manual_rows = _filter_live_board_to_phase6_model(manual_rows)
            if manual_rows:
                save_last_pulled_board(manual_rows, [])
                request_log("MANUAL_BOARD_IMPORT", "SAVED", f"rows={len(manual_rows)}")
                st.success(f"Loaded {len(manual_rows)} manual Underdog rows into the board cache.")
                st.rerun()
            else:
                st.warning("No valid manual rows found. Use columns player, prop, line, team, opp, matchup, position — or paste market/player/line/matchup groups.")
        if st.button("🧹 Clear Saved Board Cache", use_container_width=True, key="clear_saved_board_cache_btn"):
            save_json(BOARD_CACHE_FILE, {"pulled_at": None, "source": "CLEARED", "row_count": 0, "rows": []})
            save_json(MONEYLINE_CACHE_FILE, {"pulled_at": None, "source": "CLEARED", "row_count": 0, "rows": []})
            st.success("Saved board cache cleared.")
            st.rerun()

    # Keep every supported market active in the engine. Main-page market sections
    # and pagination prevent the expanded board from becoming heavy on mobile.
    prop_filter=list(ACTIVE_NFL_MARKET_ORDER)
    st.session_state["xgb_assist_enabled"] = st.toggle("XGBoost Assist after grading", value=bool(st.session_state.get("xgb_assist_enabled", False)), help="Uses your graded results to assist the main projection before Monte Carlo. Stays neutral until enough grades exist.")
    if st.session_state.get("xgb_assist_enabled", False):
        st.session_state["xgb_min_rows"] = st.slider("XGB min graded rows", 25, 250, int(st.session_state.get("xgb_min_rows", 50)), 5)
        st.session_state["xgb_blend_weight"] = st.slider("XGB max blend", 0.05, 0.40, float(st.session_state.get("xgb_blend_weight", 0.22)), 0.01)
    st.session_state["advanced_sim_assist_enabled"] = st.toggle("Bayesian / Markov / Poisson Assist", value=bool(st.session_state.get("advanced_sim_assist_enabled", True)), help="Adds bounded Bayesian historical-log updating, Markov game-state volume, Poisson event-rate nudges, and Elo/efficiency matchup context before Monte Carlo.")
    st.session_state["smart_calibration_enabled"] = st.toggle("Smart role calibration", value=bool(st.session_state.get("smart_calibration_enabled", True)), help="Calibrates by prop, position, role and data quality only after enough prior graded rows.")
    st.session_state["team_volume_reconciliation_enabled"] = st.toggle("Team-volume reconciliation", value=bool(st.session_state.get("team_volume_reconciliation_enabled", True)), help="Prevents listed receptions, receiving yards and carries from exceeding realistic team/QB volume.")
    if st.session_state.get("advanced_sim_assist_enabled", True):
        st.session_state["bayes_min_games"] = st.slider("Bayesian min player games", 3, 12, int(st.session_state.get("bayes_min_games", 5)), 1)
    st.session_state["ensemble_ml_assist_enabled"] = st.toggle("Random Forest / Tree Ensemble Assist after grading", value=bool(st.session_state.get("ensemble_ml_assist_enabled", False)), help="Optional ML assist from graded results. Stays neutral until enough graded props exist.")
    if st.session_state.get("ensemble_ml_assist_enabled", False):
        st.session_state["ensemble_min_rows"] = st.slider("Ensemble min graded rows", 50, 400, int(st.session_state.get("ensemble_min_rows", 75)), 5)
        st.session_state["ensemble_blend_weight"] = st.slider("Ensemble max blend", 0.04, 0.30, float(st.session_state.get("ensemble_blend_weight", 0.16)), 0.01)
    primary_lines_only=st.checkbox("Use one primary line per player/prop", True, help="Recommended. Removes alternate-line ladders from the main projection build so the app stays fast. Alt lines can still be reviewed separately.")
    min_score=st.slider("Minimum Data Score",0,99,0)
    show_all=st.checkbox("Larger player-card pages", False, help="OFF renders 12 cards at a time. ON renders 24 at a time. The app never renders all 446 cards at once.")
    st.session_state["nfl_card_page_size"] = 24 if show_all else 12
    st.session_state["nfl_table_page_size"] = st.selectbox("Table rows per page", [25, 50, 100], index=1, key="global_table_page_size")
    st.divider()
    st.caption("API keys can be added in Streamlit secrets or Railway variables later.")
    show_feed_debug=st.checkbox("Show Underdog feed debug", False)
    with st.expander("Projection Data", expanded=False):
        _render_projection_data_admin()
    with st.expander("Admin: Phase 6 Database", expanded=False):
        _render_phase6_admin()
    st.code("STORAGE_DIR=nfl_engine", language="bash")

pull_id = int(st.session_state.get("board_pull_id", 0))
should_pull_live = (source_mode != "Demo board only") and (bool(st.session_state.get("manual_board_pull_requested", False)) or bool(st.session_state.get("auto_pull_on_load", False)) or bool(locals().get("auto_pull_on_load", False)))
live=[]
moneylines=[]
if source_mode != "Demo board only" and use_saved_board_if_blank:
    cached_board = load_last_pulled_board()
    cached_money = load_last_pulled_moneylines()
    live = cached_board.get("rows", []) or []
    moneylines = cached_money.get("rows", []) or []
    if live:
        request_log("UNDERDOG_BOARD_CACHE", "USING_LAST_PULLED", f"rows={len(live)} pulled_at={cached_board.get('pulled_at')}")
if should_pull_live:
    with st.spinner("Pulling Underdog NFL board lines..."):
        pulled_live = fetch_underdog_nfl_props(pull_id)
        pulled_moneylines = fetch_underdog_nfl_moneylines(pull_id)
    st.session_state["manual_board_pull_requested"] = False
    if pulled_live:
        live = pulled_live
        moneylines = pulled_moneylines
        save_last_pulled_board(live, moneylines)
    elif not live:
        request_log("UNDERDOG_BOARD_PULL", "NO_ROWS_AND_NO_CACHE", "Manual/auto pull found no rows and no saved board was loaded.")
raw_all = live if live else ([] if source_mode=="Live Underdog only" else DEMO_BOARD)
raw = _select_primary_market_lines(raw_all) if primary_lines_only else list(raw_all)
projection_cache_key = _board_projection_cache_key(raw, primary_lines_only)
cache_hit = (
    st.session_state.get("nfl_projection_cache_key") == projection_cache_key
    and isinstance(st.session_state.get("nfl_projection_cache_rows"), list)
)
projection_errors=[]
if cache_hit:
    projected_base = st.session_state.get("nfl_projection_cache_rows", [])
else:
    projected_base=[]
    started=time.perf_counter()
    total=max(1, len(raw))
    # Adaptive Monte Carlo keeps large full-market boards responsive on Railway/mobile.
    # Smaller boards retain more samples; large boards use enough samples for stable
    # probabilities without forcing millions of unnecessary draws on every refresh.
    sim_count = 5000 if len(raw) > 250 else 7000 if len(raw) > 100 else 10000
    progress=st.progress(0, text=f"Building NFL projections: 0/{len(raw)} · {sim_count:,} sims each")
    for idx, _r in enumerate(raw, start=1):
        _canon = _canon_prop_label(_r.get("prop")) or _r.get("prop")
        if _canon in ACTIVE_NFL_MARKETS and _canon in prop_filter:
            _rr=dict(_r); _rr["prop"]=_canon
            try:
                projected_base.append(project_row(_rr, sims=sim_count))
            except Exception as exc:
                projection_errors.append({"player":_rr.get("player"), "prop":_rr.get("prop"), "line":_rr.get("line"), "error":str(exc)[:240]})
        if idx == total or idx % max(1, total//25) == 0:
            progress.progress(min(1.0, idx/total), text=f"Building NFL projections: {idx}/{len(raw)} · {sim_count:,} sims each")
    if bool(st.session_state.get("team_volume_reconciliation_enabled", True)):
        projected_base=reconcile_team_projection_volume(projected_base)
    flush_tracking_state()
    progress.empty()
    st.session_state["nfl_projection_cache_key"] = projection_cache_key
    st.session_state["nfl_projection_cache_rows"] = projected_base
    st.session_state["nfl_projection_cache_seconds"] = round(time.perf_counter()-started, 2)
for _p in projected_base:
    _x=_p.get("xgb_assist") or {}
    _p["xgb_status"] = _x.get("status", "OFF")
projected=[p for p in projected_base if p.get("data_score",0)>=min_score]
if projection_errors:
    st.error(f"{len(projection_errors)} rows failed safely instead of freezing the app.")
    with st.expander("Projection errors", expanded=False):
        st.dataframe(pd.DataFrame(projection_errors), use_container_width=True, hide_index=True)

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
    build_note = "cached projections reused" if cache_hit else f"built in {st.session_state.get('nfl_projection_cache_seconds','—')}s"
    st.success(f"🟢 Underdog NFL feed: {len(live)} valid rows · {len(raw)} projected rows · {build_note}. Last board pull: {cached_meta.get('pulled_at') or 'current refresh'}.")
elif source_mode == "Live Underdog only":
    st.warning("No live Underdog NFL rows were detected. Click 🔄 Refresh / Pull Underdog Board Lines in the sidebar when props are posted.")
else:
    st.info("Demo/testing mode is active. These rows are for UI testing only and should not be treated as real plays.")

if 'show_feed_debug' in globals() and show_feed_debug:
    req_log=load_json(REQUEST_LOG,[])
    st.caption("Latest Underdog/API request log")
    st.dataframe(pd.DataFrame(req_log[-25:]), use_container_width=True, hide_index=True)

if not _phase6_existing_database_ready():
    st.warning("Projection database is incomplete. Projections still run with safe fallbacks, but use Sidebar → Admin: Phase 6 Database → Build / Repair Projection Database for stronger player, defense, team, and red-zone inputs.")

page_options = [
    "Today / Weekly Board", "Pass Yards", "Receiving Yards", "Rushing Yards", "Volume Props",
    "Touchdowns + Turnovers", "Fantasy + Explosive", "Kickers", "Defense",
    "Closing Review", "Exposure", "Best Edges", "Player Cards", "Alt-Line Ladder",
    "Correlation Builder", "Save + Grade", "Learning Dashboard", "Money Line", "Backtest"
]
active_page = st.selectbox("NFL App Section", page_options, index=0, key="nfl_main_page")
st.markdown("<div class='nfl-page-note'>Only the selected section is rendered. This prevents hidden tabs and hundreds of cards from keeping the app on RUNNING.</div>", unsafe_allow_html=True)

if active_page == 'Today / Weekly Board':
    st.markdown("<div class='section-title-pro'>NFL Board</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title-pro'>Slate Context</div>", unsafe_allow_html=True)
    _render_slate_context(projected)
    _render_prop_table(projected, "NFL Board")

elif active_page == 'Pass Yards':
    st.markdown("<div class='section-title-pro'>Passing Yards Board</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") == "Passing Yards"]
    _render_prop_table(rows, "Passing Yards")
    _render_player_cards(rows, header=None)

elif active_page == 'Receiving Yards':
    st.markdown("<div class='section-title-pro'>Receiving Yards Board</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") == "Receiving Yards"]
    _render_prop_table(rows, "Receiving Yards")
    _render_player_cards(rows, header=None)

elif active_page == 'Rushing Yards':
    st.markdown("<div class='section-title-pro'>Rushing Yards Board</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") == "Rushing Yards"]
    _render_prop_table(rows, "Rushing Yards")
    _render_player_cards(rows, header=None)

elif active_page == 'Volume Props':
    st.markdown("<div class='section-title-pro'>Volume Props</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") in ["Pass Attempts", "Completions", "Receptions", "Rush Attempts"]]
    _render_prop_table(rows, "Volume Props")
    _render_player_cards(rows, header=None)

elif active_page == 'Touchdowns + Turnovers':
    st.markdown("<div class='section-title-pro'>Touchdowns + Turnovers</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") in ["Passing TDs", "Interceptions", "Anytime TD"]]
    _render_prop_table(rows, "Touchdowns + Turnovers")
    _render_player_cards(rows, header=None)

elif active_page == 'Fantasy + Explosive':
    st.markdown("<div class='section-title-pro'>Fantasy + Explosive Props</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") in ["Fantasy Points", "Longest Reception", "Longest Rush"]]
    _render_prop_table(rows, "Fantasy + Explosive")
    _render_player_cards(rows, header=None)

elif active_page == 'Kickers':
    st.markdown("<div class='section-title-pro'>Kicker Props</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") in ["Kicking Points", "Field Goals Made"]]
    _render_prop_table(rows, "Kickers")
    _render_player_cards(rows, header=None)

elif active_page == 'Defense':
    st.markdown("<div class='section-title-pro'>Defensive Player Props</div>", unsafe_allow_html=True)
    rows=[p for p in projected if p.get("prop") in ["Tackles + Assists", "Sacks"]]
    _render_prop_table(rows, "Defense")
    _render_player_cards(rows, header=None)

elif active_page == 'Closing Review':
    _render_closing_review(projected)

elif active_page == 'Exposure':
    _render_exposure_dashboard(projected)

elif active_page == 'Best Edges':
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

elif active_page == 'Player Cards':
    _render_player_cards(projected, header="Clickable Player Cards")

elif active_page == 'Alt-Line Ladder':
    st.markdown("<div class='section-title-pro'>Alt-Line Ladder</div>", unsafe_allow_html=True)
    names=[f"{p['player']} — {p['prop']}" for p in projected]
    if names:
        choice=st.selectbox("Choose Player Prop", names)
        p=projected[names.index(choice)]
        st.dataframe(alt_ladder(p), use_container_width=True, hide_index=True)
    else: st.warning("No props to ladder.")

elif active_page == 'Correlation Builder':
    st.markdown("<div class='section-title-pro'>Correlation Builder</div>", unsafe_allow_html=True)
    st.write("Use this to avoid bad parlays and find positive stacks.")
    if df.empty: st.warning("No player cards loaded.")
    else:
        labels=[f"{p['player']} — {p['prop']}" for p in projected]
        left=st.selectbox("Leg 1", labels, key="corr1")
        right=st.selectbox("Leg 2", labels, key="corr2")
        p1=projected[labels.index(left)]
        p2=projected[labels.index(right)]
        corr=_correlation_warning(p1,p2)
        st.success(f"Correlation Read: {corr}")

elif active_page == 'Save + Grade':
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

        st.subheader("Import Results CSV")
        st.caption("CSV columns: player, prop, actual. Optional: team, line, result.")
        result_template="player,prop,actual\nPatrick Mahomes,Passing Yards,278\nChristian McCaffrey,Rushing Yards,82\nJustin Jefferson,Receiving Yards,91\n"
        st.download_button("Template: results CSV", data=result_template.encode("utf-8"), file_name="nfl_prop_results.csv", mime="text/csv", use_container_width=True, key="results_csv_template")
        result_upload=st.file_uploader("Upload results CSV for selected saved slate", type=["csv"], key="results_csv_upload")
        if st.button("Grade Results CSV + Learn", use_container_width=True, key="grade_results_csv_btn"):
            graded=grade_from_results_csv(result_upload, saved_rows, grade_note=f"results_csv_{choice['key']}")
            if graded:
                wins=[g.get("win") for g in graded if g.get("win") is not None]
                hit=round(100*np.mean(wins),1) if wins else "N/A"
                st.success(f"Imported and graded {len(graded)} rows · Hit Rate: {hit}%")
            else:
                st.warning("No matching result rows found. Check player/prop names.")

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

elif active_page == 'Learning Dashboard':
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

elif active_page == 'Money Line':
    st.markdown("<div class='section-title-pro'>Underdog Money Line</div>", unsafe_allow_html=True)
    st.write("This tab scans Underdog for NFL moneyline/winner markets when they are posted. It will not create fake moneylines if Underdog does not expose them yet.")
    if moneylines:
        st.success(f"Live Underdog moneyline-style rows detected: {len(moneylines)}")
        st.dataframe(pd.DataFrame(moneylines), use_container_width=True, hide_index=True)
    else:
        st.warning("No Underdog NFL moneyline rows detected right now. Player props can still load normally; this tab will populate automatically if Underdog posts moneyline/winner markets in the scanned feed.")
        st.caption("Tip: most DFS-style Underdog feeds focus on player props. If moneylines are not offered there, keep this tab as a monitor and use sportsbook odds APIs later for true moneyline pricing.")

elif active_page == 'Backtest':
    st.markdown("<div class='section-title-pro'>Backtest + Edge Buckets</div>", unsafe_allow_html=True)
    _render_backtest_dashboard()
