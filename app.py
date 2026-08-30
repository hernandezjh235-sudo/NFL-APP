
# -*- coding: utf-8 -*-
"""
NFL PROP ENGINE — Railway / Streamlit ready
Built from the MLB engine structure: clean UI, player cards, projections, pure upside,
alt ladder, CLV, before/after save, grading, learning dashboard.

This is a live-only game-day build. It never substitutes synthetic lines for a missing
Underdog feed, and it only projects markets with dedicated player-stat models.
"""

import os, json, math, time, difflib, unicodedata, hashlib, re, io, zipfile, html, textwrap, base64, gzip
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st

# -----------------------------------------------------------------------------
# EMBEDDED NFL SAVANT ADAPTER
# -----------------------------------------------------------------------------
# Kept in this file so Railway/Streamlit deployments need only app.py.
SAVANT_BASE_URL = "https://nflsavant.com"
SAVANT_BOARD_ENDPOINTS = {
    "receiving": "/api/leaderboard/leaders/receiving",
    "ngs-receiving": "/api/leaderboard/leaders/ngs-receiving",
    "route-tree": "/api/leaderboard/leaders/route-tree",
    "passing": "/api/leaderboard/leaders/passing",
    "ngs-passing": "/api/leaderboard/leaders/ngs-passing",
    "rushing": "/api/leaderboard/leaders/rushing",
    "ngs-rushing": "/api/leaderboard/leaders/ngs-rushing",
    "pressure": "/api/leaderboard/leaders/pressure",
    "penalties": "/api/leaderboard/leaders/penalties",
    "movers": "/api/leaderboard/movers",
    "league": "/api/league",
}
SAVANT_REQUIRED_BOARDS = {
    "receiving", "ngs-receiving", "passing", "ngs-passing",
    "rushing", "ngs-rushing", "pressure", "penalties",
}
SAVANT_OPTIONAL_BOARDS = {"route-tree", "league", "movers", "charting"}
NFLVERSE_NGS_FALLBACK_URLS = {
    "ngs-passing": "https://github.com/nflverse/nflverse-data/releases/download/nextgen_stats/ngs_passing.csv",
    "ngs-receiving": "https://github.com/nflverse/nflverse-data/releases/download/nextgen_stats/ngs_receiving.csv",
    "ngs-rushing": "https://github.com/nflverse/nflverse-data/releases/download/nextgen_stats/ngs_rushing.csv",
}

BOARD_FILENAME_HINTS = {
    "ngs-receiving": ("ngs-receiving", "receiving-air-yds", "air-yds-tgt"),
    "route-tree": ("route-tree", "route_tree", "target-share-route"),
    "ngs-passing": ("ngs-passing", "passing-cpoe"),
    "ngs-rushing": ("ngs-rushing", "rushing-rush-yoe", "yoe-att"),
    "receiving": ("receiving-epa", "epa-tgt"),
    "passing": ("passing-epa", "epa-play"),
    "rushing": ("rushing-epa", "epa-att"),
    "pressure": ("pass-rush", "pressure"),
    "penalties": ("penalties", "flags"),
    "movers": ("movers", "rank-movement"),
    "league": ("league", "team-metrics", "team-advanced"),
    # nflverse/FTN play-charting is supplemental (not one of the 9 required boards).
    "charting": ("ftn-charting", "ftn_charting", "charting"),
}

BOARD_REQUIRED_COLUMN_GROUPS = {
    "passing": (("player", "name"), ("epa_play", "epa", "epa_per_play"), ("att", "attempts")),
    # Accept both compact NFL Savant exports and the full nflverse NGS files.
    "ngs-passing": (
        ("player", "name"),
        ("cpoe", "completion_percentage_above_expectation"),
        ("xcomp_pct", "xcomp", "expected_completion_percentage"),
    ),
    "receiving": (("player", "name"), ("epa_tgt", "epa", "epa_target"), ("tgt", "targets")),
    "ngs-receiving": (
        ("player", "name"),
        ("air_yards_per_target", "ayds_tgt", "air_yards_target", "air_yds_tgt", "avg_intended_air_yards"),
        ("tgt", "targets"),
    ),
    "route-tree": (("player", "name"), ("tgt", "targets"), ("screen",), ("go",)),
    "rushing": (("player", "name"), ("epa_att", "epa", "epa_attempt"), ("carries", "att")),
    "ngs-rushing": (
        ("player", "name"),
        ("yoe_per_attempt", "yoe_att", "rush_yoe_att", "rush_yards_over_expected_per_att"),
        ("carries", "att", "rush_attempts"),
    ),
    "pressure": (("player", "name"), ("pressure_pct", "pressure"), ("pressures",)),
    "penalties": (("player", "name"), ("penalties", "flags", "pen")),
    "movers": (("player", "name"), ("rank", "current_rank"), ("prev_rank", "prior_rank", "delta")),
    "league": (("team", "abbr"),),
    "charting": (("nflverse_game_id", "ftn_game_id"), ("nflverse_play_id", "ftn_play_id")),
}

NON_PLAYER_SAVANT_BOARDS = {"league", "charting"}

TEAM_ALIASES = {
    "JAC": "JAX", "WAS": "WSH", "OAK": "LV", "SD": "LAC", "STL": "LAR",
    "LA": "LAR",
}

_PACK_CACHE: dict[str, object] = {"signature": None, "pack": None}
_BANK_CACHE: dict[str, object] = {"signature": None, "banks": None}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _finite(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


def normalize_savant_player_name(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace(".", " ").replace("'", "").replace("-", " ")
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", text)
    return " ".join(text.split())


def normalize_savant_team(value) -> str:
    team = re.sub(r"[^A-Z]", "", str(value or "").upper())
    return TEAM_ALIASES.get(team, team)


def _canonical_column(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).strip().lower()
    text = text.replace("%", " pct ").replace("+/-", " plus_minus ").replace("/", "_")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    aliases = {
        "rank": "rank", "player": "player", "name": "player", "pos": "position",
        "position": "position", "team": "team",
        # Full nflverse NGS exports use these field names.
        "player_display_name": "player", "player_position": "position",
        "team_abbr": "team", "player_gsis_id": "id",
        "epa_play": "epa_play",
        "epa_tgt": "epa_tgt", "epa_att": "epa_att", "epa_target": "epa_tgt",
        "epa_attempt": "epa_att", "success_pct": "success_pct", "comp_pct": "comp_pct",
        "success": "success_pct", "succ": "success_pct", "xcomp": "xcomp_pct",
        "xcomp_pct": "xcomp_pct",
        "completion_percentage": "comp_pct",
        "expected_completion_percentage": "xcomp_pct",
        "completion_percentage_above_expectation": "cpoe",
        "avg_time_to_throw": "time_to_throw",
        "avg_separation": "avg_separation",
        "average_separation": "avg_separation",
        "avg_cushion": "avg_cushion",
        "average_cushion": "avg_cushion",
        "catch_percentage_above_expectation": "catch_pct_above_expectation",
        "target_separation": "target_separation",
        "aggressiveness": "aggression_pct",
        "pressure_pct": "pressure_pct", "tgt_pct": "target_share",
        "target_pct": "target_share", "rztgt": "rz_targets", "rztgt_pct": "rz_target_share",
        "gltgt": "goal_line_targets", "gltgt_pct": "goal_line_target_share",
        "rz_carry_pct": "rz_carry_share", "gl_carry_pct": "goal_line_carry_share",
        "yds_tgt": "yards_per_target", "y_tgt": "yards_per_target",
        "y_rec": "yards_per_reception", "yac_rec": "yac_per_reception",
        "ayds_tgt": "air_yards_per_target", "air_yds_tgt": "air_yards_per_target",
        "y_a": "yards_per_attempt", "ya": "yards_per_attempt", "ypc": "yards_per_carry", "yoe_att": "yoe_per_attempt",
        "rush_attempts": "carries", "rush_yards": "yards",
        "avg_rush_yards": "yards_per_carry",
        "rush_yards_over_expected_per_att": "yoe_per_attempt",
        "catch_percentage": "catch_pct", "avg_yac": "yac_per_reception",
        "opp_db": "opportunities", "qb_hits": "qb_hits", "ttt": "time_to_throw",
        "aggr_pct": "aggression_pct", "aiay": "intended_air_yards", "tgt": "targets",
        "att": "attempts", "comp": "completions", "rec": "receptions", "yds": "yards",
        "rz_carries": "rz_carries", "gl_carries": "goal_line_carries",
        "auto_1st": "automatic_first_downs", "measured": "measured_flags",
        "epa_cost": "penalty_epa_cost", "wpa_cost": "penalty_wpa_cost",
        "pen": "penalties", "flags": "penalties", "pen_yds": "penalty_yards",
        "prev_rank": "previous_rank", "prior_rank": "previous_rank",
        "current_rank": "rank", "delta_rank": "rank_delta",
    }
    return aliases.get(text, text)


def _raw_bytes(source) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    if hasattr(source, "read"):
        data = source.read()
        return data.encode("utf-8") if isinstance(data, str) else bytes(data)
    raise TypeError("unsupported Savant CSV source")


def read_savant_csv(source) -> pd.DataFrame:
    """Read a Savant export with BOM/comment attribution lines before the header."""
    raw = _raw_bytes(source)
    if not raw or len(raw) < 10:
        raise ValueError("empty Savant file")
    probe = raw[:2048].decode("utf-8-sig", errors="replace").lstrip().lower()
    if probe.startswith(("<!doctype html", "<html", "<?xml")) or "<body" in probe[:500]:
        raise ValueError("HTML/XML response is not a Savant CSV")
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "," in stripped:
            header_index = index
            break
    if header_index is None:
        raise ValueError("CSV header not found after Savant attribution comments")
    frame = pd.read_csv(io.StringIO("\n".join(lines[header_index:])))
    frame.columns = [_canonical_column(col) for col in frame.columns]
    frame = frame.loc[:, ~frame.columns.duplicated()].copy()
    frame = frame.dropna(how="all")
    if frame.empty:
        raise ValueError("Savant CSV has no data rows")
    return frame


def _schema_score(frame: pd.DataFrame, board: str) -> int:
    columns = set(frame.columns)
    groups = BOARD_REQUIRED_COLUMN_GROUPS.get(board, ())
    return sum(1 for choices in groups if any(choice in columns for choice in choices))


def detect_savant_board(filename: str, frame: pd.DataFrame) -> str | None:
    name = Path(str(filename or "")).name.lower().replace("_", "-")
    # Verified/full nflverse NGS exports overlap in schema, so filename identity wins.
    for board in ("ngs-passing", "ngs-receiving", "ngs-rushing"):
        if board in name:
            return board
    hinted = []
    for board, hints in BOARD_FILENAME_HINTS.items():
        if any(hint in name for hint in hints):
            hinted.append(board)
    candidates = hinted + [board for board in BOARD_REQUIRED_COLUMN_GROUPS if board not in hinted]
    scored = [(board, _schema_score(frame, board)) for board in candidates]
    scored.sort(key=lambda item: (item[1], item[0] in hinted), reverse=True)
    if not scored:
        return None
    board, score = scored[0]
    minimum = max(1, len(BOARD_REQUIRED_COLUMN_GROUPS.get(board, ())) - 1)
    return board if score >= minimum else None


def _season_from_name(filename: str, default_season=None) -> int | None:
    matches = re.findall(r"(?:19|20)\d{2}", Path(str(filename or "")).name)
    if matches:
        return int(matches[-1])
    return int(default_season) if default_season is not None else None


def _prepare_savant_frame(frame: pd.DataFrame, board: str, season: int | None) -> pd.DataFrame:
    """Adapt supported Savant/nflverse exports to the app's canonical board schema.

    The full nflverse NGS downloads contain every season and both weekly + week=0
    season-summary rows.  For a selected season we keep REG rows and prefer week=0
    summaries so one player does not get duplicated 18+ times in the feature bank.
    """
    out = frame.copy()

    # Scope multi-season nflverse files to the requested season.
    if season is not None and "season" in out.columns:
        season_values = pd.to_numeric(out["season"], errors="coerce")
        if bool((season_values == int(season)).any()):
            out = out.loc[season_values == int(season)].copy()

    # These Savant feature boards are regular-season priors.  If the source contains
    # multiple season types, prefer REG without rejecting files that omit the field.
    if board in {"ngs-passing", "ngs-receiving", "ngs-rushing"} and "season_type" in out.columns:
        season_type = out["season_type"].astype(str).str.upper().str.strip()
        if bool((season_type == "REG").any()):
            out = out.loc[season_type == "REG"].copy()

    # nflverse NGS week=0 is the season aggregate. Prefer it when present.
    if board in {"ngs-passing", "ngs-receiving", "ngs-rushing"} and "week" in out.columns:
        week_values = pd.to_numeric(out["week"], errors="coerce")
        if bool((week_values == 0).any()):
            out = out.loc[week_values == 0].copy()

    # Board-specific compatibility aliases / derived fields.
    if board == "ngs-passing":
        copies = {
            "avg_intended_air_yards": "intended_air_yards",
            "pass_yards": "yards",
            "pass_touchdowns": "touchdowns",
        }
    elif board == "ngs-receiving":
        copies = {
            "avg_intended_air_yards": "air_yards_per_target",
            "avg_yac_above_expectation": "yac_above_expectation",
            "rec_touchdowns": "touchdowns",
        }
        if "yards_per_target" not in out.columns and {"yards", "targets"}.issubset(out.columns):
            yards = pd.to_numeric(out["yards"], errors="coerce")
            targets = pd.to_numeric(out["targets"], errors="coerce").replace(0, np.nan)
            out["yards_per_target"] = yards / targets
    elif board == "ngs-rushing":
        copies = {
            "expected_rush_yards": "expected_yards",
            "rush_yards_over_expected": "yards_over_expected",
            "rush_pct_over_expected": "rush_pct_over_expected",
            "rush_touchdowns": "touchdowns",
        }
    else:
        copies = {}

    for source, target in copies.items():
        if target not in out.columns and source in out.columns:
            out[target] = out[source]

    return out.dropna(how="all")


def _frame_is_valid(frame: pd.DataFrame, board: str) -> tuple[bool, str]:
    if frame.empty:
        return False, "no rows"
    groups = BOARD_REQUIRED_COLUMN_GROUPS.get(board, ())
    missing = ["/".join(group) for group in groups if not any(col in frame.columns for col in group)]
    if missing:
        return False, "missing schema: " + ", ".join(missing)
    if board not in NON_PLAYER_SAVANT_BOARDS and "player" not in frame.columns:
        return False, "missing player column"
    return True, "ok"


def _normalized_frame(frame: pd.DataFrame, board: str, season: int) -> pd.DataFrame:
    out = _prepare_savant_frame(frame, board, season)
    if "player" in out.columns:
        out["player"] = out["player"].astype(str).str.strip()
        out["player_key"] = out["player"].map(normalize_savant_player_name)
    if "team" in out.columns:
        out["team"] = out["team"].map(normalize_savant_team)
    if "position" in out.columns:
        out["position"] = out["position"].astype(str).str.upper().str.strip()
    out["savant_board"] = board
    out["savant_season"] = int(season)
    return out


def _iter_payloads(uploaded_files):
    for item in uploaded_files or []:
        if isinstance(item, tuple) and len(item) == 2:
            name, raw = item
        else:
            name = getattr(item, "name", "upload.csv")
            raw = _raw_bytes(item)
        name = Path(str(name)).name
        raw = bytes(raw)
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for member in archive.infolist():
                        if member.is_dir() or not member.filename.lower().endswith(".csv"):
                            continue
                        yield Path(member.filename).name, archive.read(member)
            except zipfile.BadZipFile as exc:
                yield name, exc
        else:
            yield name, raw


def import_savant_payloads(uploaded_files, savant_dir, default_season=None) -> list[dict]:
    """Import ZIP/multi-CSV payloads, preserving originals and normalized boards."""
    root = Path(savant_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    selected: dict[tuple[int, str], tuple[str, bytes, pd.DataFrame, str]] = {}
    for order, (name, payload) in enumerate(_iter_payloads(uploaded_files)):
        result = {"filename": name, "detected_board": "", "season": default_season,
                  "rows": 0, "columns": 0, "valid": False, "saved_path": ""}
        if isinstance(payload, Exception):
            result["detail"] = f"ZIP read failed: {payload}"
            results.append(result)
            continue
        try:
            frame = read_savant_csv(payload)
            board = detect_savant_board(name, frame)
            season = _season_from_name(name, default_season)
            if board is None or season is None:
                result.update({"detected_board": board or "UNKNOWN", "season": season,
                               "rows": len(frame), "columns": len(frame.columns),
                               "detail": "board or season could not be detected"})
                results.append(result)
                continue

            prepared = _prepare_savant_frame(frame, board, season)
            result.update({"detected_board": board, "season": season,
                           "rows": len(prepared), "columns": len(prepared.columns)})
            valid, detail = _frame_is_valid(prepared, board)
            if len(prepared) != len(frame):
                detail = f"{detail}; accepted {len(prepared):,}/{len(frame):,} rows for {season}"
            result.update({"valid": valid, "detail": detail})
            if valid:
                selected[(int(season), board)] = (name, payload, prepared, f"upload_order_{order}")
            results.append(result)
        except Exception as exc:
            result["detail"] = str(exc)[:180]
            results.append(result)

    manifest = load_savant_manifest(root)
    entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict)]
    for (season, board), (name, raw, frame, selection) in selected.items():
        raw_dir = root / "raw" / str(season)
        norm_dir = root / "normalized" / str(season)
        raw_dir.mkdir(parents=True, exist_ok=True)
        norm_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / Path(name).name
        norm_path = norm_dir / f"{board}.csv"
        raw_path.write_bytes(raw)
        normalized = _normalized_frame(frame, board, season)
        normalized.to_csv(norm_path, index=False)
        checksum = hashlib.sha256(raw).hexdigest()
        entries = [e for e in entries if not (e.get("season") == season and e.get("board") == board)]
        entries.append({
            "board": board, "season": season, "source_filename": name,
            "source_url": "UPLOAD", "pulled_at": _now_iso(), "checksum": checksum,
            "rows": int(len(normalized)), "columns": int(len(normalized.columns)),
            "parse_status": "VALID", "raw_path": str(raw_path),
            "normalized_path": str(norm_path), "selection": selection,
        })
        for result in results:
            if result.get("filename") == name and result.get("detected_board") == board and result.get("season") == season:
                result["saved_path"] = str(norm_path)
                result["valid"] = True
    save_savant_manifest(root, entries)
    clear_savant_runtime_cache()
    for selected_season in sorted({season for season, _board in selected}):
        try:
            build_savant_feature_store(root, selected_season)
        except Exception:
            # The normalized source pack remains valid even if a derived cache cannot
            # be written. The explicit UI rebuild action can retry it later.
            pass
    return results


def load_savant_manifest(savant_dir) -> dict:
    path = Path(savant_dir) / "savant_manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_savant_manifest(savant_dir, entries) -> dict:
    root = Path(savant_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "updated_at": _now_iso(), "files": sorted(
        entries, key=lambda e: (int(e.get("season", 0)), str(e.get("board", "")))
    )}
    tmp = root / "savant_manifest.json.tmp"
    target = root / "savant_manifest.json"
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    return manifest


def _json_board_frame(payload, board: str, season: int) -> pd.DataFrame:
    rows = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        candidate_keys = (
            ("teams", "rows", "data", "results") if board == "league"
            else ("rows", "leaders", "movers", "data", "results")
        )
        rows = next((payload.get(key) for key in candidate_keys if isinstance(payload.get(key), list)), rows)
        if not isinstance(rows, list):
            # Some API versions group movers by category. Flatten only list-valued
            # groups and retain the group name as an audit field.
            grouped = []
            for category, values in payload.items():
                if not isinstance(values, list):
                    continue
                grouped.extend({"category": category, **item} for item in values if isinstance(item, dict))
            rows = grouped or None
    if not isinstance(rows, list):
        raise ValueError("Savant JSON response has no recognized row collection")
    flat = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        merged = {**row, **values}
        merged.setdefault("rank", rank)
        if "name" in merged and "player" not in merged:
            merged["player"] = merged.get("name")
        flat.append(merged)
    if not flat:
        raise ValueError("Savant JSON board is empty")
    frame = pd.DataFrame(flat)
    frame.columns = [_canonical_column(col) for col in frame.columns]
    frame = frame.loc[:, ~frame.columns.duplicated()].copy()
    return _normalized_frame(frame, board, season)


def _request_json(session, url, timeout=(4.0, 14.0), attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            ctype = str(response.headers.get("content-type") or "").lower()
            if "json" not in ctype and response.text.lstrip().lower().startswith("<"):
                raise ValueError("endpoint returned HTML instead of JSON")
            return response.json(), response.content
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.35 * (2 ** attempt))
    raise RuntimeError(str(last_error))


def _fetch_nflverse_ngs_fallback(client, board, season):
    """Official nflverse NGS fallback. Keeps NGS independent from fragile nflsavant endpoints."""
    url=NFLVERSE_NGS_FALLBACK_URLS.get(board)
    if not url: raise ValueError("no nflverse fallback configured")
    resp=client.get(url,timeout=45)
    resp.raise_for_status()
    raw=resp.content
    frame=read_savant_csv(raw)
    prepared=_prepare_savant_frame(frame,board,int(season))
    # nflverse column compatibility names
    if "player" not in prepared.columns and "player_display_name" in prepared.columns:
        prepared["player"]=prepared["player_display_name"]
    if "team" not in prepared.columns and "team_abbr" in prepared.columns:
        prepared["team"]=prepared["team_abbr"]
    if "position" not in prepared.columns and "player_position" in prepared.columns:
        prepared["position"]=prepared["player_position"]
    prepared=_normalized_frame(prepared,board,int(season))
    valid,detail=_frame_is_valid(prepared,board)
    if not valid: raise ValueError(detail)
    return prepared,raw,url

def refresh_nfl_savant_data(season, savant_dir, force=False, ttl_hours=12, session=None) -> list[dict]:
    """Refresh stable Savant JSON endpoints; preserve last-good files on every error."""
    season = int(season)
    root = Path(savant_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = load_savant_manifest(root)
    entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict)]
    by_board = {(int(e.get("season", 0)), e.get("board")): e for e in entries}
    client = session or requests.Session()
    client.headers.update({
        "User-Agent": "NFLPropEngine/7.32 (+cached-public-data-adapter)",
        "Accept": "application/json,text/plain;q=0.9,*/*;q=0.5",
    })
    results = []
    for board, endpoint in SAVANT_BOARD_ENDPOINTS.items():
        existing = by_board.get((season, board), {})
        age_hours = None
        try:
            stamp = datetime.fromisoformat(str(existing.get("pulled_at") or ""))
            age_hours = max(0.0, (datetime.now() - stamp).total_seconds() / 3600.0)
        except Exception:
            pass
        if not force and age_hours is not None and age_hours < float(ttl_hours) and Path(str(existing.get("normalized_path") or "")).exists():
            results.append({"board": board, "season": season, "status": "CACHED", "rows": existing.get("rows", 0), "detail": f"{age_hours:.1f}h old"})
            continue
        query = f"?season={season}"
        url = SAVANT_BASE_URL + endpoint + query
        try:
            payload, raw = _request_json(client, url)
            frame = _json_board_frame(payload, board, season)
            valid, detail = _frame_is_valid(frame, board)
            if not valid:
                raise ValueError(detail)
            raw_dir = root / "raw" / str(season)
            norm_dir = root / "normalized" / str(season)
            raw_dir.mkdir(parents=True, exist_ok=True)
            norm_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / f"{board}.json"
            norm_path = norm_dir / f"{board}.csv"
            raw_path.write_bytes(raw)
            frame.to_csv(norm_path, index=False)
            entry = {
                "board": board, "season": season, "source_filename": raw_path.name,
                "source_url": url, "pulled_at": _now_iso(),
                "checksum": hashlib.sha256(raw).hexdigest(), "rows": int(len(frame)),
                "columns": int(len(frame.columns)), "parse_status": "VALID",
                "raw_path": str(raw_path), "normalized_path": str(norm_path),
            }
            entries = [e for e in entries if not (e.get("season") == season and e.get("board") == board)]
            entries.append(entry)
            results.append({"board": board, "season": season, "status": "SAVED", "rows": len(frame), "detail": url})
        except Exception as exc:
            if board in NFLVERSE_NGS_FALLBACK_URLS:
                try:
                    frame,raw,fallback_url=_fetch_nflverse_ngs_fallback(client,board,season)
                    raw_dir=root / "raw" / str(season); norm_dir=root / "normalized" / str(season)
                    raw_dir.mkdir(parents=True,exist_ok=True); norm_dir.mkdir(parents=True,exist_ok=True)
                    raw_path=raw_dir / f"{board}_nflverse.csv"; norm_path=norm_dir / f"{board}.csv"
                    raw_path.write_bytes(raw); frame.to_csv(norm_path,index=False)
                    entry={"board":board,"season":season,"source_filename":raw_path.name,"source_url":fallback_url,
                           "pulled_at":_now_iso(),"checksum":hashlib.sha256(raw).hexdigest(),"rows":int(len(frame)),
                           "columns":int(len(frame.columns)),"parse_status":"VALID","raw_path":str(raw_path),
                           "normalized_path":str(norm_path),"source_family":"nflverse_ngs_fallback"}
                    entries=[e for e in entries if not (e.get("season")==season and e.get("board")==board)]
                    entries.append(entry)
                    results.append({"board":board,"season":season,"status":"SAVED_NFLVERSE","rows":len(frame),"detail":fallback_url})
                    continue
                except Exception as ngs_exc:
                    exc=RuntimeError(f"Savant failed: {str(exc)[:90]} | nflverse NGS failed: {str(ngs_exc)[:90]}")
            fallback = "LAST_GOOD" if existing and Path(str(existing.get("normalized_path") or "")).exists() else ("OPTIONAL_UNAVAILABLE" if board in SAVANT_OPTIONAL_BOARDS else "UPLOAD_REQUIRED")
            results.append({"board": board, "season": season, "status": fallback, "rows": existing.get("rows", 0), "detail": str(exc)[:180]})
    save_savant_manifest(root, entries)
    clear_savant_runtime_cache()
    try:
        build_savant_feature_store(root, season)
    except Exception:
        pass
    return results


def _manifest_signature(root: Path):
    manifest = root / "savant_manifest.json"
    try:
        stat = manifest.stat()
        return (str(manifest.resolve()), stat.st_mtime_ns, stat.st_size)
    except Exception:
        return (str(manifest), 0, 0)


def clear_savant_runtime_cache():
    _PACK_CACHE.update({"signature": None, "pack": None})
    _BANK_CACHE.update({"signature": None, "banks": None})


def load_savant_pack(savant_dir, season=None) -> dict[str, pd.DataFrame]:
    root = Path(savant_dir)
    manifest = load_savant_manifest(root)
    entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict) and entry.get("parse_status") == "VALID"]
    seasons = sorted({int(entry.get("season")) for entry in entries if entry.get("season") is not None})
    selected_season = int(season) if season is not None else (seasons[-1] if seasons else None)
    signature = (_manifest_signature(root), selected_season)
    if _PACK_CACHE.get("signature") == signature and isinstance(_PACK_CACHE.get("pack"), dict):
        return _PACK_CACHE["pack"]
    pack = {}
    for entry in entries:
        if selected_season is None or int(entry.get("season", -1)) != selected_season:
            continue
        path = Path(str(entry.get("normalized_path") or ""))
        try:
            frame = pd.read_csv(path)
            if not frame.empty:
                pack[str(entry.get("board"))] = frame
        except Exception:
            continue
    _PACK_CACHE.update({"signature": signature, "pack": pack})
    return pack


def _prefixed_record(row, board):
    skip = {"rank", "qualified", "v", "savant_board", "savant_season", "name"}
    record = {}
    for key, value in row.items():
        if key in skip or (isinstance(value, float) and math.isnan(value)):
            continue
        if key in {"id", "player", "player_key", "team", "position"}:
            record[key] = value
        else:
            record[f"{board.replace('-', '_')}__{key}"] = value
    return record


def build_savant_player_bank(pack: dict[str, pd.DataFrame]) -> dict:
    by_id, by_exact, by_name_pos = {}, {}, {}
    for board, frame in (pack or {}).items():
        if frame.empty or "player" not in frame.columns:
            continue
        for _, series in frame.iterrows():
            row = series.to_dict()
            name = normalize_savant_player_name(row.get("player"))
            team = normalize_savant_team(row.get("team"))
            position = str(row.get("position") or "").upper().strip()
            if not name:
                continue
            exact_key = (name, team, position)
            record = by_exact.setdefault(exact_key, {"player": row.get("player"), "player_key": name, "team": team, "position": position, "boards": []})
            record.update(_prefixed_record(row, board))
            if board not in record["boards"]:
                record["boards"].append(board)
            player_id = str(row.get("id") or "").strip()
            if player_id and player_id.lower() != "nan":
                by_id[player_id] = record
            by_name_pos.setdefault((name, position), []).append(record)
    for key, records in list(by_name_pos.items()):
        unique = []
        seen = set()
        for record in records:
            marker = (record.get("player_key"), record.get("team"), record.get("position"))
            if marker not in seen:
                seen.add(marker); unique.append(record)
        by_name_pos[key] = unique
    return {"by_id": by_id, "by_exact": by_exact, "by_name_pos": by_name_pos}


def build_savant_team_bank(pack: dict[str, pd.DataFrame]) -> dict:
    teams: dict[str, dict] = {}
    pressure = (pack or {}).get("pressure")
    if isinstance(pressure, pd.DataFrame) and not pressure.empty and "team" in pressure.columns:
        for team, group in pressure.groupby("team"):
            team = normalize_savant_team(team)
            if not team:
                continue
            g = group.copy()
            pcol = next((c for c in ["pressure_pct", "pressure__pressure_pct"] if c in g.columns), None)
            ocol = next((c for c in ["opportunities", "pressure__opportunities", "attempts"] if c in g.columns), None)
            scol = next((c for c in ["sacks", "pressure__sacks"] if c in g.columns), None)
            hcol = next((c for c in ["qb_hits", "pressure__qb_hits"] if c in g.columns), None)
            values = pd.to_numeric(g[pcol], errors="coerce") if pcol else pd.Series(dtype=float)
            opportunities = pd.to_numeric(g[ocol], errors="coerce") if ocol else pd.Series(1.0, index=g.index)
            top = g.assign(_pressure=values, _opp=opportunities).sort_values("_pressure", ascending=False).head(4)
            weights = pd.to_numeric(top["_opp"], errors="coerce").fillna(1).clip(lower=1)
            top_rate = float(np.average(pd.to_numeric(top["_pressure"], errors="coerce").fillna(0), weights=weights)) if len(top) else None
            sacks = float(pd.to_numeric(g[scol], errors="coerce").fillna(0).sum()) if scol else None
            hits = float(pd.to_numeric(g[hcol], errors="coerce").fillna(0).sum()) if hcol else None
            teams.setdefault(team, {}).update({
                "pressure_top4_rate": top_rate, "pressure_top4_opportunities": float(weights.sum()) if len(top) else 0,
                "pass_rush_sacks": sacks, "pass_rush_qb_hits": hits,
                "elite_edge_presence": bool(len(top) and float(top["_pressure"].max()) >= 6.0),
                "pressure_player_count": int(len(g)),
            })
    league = (pack or {}).get("league")
    if isinstance(league, pd.DataFrame) and not league.empty and "team" in league.columns:
        for _, series in league.iterrows():
            team = normalize_savant_team(series.get("team"))
            if not team:
                continue
            record = teams.setdefault(team, {})
            for key, value in series.items():
                if key not in {"team", "savant_board", "savant_season"} and not pd.isna(value):
                    record[f"league__{key}"] = value
    penalties = (pack or {}).get("penalties")
    if isinstance(penalties, pd.DataFrame) and not penalties.empty and "team" in penalties.columns:
        for team, group in penalties.groupby("team"):
            team = normalize_savant_team(team)
            if not team:
                continue
            pcol = next((c for c in ["penalties", "penalties__penalties"] if c in group.columns), None)
            ycol = next((c for c in ["penalty_yards", "penalties__penalty_yards"] if c in group.columns), None)
            teams.setdefault(team, {}).update({
                "penalties": float(pd.to_numeric(group[pcol], errors="coerce").fillna(0).sum()) if pcol else None,
                "penalty_yards": float(pd.to_numeric(group[ycol], errors="coerce").fillna(0).sum()) if ycol else None,
            })
    return teams


def _savant_banks(savant_dir, season=None):
    root = Path(savant_dir)
    signature = (_manifest_signature(root), season)
    if _BANK_CACHE.get("signature") == signature and isinstance(_BANK_CACHE.get("banks"), dict):
        return _BANK_CACHE["banks"]
    pack = load_savant_pack(root, season)
    banks = {"pack": pack, "players": build_savant_player_bank(pack), "teams": build_savant_team_bank(pack)}
    _BANK_CACHE.update({"signature": signature, "banks": banks})
    return banks


def _sample_size(record, position=""):
    position = str(position or record.get("position") or "").upper()
    if position == "QB":
        keys = ["passing__attempts", "ngs_passing__attempts"]
    elif position in {"RB", "FB"}:
        keys = ["rushing__carries", "ngs_rushing__carries", "receiving__targets"]
    elif position in {"WR", "TE"}:
        keys = ["receiving__targets", "ngs_receiving__targets", "route_tree__targets"]
    else:
        keys = ["pressure__opportunities", "penalties__measured_flags"]
    return int(max([_finite(record.get(key), 0) or 0 for key in keys] or [0]))


def savant_player_context(player, team, position, savant_dir, season=None, player_id=None) -> dict:
    banks = _savant_banks(savant_dir, season)
    player_bank = banks["players"]
    name = normalize_savant_player_name(player)
    team = normalize_savant_team(team)
    position = str(position or "").upper().strip()
    record = None
    status = "NO_MATCH"
    if player_id and str(player_id) in player_bank["by_id"]:
        candidate = player_bank["by_id"][str(player_id)]
        if not position or not candidate.get("position") or candidate.get("position") == position:
            record = candidate; status = "ID_MATCH"
    if record is None:
        record = player_bank["by_exact"].get((name, team, position))
        if record is not None:
            status = "EXACT_MATCH"
    if record is None:
        candidates = player_bank["by_name_pos"].get((name, position), [])
        if len(candidates) == 1:
            record = candidates[0]
            status = "TRADED_OR_TEAMLESS_MATCH"
        elif len(candidates) > 1:
            return {"matched": False, "match_status": "AMBIGUOUS_REJECTED", "candidate_count": len(candidates)}
    if record is None and len(name) >= 7 and position:
        fuzzy=[]
        for (candidate_name,candidate_position), candidates in player_bank["by_name_pos"].items():
            if candidate_position != position or len(candidates) != 1:
                continue
            score=difflib.SequenceMatcher(None,name,candidate_name).ratio()
            if score >= 0.94:
                fuzzy.append((score,candidate_name,candidates[0]))
        fuzzy.sort(key=lambda item:item[0],reverse=True)
        if fuzzy and (len(fuzzy)==1 or fuzzy[0][0]-fuzzy[1][0] >= 0.04):
            record=fuzzy[0][2]; status="STRICT_FUZZY_MATCH"
        elif fuzzy:
            return {"matched":False,"match_status":"AMBIGUOUS_FUZZY_REJECTED","candidate_count":len(fuzzy)}
    if record is None:
        return {"matched": False, "match_status": status, "sample_size": 0, "reliability": 0}
    out = dict(record)
    sample = _sample_size(out, position)
    board_count = len(out.get("boards") or [])
    target = 300 if position == "QB" else 100 if position in {"WR", "TE"} else 160 if position in {"RB", "FB"} else 400
    reliability = int(_clamp(35 + 42 * min(1.0, sample / max(1, target)) + min(18, board_count * 5), 0, 99))
    out.update({"matched": True, "match_status": status, "sample_size": sample, "reliability": reliability})
    return out


def savant_team_context(team, savant_dir, season=None) -> dict:
    record = dict(_savant_banks(savant_dir, season)["teams"].get(normalize_savant_team(team), {}))
    record["matched"] = bool(record)
    record["team"] = normalize_savant_team(team)
    return record


def savant_matchup_context(team, opp, savant_dir, season=None) -> dict:
    offense = savant_team_context(team, savant_dir, season)
    defense = savant_team_context(opp, savant_dir, season)
    pressure = _finite(defense.get("league__pressure"), _finite(defense.get("pressure_top4_rate")))
    rush_def = _finite(defense.get("league__def_rush_epa"))
    pass_def = _finite(defense.get("league__def_pass_epa"))
    off_epa = _finite(offense.get("league__off_epa"), _finite(offense.get("league__epa")))
    off_success = _finite(offense.get("league__off_success"), _finite(offense.get("league__success")))
    def_success = _finite(defense.get("league__def_success"))
    components = []
    if off_epa is not None and pass_def is not None:
        components.append(_clamp((off_epa - pass_def) / 0.30, -1, 1))
    if off_success is not None and def_success is not None:
        components.append(_clamp((off_success - def_success) / 12.0, -1, 1))
    score = 50 + (float(np.mean(components)) * 18 if components else 0)
    matchup_samples=[_finite(defense.get(key)) for key in ["league__plays","league__dropbacks","pressure_top4_opportunities"]]
    matchup_sample=max([value for value in matchup_samples if value is not None] or [0])
    matched = bool(offense.get("matched") and defense.get("matched"))
    penalty_environment=_clamp(((_finite(offense.get("penalties"),0) or 0)+(_finite(defense.get("penalties"),0) or 0))/180.0,0.0,1.0)
    return {
        "off_team": normalize_savant_team(team), "def_team": normalize_savant_team(opp),
        "off_epa": off_epa, "def_epa_allowed": _finite(defense.get("league__def_epa")),
        "off_success": off_success, "def_success_allowed": def_success,
        "pass_pressure_matchup": pressure, "rush_matchup": rush_def,
        "explosive_pass_matchup": _finite(defense.get("league__explosive_pass_allowed")),
        "explosive_rush_matchup": _finite(defense.get("league__explosive_rush_allowed")),
        "matchup_score": round(score, 1), "matchup_reliability": 82 if matched else 52 if defense.get("matched") else 20,
        "matchup_sample_size":int(matchup_sample),"penalty_environment":round(penalty_environment,3),
        "source": "NFL_SAVANT", "matched": matched,
        "consumed_factors": [key for key, value in {
            "team_efficiency": off_epa, "defense_efficiency": pass_def,
            "success_rate": def_success, "pressure": pressure, "rush_defense": rush_def,
        }.items() if value is not None],
    }


def savant_data_readiness(savant_dir, season=None) -> dict:
    root = Path(savant_dir)
    manifest = load_savant_manifest(root)
    entries = [entry for entry in manifest.get("files", []) if isinstance(entry, dict) and entry.get("parse_status") == "VALID"]
    seasons = sorted({int(entry.get("season")) for entry in entries if entry.get("season") is not None})
    selected = int(season) if season is not None else (seasons[-1] if seasons else None)
    selected_entries = [entry for entry in entries if selected is not None and int(entry.get("season", -1)) == selected]
    boards = {entry.get("board") for entry in selected_entries}
    missing = sorted(SAVANT_REQUIRED_BOARDS - boards)
    required_present=sorted(SAVANT_REQUIRED_BOARDS & boards)
    optional_present=sorted(SAVANT_OPTIONAL_BOARDS & boards)
    optional_missing=sorted(SAVANT_OPTIONAL_BOARDS - boards)
    rows = sum(int(entry.get("rows", 0) or 0) for entry in selected_entries)
    status = "FULL" if not missing else "PARTIAL" if boards else "MISSING"
    return {"status": status, "season": selected, "boards": sorted(boards), "missing": missing,
            "board_count": len(required_present), "required_count": len(SAVANT_REQUIRED_BOARDS),
            "required_present":required_present,"optional_present":optional_present,"optional_missing":optional_missing,
            "rows": rows, "updated_at": manifest.get("updated_at")}


def build_savant_feature_store(savant_dir, season=None) -> dict:
    """Persist the normalized player/team banks used by projection lookups."""
    root=Path(savant_dir)
    readiness=savant_data_readiness(root,season)
    selected=readiness.get("season")
    if selected is None:
        return {"status":"MISSING","season":None,"player_rows":0,"team_rows":0}
    clear_savant_runtime_cache()
    banks=_savant_banks(root,selected)
    players=[]; seen=set()
    for record in banks["players"]["by_exact"].values():
        marker=(record.get("player_key"),record.get("team"),record.get("position"))
        if marker in seen:
            continue
        seen.add(marker)
        row=dict(record); row["boards"]="|".join(sorted(row.get("boards") or []))
        row["sample_size"]=_sample_size(record,record.get("position"))
        players.append(row)
    teams=[{"team":team,**record} for team,record in sorted(banks["teams"].items())]
    store_dir=root/"feature_store"/str(selected); store_dir.mkdir(parents=True,exist_ok=True)
    player_path=store_dir/"savant_player_features.csv"; team_path=store_dir/"savant_team_features.csv"
    pd.DataFrame(players).to_csv(player_path,index=False)
    pd.DataFrame(teams).to_csv(team_path,index=False)
    metadata={"status":"READY" if players else "PARTIAL","season":int(selected),"built_at":_now_iso(),
              "player_rows":len(players),"team_rows":len(teams),"player_path":str(player_path),"team_path":str(team_path)}
    (store_dir/"feature_store_manifest.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
    return metadata


def attach_savant_context(row, savant_dir, season=None) -> dict:
    out = dict(row or {})
    player = savant_player_context(out.get("player"), out.get("team"), out.get("position"), savant_dir, season, out.get("player_id"))
    team = savant_team_context(out.get("team"), savant_dir, season)
    matchup = savant_matchup_context(out.get("team"), out.get("opp"), savant_dir, season)
    readiness = savant_data_readiness(savant_dir, season)
    out.update({
        "savant_player_context": player, "savant_team_context": team,
        "savant_matchup_context": matchup, "savant_readiness": readiness,
        "savant_player_match": bool(player.get("matched")),
        "savant_team_match": bool(team.get("matched")),
        "savant_sample_size": int(player.get("sample_size", 0) or 0),
        "savant_matchup_sample_size":int(matchup.get("matchup_sample_size",0) or 0),
        "savant_reliability": int(player.get("reliability", 0) or 0),
        "savant_season": readiness.get("season"),
        "savant_updated_at":readiness.get("updated_at"),
        "savant_status": "FULL" if player.get("matched") and matchup.get("matched") else "PARTIAL" if player.get("matched") or team.get("matched") else "MISSING",
    })
    return out


def _shrunk(value, prior, sample, target):
    value = _finite(value)
    if value is None:
        return prior, 0.0
    weight = _clamp(float(sample) / max(1.0, float(sample) + float(target)), 0.0, 0.92)
    return prior * (1 - weight) + value * weight, weight


def _metric(record, *keys):
    for key in keys:
        value = _finite(record.get(key))
        if value is not None:
            return value
    return None


def _route_profile(player):
    routes = {key: _metric(player, f"route_tree__{key}") or 0.0 for key in [
        "screen", "swing", "quick_out", "slant", "curl", "drag", "dig", "corner",
        "deep_out", "post", "go", "wheel", "texas",
    ]}
    short = sum(routes[key] for key in ["screen", "swing", "quick_out", "slant", "curl", "drag", "texas"])
    deep = sum(routes[key] for key in ["corner", "deep_out", "post", "go", "wheel"])
    intermediate = max(0.0, 100.0 - short - deep)
    return {"short_pct": round(short, 2), "intermediate_pct": round(intermediate, 2),
            "deep_pct": round(deep, 2), "screen_yac_pct": round(routes["screen"] + routes["swing"], 2),
            "vertical_pct": round(routes["post"] + routes["go"] + routes["corner"] + routes["deep_out"], 2)}



def attach_best_savant_context(row, savant_dir):
    """Prefer current-season NGS/Savant once real boards exist; otherwise use prior season."""
    season=int(NFL_LAST_SEASON)
    try:
        pack=load_savant_pack(savant_dir,int(NFL_CURRENT_SEASON))
        if any(isinstance(df,pd.DataFrame) and not df.empty for df in (pack or {}).values()):
            season=int(NFL_CURRENT_SEASON)
    except Exception:
        season=int(NFL_LAST_SEASON)
    out=attach_savant_context(row,savant_dir,season)
    player=out.get("savant_player_context") or {}
    # Flatten only the small set of NGS/route metrics consumed by live projection logic.
    flatten={
        "avg_separation":["ngs-receiving__avg_separation","ngs_receiving__avg_separation","avg_separation"],
        "avg_cushion":["ngs-receiving__avg_cushion","ngs_receiving__avg_cushion","avg_cushion"],
        "catch_pct_above_expectation":["ngs-receiving__catch_pct_above_expectation","catch_pct_above_expectation"],
        "time_to_throw":["ngs-passing__time_to_throw","time_to_throw"],
        "cpoe":["ngs-passing__cpoe","cpoe"],
        "yoe_per_attempt":["ngs-rushing__yoe_per_attempt","yoe_per_attempt"],
    }
    for dst,keys in flatten.items():
        for key in keys:
            if _usable_context_value(player.get(key)):
                out[dst]=player.get(key); break
    out["savant_selected_season"]=season
    out["savant_prior_fallback"]=bool(season!=int(NFL_CURRENT_SEASON))
    return out

def savant_shadow_projection(row, legacy_projection, season_mode="REGULAR") -> dict:
    """Create a line-independent, bounded shadow projection from Savant composites."""
    projection = max(0.0, _finite(legacy_projection, 0.0) or 0.0)
    prop = str((row or {}).get("prop") or "")
    player = dict((row or {}).get("savant_player_context") or {})
    matchup = dict((row or {}).get("savant_matchup_context") or {})
    sample = int(player.get("sample_size", 0) or 0)
    reliability = int(player.get("reliability", 0) or 0)
    mode = str(season_mode or "REGULAR").upper()
    factor = 1.0
    components = {}
    profile = {}
    notes = []
    already_consumed = {
        str(value).lower()
        for value in ((row or {}).get("projection_consumed_factors") or [])
    }
    skipped_consumed = []
    if not player.get("matched"):
        return {"active": False, "status": "NO_PLAYER_MATCH", "legacy_projection": round(projection, 3),
                "shadow_projection": round(projection, 3), "factor": 1.0, "reliability": 0,
                "components": {}, "notes": ["Uploaded/cached Savant player match unavailable"]}

    if prop in {"Passing Yards", "Completions"}:
        attempts = max(sample, int(_metric(player, "passing__attempts", "ngs_passing__attempts") or 0))
        ypa, wy = _shrunk(_metric(player, "passing__yards_per_attempt", "passing__ya"), 7.0, attempts, 220)
        epa, we = _shrunk(_metric(player, "passing__epa_play", "passing__epa"), 0.02, attempts, 240)
        success, ws = _shrunk(_metric(player, "passing__success_pct", "passing__succ"), 45.0, attempts, 220)
        comp, wc = _shrunk(_metric(player, "passing__comp_pct", "ngs_passing__comp_pct"), 64.0, attempts, 200)
        xcomp, wx = _shrunk(_metric(player, "ngs_passing__xcomp_pct"), 64.0, attempts, 220)
        cpoe, wo = _shrunk(_metric(player, "passing__cpoe", "ngs_passing__cpoe"), 0.0, attempts, 220)
        accuracy = _clamp(((comp - 64.0) / 10.0) * .35 + ((xcomp - 64.0) / 9.0) * .35 + (cpoe / 8.0) * .30, -1.5, 1.5)
        efficiency = _clamp(((ypa - 7.0) / 1.4) * .42 + ((epa - .02) / .22) * .33 + ((success - 45.0) / 9.0) * .25, -1.5, 1.5)
        pressure_vulnerability = _metric(player, "passing__pressure_pct", "ngs_passing__pressure_pct")
        opponent_pressure = _finite(matchup.get("pass_pressure_matchup"))
        pressure_score = 0.0
        if pressure_vulnerability is not None and opponent_pressure is not None:
            pressure_score = _clamp(((pressure_vulnerability - 16.0) / 10.0 + (opponent_pressure - 5.5) / 4.5) / 2.0, -1.2, 1.2)
        use_pressure = not any("pressure" in value for value in already_consumed)
        applied_pressure = pressure_score if use_pressure else 0.0
        if pressure_score and not use_pressure:
            skipped_consumed.append("pressure")
        if prop == "Completions":
            factor = 1.0 + accuracy * .028 - max(0.0, applied_pressure) * .012
        else:
            factor = 1.0 + efficiency * .034 + accuracy * .012 - max(0.0, applied_pressure) * .015
        components = {"accuracy_composite": round(accuracy, 3), "efficiency_composite": round(efficiency, 3),
                      "pressure_matchup_diagnostic": round(pressure_score, 3),
                      "pressure_applied": round(applied_pressure, 3),
                      "sample_weight": round(max(wy, we, ws, wc, wx, wo), 3)}
        notes.append("QB accuracy and efficiency are one correlated composite")
    elif prop in {"Receiving Yards", "Receptions"}:
        targets = max(sample, int(_metric(player, "receiving__targets", "ngs_receiving__targets", "route_tree__targets") or 0))
        ypt, wy = _shrunk(_metric(player, "receiving__yards_per_target", "ngs_receiving__yards_per_target"), 7.4, targets, 70)
        epa, we = _shrunk(_metric(player, "receiving__epa_tgt", "receiving__epa"), 0.08, targets, 80)
        catch, wc = _shrunk(_metric(player, "receiving__catch_pct", "ngs_receiving__catch_pct"), 65.0, targets, 65)
        air, wa = _shrunk(_metric(player, "receiving__air_yards_per_target", "ngs_receiving__air_yards_per_target"), 8.5, targets, 70)
        yac, wv = _shrunk(_metric(player, "receiving__yac_per_reception", "ngs_receiving__yac_per_reception"), 4.5, targets, 70)
        efficiency = _clamp(((ypt - 7.4) / 2.4) * .55 + ((epa - .08) / .32) * .25 + ((catch - 65.0) / 15.0) * .20, -1.5, 1.5)
        depth_yac = _clamp(((air - 8.5) / 5.0) * .58 + ((yac - 4.5) / 3.0) * .42, -1.4, 1.4)
        factor = 1.0 + (efficiency * (.037 if prop == "Receiving Yards" else .018))
        if prop == "Receptions":
            factor += _clamp((catch - 65.0) / 100.0, -.025, .025)
        components = {"receiving_efficiency_composite": round(efficiency, 3), "depth_yac_profile": round(depth_yac, 3),
                      "sample_weight": round(max(wy, we, wc, wa, wv), 3)}
        profile = _route_profile(player)
        notes.append("Correlated receiving metrics are collapsed into one efficiency composite")
    elif prop == "Rushing Yards":
        carries = max(sample, int(_metric(player, "rushing__carries", "ngs_rushing__carries") or 0))
        ypc, wy = _shrunk(_metric(player, "rushing__yards_per_carry", "ngs_rushing__yards_per_carry"), 4.25, carries, 110)
        yoe, wo = _shrunk(_metric(player, "ngs_rushing__yoe_per_attempt"), 0.0, carries, 100)
        epa, we = _shrunk(_metric(player, "rushing__epa_att", "rushing__epa"), -0.03, carries, 120)
        talent = _clamp(((ypc - 4.25) / 1.1) * .38 + (yoe / 1.25) * .37 + ((epa + .03) / .20) * .25, -1.5, 1.5)
        rush_matchup = _finite(matchup.get("rush_matchup"))
        matchup_score = _clamp((-rush_matchup / .18), -1.0, 1.0) if rush_matchup is not None else 0.0
        use_matchup = not any(value in {"legacy_matchup", "rush_matchup", "rush_defense"} for value in already_consumed)
        applied_matchup = matchup_score if use_matchup else 0.0
        if matchup_score and not use_matchup:
            skipped_consumed.append("rush_matchup")
        factor = 1.0 + talent * .035 + applied_matchup * .018
        components = {"rushing_talent_composite": round(talent, 3),
                      "rush_matchup_diagnostic": round(matchup_score, 3),
                      "rush_matchup_applied": round(applied_matchup, 3),
                      "sample_weight": round(max(wy, wo, we), 3)}
        notes.append("YOE talent and opponent rush matchup remain separate")
    else:
        return {"active": False, "status": "WORKLOAD_PROP_UNCHANGED", "legacy_projection": round(projection, 3),
                "shadow_projection": round(projection, 3), "factor": 1.0, "reliability": reliability,
                "components": {}, "notes": ["Savant does not manufacture workload volume"]}

    # In preseason, Savant is only a prior for per-opportunity efficiency. Workload is untouched.
    cap = 0.035 if mode == "PRESEASON" else 0.075
    if mode == "PRESEASON":
        factor = 1.0 + (factor - 1.0) * 0.45
        notes.append("Preseason Savant effect down-weighted; rotation remains workload source")
    factor = _clamp(factor, 1.0 - cap, 1.0 + cap)
    shadow = max(0.0, projection * factor)
    if skipped_consumed:
        notes.append("Already-consumed factors skipped: " + ", ".join(sorted(set(skipped_consumed))))
    return {"active": True, "status": "SHADOW", "legacy_projection": round(projection, 3),
            "shadow_projection": round(shadow, 3), "factor": round(factor, 4),
            "reliability": reliability, "sample_size": sample, "components": components,
            "route_profile": profile, "matchup": matchup, "notes": notes,
            "market_inputs_used": False, "workload_adjusted": False,
            "already_consumed_factors": sorted(already_consumed),
            "skipped_consumed_factors": sorted(set(skipped_consumed))}


def distribution_conflict_audit(expected_mean, p50, line, pick) -> dict:
    mean = _finite(expected_mean)
    median = _finite(p50)
    line = _finite(line)
    pick = str(pick or "").upper()
    if mean is None or median is None or line is None:
        return {"conflict": False, "status": "NO_LINE", "median_edge": None}
    mean_side = "OVER" if mean > line else "UNDER" if mean < line else "PUSH"
    median_side = "OVER" if median > line else "UNDER" if median < line else "PUSH"
    conflict = (pick in {"OVER", "UNDER"} and mean_side != pick) or (mean_side != median_side and median_side != "PUSH")
    return {"conflict": bool(conflict), "status": "DISTRIBUTION_CONFLICT" if conflict else "ALIGNED",
            "mean_side": mean_side, "median_side": median_side, "pick_side": pick,
            "median_edge": round(median - line, 3), "expected_mean": round(mean, 3),
            "p50_fair_line": round(median, 3)}


def side_distribution_audit(rows, season_mode=None) -> pd.DataFrame:
    records = []
    grouped = {}
    for row in rows or []:
        mode = str(row.get("season_mode") or season_mode or "REGULAR").upper()
        grouped.setdefault((mode, str(row.get("prop") or "UNKNOWN")), []).append(row)
    for (mode, prop), group in sorted(grouped.items()):
        total = max(1, len(group))
        picks = [str(row.get("pick") or "PASS").upper() for row in group]
        passes = sum(1 for row, pick in zip(group, picks) if pick == "PASS" or str(row.get("action_tier") or "").upper() == "PASS")
        edges = [_finite(row.get("edge")) for row in group]
        ratios = [(_finite(row.get("projection")) / _finite(row.get("line"))) for row in group if _finite(row.get("projection")) is not None and (_finite(row.get("line")) or 0) > 0]
        low_workload = sum(1 for row in group if str(row.get("preseason_workload_confidence") or (row.get("preseason_workload") or {}).get("confidence") or "").upper() in {"LOW", "UNKNOWN"})
        partial = sum(1 for row in group if str(row.get("savant_status") or "").upper() in {"PARTIAL", "MISSING"} or str(row.get("audit_label") or "").upper() in {"PARTIAL", "STALE"})
        conflicts = sum(1 for row in group if (row.get("distribution_conflict") or {}).get("conflict"))
        records.append({"season_mode": mode, "prop": prop, "rows": len(group),
                        "over_pct": round(100 * picks.count("OVER") / total, 1),
                        "under_pct": round(100 * picks.count("UNDER") / total, 1),
                        "pass_pct": round(100 * passes / total, 1),
                        "median_edge": round(float(np.median([x for x in edges if x is not None])), 3) if any(x is not None for x in edges) else None,
                        "median_projection_line_ratio": round(float(np.median(ratios)), 4) if ratios else None,
                        "low_workload_rows": low_workload, "partial_data_rows": partial,
                        "distribution_conflicts": conflicts,
                        "warning": f"{round(100 * max(picks.count('OVER'), picks.count('UNDER')) / total):.0f}% {max(set(picks), key=picks.count) if picks else 'PASS'} - inspect workload/data priors" if max(picks.count("OVER"), picks.count("UNDER")) / total >= .84 else ""})
    return pd.DataFrame(records)


def build_savant_backup_zip(savant_dir) -> bytes:
    root = Path(savant_dir)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")) if root.exists() else []:
            if path.is_file() and path.suffix.lower() in {".csv", ".json"}:
                archive.write(path, path.relative_to(root))
    return buffer.getvalue()

APP_VERSION = "NFL v7.53 — SELF-CONTAINED VERIFIED NGS + DATA INTEGRITY"
MODEL_VERSION = "nfl-prop-engine-v7.53.0"
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
MONEYLINE_RESULT_LOG = LOCAL_DIR / "nfl_moneyline_results.json"
MONEYLINE_SNAPSHOT_FILE = LOCAL_DIR / "nfl_moneyline_snapshots.json"
MONEYLINE_CLV_FILE = LOCAL_DIR / "nfl_moneyline_clv.json"
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
INJURY_OVERRIDE_CSV = LOCAL_DIR / "injury_overrides.csv"
FINAL_INACTIVES_CSV = LOCAL_DIR / "final_inactives.csv"
ROLE_OVERRIDE_CSV = LOCAL_DIR / "role_overrides.csv"
DEF_INJURY_OVERRIDE_CSV = LOCAL_DIR / "defensive_injury_overrides.csv"
MANUAL_OVERRIDE_FILE = LOCAL_DIR / "nfl_manual_overrides.json"
PRESEASON_ROTATION_FILE = LOCAL_DIR / "nfl_preseason_rotations.json"
PRESEASON_PRIOR_FILE = LOCAL_DIR / "nfl_preseason_efficiency_priors.csv"
SAVANT_DIR = LOCAL_DIR / "nfl_savant"
SAVANT_DIR.mkdir(parents=True, exist_ok=True)
SAVANT_MANIFEST_FILE = SAVANT_DIR / "savant_manifest.json"
SAVANT_PRODUCTION_ENABLED = str(os.getenv("NFL_SAVANT_PRODUCTION", "0")).strip().lower() in {"1", "true", "yes", "on"}
# Production remains disabled until walk-forward validation approves individual layers.
SAVANT_VALIDATED_PRODUCTION_PROPS = set()

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

# -----------------------------------------------------------------------------
# REAL BUNDLED DATA AUTOLOAD
# -----------------------------------------------------------------------------
# Railway's persistent STORAGE_DIR is separate from the checked-out GitHub repo.
# The repository already contains populated 2025 baseline files at its root, but
# older app builds looked only inside STORAGE_DIR/nfl_engine.  This bootstrap
# copies verified, non-empty repository data into the active storage folders on
# startup. It NEVER replaces a valid persistent database with an empty template.

def _fast_csv_data_rows(path, stop_after=None):
    """Count CSV rows, optionally stopping once a validation threshold is met."""
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size < 60:
            return 0
        rows = 0
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            next(fh, None)
            for line in fh:
                if not line.strip():
                    continue
                rows += 1
                if stop_after is not None and rows >= int(stop_after):
                    break
        return rows
    except Exception:
        return 0


def _fast_json_items(path):
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size < 20:
            return 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        return len(payload) if isinstance(payload, (dict, list)) else 0
    except Exception:
        return 0


def _bundled_file_is_real(path, minimum_rows, kind="csv"):
    if kind == "json":
        return _fast_json_items(path) >= int(minimum_rows)
    return _fast_csv_data_rows(path, minimum_rows) >= int(minimum_rows)


def _copy_real_bundled_file(source, target, minimum_rows, kind="csv"):
    """Copy only validated real data; preserve an already-valid target."""
    source, target = Path(source), Path(target)
    source_rows = _fast_json_items(source) if kind == "json" else _fast_csv_data_rows(source, minimum_rows)
    target_rows = _fast_json_items(target) if kind == "json" else _fast_csv_data_rows(target, minimum_rows)
    result = {
        "source": str(source), "target": str(target), "source_rows": int(source_rows),
        "target_rows_before": int(target_rows), "status": "SKIPPED",
    }
    if source_rows < int(minimum_rows):
        result["status"] = "SOURCE_MISSING_OR_TEMPLATE"
        return result
    if target_rows >= int(minimum_rows):
        result["status"] = "TARGET_ALREADY_VALID"
        return result
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        installed_rows = _fast_json_items(target) if kind == "json" else _fast_csv_data_rows(target, minimum_rows)
        result["target_rows_after"] = int(installed_rows)
        result["status"] = "INSTALLED_REAL_DATA" if installed_rows >= int(minimum_rows) else "COPY_VALIDATION_FAILED"
    except Exception as exc:
        result["status"] = "COPY_ERROR"
        result["error"] = str(exc)[:180]
    return result


def install_bundled_real_projection_data():
    """Install real repository baseline files into Railway's active storage."""
    repo_root = Path.cwd()
    tasks = [
        (repo_root / "nfl_player_usage.csv", USAGE_FILE, 25, "csv"),
        (repo_root / "nfl_player_logs_last_season.csv", PHASE6_PLAYER_LOG_FILE, 100, "csv"),
        (repo_root / "nfl_player_summary_last_season.csv", PHASE6_PLAYER_SUMMARY_FILE, 25, "csv"),
        (repo_root / "nfl_team_context_last_season.json", PHASE6_TEAM_CONTEXT_FILE, 16, "json"),
        (repo_root / "nfl_defense_ranks_last_season.csv", PHASE6_DEFENSE_RANK_FILE, 28, "csv"),
        (repo_root / "nfl_travel_stadium_context.csv", PHASE6_TRAVEL_FILE, 100, "csv"),
        (repo_root / "nfl_team_advanced_last_season.csv", PHASE6_TEAM_ADVANCED_FILE, 28, "csv"),
        (repo_root / "nfl_trench_context_last_season.csv", PHASE6_TRENCH_FILE, 28, "csv"),
        (repo_root / "nfl_red_zone_usage_last_season.csv", PHASE6_RED_ZONE_FILE, 25, "csv"),
        (repo_root / "nfl_overtime_context_last_season.csv", PHASE6_OT_FILE, 28, "csv"),
        (repo_root / "nfl_team_context.json", TEAM_CONTEXT_FILE, 16, "json"),
    ]
    diagnostics = [_copy_real_bundled_file(*task) for task in tasks]

    # Last-season team context is a safe baseline hook when no separate current
    # context file has been supplied yet.
    if not _bundled_file_is_real(TEAM_CONTEXT_FILE, 16, "json"):
        fallback = repo_root / "nfl_team_context_last_season.json"
        diagnostics.append(_copy_real_bundled_file(fallback, TEAM_CONTEXT_FILE, 16, "json"))

    installed = sum(d.get("status") == "INSTALLED_REAL_DATA" for d in diagnostics)
    valid = sum(d.get("status") in {"INSTALLED_REAL_DATA", "TARGET_ALREADY_VALID"} for d in diagnostics)
    return {"installed": int(installed), "valid": int(valid), "files": diagnostics}


BUNDLED_REAL_DATA_BOOTSTRAP = install_bundled_real_projection_data()

# -----------------------------------------------------------------------------
# VERIFIED EMBEDDED 2025 NGS FALLBACK
# -----------------------------------------------------------------------------
# These are the verified 2025 REG season-summary rows from the full nflverse NGS
# passing/receiving/rushing exports previously validated for this app.  They are
# embedded so Railway does not depend on a fragile browser upload or remote NGS
# endpoint.  Existing valid persistent boards are preserved; only missing NGS
# boards are installed.  Projection formulas are untouched.
_EMBEDDED_VERIFIED_NGS_2025 = {
    'ngs-passing': 'H4sIAP6Lk2oC/22b23Ybx5JE389X8ANKvep+eZR8l2Udj+UzHj9pwSQkcUwRWgBlW/P1syOrAQKWRRoEQLC7KiozMiKrfNhuDrt7d7Afrx8+fdi6P7fb392Hu82n7f71ze1Bz17fb95vj+992B1uH275o4ft5v3rzW+/7d3mj7evH27fb18/7F4/vNvv/rR3rnfvP9xtH7Y3rze3+9efNvubg71/e/+wvb/57O3TK+765s12v71/uN3cuc3bt/vt4XD7x/aeH+795q+/XZgxPmzur7d/uwhDOTzcXv/OxR8etu8/PBzch83hsN7Qnj7sPl6/u9n9eX9wGtP+evtBE5u/Zab7zcPt/Vu33s5+8/j89Qf9wf3D5u3Wbf/6sL3WeP751//4Lsjt/ti+nn+6MUCPEzjNSJO9eGNdgreH28Pr25vjyze3+8PDxRrdbf72xv9u94ct6/jx/W/b/fHNw7vdfn7sX9HH4n766hvn3Rebu+1vV7/c3t3dbt4f3H89c198+51LSxilV59j9rHH5ipvtHL6qq4vOflRfGk+5RiDexKXmFJquYfYas3FBb+UmlLnCqEOH1yNSwh51DKGzyNG94RP+JyDr7mm3npKrtTu0sjRcdPmhl9CrKGl5vtg1C4l70pf/OhlhMJFGKareRktxZZrbr235J7UpfOqp/VzycWwpJi8j7kE3uJv6uJ7KFw75t5rdd4/8T6NEfrExJ0w0TvL8dU5dj8Qa++2f169eti8ebPb3wi9F09/cnFpY8Ry/EquLTwy7TZSDzknN5jBAN6QfGdmxT0JS89l8LtUx8glctelML2ae8wh+qpp5wDgqQXQy8OFxYNrb4Mp+RbBbjSXm+eB9QH+oVUEfBBPHez6hGqEGnMbvgbhkJYCkK2lXkfKjavmVkNu2afai+8usrCBYGixFwDVKsbRYk2hR9ZmTORizVzt+e7dvTvBMdwPy/HFZcy93+5391e/bCZmP3/1EswGS5k1Nh9CYA5L64yL+dVQQQsQi0aqQCO+fPUhpp4UV4F3zv51V/wCQoQCIeyFCvCW1oGlZO8SqLlQCK/uidmhyGYdKkGXDMWFRXr81oQ784xl/XZP0uJzC4//Znh1hp4bYTiiFisR2rV530ppE6PsK5mzzt7Z7INCa3MJz5eb+9vt3dXzHRQoeL57+aVCiplkwrzHABh1yaVFn3wtIeVAOkbPRIpig/vZjFvhk6xLSTEQ4KEtobdiQwutsNojNWWasnAoyHrtlTQigFLPwCRoh0USadhzrsRZKaU6lt3Vtoyq6xFShRBmOCxerfzxwnXBi5yFEFIUOkQugQwx5AQEBF0kyDPpRlylvCafRr/O3s3ZEwdfLvb0HKDnu+3V13eb6+ud8dV3ih6iMzELKMh7FrwsjL63WCzPQlUK8vvaQYSEJ50II5DzlmCRJHIB0oh1wBJNj8HlAa00YG0lhggLTEzP1z1zZeWSwomxN2VcbcqeBCbNxQJQfiGpyUDw47HojRpsKUA+Enun30KakdBNhGos5LSGVcLC2pABvgJqOmYbGQkOh+2Hd26FgqE8X+bzc7Ce7T9db69+3X2ktgkt46eecqpMn9hoMEPmDTFI5b4BhiG8CPSQTt9Cy8NO0DeIRsaqpEsqATXOH5AOgAJxzXBQyJZz8HE6fme4CW73SpfkeOyNtO0jMx394FfZ6Gj0BLvMH3qDPM4EWBpklkHWCTRCWESWqCJRKKpm9MyKUz9YuBRJt6KEVLKv3A57TjjchGO4Z4s9O8fr+0932/3VDx/3+80nAfb0p+8AjFoCIXJ1glr5oVBJpy/GDSMQYJWM6KPDkk+MNcdIHtIS0C6kRWURwKhVLKVWllUoFIVRyRj9CYu8XoE5wsXgzOdddcl16gGkHCo1NYinibdKKsc4ekjzyq5q5qEStYFUThZhyrFRKjEMSIw9DG4LOYFO9DDF34ex5iJJMbFwKxbBfb/Mp+dwvdi83+yvnm+QXnAaeD17+gJ4PKGQRdlDK7D+o54jADrEAiCoimIpVbT6qbauIKfuBWLPF2bFNSjXrhQiAFoDmGph5MF6HK/AErMcKIMIO8I0sDqXJ0/5paotWDLjaIHVlDXEmyLJVS4LmgouNECYKxbL6cZ2tQVeU7FMVTLoH4ZiWEEAdQLhjkB092JZn18m4+bm09UXu93vgurlr88JrUI4PP4DOwBrBO/8Yv7kGVnFss5vYTaIIE2HDzbiI5TFkyxN5YHxwksQ9qD0EDyhBRiXkgVk4XQR/Q26JDFJEGNeWcQJD6FBiAmUQmHxyK+k6EFDGOF3uJUkmrdiHMpF6tD6OQstRsaiIMRIaPIwik9QNVyAe65VkM9OJJwhkUlDPfk7xT8j2DAXJ4pHSDI835I9gguLF0lH+KhTloi2qhpI4HXEZDCcohQgKCIT+AQU/4iABlT0EWp878IxGL1QNOwCcFprEqJU/KCUJiGL1ULIoIUBYjzybpKYRIWRhPPRZJZylZnPR0ZP5aSKpPXRFAOMhdylctbKMitAI/KFUPNHrKAWRgAYbgVjwPHz6QVaHzE/91ffbvfo/YcpQ7+QpPI2TBJBdVqipRQwI7kHmoECzm9JdJaTW5oIHUiWIaoLI4ilAZm7EAsDbYZ46pHfJsFE4UaxoT1JHxYVKIldJoA4gOrcyJK4fBjmlgxwXFQ4oYG4INkIvCEJ6yFZv0htYBxUXro3TgeUIMFMtUFDfDaEFR8AXafvjtOHGJ8v64tzlL7Z3u+uXr2/fXhnCP23hRTUSfoR42Q6dZBQwo5ApSZowYxBDpEpcU+UiCTgSmQqJA5LkD+BpYZztZYU/jhc7uCaEp9vntowiYVaBtYyBVR7B3eJs4qz+HGdSwhvxopbwg2Iz0g3RtKOF7YgwyShfNf34MHGupVU5hu9CjV0PbyolPWWw7I3wAXDSyZO0KD+4r76+BaP7SYezX2z2LMLmb48X/A2+91HU+nf/vs/QowEG+tcuWMhYkKmuHh7RId6f+J7Mb6EgyL6+KEm4cAKYoOUu0lqPWI5uGyT08NvKg6lM7NMRsWkgSmxiDxY9eggslmAWCFnzQyvmEyGJnEefICxMeUAPoR6KYJEdUO+tLWC7UK98w54UfNQ5TILRvAkstYVXikdqXdSDjXBVRTD2xsc/opJA6H59JLid9e/X/34cX9j8uHV1zLT3OFkWBnZIg7jT1htJC9Gd5HgowYyPG5sZRH7Zs66MsVuFG9ut02XO0SuWaInkCXVJBrKUXierkIWUaIkB5ioskLcpfgu65VAs5lkqNibFswuVwGpWD2+F1hjv/5SUzDq8l7yqrOkLMLnQ5moofryhMNNOPjNs8WeXrro/fXHwxU/bncPG2H2y9NXirQS61ldbItdEDOTqNiyJtQ4anyZOROnMRzQJ5OnoIekwo9O488oAbCOEhwRpVqeMB1QNZ+QRcbiSmuNqeFdhymZqZcGp3RJucEEQe0NnENQoQiTE9PxJZXaydOs9/HKUhlnmTXUH6CEqUsJeTFLl6I4yocozTpRcEcUeGNZn18E1+Z3idPNpzd4pOmdnwkqBHkzjy7WkfkhuaA0xCoj62Ql9yfDQp9fJjVBMUJcuCXNHraRxKb8d9iiar5eKkOioUqHPhEjM1gMjn1xG0gJfkvG+EG9GjxLR9IrXzA+Yulqwc+gMpzUxqyMZBdQzQeFeoUMGHAWaTJalcZgwJGnUOjnY1llF6VzIuJOiFQi7PjiwlPv+ZyA25rw+krFkRgQsQ97IL5UqpWEeD+I1no0BeLBWSKwi+kuZttlxKkNpIRQk6WNxwfxBx4pSiCQiEOcEyWFqcCwOyLJqTmgZMFey1ljCshu3N98cKaaJTsNgPlgDQj4DvABjQfJaIQeAkbWFvRBrKgZh1yAL7g4iA1poU7+djnulcf4owmFMyhktwXX9jIfrx97D9AXWRWyjIwiCvsm4ZWNlqOu6CUkIH01CpsWuZv1YyJFvqNF/EeS8hIPI5Y88ryjUGFnVhtWCWKtWSh41SiDlD/ZW3yeE6eIN6obEADZLQGlrhAmUh3FsZxdFVdPkSQIz0QO61zVWIL2PXw1/TVVNERstmxryappVMTOhdQZXIUFaeF+uL1+t3lsRoDN582IL2/v7nb3V99sfturbyG1+kLhJVFwsl0kg023nMv6HqPmTzmNKp1qSAAxU6QUNlHzZ3I1h0WKSDoCf0gtfYLK6qx6RRbxNsU0dMIjqXca1d6CwEjp+V3NMxbI+vQW9Gl8j/kf63fSRdu5cU/qRVImuQL8l+WMlJFYEvwuEQD1HJU9vmUFxB0B6YTY+vxS4O9vNvdXL3Z/WEZ+YzRGzJJy6Oo+NDBrKD/+g8Y6epFQKxL13iBTFBKMQpCfggy8gybUKXJZ00UPVIBmdb36vIsaaaw179rPpHx0SUEZFWjUlgWSZHqkusQHZl1xtb6kAiWflJNU2W5tBkS1FriLY1mYgezAg4n+5UVIfYRKMC5koIyCsCMnjk6bBM4rHM7gMP2qZ+d4/WxtiVfvdh/fmnx9+W/rSniAkEZuokVCigvL+GWShOzpDPrsn0nRHNRJ6rA8tzWzDa2TRKRrM4dbvTwVNUCNCqP9Ve0dNV+SyFeHUB0IN9T65JbAhotXu9isIilHMncRH5cFPoIS/suS8FII4pB29iFxGIV5CCGWK1gLtYIVmUtwh3oKMaqKgeFWMKr7eZlPz/F69W57s/0IYpv7m+3+cMzLpDagLCuKU1gh+Qmo8yBri/QfpplK2GapJKBO7TtlUVrQcxGMEUUBVCVggRHB5qEnEyII8PbYS2RyKLjsvXUn1LZhlurnifWpPcSrSkcVESZ1wVEwahPy9/gPgllaWB6+qrRg6gmZUMwlZfOvFBuJs89Gcmw7sy4rIO4ICCN6tawvLoDbvL/6crO/30198eqrp8pMJGUSl0OdXbaIhfRq+WIoKaIEGlKV0szyDO0KWAUgSSs5iY6SFwnr6NULIB2yQk2bR8gDKQQ/Zh8a1h7SWUH9CbUispckQIRlN9Q+IlFjFOdDf2rXE2tNDj5Xa/TajoZMIpad4fJBlhgYH/8NwYbBH7XL4Ld/GMlRX/BZ4HBHOBjCq2V9ccllh3dXT+/utrMB9p+vjcuCehFiA8YiDY19hXTQWKJoRRnrDBhcJ+ZZ/ggQNFrswTokQa0xKx44HwajCPHamlGdTUoiuQT1HCMxwAdIoly9swZELFPwU1NYJqoqrAlvY+tN8EPvRMnsC87GDg5zfVNbQOjY433VHwYwdRKwvdAG4/18KCdF1gyNjxs38cDdPl/s6QVgm/325uqb3Zs3AuzLr34Wlw1EIpU9TN9YUNPYRqYX1aXXDiTTsJy0RxOOxbOAhRkit1uRs8ylakmJGW2zqbUZmSTyRT2oYiiTsZDV0CPrQsnkb5Bj2TY6cFldxl2NQziHUmwbHeietqoIqwVZ7Sxwb7gobcRRAORMusq22ibU2Crzrlj26hvhO0hlfZrB9qPISJDABMMZGNbC17NzsH7cPOxvsZQ/bN7t3k9V9r2aO9gaGSwiCx7KKoN6lBpTMz909VPxgjZptc6UH1Kt2YACQ3iF0m3NZIkCKrx8MYk9TO33NshnWGVU7cC6oj6N7EWM1r0f5DNBl4aEtPRsUVFseZjQhHTQ0FVascg+q54Eo0YST72loo25Orv3Ak8pq08KXhXwoY05NTVWqLSvu0LhjlBwyx+X9cXlDtoftzhKhMjhsXOhIDHOF6N2bQgFSepR7RHbtwgc9Su1t7n2poGkq8iiOGqw/sqxcnCVYOoCWLsqQ5NNBDdZ+nkJ7WooLNEQQ47RgZrcjrWPyc0mKa8YhOiR9yAfCWZjflYKI0KwzE0PY+ChXmESnNkUf1H5CNqWETn8fSSruFD/2+BwE44p+fX0HxqIX8s2HR5b0ygor37B/M9CDcKiREuc2U42BZvl9DMdtadM5lHZ7R202BntktO5yLGrPCHOtN1qZwZa0D4c4Rusg4WsUMe1OQsxnSY49pzJz9hnjFmXEBlsaKmE58fGpvk0yV2PFZGFnPxlbZysfj/yBf5SMgdtaHH7dhL9+dRNXLEQf82nF5tEt/vfr77YfTzc3htcT39+oYxEKwdtynptvVJ3gmSW9RpkXYTZUEt2HdnsWAR1mhjUUIzbJq1aJypvCKGhrWwIpFN28V8k8bQJ+ewy2G91EaM6IsRYl5+J6hNldZkkic3Nt7Prrtsh6C9EIEOOtl9MOYT3gUgS26qkTmUU7a0h/drnQ5lbkTrHYIC4IyAIgu+X9cVF92J39fL2r0n5L6eqELnnSFgbXlUersnLKUS0faQqQx552Zg28wA679rgSirlFMl4Fg5VlI9AV2ta8TC3ITNrQt3iJpgymeMoYg9WJG0bkruurWhQneck0nLc+fCSi2IyMdjj1q81NltWY2zumygn5WihBjS/vCd5nNW6xatozKduIqMACycsWLJnC08uq+Onm+391dwIPzw2xLRZT/2AfIYUQ5GkRm1RvmShbf9DXQIdscnqe1oisN7qb1CbeNd6r4+IB9tcI+6qqbeYvQk3DIFtpMhiyfsp7yDqPjchCXBqbCJ2ZKe5me1nQwsInWYXF3gDOju+TlKuKUmR9RJUF5JJMCQGtyTipHYYCC+AX8JWe5+n8zhHQNwREFJ0WZ9fwvbXYSfY5g7Iy1+/cdaAoy5rB4rREGFcjhDARNi3dOsJjKw9aI1UByMAg9jLEkNJcaomMRITgtU+t1f3dFDUCI+5xeqVTaQ9IgaZoUZPlO8x1schqdPQ4W4I0gJldqcpBNCPTvlExZeEC3UBomO0az8Ex02BKtphM84HELVLQiHIbPGqHB4lARJLJ7lPUZloOEOjGmCXmyFPNzqB89Pu5u3qjn78TiqMKzW1t7hhIIJAgzLtKdDMP6pXj3BADFGvZHqn1UEkYv/kWlQjQ15saznLww9VjaqJqKseyRYGoOMzbWqTJJdUnY4OJdvKzzr0BYOxKkH6PKs1J8MpJYHkKtpM0KkNyTIRNssi+T8dB94QIlT1YW2kwghsrR3FoCRTvtxXO9NivVWERVXUCYc7wtHd02V9ftEZm+2gqx+395PGVtof/lEs6+yEeMYoH+hStbYFvCkQ9GVdCwiNYXk7ApMVY63K8pC4EKr2ArEupVXr8hVz4R7w5KfWL8KBihLIfzdmYoqeOiaNoB1psr5fpPp1xAS+lvRP8mlJZckX28LVQb92Pvqo/eem5ivZq4r82VhOidlO/bEJiI572bPLrLze/fbp6tn+9nDYPjw8nqggjiXvWpNzVTtMp3Nkj/SoowOWDISz/EyY7TEd6poVSq2JUNCoiv/1ZJ1aecCqIxpG5KZJctD5Oh2BI/W0AaDsBBuYrdsmJenHvIuOaZQpYrPEcdY+AeTarJlKrNe8vqtWj8bk1T+XeDE2G+rqUAuDuvuzd6Edk64tlfqo91mEiYg7ISKFcXxxgZy23364/oLEfWdbST9899JOVqBcSD/0IxJfO5QsEpaOhZX9VZ7GNvOyn7+DGstqDIiF7NiXegXYnCQxKrnitSv1eCoIJGJWHCWT+6xAEz2owQ9XqnMPKUi78hdStFHFacqzBokfz9S5J4i+ehwva2D7IhQG1BdFA26rn49kjTDW6PnufvPwbnPvTkBoB/z44jLQMJlX337cP0w6+9aCTDt+iAqthfYItcV+3srqyxCjaHdCnWWr6jrYQYzhd9Uam8fmvI5awHJCnChTgeQS2iKK2oeFdEqTeUcqIMedbQdBd1IYLE6XqawSzmreEi7q0OdF5tHrnFVQE5IoU6C348201Dr11NRwpaBE4VaBx3cd8dE2sTpETcc7W7QzRqc98T4mHG7CEbQfrmcXDcX99o/d/urF5s/9VoeNwez50/8xVcYCajOaPOwm9BHlKCEZNJV7MKsSIdZfD7Nzj4GLEZGBONDZBcVaPOsaSjoy22FFliE0ERrOBQXHFWy7EtKr3gEx8aGGmLaT8D7Nn7b30lRUfZxdWYxWdWonHVdUkjpqg+o4ADu/KsDExzo2owUMZgULnHzW7w8rJu6ESVBv8fjq0mD+fvXjfnu43k1K+/LpC9sb7+sxWjvUqP619kfW/6K1Y8XYJKEep4bM0kVEAVHHsqlmXXgmfJ2OVNtJukjZtQNwCXNYIgpYjxJgIFfIyGTbvGPoTNXZcVjTZG25bKZrU0kW/9GdLUr8x3vbsc2kjVQEPnUrmi8lz+E5kMDjHllNp8h+2ny637oTJhmXeXxxefR3L5H2y/b+4f+OnKazrUjs01YutUC7Z4/H63RSpZ+dajPZIZ+FKlFvx7bitIuhw2VUdi7UtNVLGqqLzaMcq7pl4fEaMpoyECbG5JuoJopv9S4I/jEP2KGwQKlQIitpaGZTLa/5SgcS1L0QQjo4o5O387Sr9WKgXR0xItrsxJtOgLfTwR4C8YiGm2gEOwuspxft2A+Kvf3VT5uHBzW9T61/M3fwZ9M2hg6JxfU4HJa/GmQEiZQj1jp762NXbZ7ggHVoJ1v9HNrMYDpxDO0fFNV+PCyMot1agwzAqnYw1IrWYSgqiboyXZCR5rCFGsFNFOh0pkL9WGv5qi1H+oEYhVdbwHgg9YR0eLorbsn11qNVTx3Vk1YCpG4n3WX5dUyDguyPkA1tZK54uCMe6mKvzy/Y7ePm6ufN290fm9u73WZGmhrZVVuO3Iro1ZYuY2s6aHts1aucygYC0HywM1EIPKhIncVkfezyeKSxWi/BS5dX23Ga5xBwOfJT64Mdp9Y2i53DKCbWBMbj8XF+Zbj1U35m7RKIKlHO0c5xSlVqVezexc7a6SxVsY66DpBV2YMoK2j/U8Zpe4llAw53BgdMtzy+/Nf/A9McT2YXNAAA',
    'ngs-receiving': 'H4sIAP6Lk2oC/3WdW3vbxrX37/en4N2+QfFgBnO89CFN6kOSbXs3bW/yMBJrsZbFvJSU1P307/+3BgABxVtSaICiyLVm1uG/TpP7w/7+dNfd2z8/P3z59dD9fjh86n693X85nH++Pt5z9fPd/vNhfu7X0/3x4ag/ejjsP/+8/+WXc7f/7ePPV4/3NzzL9f3h1/15/zDfHu8eDnfXh+uf98fzz1/25+v77tfD+epw9/Dz/c3+fPj59M+vveZ8uDr8ypvcdw/788fDw313tX+4uvl5+uP9x0O3vPLnh9Pj1c316Xe9ms/8sr+yfw///vVw9aD3nZ/QvyL69Nth+lUjc+Lt4/3x/ufj9Xz7z+P5/mHD/O3+yRP/OpzvD1qgx8+/HM7zk/c3p3N72X/5wcfu3TffdkP3dn/+7Xi3e3v8fN/99K57+c33Xe6LS8HpNfaYutBHX1KtpQbn9E9X+ppSST6HONQyhM4NfQghD6mE4GvK3Zi76Lrs+xjq4Goacgmj60bv+6FzXeqji26cf4KeGIJPQ6w5pUHv2+kNs4++ulji6MfqumH40zCMRa+YiO6MaFe7tz1Xa64+nA+7D49Xn8S9mHrzV/GUXMljTKN3hXcb++Rd9S6XNDqX4GAcYhhDLqKj5M7XfiiuhCGLn+JrF3NXfZdcX2OK+kO9yyjWUxXtXexi70MtPsTkwiCe9MSgdx+CPjUkr1Ubej96LUbxsYZBXM0siSoo7iaKXfehb5drlt48Ptwczrvnj+frwx1cvfjuL2Ir+Cg2xiEnPTptVRJRMcSc9F9MeoVPoYzLV1f7PI4hJKfN89n7LuQuDV0u/bj+6sSj+PJ8hNjIfgzaoRj1iz5UN9ShjCENLkTxVb2WyMUhin8tWuNLKzfGiexuItuJj75dr3l7dTpf7+92z66l2idj7u1fkEOfa8mD8/Y4dr4vKfuqpRsGbWTtXOjDIEEqesEYXGHTRJu2ILthdJLDIAa0dWMviY5Re8Wnigdt99CNkoJYqi8xu2CP+gi9RLI6ICwBViQnKQ5Oj3WE60UOa5jo7ma6x+5VP11vubu/2b00IyDO/vL9SzhzEp8y/Xh9bCpDHLTG0h4X9YI8SuZiaN8Sz9iXMkoxaghjdpIuaWHRJvV1WH13MUpsu8Ab5uyDG3MdtRSxQxO1dFl8Spq1dkPvxqCNrto2rw8PF8aykfy47xrRXmzZ1VbD9r8d73ffPcpEmpK9evY3kS0WctSyjq5EiXmR+UiwI4lPy1fmhdpFSWAUNa5qa0snSUq+9+sv7WcxexElc+uNkpznMekvh1JTrAl7kaXQQ9We5aSX1kUIJRaN2G4i1nkpWLvebtON7OBBtvx4gKHv//7K6JSZiK49eHEmXZKFkiksIimj9tI7D49Jay1B0XrHKsMgHQraodJrQbRxpkyypElqOX8XGBvZZ2mNTMoQuz8NCLksprYlaNXyOO1Lll0xGruZRsRtul7z8bJ/1e/enk7nw2wlkgz6SshFmjRLyyTL4RC42DknRZL6uD6hXGHE5mXfxaErMm2lL36UMZb5l12RvUhamC7ZLujPQu/8UCRDPgQnU4/SDLU6Ua8NHqvEr/EQis/dy8Pd3fH+QfrSiPSi2K7WTLy4OUu6frjd/9b24geYGGRxLwZKqir3I21xTvteKkykUXvisjaHbR+r+MxxlP8YvE9F5kfCJKOYRPHs3njUn6ZRHFWtzKD9xS5ELb3MGnYu2bbI5rvVM9OuyDU1YrtGrITrRW+XGy97vLrZH253Px4fHj7v72Y7kPqsXQheFkuamXP7+FE+KWDQRglVLRKO+Uf2SUubwyjZ8XmQx+2KOHLmaYfslx89UWGIdywBsQ3e+XF0uFdRjXvV6+TE9WSVyUsTP0k+d6a2m6nV+7/tp5s1V9/8JqP9zd3H8/5z9+Ebww5wpNUZixyMHiMSnzOO18kaFBTX6/MkKtKTmLVr8i+y5XKb0vdQJR+SObl4WTb9NqDlIl2KoTecoIP2M+GFUDUZaz7GZXZIG5OXZ1KaIcNYZMehtZto1V3fLrcid/x8EG54efxkMvdhYkf6jWxXuSAsW8G5ZwmiPkaupfTaveKG+QdLnUKSw5Lb8Pp0jIDwjESOXZ1/qjzTaJY6IANr2xixOVosAR99spZCnHk9MUpbpb5+qBcHq3WZyO6MbK3Di56rNWN/k/ETcnhzEFp9aEbh2TtYAwdII6OQnJRWHgJJcEmwRiKFtojZEM26eeCRNkSyF+RoJFGy1qGLwhsFg2/qg04GeaiVndbT2j0pjIRAQt4USZsUo8OF5zCjBVkJyWkjtZtJFTt/66ebNUfvjlefvux+POzP9/vbWzh6/2cYchdMKrX0khS5OhlTwciqnXBjjxuS/EsgZb47V3oxWmSS0Qr5DKk+6EBqJ4n0RQuh38pKe9yQDHjvsGrLFzZIVrGMMQqRaO1gLyMZhkJk0dMM8mQOU6O7W+h23bt+vlmz99P+7r9f7m8Pu3enX453EyL6/u/fGova/ousyNEmyYlWtADHksRR9mTtKD3IF/Pi4bJhWAFU0y+33iknLDcJpDQWxRValX2QXZKgjwAGxMWByoVnk1sQuUPqFDLdHu67hWI991M/322Aw+F8/rJ7e/Vm/3g+Gmc/PXsvzpKke2PdZQZBesWAjWE9KeH6FXpJldGQR9Kfenm1ApCVJG6BrEDpJJs5h4tB4YlUwXli30lq5WixIMk2S35lqONsFeW0ayO8WwgXgx/6+W7N4Oub/e3xdvf+Zv/paMjo+f/+ufGXmvlQBCXII5mpGOaoHZyNoey54HmNFfiOMa7CFb7j8ak5VPhlu5W1TsK1F5EEKgmseAFIfZTkz4Ecs0D8oPeUrC6eSway0dpNtAqdv+7b9Rbqnb7s/nze3326bRvWDH0CiEsfQtFCwckwCO4JTw9CrkRUgFphTHMEgVcIcnovXO4APw4br48MoD4xONQJUoyC2XNAY+DCXCIOMUuVQeS+ZmFz3km+Py8ahqZCa7fQ6gil5rsNRjoohDyehMofPyp6Nv36hqi0LlhPD+JJrBE1CCjIM0btjiK3dfRH2FiE0wgWBD4VVyoGS1iQ9oIWT+q5kE0IQcj2Yln2oYrgJJMiA6S3RIgzUaKiDwXbiU0Ukp33q2BeXh4+Q3c30z0KO03Xf0RP356uf28b9uG58ZaHslawQvAr8yXtGiqYvEjUehmHJIxnyEoahQI5vJd8TZlRoPYyDQZo9bYy8Jdv9k8cxiLf5IjgzWYWXHVwVas2q5RUV+bvdN1NVAqVvujb9Waj9ue7w+0tePbu8AVenn14w6cSjcXpC72SpXZoLUT6EWvh8DnTl6BtUNyu1yj6k7MaZS08qhVCH9ZfemI0mCFPVibXh9HVp0gGnJR4nPyxVzg1PTNLoELvOlPcTRS7hm51ubERh7vrs5zB7vnpUS9fPFgcwmqd2SOLalwJiq490iYHt/imikfTSgv1jEURVwmWd5EDq9pc0TVITwSHFRU6819gDQGT+Yf4V1ZOHwomqXgGqaw+2sv2yjkvtkIqmReau4lmfdrrvl2vWft2fz7LX+9+Ot4unusVcE0OV+pvgmEh4hCz9FhmyRiQ5JFBInEhjy1HLgPbD6JG26oFGbDvcs6VBAyrrjdTgBl4usYp5C2xho1zLiAbfU4obLxBRAH4mEwzFXEvPkyeYaG8myiP3bd9u1yz9/x8uFPssnv/ILPy8QDuJfIVf2lADmUUxQX5AqkXeS3Zuii3LEejAD8JPQ2km7AR4Nw+bbFfaHkJWXu/yTIpzo+C/Y58mDYsNiFk02TZ5QUHoq4pfhc0m6jsZioFxJ/3082am3/sZdlvT78fzmYDnz9Dt0IMYc48RHYqkgITG5ZjqRhBfbRMuzzYSExMGCj5k0RGMaGYRJsn/JjZgPlVCv7kEZybcmVyyopj9HZJwscTsm+Yd8JIxE5GQ0G0F5TTZytunG28fFgDiXfdTHfo/tFP1xuMePp82P1w/Xj3n1UcrMD2EsZatqdoQeVItH4Khw1k5A0MEb4X+djmCpCVfeiqpE0R8epLUmnRSTLwvknI6A0tD4L5I/9CtiyD0AQb9bR04QIQqxHdTUQr2njXt+ttwvb2+Gn30/7+5nj38WHOlj2DOU9Cb/pGt1MUesKpKOCI0Ea83/wqj2ZOQAZVClkw9sF8ciaXsnbBo8uzVMZZInFsYs6wJ4BGOhvNeSm4M1CQSXYu+6ZtbZR3K8qTwsrL7ZrJH/fnTwpZtlxOekasOGsGMFGrLIsVZP9cDIY6hgUmKVCU4Rc6H/ECSbiqkjmTa2e/VrgryJxlk035CYnrKnM2KsJTjCcJT7hECQlZD6ewJ3i947AkzjAA5nkfTr+S81xRL0Pz4//B6qv9J6Hk3dvDl0kJJzZXvtq8dWY5QarOKQTEXGoNLvutTRJyHMh6Cp6TpM+kdQYSUFqZ5dt3sh9T7lM+ZA2oRyFHKV0SJ1EROaIqlCqLqaBJklrSzGnQHk90dxPdLRf15YkOttU47u+0kw+TN/gWLKLFXYuqAPoQZEC1yINW0WH7FR2ujJAnxyQtBfAngmJtjJ4W2E/j6lvb2vSQxO4l4eUJBknlB9yhhNGRtdZd1QaSKJVHWOynk09ZCO8mwqvQSbvc2Bh29Bqof3M4XJsX/+aZsTdsspjSJ7ksBcaevADppl7WfExTJSFgZiyjIHxOsQE/R61BTC8YkpqEODfUNfYSf1l6baBUPGTL7xD3OUtN6A1xD3rrQUoocZGgLwkrLedEdzfTLRLf9dPNmr33+8+7N/sfT+eHfcvvfIA3BZyywtElR8AsXkMWZwHwI0MoG0HmY9QuSouC5Xckl4qvJEERzCFPq6cLybrLC6V3pS4I2Ut2E5ZYZiT7QYvg8QkVjfMUBRJ1ootPiJDazaRqod73080W9P/1dPew373/fHy4YbN+NJ8AHJQllkcSpjNcTInIC64oXKbIIOmkWKVIWFvmZQ6JTb0cQqJ+IlmUyxPaMk0TTpOZl7fDFIljQybSNAE0IfrBHi2sUBAvRrRicgXsllwSaaBK+Ak+mhClINNMd9foTgKUdvWkivf/Ho/3h93z8+l3U7PXL2BNDmGlA1gyYfCWRwotY6ptXLJSlubRVgHfh+AH4HC15FXqvUuXH9nQ2ViOMktkr9tmYj3lLIX05Va0ppaWI/CQkZWhFbwJbomp9T4z3V2jO8oh2NWatx9u95+P14fdP/ZXVzeH/eP9xZ3LEo3BZEJ2uKAFMugyXdg5FyhVkTjQ4uIySvVGpAAjXs4CGL16UihZvrBKHZOolwsjknZ6J0vCJVkfrYpAjlZ1wcYRLzfR2F1oVDzzQ7/cbn3bg7bpy/7u4RJLUwNY7D3INQxAn0CCVkhd+DG7zTpXWa9KQUqBaSbfS9xZsYcb5ROOt2hGjtotSiv9guEscJ0UneMDye7o84W9iDy1u+OcYlQAD8UN+DeqJeo/9u36qTE8HHbvjleHiwC6bdQoWRC4ZDvkcy0bHLal5iJ1lIUXTBmpkFo6TkCDjNx6eyTQJn/SOCHOVUCUcfaygXycFhW+Eqo2aBslh/5iM/JEcGcEB1lALjYMiUPQsSKck4WeLw0eyyJnWfMWYCF0ti/UWNkSWMgCVKuaovZK654j+daRWpUsIJVZ14dNVUZBt5mL2KIUlEsmRxaAaJw3Ei9Raynk4cmy1pkZLZ0R2y3EAiX7+W6LNv5N8IL9+NP3/zp+/GV/8VqSwRQIB+3RYmrFSlQInDTHrIUHL9oTpZDZlg1HQLWeA+GLIyRN2jGSQfKp+NeRioJr5Qg3mCRq64V2Y028KwAZSJFlGpKZ+UwUJzcpAxKGS+FfHrOR323IF7R61a+f+Urq+ye5gpsvi1RKGLc56iDYIx+jRY2E8LL4WmUyMlkagQN3PWVRxeDOEaNYWVl4WNI24voU4+lfIZLRT+VKmXNpUyWDr0cCHpwkYVLA4mJOgqRSgbeBTLfspcDonAWfqHbd3/p2uXVmh19OO7m9x8PtJYlK4k0GXJuCyKFvAUMo9FspAynM8mswaBlxLWyU6gs3OEs2VgK3FbKU+dF/pm6gbyEI3mcEU4csKxPBa4pv9Q4EIFH7NHNDbNB9+HJ/83jXTaSSD2mXa26e3R6udj8eaaG51MZ8fgKBB8KPzOcEWhtcNQivDQnCVxg7sVcqQLmCZOlsKFInYmc/74bDLY9TqtGX1tPQspiWwhTaVeSiWCaQE6d+mDEtwzgOS55HjjoYyd1Esjb0Wd+uN4nTL7fUXU5XnyytYI0osCXTrj2pGDpsvaE72mhINgKAoo+bTZLeS8RGSjNypJHUVbSq7BbYa/On1JXMhqVYaD+ReywKCPSB3Z/0ewUx9fJM48d7EqUQ283EWnZ7utmU/D4fPu1333z85fHTfsksyv0WGCGol0FQcI2xRY0EBKrJRB3IJEgNZIhlQ2SctcpyV3qRvECVb02y1sKqkbeSAE/RdCALH4dsj7hl+WRhLnJH+jQsokJr7a0nbVnyUvUHmxqt3USrp+Znl5sdolb+HWxa0UIgl8YTcrJ13ERGwGaKyNlZq4ToKJcyfja/NeSBoFe6TJKdpCnIsF8rkkymcQZOKmFcpyIEnATLBKKBHYRaYEJr3LEE9FgX6Yt0b3y4OX3e33cX0gsbttxuseHVJ/F4e3u8uyS6HaTOuQBqfwlYrw+kfurYOrmydaqg6k+0dZTzUwj/Z6pA3tcAlUd716EL/SckaJNiWD1YpcwTakqsZXuFvuZcY5SkQHM30yy88bafbra+TCJLaHl9fXu4pEG0inmUfMukkXojcpaoC7h5Ks8mlvpgSaO8l7DBgGYl2WbqmTQIJMpg1RqEkrU2eGclThCTtMm8mrcSunBMDtRvMAnj7Kj0puNEWzfRJm161bfrbRb4cEfb0+1t6+h68wzvhNOVvdbbkBzSuumt5TgUP6UYgYLaJO2NCBEypbeEIiAGUDJbB09o1dGqItA+buCxTNkUjkgJQ1l+bImss0j+iOQUgb+jk0LbKhtcZ3MuJFonortGtDh93dvlNkd6stS2peekU2b2XNhkOiFCaB01khpQ4RLc0PsPhHaKzD3FWSCu1Et6PbZ6c5FKgRlb2tCRZVXYNVingzRIqG7eq4jJEAYhs2staoG+p0R1gK6apeg8lkZuN5ErN/C8f/6HbOKL0+lX2fLXj7/+egFMThB7nSYTklHYONTBzHSgN4AYaAZ4I70BoxUsqF0RioiQPIBu16VY4Yc6TtUVV8i9FKuWg7gELMSkRHZQ6OnMJ0owogNCuaVXaASjNJI7I1nA60XP1Yanw0EoQhH+518u6JZ9R2NbbsKKpoRtoOtEoybaY+1Jwr88krrQ5gR6IIQKAX/knfJXemyGnCfbp6hja14r5U6STyW41p0iL1zBmhQz07gUWPSBLw7X+zOpGSO86Imeq63Jkw3cfZAOns4IYKtAuKHJhtBDojVTxrdQ3lI0XuYqpaUQHe1OshHJ8mEjOV8CxtyvTB3NOqmav9U7jpucr9wFrSw0JilOkESB9ipFAUHdXKxjdmmRTI3abqK2EAO36w1A+ny6+9O7/e79Q3+J8VtCZtD6aAOsotty2nHQWyciyCT4rP0hjpw1ZsTw0TVKgxd1urZd1FGTfJMYj9pLbYY0Tk7VIIUh2mWFeDQcL/tgLEYS49YeEDC9gfixLB1FbN9Efnch3yDTcrtm9a8yia8O//ynNLBlDFsjjtxrwk/hIEjIymFJUkjOePG+9Bs57TMZUo/88Be1i8Clpylt81POCmDyP0sfDPZWflqhYsW+WrlZBlgWSUsJe0vJSCIdofXq5njsLvTq93/tl9utu/p8QCx/OsqT7ed2a9vBuITsEg6ye2IH4EnhZRwtHskk96ae626UhJFOSoIcnvSXVdI94X/2q45rEsDVjCMtgG4l5VhL/XEaKcrpYxNtAjJdIDipv7t0CYSF8m6hnJBrvtmw+PjqcQou31/dPN5PraEWdA3WTVLbQwu6gPOKHRMBNFEvD2Qxs3gZLBVtTZSReHLb0zGGODWDieQ1BtYTMiXekqCK5ugLxTJGrCjIKi+2ZCT0s37KJwTXJZ6cn9pWzK5udt+cH/6DXWlB10Dhq+TpEZOpPQilFME39A2c4YdVekcyKidAZ6w1mURrefNUbdelaJ4OU2WTnCIFnRKyOWRZsNFya/KecXlmSdATfBul+/OXzojVBv+j52qT3xVvAlUfd+9vmI24XgC9UETYODYvgEZiBdeQ6f/Hri2JFyIUKn+InafJkxRbtDQAbVZDbD1XVKfdpHXyL+t8zkjPnKgOtB6UisP2vJMMjJjBGs+hCjo+093NdDsyvtPNJtUmuL/7fn/1uG/w6p3xJt8rTcapFWcldiEdsUX+qZJSop9Fr2BdHXGndYOxV/o1a0xs4Rq41x8VwmupkZ7PLrYkR6RMP5CtHi3xaKFmjJZ2oHVe2iprKfhYC65T27rkpWSOobprVDvf/djb5Zqt1ru8++5wp+3FvYHoKTm3pI3EZoQVgT+6OqrggiIMYVyZsQIGkaUDEvqKL6IObaUFOeGIQrbSV5fxBmY5gATrfiqZEjpTtPt4ZysTUZMLdEgU8nmzhonTuee60aq9+663y63V+B2TL1WcYrDvf8Ap1yd4MUmlKDEIFCTL9FG1tU4/LGM1y69VViyqP6CoTFpeRjHTEXWZQREsmyoNAEbJ32AR2xDpzpY1zdbcPcpbZmsIKxQOcZdRKGy2/UOYyO5msgsVsOnmaRna+ox+uZ+qX2INyU9zpwexEl25Ei4yscE6pxTeXl6CCFo+WBYFmJQI/0v8Y1eONm+qfnm/QST0RqCJ2YJM+CDPGqhqa8cFA5byUKFNEZq7RrNE411vl9u2nNvbPR1Gh+vD+YE9o5YCZxKtQQpMD4lVh1AsfbD0gF5IAV0b6EntwRpYKrBfTAhJUPaRNJJc60EX4/QAIp6ASOjNSqwFQ1hfTkSIg3qe9Q4IHLONAy31YTb4tE9MhHcz4UKPL/vp5mv92pcmlmfvGn9SjoFuiGBTC73edHSFrjHqlEBHGTraPKr8MkGmoyuloBYe400y29G+7PopbTUlryTLaW7YHi6o2Xp0PAkqxlBoTwNpieFsKHaQEVmCGSzM0rs9US5xf/uVLhYtA00sr493V/vjNftHk6L4S3kpiY+trDgUWvl50qrOwKWlEmH4S9wpNCHHQ3MIGV0pZ+25HadIvC4J+8R8TSEdb4/e0LeQFZi1pDLSgiQMKpw1OBrglsSojPI4kd3NZBdqYdPNpmJ0Pt1dH3bf7q/vGToiAlVkTRqw9TonhkawlS4ZhSNIX3654hxWOQtrOWXsAxgVJEhUWCqJ+8FCvywZxjUlSxm04pfbZpajJFFwteIlDGSNVHil2B6XvTRys3KN6m6mWq/+oZ9uviaabxnqm6JrsWY9/mAKz6RIsRKWtIJG+RzIbWt9ZSnHVjkfyNyIZF9mGKXtWign8aYlqNr3TGKcHkS6gZ15EFLgS7KDjM4ic40kLdzb3i7/mAv9iRYr2xFSvCKbTQ5kZmhtwF5g4UdmtbBn5Atr8asAGqYIdgY65cjC02rkic225f7iWq6DBqswXFpnbdAQSCI7P1KlsQJRJcGSadCKqz3xZUqKTlTLS3/o2/VGlyjOfvntaND+6tPKzBMcLSkNGiGphgAjZOyT2QoJVpm7AEyX5HwTY4e1kucdWpqt95uum3H0U44tuKcZcnpQExCANiTiaBlj/bG9zi9tVMzkrOjuGt2OblK7fNKfctj9+XD++Dh5Z/IEkg8yX+sQXhFMxcQKs/nmS6NrHle2ylpnadSWfsnS6fmEmcc9l6EfV7GNhHFANIv10q2cRbKwmrE+gUbBJOr/PdnYSJlBTpq5tYk9qszQ3S10FzJw890WfNw/HJ8EnOSAxWEoMQ3Td4vyx4FO/zCCCKz11zBUm420uHpkXIQwIzJmRAAW8F0yA2HJ45BlDMV2UDubLj6AIZdeIKcKT2a9rzfpFOBPhSqnrKkfFgRC412jfR18FkDI14JPQeZ/SkBfHj9+nJPA4lArRztApMvUyh+CP4DD0Br7Ss8w2BpiaROj7J7kTiaUQeAWd5an8Zms9zzdQuJ1yXS3bsJgNQLJSWHLtNBsMNJETDG7azp5J7K7RrZue7va2JUbwas1dvz7t2a96FaURjMaYHWdRNNvspEThna0o+UygAkMoRxJgTxTPbagM9AjsBrtGeZBkNiyH+7yZZU1YhWqTIqwLX2vzQZ2UnnL+Mq5wieHDdXXDL8t8BHb8hX4+OJ0e9i9/nwwjPXCMJa2Z1x+rM0PyyxfJI+KXBZGPy+t3rg1BnokMvLtip+q2ZXS2u7HkCdJFNnbKSQ5ALofGERs/VI0glkNNCHIdZkYq43MzsiURLzoudpUjW6PV7tnX06f93en85zSgZMletKykNwhUqky8I4yt+UaaQtLEriYrRlTC5xhAwSWOikXwRVBf3uJgg7AootzolumXqvUHqxtSqCQTKZEwbcaiwe9Seo9AadfjH9uVHcL1bH7pp9vnoyNfTz953j4vPvh0+nu0+8ndmrib1jaEMc2/CFfJDhIzF9N2cdpINlZg6bwBzXVFLz+gGkzugQqrlqKIZNXqRDC39J8H6yRfuW2mUWtFvtV0KATx6RSCg05eZFBrUK9EN7NhNveTTeb0fP99fXu7dUL/aL14Dc4pRhFKuxkgLV7ZiAVogXm0xRrZOsTZu7DkykDP1GpwLERYjLCEamQySDa2O/0IiuLCiTPwyAZ9Dh/s6MZ4Yt0WA3FhltoK5PFpyx/GamnvaB7dnd9PvzeLYTrqTf9fLdJrl6f7vbH3dvjw9XNoc2TkTGOVuRL8w8eXERrpeXp6EBjkArzPM4/4hDEwpgV20EuWQqaGSpgAHr5EWNxnFx4HBcfY42PZFZEvU0DW7+KrHTGtVIvVDSaLo0CM+HdQjhM9/PdBqOc8eJvBDCnaObDG2PvMkMRLblKzk02RJaACoI8XC7mXufpeHkywS5hwIHpcrIGDX4NT4bJqqtTMFMVHCz23wbWhGRQgGizusCvYMcj2HhPWWpNcqqlkd1NZEehk3a52TsquEtGvEWhlHxWUbFZkRH0JK0bgyX4RemAH8IWjORThYkUbw1UPog/MT429iLaLADK8vxh7h2wyCjYjJpgPLa34A+QRqGXyICcI7ZeWt0EU5+dH24ez1OjmzzGsz92ur3afxHY330nHzfVar/74X/hZ7wE1cXsJKO0dJQygGL5YVLYbsoY2FzjwBi8QBpJoIyhZNaI0anlZQV3Fud8wVgv6VmDczhgwWG5UEXnNjrBARMUuCh4xlWD2Ex4NxNeyA9PN1v+/vut4PPuxc3+vjXlG9DKsnar2KM1tyb7fIJFKp5+XJ0WwsCBMxTbCipk5IRDipVoLvrKogt+eQOT9L4X63sTcpHPV1RBlZ0WDxkSfa7WS/47LpUmYfSZ3q7RC1N2tZ1mfNgfz6e9zOPb4+1tG+1mvBa2VhphRzyY3acLnaCHXQMcWxGXzLtZCUmuYoWRcVIrDVLIjbmfoQytsXVGV5YrcGXb4UnmO2FGZRG9QWR6bytzr4qxfblEN2EhvluIDzbT2G6ewJBfvuzoyj/OQOvNxOQFAXtru8aBE+QwVkBITF/ByoTQXg+c9MhVorg2mptL/apljlDbemeLjbxa04yzGWzy5njpHgypOMAzNBTzBYxILY3Y7kJsYbRsuX0yXXZ8vN+9v91/ebhM2cLVJjcju09iU3sjlSLLg7loGR57BBd7p2eYPpeSRsawEpOnfV7XBro4zsdZAPD9/MMyibeRgoGhffJzwv+UpcjXWWZ3MiWU5RrZ3Uy2I4k13Xy9+/718fxpZU6exqWR+oF0SGtMWJWxBuUyKGJ1bNmSlIC3ZFB9saIaZwtcqoJap7FO3aYuGbzRkgkhgKw5fYGOD3KQ8zN59mWBeeNLy71RK9kRjtTVtmFCMQxYczpDoSWtaFNoM2Mh2Bw7MUwB4NVgpz44kk6R1jXGlgJdzjHDUfHJcv5yNUyWcZrMqvUiLOUXga9tWkwaFElUyItNzcHCLRXFTdbpv8AQBWwQ3c1EM2E7XW8y+vvz6fZ69+f93d3RtOvFGwK0xIFCziIyGW078UL2IcpsYK3oFMWGKaoBltg8IAgZKa0wWxNlJTk6DCPBFvUZ0WcJ1nleyTF/fJmTHMlhyQ7KWKRoE4E2VGAdhRKPxX7oQyequ4lqad53fbve6thvx+vd9/86fXpcMVZWM6NmG6n/KE6isdTG3bi51MzIGwr/mm5IZmvanq5Sx6lxVsHsJevt7OAqb7Vgmvwtoa+/1c47mh5loC8nRMRGaNcI5ba3y63/erTaNCy2MPo98sf5SqvDUTyH+5AZY8SL4VAa0NO4Blqy/JDFMTHaTVoUGSpLrnfrLwnkfCjJSKXwwpfWS4JABxAFI+uDY0YV3D+Q/FmaZiX2tXsOwQo1Z7qFEskStLuvnOnz+nB7ZYONr18Yd34bv5MNJaiQ/c3RzuPAKZfpx7fqpmdEM1Jar5ajo/1oYC5m+Qla5Dkn7KyLeZrawjyiSRGLakd62Gkl/I5iTFyKZcxTzkf7NJqtCc4uN9tmJw68OjxeW/AySaDecNV77TlujDfk8JSxZecqlXY77Yo5kCHQe0rqlJBFYfPSA86QrbCVnyYJDC+NNsormbNTi8baZk+FxuZnSi5LI4EU1GjsGo1WSeJqY/q+3B7s+BjrqGr4HWLnY7nof7VDa0gESMll76i2pNnqMYbZTo/hw6kokIcopU1qcubD2obWJc9Rp4Zf4mZSIMLQ7bCVQnfu9ESql26P3GjtGq1F9s6uNo5pf33Y/fDw0IDEB3KlkSTsBZiyfDTmSVW9YJslXGjALMCKzBBe6W1UP+PyqYJgwIudg1PMdjnadum8znNLtjxnG0S1wSnekeCZE3GQKrMNjJwxGUzOP9bLQFg1krtGsnUc2eXWNtCL+P3+eNsyHC2HGKNbTTlZvcjhNlKxvnFL+aalQC5PggGnfkQEmSmlequkx9DnlbxpPUK45NdaPYPSrR3DEIjIiw280LDFARGOLoFKRaws8VV1jehuIhp82y438RVB9HH3l9P98bd2UMKLmbPtBJ+1c8RaMw3lBP+c1baMURrcjV7wleOBGKa0kwWkT6kv68Kz8GCcFSk8if0j0cwYGVASPm4xiZALnTAyqwI1S34b1NYo72bKC7HidLNtVgQhTf200/hlNFJlaYQOUrLpwcQRLxQjxUw7XiBfuqG0s8wWmzP2ci6M0hYFwUJJfbZzDNsjRnCe4FOQ2gr2oTDQQT+9PtKOt4h05ybxlawZc473x0br3Pubu+d9u9zq1i3nOT5rqWFC4r98MH4ck4c+k3nKNuQmSMvQE9DGfBRB66rpvMiuQzYJJDu60VnTuc3LLd+0dPip9iVvRSV5GjOyflkacRSQ+DjYiY4c3keoIxkeyoL+SJ1MZHcT2Y5xy3b9lWmO7x4582w+F6dh2uifdOBJCzxj/4F+MTJr2c4d0CbF6cAz6X5MKIqwYrWqWMYFc3ijqLNTgxj0mPu1ZVs3wzoknhwD+dlxih1V2ZBkvbQGHFm4jBpJFss80bGiXCz+rb/cbw+iu7ub2qissm4bKKFvLf1shxgjJzsmm55ZeVW6aKm4y4sK4NDbC7QNf/S93vvpQLrK6RKXJCVP4OrtgEXJpGkZje+h2qB8SfnSQFUardNkYlkmcZ4cxclxPxLdawMVrZAutKfIkal77Y/tV7KTMTksZaRsV9rRmRIvKo4k1jCZdMgLs4+tiO4oxrIzCcQl9ywDpF+0KAT0R0Czjil7FIljVuj7yA02BVlnh/fzNBgux0EEI7ybCRcgJDK2m6fDfH+WmTmcPz9utivE9ekjtPRRYRXcc3Io1nTKMRHzj7MTVKVRVGLkzwWuKRwFmleqXNHyI7uSpsImDTirlI3WgUOgKM/IklYa3+lTGQV9BTHJL0wemgkjkd2tyJZL+7G/3G/LzZ8OOw5Pu5+6wzixsA0RmrLTylR5vZymtiGQw6SYktevqD3X0WqfCfcusOsZH2un4LRuCmH61JSNUwiHdTHCWzEigGZHUuA2jelIMhu72WqdrWpEKyE0d41mmx2wy6+0P9Dfd9s6+ZoVCatxWctEEWuAfIDVdKAzVB0v43/4AVEQaMVg3jW3oqa1ho2r1re59952KYdE71d7wIHjilExDuIMlrExZ+g5ccAvJ/MFfObUADETbg0Q082TdA0HLO7enH473O7vri/FI+vUaJ2EwlWjLR4dipWEfBqmBzC8rGAloiBrmuy4UW/jHuOlMUdPuHmUqoyXzgpiZ9qSBU8lD+AzO5+TEXXSbuI7j6sye5rp7RZ6LWUz32137nd6aF/fnf596Vqxox+WGr+1inlQMRmbwSaqBndpRimW2KYBPNDkOForSLKuFQaIp7kRx5x+mLoIoqH51UQ/p356WxrOz7EeOJmudpgwM/7LaapCenUiujOireOIq+1+PZ4fYHX3/vFhykS9tBKSz34bpnjwu5QrYpy9oSqc2KRG3masUDphEanKYGcPkuuLtZ/0rB0+RRIxT0lEb0ddOZrDhsKZOlRWGEnn/BBnwG5cRtIDp7EsBHcTwXZ8U7teM/Y/j4c7K69T2ZwYa8UjHzftwBgrZtKMNgF863mWz76M7THmwrQyJ45i0piwKuGPpYds+NFGxdymF5opAzvqiimmdgKuAePC/JHngKfLpGmYKe8Wyl33P/18s80AH9YZ+4aO5UwUAQeqNnhp13OgAL2LFDyJLjn3zZuU0Uvl7bRpP5D4Bsm0fFRtGfvV2Dccp+bciMO3sR2foZ1mE0c7L1vKLhxAgpYyQcmXxnVRvf/MwXWKmWfacXBfydl/ezidPxJ52mTZPCcSe3CdoJO8tRttvIvagKfKyMg3o8Dkhjncibk+O54kMagsx8gAWeoq5zFaZoqDjWn+s3878tiW9UD4aSob6XukaCYRJaswWMPMYKjcSc5p5fGu1uUkHatMNbq7me6x+7afrp8kp/Z3D4fds+upIb9ltZl1mI5xaIVpUUe7MKOMOZuHYwTpcoipUGJgh523c4FH2hgdQSkDC2vb1CqbpsltnIGTQqS6DNAMtfm1zTOTW0MDJmK7RqzYfdnb5Ve267VC7luDW5ahYquWKXlrJFeMKAcWa2GeluxaXJ9XZ7vHsZGcISKdDIaNFWCTCl0aljhHwZdp9soOXa2pnfHVOrwppVWOIedcEE4G8pyrKI3g5LSZM61amTdrorpw8la7/spJ9d/tz+fjqikz0DS1PZMzcCKRgGRgN0zXIm1HlMHpDPLOCtOOSb46ZINaTHMn0iVWB7R/tYfz8R2D30AtDh1isjW3sRrrITNNZHDX05h56fWo82H1C90KB972890mxbP/cn8j5/b89Did0/r9N3jWejkHzA7yHu0UDhytvHPhrBxPmLa8olX/hEeosTJiTK7UzhvIm01up8JhnWg3WL6tBm1n2BOXiiMbX5LkcFImbY92PP985FGZye4msms7Fe7Jia3fPt7d7WlZv/0890eIs9U5TVabs67haD2oHKVLmSSu856cZZoTR8gxks3ksOVObQBwjHNkXcfN8LpkU2Cr4jVpT7CuD9lMhlAZpUs1LdNY5BYbpZ1Rym3P1ZqV749XJ4oQt6saLbwoBtHbtUezh5zxktBdZqvI9IBJKF7SpCd8yxi77Yt+OeCg3abj3Tk3n7KtjVCwqqW3ThWLJuh/4Si5wpliHNtX2qF4Wp4lDZLwaZDbzeQ6333fv/jaKK1Q/ofT3cfD/WQ0OK64zmEhBTZmOSQBldlRUhfmw91KNK0LVdCO/x2BkFXmYDiTO1q8L8cY1SldH+308M1xh8SenhMAOZBMbsv6czh5znG25oAdnE08XQfW4TcRXeh8a9dPbManXesDMcY44I5MxeZAAN/bKSqVxv/puPpCH9iCKoDDnvYxcnScs2tnOtvRWnETMASLqmODaasTXex8f0n1QI3Z47r+ZK2ZZMhGO85oOayaZgIju5vJLvwPLqabTWbu1e45Z01aEzADqYH/pUW5pOKpw6HFzNXQX2+YiBawSza/GFhkssAzgzBS1pP45NSvN4aOqjqVizh63KKYxPiMGylFGZQqnExG2c1a+WcDyCQJLRrdRKr26Vnfrjd++LWdvL+//eecsgqMgNpRhwxv2WnO7YSAyAQEklB7Eb0+elQQI9r/KYIDxUm+VQ6zoPuyXr6T3Esb7ZYxGS5VQculUJSl8pI4qsWyZANDGZ7/pwdufsb0gfM3ZPc4Y3amO3DQZ7v+r/8PK1v+PsFmAAA=',
    'ngs-rushing': 'H4sIAP6Lk2oC/3WaW3vbyJGG7/MreJcbGE+fG30py874MOOZ2EpmNzd6KAm2GEukl6Q8q/31+36NA0EnkWQYgChUV1fVV19V4dCvD7ttc6j/XR+fv/XNH33/tfn2sH7u99d3m4POrrfrx36692132Bw3/NGxXz9er29u9k3/+fPmdtNvb5+bb/3+tt8er9fHY//47Xi4/nLsr/vNl/vj9V3/ud/e9ftDs/7+5fq4eeyvj7vrh92h2T8d7ue/GK6e1/u74YOLy3p63D3d3t/t/tgephV9OWwO15u76fLzZn84ni35Yf3DjX+yih61nh5v+v1083C/248f6//3W3977O/+RXg9vd595+PTZ/7jL67ZC2k1fODb7fH8139yxsXm4+ufGtO83PxzvV193N1stjLHx5fNxdXPjW+LSd7EznYp+65rbGiTDzZMBz5hQpeCT7mzyRTXuC7zqdw1sbWhdC7mzpfsrG9yY8wLY3wXgxvkNbO83Lxs5wtrS2m74q0PJaVoGx7SWpO6FI3nnmsM6+oikp0PvhRjuROCTSVZ600OzsSlcu+f9/129fvm4WGzfjxIuZ8vPrJ0b3OJ0WUbU4yNc60vvnNuOIbGtSmGbEKInod7llEa66JrQtt5lErTsbFm1C13YRTXzOKcb96385U1Obeps57tMqyWzYospOMxpXSoiCoxlS5nW5yNLlipG2JAy5IMArwNS+Xerb/vtsf+TL1XF7Kd7VwKORcfTQxW+iVbTMjjEf1yx3fgHHmBHUA369jN0OYUu+yNrUfu2lHBVEqeRJ5U9L55t1QxuJYFW+u6TrsTEwqYrsvWYqOCPsl5bw2mcpjAxaoh+hVn2Irsol1q+PawWd+vflvf3ve3O6n3/hLt2KHQFTscq2cGk9A2JWtslnJoiZeOP6jQNSE5+TTbig/6zPIsv5hsZ1GtymomWdj1bTtd4HxtZwq+Lk140ouQz26E5gV6WKxm5FnFsRo08x0mQNmAu+ZYlpq9Wn/f3K1+YTO/7B77/XO13esr1hhx9IwRfPLWlcaHFk15su0wla++mU1HTBRc12YsFLuGRWC6iGuFiA+lKqkb1Yupi4PAZiGQW+3ysstYLgUjvWLO8ndM42PBZ7Rn6INmbHRQ0MccFXo+R/bdEulEYzjXsN/vN7dfV2/67aDey+qaDjeO2ScMhks0vrSGXQI22CLP1jo21vuOO2xucLbBVVCxRMFKiYjJSLL8suHPBwVdTmGS1wzynEO94RS/FpqxMY6tidpS0xoeFXLwRuFtW1wkEywuBERWp3TWRIcJQDHWslTsr0+A1f1uu3r3dPeVU+l2+fNr9t921kgPEKoTWMQWJ/cFYApewOjaAsBgOc+Sug5betPweVm9gETzpyfEDIr6WWAzCcQ5/9pOF0SB4KyzBJ/tUC91oIyzXVdA7wFEvPEFNywGn3EuVdcMAGDmyjp2e6nf5f1+cyAh3O03X576/5N6v198Emj6xNO8bfkoXmjYMYcpLLuPifhVNIZdwD9cF7Mdjs1kpA6EHp593H27J/WdJPjUXLanywBe2UBUYmsyAvmDaIvJdckQt3i+YKRE4DNYw0dCqSiCr05Cgz9TaP24+vRVOf6mgsiH//4Jbdhq0laOwwFTgHwgw/hPxoqJyM4e33A4ObsOPFppaGIJ879EJE3GwnUQ1u+x1SwQALps5yuiuiWOsbQzAukmkEetyxJhawCgiv+XO+ncQE8369Wbp5sbEn/1vprTcjBKGwkvANsbZ5SGXfRZ6RScRqGMCwAaVs4CcIAmXAhR9QnX+dg5U2ZYBF/iIKyZhOGvl+10EYNCEuyEAwhpmxdE8+KGL4JFVxIpG6QOoK731feSX3wvVbtY7xVYu21fw+qXtx8qaieH1wKqALeScWsEEwVikknS5ABDDHv8L3SKwrj8btyojXd4Un1+MzyftVy0wylMoHVK+S4YU8CaFzYub+DfaMK+eaVBHC1JFJpwMmpGyLmlJh/bd+3qzXr/vR+x/YPIA8ju5q/Go0VOmQh09X9xquy0dbh6EP+BaqGV4RckavgYuKf/F/iARaBP/f7YjMKIw4/teE7IyasFZSwwYY9iljfgdegFnmciCzTEP2JFBzAzanujeMlZML1+XG+f+geRjpEu/vRSHkQSBwocMCOAcV21EQ6Ul5mKp0DsZFKC2E/AkMm+42Ob8bGY+XU7noNRLbvSkWoIMHz2hUvLG6CH3MxC3bwxJCMWMGTfH9jSY39YXe52X2sy+ttflIzweigDmUeEAO8G940wJ6NLDZi4YAz8nuBg90hFLoDYIRFfoITXpo9UwuEYVVRTRQXoUT1B9w7yyOIKtNnxMK6tnNpE65W/yUJDQkoxEcKhxn1HIkGUsYmst1Tng3KrgvNG6rz59W9KIhYnKgQfZsPZ2bcC+YI8ioVXhZIUmhMNixJoizrgw/hZ6XwyiJ9tE8AgibrfUcc0gzj0/9AOp2wf3kNaYeWF6G5SICYM8e4i+1h1gMjA4uEgOcaSa9QQ0Kevs9x6te//3j8DAW+Gcm3wsA+vUQ6yR75hfSTTUm0VF1/itLBm60gefEZWABYamCjGwikWX005BU+YJTYniSDJVXu6xBdayQRs0FS4ScYlTgyoxHVltV2ti8AoHD3hzlKSUJ+//XlR8tCL+q23X9abit9v3sp6DrwOMHFREdwBIb5S6eASYCx37PKJGAEOuDnMq1FlBsaTyAK4GxcJia2o0ppZGot4385XmAinCWgXlerIRpl9FhcBC0glongOHsE9K68tqXol/IbkMB7OvPJ9v932x/vV7+uHr/1+9fbtW+n36fWF2KkRS6EuoJAw5FeKIYONwFN8r6Yhi3KEAJDKEYXxNPJUloJUYzi1GxjHpKBX3ppkNoNMrEvNNZ5GcjVwCi0LsJLYePZUJkIlQDzITuRC9pSYhAURLJXT+g5CBboA/f6snPwHBQFBt97f7CTzpJtXMUD+TBBw0p6nsMNoIO4QbAJ2Kr+5eMaH4HXCbkHhgiIukIQgiVVgsxAItP6jXVzzdOgDmEchp+VC46EXtZQ1rpRKZUl8aIy/+giVl4akZjFLqB94faYg6XB9vF9vV1fr54fdXgq+/fBK0Uc5jm92AIihtsFvUA2k8HI5Ff8toAP0V+7O9hFEKv8oPALcmxRnhG1edMlOdQnx42eJzSgRZHzXjufgIvU+2RXiCVtHbCGYSdbJd7B5I+2S9RgsZIWJyLLsF5QMfK2Au3yeA+4fn/ernzY3N4dTxUU0QRthk5THCHEWPmSU5zvla1VcqE1Ugu764Uao0OkFLeyus+PnWaCfrYcbDOKaQRzS2+GMipD0CHLI8bLqmNQKWcjKxrna0sBxWI9TPc22Vq8EW87aNg/rrz3pbP/0eGprqPjjac5iAjASTfyiAq11Pxw9TF9RLQjwLdWWTS1SCXJUmUk7VK4bRDWDKBLGy3Y45ZO1nskiikElABxaCZiyKYiOCxdRwrBB0BrhZSW3OZ6Xid8329WnzfbLQ39cD5WiCDqRq2bSHEWqO3gSuAA4AINFyqBk6YYfdSoKCUmxlkQBOtwO/4QaTmhBJh/lNQt5hNSrdnEd1LMo8NLOgOBwC3bxdB1LpUyUBNCGRLmH6/gaZbWn4MlxNQrOTLXv+9seMvjwMGj3Tk6HgxZhNiZX9oKALvNFbdGkAelrIYrToaJRxUBuSYlMASRnohqOMaGhm4Q1VZgu23qGCSAzqTINkFDE0sF1fKUiKvKVqMU10JI87swA9JMIbyiDz8z2EWBar+9Wv99vjr20uhILxGkTAOjrMagECfH0HavJ2EZMSkDDQQbWntVX82dfs05ZmzMKawZhFn47nBGxoFsGxOQXRrbqzm74ygzBQnitU3IZinugH5XmZZwRkN32efXb7uFhLK2uXn+opZU3atPBy5PiilQV9ExwIUOjm8rjoWl4RS1AR2N1UgxoEvZ1wDRrWDgj2VXSmkka1rpqpwsIMg5BGQCjY/e96utWNA73IiBq6gJ6BY34X0Hl2gkV8SmJygW4P1fs4/36cbe92/erT8f+e79dUiug1MqRwbRcC/wId052PIo2knnHTkhtvJC4quNBPBYfnNsWXmTvJLE5SQRNPranSyAbYOu8mJhXf8DmyDMhNk65pQNQW2E/lETdDAqj2u4tYEww4BoRGf8d/fhpvdn+0Q/h9tvbK4GJugdeGRBgslXHMy6lKlx18fgtNKE49uIe+awTNBsQRygz95gF8lfv2/kKe7RqChK3XpQCzCSsA8r4pD31NezIxi6op4Wz+xp2sZx+zurjT+v/eYIdv1zvvz4MheVvcEfUw/NwSbh3lwcTmrkX4qqD4lAeAKAW5z8VaEYq1m7G4pO47mTDwHaP8ppJHnD4qZ0u2BbEKNKtwLDBdEWh39XWRfVHcg4+pdAx6ozVoswuvt0599jfwTx+WY+uORT/2dT+v/rRtYSBRzo9jsK09o+VnAkEMMvWo3qFLKWrnWzCQkUHqkArppwGsY2jsGYQBsV81w6nMaszZIyyFhJEYVqn1rdIjFer0ADI4K/4GeyiZjSoASyOOgG6eI6Sm9vd6tXuj7uH/tSn0eMo57RR1J1K0sBKmFvYNUk7nA6K5rwSm1g8GFI7vGN6GUnWpFIS25CwZhTGAtrxtFijyQ141cG/jfrloCYIIVYIU1BSJk2ynyzDqsVQ22k++hMjOEOSl0+3X59Xb/fk0S8j8oNyYpngI8WnFxUgroNUCEm8LA8YMgeR1ydIzKqpyWbwLHCNGCHE5+4TIWKxz/7P7/vNoW9GeRoXTacG+sXzo7xDDboXMI8f7higW8wmdkb0Kg38cFFK2O6MjPz5YoDIPzafj6fKTA6GOlphbeNqisFWYkiepFTN+iGhEFKkKVWL+xKJ6vhQflC5s7cdT5hKT0AbgBylNYO0IBpSz2CFQglHzujYB42eOsGIxjeaBNmKGsSYhiCdFUuQxf5zV+0XCvh1/7C6XO+PfSX1Fx+FGk6jvemnaJAy1SGiXXWSQjl6QiJoRKPk7DVOWrYRZkh0YRLWjMIgjL+04zm8heoEfxAJTCY3L1I+u1HnKBmE7tQc9UU5oJagy69yDhqH+9W79e1uIPS1F0VBoh3P6nARNapxSZxULsOxtkG6oUWpPht6wlWKZi2tiqhOo796PNF51YJV1tO6GaWpThlPxa6c8NM7jKRpUF5eg+vSi4QV5DWAotE4aMjXgqfxeN60vl8f+tXL/e6PCoaXFQwpeqyq48K2q0UA4i5ZuxQjhk4sygkPjXgxUeacUkwtB+KpK4/kQVYzyKod3uG0BIxhRZ06cUsQHjfU8DVbNahy7RR6FewRbLFW010xPjesp7b9zhD+4nB/VHu3X2+PNXn9/He1niRi4hGUJKmFKbHUCMpRXiq8sBk5q2A7XG/xy1hOvQ4tcBDQjAKc+rvDKVSBYkRzLHiYGsEv4vLaqeVr1H8gQ6JqpRaVKRJVsWifMN4PWNF/R5cLnH3bD+nqoloI71OjhB9fieKpaJGBSCPqQJDz1UZuNGhAGUMB9kPRMhXJBdUGWc0oi1T2qh3PQRkxFxUfNmmgFCwBBbqAxCTCVPNVqaUJT6WIr/jXkViCSkuo0L+Ofo4bZeHby/Xnz/uBY3z6ixAe/1WDCWMbAXoBPoxmt8RtHsau+gAgaTT9Fwmxmr1p7kp+I9tAlUMnZjgPzb1oyCy0OQkFQC/b06XqKpXaFg6V1cN4oZr5dCPXSQNFqOJb41cKkMo2QnDJuakLet4KuN3tn1eX+93ha//84hdND4/H07Ar5Lhsy4wN7Q48LBbjwT9jpasNXiQiPBlMFNyNT29+fDq78q798WZS+e/VjspKWmoidJVXRec6jcU1edU0jwhT8KN1JVKLIdv5vOv33e7uGR61/3qYesOKMtiKVVMyqIMBJkYLywlGnlFzmLVK/0Q7wd6wXSRYQb2KHMJQhX6O8zgl1Mz3bvfnvz6tv292T4dmkAeV+r0dTuEyangjMlHAhMpKrSbTTgkxyj5cevhUKXpPJFcyZTTG/Le94fdrStvH1Zun7XF+FSBGAxywV2KrUU1hk+rLI9CkOvwB+lGU5KWaVK+xEJWp8n2xwqTWNPezneMN0/pRVFNFOTUU6xlAoO52oLokCRO9ZBY5ttFIWnMSaQQTrN0kU7oRD0Nmm6HfBQ5WynljYPPDqzefNJXAe13VZU7FqcQaYHYgGqHo7RNl41Anrn7ZKHHzxCurs1NlnF638cvXbVSeUI1oSBgTaQFUXF67IR8bzXMpK9VOhoYOzneq+vx5Y+Bd+76F8t7cjJPxV7WADsnwGG21XrFpfFLvl7JPndgwsHi1k4mlsZvPcxtVMqJ4msLgo2xxOTUGkl57ePd1/fyw2TaTvErkpwsokuYfBXjASkGjGzXyQXN5Qw1f6iZ2TRkTVHZlLC7xJiADxzRnL6NcPT/0+9XFw8OXfjOyqKufa32oZgJ+U/QSkioUsCO6MBylG4jRiY/Ug3gW1NeGOvXHJ+J0nH0wq99TxTWzOJZx1c5XVKZ6G0KiwQgi8YWLyxuucl+9IuO19UV+PRCODsjOYL9sfJ6cH9SIe79+XO/XtTPwa52lFqv2sVWrgeiB+VLsqG3MptaXUcgGae5zw96Bq1AnmZqPgTlBszV3ovUktDTIakZZ0OKLdjyPGiCpdZvEknC/EpfXphKpjLnARXZMbVK54xlMbB7X283q75u79cPQGb2sb7ORGkiFMBVos6AojcNKSoMUh+kRKDd8q+6DIwVfJ2PdosCbJ2OiYKOsZpAFc3rfDqe4MEEJlsL5wU+glAwBfAj21HyvbQxhcdJB6apGVTofhQGsh9Xr40Y9Bmny7uK/6jS8mhNymcRu9W4X/ow56jHXXtQwQalHbJRU6htVI0K7+cP21K4BwEZ5zSTP4m7zOVmwhRWhkPWiLEEvYVH9BBFslafytcriwfJoXR7qrEwy0dRP7xycl/qErd7LW+/1vtzYpPHCN9IaAV/UwdN4nL2BMljiX7WiUEKlFAChtgBZS9O8WuvjhnqzSBMlYeVc6zvNE6q0ZpTmhVHjOToQEviECINm0rYzCkqWqw6hF353eieqqzw+qOkljMB2sB69H0G2P8eI/W7br9jL2+fTqyZUxnqpqv7UesQs26KVOWEQMMRmo5YATAmPEbK7Hz45Ox9cb5DVDLIAr6t2OE0lUMdAfAg7OTIMMOrRIJqpIxPU8TglN6mEVb8J0OeCWG/zLFX6lcDcaCK7fvx2HLLUGFLaF9ZLNtdQHwAikti6MbAUUtzAXaJ4mhEZDHpnpBYj88eczDdxClL+JK+Z5HXNr+10HlJtF+DZKZCfXNNZJZKkggBnr+MfPIUwoNjTdGVo0CyFmfSn/wfawerseSwAAA==',
}

def _bootstrap_embedded_verified_ngs():
    target_boards={"ngs-passing","ngs-receiving","ngs-rushing"}
    if int(NFL_LAST_SEASON) != 2025:
        return {"status":"SKIPPED_SEASON","installed":[],"season":int(NFL_LAST_SEASON)}
    try:
        before=savant_data_readiness(SAVANT_DIR,2025)
        missing=set(before.get("missing") or []) & target_boards
        if not missing:
            return {"status":"ALREADY_COMPLETE","installed":[],"season":2025,"readiness":before}
        uploads=[]
        for board in sorted(missing):
            blob=_EMBEDDED_VERIFIED_NGS_2025.get(board)
            if not blob:
                continue
            raw=gzip.decompress(base64.b64decode(blob.encode("ascii")))
            uploads.append((f"nflsavant-{board}-2025.csv",raw))
        results=import_savant_payloads(uploads,SAVANT_DIR,2025) if uploads else []
        installed=sorted({str(r.get("detected_board")) for r in results if r.get("valid") and r.get("saved_path")})
        after=savant_data_readiness(SAVANT_DIR,2025)
        return {"status":"INSTALLED" if installed else "NO_CHANGE","installed":installed,"season":2025,"results":results,"readiness":after}
    except Exception as exc:
        return {"status":"ERROR","installed":[],"season":2025,"error":str(exc)[:240]}

EMBEDDED_NGS_BOOTSTRAP = _bootstrap_embedded_verified_ngs()

UNDERDOG_URLS = [
    # v6 supports sport_id filtering; try the NFL-only board first so refreshes are
    # faster and the parser does not have to walk every sport on Underdog.
    "https://api.underdogfantasy.com/beta/v6/over_under_lines?sport_id=nfl",
    "https://api.underdogfantasy.com/beta/v6/over_under_lines?sport_id=NFL",
    "https://api.underdogfantasy.com/beta/v6/over_under_lines",
    "https://api.underdogfantasy.com/beta/v5/over_under_lines",
    "https://api.underdogfantasy.com/beta/v4/over_under_lines",
    "https://api.underdogfantasy.com/beta/v3/over_under_lines",
    "https://api.underdogfantasy.com/beta/v2/over_under_lines",
    "https://api.underdogfantasy.com/v1/over_under_lines",
]
UNDERDOG_REQUEST_BUDGET_SECONDS = 24.0
UNDERDOG_CONNECT_TIMEOUT_SECONDS = 3.5
UNDERDOG_READ_TIMEOUT_SECONDS = 7.5

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

# Only markets with dedicated player-stat equations are enabled for game day. Other
# feed rows are rejected rather than projected from a generic league-average baseline.
ACTIVE_NFL_MARKETS = {
    "Passing Yards", "Passing TDs", "Pass Attempts", "Completions",
    "Rushing Yards", "Rush Attempts", "Receiving Yards", "Receptions",
    "Anytime TD", "Field Goals Made",
}
ACTIVE_NFL_MARKET_ORDER = [
    "Passing Yards", "Passing TDs", "Pass Attempts", "Completions",
    "Receiving Yards", "Receptions", "Rushing Yards", "Rush Attempts",
    "Anytime TD", "Field Goals Made",
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
# ESPN's public team-logo CDN uses the current NFL abbreviations below. Keep an
# explicit map so a feed alias can never silently point at the wrong franchise.
ESPN_NFL_LOGO_IDS = {
    "ARI":"ari", "ATL":"atl", "BAL":"bal", "BUF":"buf", "CAR":"car", "CHI":"chi",
    "CIN":"cin", "CLE":"cle", "DAL":"dal", "DEN":"den", "DET":"det", "GB":"gb",
    "HOU":"hou", "IND":"ind", "JAX":"jax", "KC":"kc", "LV":"lv", "LAC":"lac",
    "LAR":"lar", "MIA":"mia", "MIN":"min", "NE":"ne", "NO":"no", "NYG":"nyg",
    "NYJ":"nyj", "PHI":"phi", "PIT":"pit", "SEA":"sea", "SF":"sf", "TB":"tb",
    "TEN":"ten", "WSH":"wsh",
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

# Keep only markets explicitly enabled above. Only markets with dedicated equations
# remain available to the parser and projection engine.
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

# Preseason is a distinct workload regime. Historical/Savant efficiency may inform
# per-opportunity output, but only rotation inputs can create playing time.
MIN_PRESEASON_BETTABLE_PROB = 0.65
MIN_PRESEASON_ELITE_PROB = 0.72
MIN_PRESEASON_DATA_SCORE = 78
MIN_PRESEASON_ELITE_SCORE = 88
MIN_PRESEASON_RELIABILITY = 74
PRESEASON_EDGE_MIN = {
    "Passing Yards": 12.0, "Pass Attempts": 2.5, "Completions": 2.0,
    "Receiving Yards": 7.0, "Receptions": 0.75,
    "Rushing Yards": 7.0, "Rush Attempts": 1.5,
}
PRESEASON_SIGMA_FLOOR = {
    "Passing Yards": 24.0, "Pass Attempts": 3.0, "Completions": 2.4,
    "Receiving Yards": 15.0, "Receptions": 1.2,
    "Rushing Yards": 13.0, "Rush Attempts": 2.0,
}
PRESEASON_SUPPORTED_MARKETS = set(PRESEASON_SIGMA_FLOOR)

# ---------- Full NFL data modules ----------
# These files provide current context. When present, they override historical role priors.
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
    "TEN": (36.1665, -86.7713), "WSH": (38.9077, -76.8645),
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
    "WSH": {"stadium":"Northwest Stadium", "crowd":"MODERATE", "noise":0.988, "surface":"Grass", "roof":"Outdoor", "altitude":0},
})

# Official-style team accent palette used only for UI theming.
NFL_TEAM_COLORS = {
    "ARI":("#97233F","#000000"), "ATL":("#A71930","#000000"), "BAL":("#241773","#9E7C0C"),
    "BUF":("#00338D","#C60C30"), "CAR":("#0085CA","#101820"), "CHI":("#0B162A","#C83803"),
    "CIN":("#FB4F14","#000000"), "CLE":("#311D00","#FF3C00"), "DAL":("#003594","#869397"),
    "DEN":("#FB4F14","#002244"), "DET":("#0076B6","#B0B7BC"), "GB":("#203731","#FFB612"),
    "HOU":("#03202F","#A71930"), "IND":("#002C5F","#A2AAAD"), "JAX":("#006778","#D7A22A"),
    "KC":("#E31837","#FFB81C"), "LAC":("#0080C6","#FFC20E"), "LAR":("#003594","#FFA300"),
    "LV":("#A5ACAF","#000000"), "MIA":("#008E97","#FC4C02"), "MIN":("#4F2683","#FFC62F"),
    "NE":("#002244","#C60C30"), "NO":("#D3BC8D","#101820"), "NYG":("#0B2265","#A71930"),
    "NYJ":("#125740","#000000"), "PHI":("#004C54","#A5ACAF"), "PIT":("#FFB612","#101820"),
    "SEA":("#002244","#69BE28"), "SF":("#AA0000","#B3995D"), "TB":("#D50A0A","#34302B"),
    "TEN":("#0C2340","#4B92DB"), "WSH":("#5A1414","#FFB612"),
}
def nfl_team_theme(team):
    team=_normalize_nfl_team(team)
    return NFL_TEAM_COLORS.get(team,("#3267A8","#8EA3BA"))

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


st.set_page_config(page_title="NFL Prop Engine", layout="wide", initial_sidebar_state="collapsed")
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
  .block-container{padding-left:.55rem!important;padding-right:.55rem!important;padding-top:.55rem;width:100%!important;max-width:100%!important}
  [data-testid="stSidebar"][aria-expanded="true"]{
    min-width:min(86vw,340px)!important;width:min(86vw,340px)!important;max-width:min(86vw,340px)!important;
    transform:translateX(0)!important;
  }
  [data-testid="stSidebar"][aria-expanded="false"]{
    min-width:0!important;width:0!important;max-width:0!important;
    margin-left:0!important;padding-left:0!important;padding-right:0!important;
    transform:translateX(-105%)!important;overflow:hidden!important;border:0!important;visibility:hidden!important;pointer-events:none!important;
  }
  [data-testid="stSidebar"][aria-expanded="false"] > div{width:0!important;min-width:0!important;overflow:hidden!important}
  section[data-testid="stSidebar"] + div{min-width:0!important}
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


# ---------- v7.48 elite NFL UI ----------
st.markdown("""
<style>
:root{
  --nfl-bg:#04070d;--nfl-panel:#07101b;--nfl-panel2:#0a1422;--nfl-line:#1b3149;
  --nfl-text:#f5f8ff;--nfl-muted:#7f93ad;--nfl-green:#5dff87;--nfl-blue:#49a6ff;
  --nfl-red:#ff4f66;--nfl-gold:#ffcb45;--nfl-purple:#a96bff;--nfl-cyan:#3de2d1;
}
.nfl-elite-board,.ml-elite-board{display:flex;flex-direction:column;gap:10px;margin:8px 0 12px}
.nfl-elite-card,.ml-elite-card{
  position:relative;overflow:hidden;background:
  radial-gradient(circle at 5% 0%,rgba(61,117,255,.11),transparent 25%),
  linear-gradient(145deg,#07111d 0%,#050a12 100%);
  border:1px solid #21364f;border-radius:18px;padding:12px;color:var(--nfl-text);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.035);
}
.nfl-elite-card:before,.ml-elite-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(#ff4f66,#49a6ff,#3de2d1)}
.nfl-card-grid{display:grid;grid-template-columns:minmax(230px,1.4fr) minmax(250px,1.15fr) minmax(300px,1.55fr) minmax(145px,.7fr);gap:9px;align-items:stretch}
.nfl-ident{display:grid;grid-template-columns:44px 58px 1fr;gap:9px;align-items:center;min-width:0}
.nfl-rank{height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:950;background:#111d2b;border:1px solid #2a405b;color:#dbe9ff}
.nfl-rank.top1{background:linear-gradient(145deg,#8f6200,#2b1b00);border-color:#f5c542;color:#ffe8a0}
.nfl-rank.top2{background:linear-gradient(145deg,#526070,#18202a);border-color:#aebccb}
.nfl-rank.top3{background:linear-gradient(145deg,#7b472f,#24140e);border-color:#c9865f}
.nfl-logo{width:56px;height:56px;object-fit:contain;filter:drop-shadow(0 0 7px rgba(255,255,255,.10))}
.nfl-player-name{font-size:18px;font-weight:950;letter-spacing:.01em;line-height:1.05;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nfl-subline{font-size:10px;color:#9fb2c9;font-weight:750;margin-top:5px;letter-spacing:.06em;text-transform:uppercase}
.nfl-readiness{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:9px}
.nfl-mini-box,.nfl-stat-box,.nfl-opp-box,.nfl-risk-box,.ml-mini{
 background:rgba(3,9,16,.64);border:1px solid #172b40;border-radius:10px;padding:7px;min-width:0
}
.nfl-label,.ml-label{font-size:8px;color:#758ba5;text-transform:uppercase;font-weight:900;letter-spacing:.08em}
.nfl-val,.ml-val{font-size:15px;font-weight:950;margin-top:2px;color:#f5f8ff}
.nfl-good{color:var(--nfl-green)!important}.nfl-bad{color:var(--nfl-red)!important}.nfl-warn{color:var(--nfl-gold)!important}.nfl-blue{color:var(--nfl-blue)!important}.nfl-purple{color:var(--nfl-purple)!important}
.nfl-meter{height:5px;background:#0e1c2a;border-radius:8px;overflow:hidden;margin-top:5px}.nfl-meter i{display:block;height:100%;border-radius:8px;background:linear-gradient(90deg,#347eff,#53d7ff)}
.nfl-market-panel{display:grid;grid-template-columns:1.1fr .9fr .8fr;gap:6px}
.nfl-market-main{grid-column:1/-1;background:linear-gradient(145deg,rgba(25,36,53,.70),rgba(4,10,18,.85));border:1px solid #243b54;border-radius:11px;padding:8px}
.nfl-market-name{font-size:9px;color:#95a9c0;font-weight:900;letter-spacing:.08em;text-transform:uppercase}.nfl-line{font-size:17px;font-weight:950;margin-top:2px}
.nfl-dist{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:6px}.nfl-dist>div{text-align:center;border-top:1px solid #172a3c;padding-top:4px}
.nfl-opportunity{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.nfl-opp-title{grid-column:1/-1;font-size:9px;color:#9bb0c7;text-transform:uppercase;font-weight:950;letter-spacing:.08em}
.nfl-opp-box{text-align:center}.nfl-opp-box .nfl-val{font-size:14px}.nfl-opp-box .nfl-label{font-size:7px}
.nfl-context-row{display:grid;grid-template-columns:1.05fr .95fr;gap:7px;margin-top:7px}.nfl-match-grade{font-size:27px;font-weight:1000;line-height:1}.nfl-context-lines{font-size:9px;color:#a7b8cb;line-height:1.55;margin-top:3px}
.nfl-likely{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;background:linear-gradient(180deg,rgba(6,16,26,.8),rgba(3,8,14,.95));border:1px solid #23415d;border-radius:13px;padding:8px}
.nfl-likely .pct{font-size:25px;font-weight:1000;color:#69ff8b}.nfl-action{font-size:10px;font-weight:950;text-transform:uppercase;margin-top:6px;letter-spacing:.06em}
.nfl-why{margin-top:8px;padding-top:7px;border-top:1px solid #162a3d;font-size:9px;color:#a7b7c9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nfl-risk-strip{display:flex;gap:7px;flex-wrap:wrap;margin-top:6px}.nfl-risk-chip{font-size:8px;font-weight:850;padding:3px 6px;border-radius:999px;border:1px solid #22364c;background:#08131f;color:#afc0d2}
.ml-elite-card{padding:0}.ml-topline{padding:7px 11px;border-bottom:1px solid #1b3045;display:flex;justify-content:space-between;gap:8px;font-size:9px;color:#91a4ba;font-weight:850;text-transform:uppercase}
.ml-main-grid{display:grid;grid-template-columns:1.05fr 1.35fr .75fr .72fr;min-width:0}
.ml-team-panel{padding:11px;display:flex;align-items:center;gap:9px}.ml-team-panel.right{flex-direction:row-reverse;text-align:right}.ml-team-panel img{width:64px;height:64px;object-fit:contain}.ml-team-code{font-size:21px;font-weight:1000}.ml-team-score{font-size:13px;color:#a6b8ca;margin-top:3px}
.ml-center-panel{padding:10px;border-left:1px solid #172b40;border-right:1px solid #172b40;text-align:center}.ml-winner-tag{font-size:9px;color:#ffcf4a;text-transform:uppercase;font-weight:950;letter-spacing:.1em}.ml-winner-big{font-size:27px;font-weight:1000;margin-top:1px}.ml-cal{font-size:11px;color:#aebfd0;margin-top:1px}.ml-scoreline{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:7px;margin-top:7px;background:#030a11;border:1px solid #1a2f44;border-radius:9px;padding:6px}.ml-scoreline b{font-size:19px}
.ml-market-panel{padding:9px;border-right:1px solid #172b40}.ml-market-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:5px}
.ml-gauge-panel{padding:9px;text-align:center;display:flex;flex-direction:column;justify-content:center}.ml-gauge{width:78px;height:39px;margin:3px auto 0;border-radius:78px 78px 0 0;background:conic-gradient(from 270deg at 50% 100%,#43e879 0deg,var(--gauge,#43e879) calc(var(--p)*1.8deg),#172737 calc(var(--p)*1.8deg) 180deg,transparent 180deg);position:relative}.ml-gauge:after{content:"";position:absolute;left:12px;right:12px;bottom:0;height:27px;background:#050c14;border-radius:55px 55px 0 0}.ml-gauge-num{font-size:24px;font-weight:1000;margin-top:-4px;position:relative;z-index:2}.ml-tier{font-size:11px;font-weight:1000;margin-top:4px}
.ml-battle{display:grid;grid-template-columns:repeat(8,1fr);border-top:1px solid #1a3045;border-bottom:1px solid #1a3045;background:#050c14}.ml-battle-cell{padding:7px 4px;text-align:center;border-right:1px solid #142537;min-width:0}.ml-battle-cell:last-child{border-right:0}.ml-battle-name{font-size:7px;color:#8fa3b9;font-weight:950;text-transform:uppercase}.ml-battle-val{font-size:10px;font-weight:950;margin-top:3px}.ml-adv-home{color:#62f888}.ml-adv-away{color:#55b9ff}.ml-even{color:#a8b6c6}
.ml-detail-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:5px;padding:8px 10px}.ml-why{margin:0 10px 9px;padding-top:6px;border-top:1px solid #16293b;font-size:9px;color:#9eb0c3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ml-pre-note{color:#ffd05a;font-weight:850}.ml-reg-note{color:#64f48a;font-weight:850}
@media(max-width:1100px){
 .nfl-card-grid{grid-template-columns:1.15fr 1fr 1.4fr .65fr}
 .ml-main-grid{grid-template-columns:1fr 1.25fr .7fr}.ml-gauge-panel{grid-column:1/-1;border-top:1px solid #172b40}
 .ml-battle{grid-template-columns:repeat(4,1fr)}
}
@media(max-width:760px){
 .nfl-elite-card{padding:9px;border-radius:14px}
 .nfl-card-grid{grid-template-columns:1fr 1fr;gap:7px}
 .nfl-ident{grid-column:1/-1;grid-template-columns:38px 48px 1fr}.nfl-logo{width:46px;height:46px}.nfl-player-name{font-size:16px}
 .nfl-market-panel{grid-template-columns:repeat(3,1fr)}.nfl-opportunity{grid-template-columns:repeat(4,1fr)}
 .nfl-likely{grid-column:1/-1;flex-direction:row;justify-content:space-between;padding:7px 11px}.nfl-likely .pct{font-size:21px}.nfl-action{margin-top:0}
 .nfl-why{white-space:normal;line-height:1.35}
 .ml-main-grid{grid-template-columns:1fr 1fr}.ml-center-panel{grid-column:1/-1;grid-row:1;border:0;border-bottom:1px solid #172b40}.ml-team-panel{grid-row:2}.ml-market-panel,.ml-gauge-panel{grid-column:1/-1;border-top:1px solid #172b40;border-right:0}
 .ml-market-panel{display:grid;grid-template-columns:1fr 1fr;gap:7px}.ml-market-grid{margin-top:0}.ml-battle{grid-template-columns:repeat(4,1fr)}.ml-detail-grid{grid-template-columns:repeat(3,1fr)}
}
</style>
""", unsafe_allow_html=True)


# ---------- v7.49 team-color + compact mobile UI overrides ----------
st.markdown("""
<style>
.compact-prop-row,.nfl-elite-card{
  --team-primary:#3267A8;--team-secondary:#8EA3BA;
  border-left:3px solid var(--team-primary)!important;
  background:
    radial-gradient(circle at 0% 50%,color-mix(in srgb,var(--team-primary) 22%,transparent),transparent 28%),
    linear-gradient(180deg,rgba(10,16,27,.98),rgba(5,9,16,.98))!important;
}
.compact-prop-row{border-bottom-color:color-mix(in srgb,var(--team-primary) 35%,rgba(255,255,255,.08))!important}
.compact-prop-row:hover{background:
  radial-gradient(circle at 0% 50%,color-mix(in srgb,var(--team-primary) 30%,transparent),transparent 32%),
  #0a111d!important}
.cp-team-logo,.nfl-logo{filter:drop-shadow(0 0 8px color-mix(in srgb,var(--team-primary) 55%,transparent))}
.nfl-elite-card{border-color:color-mix(in srgb,var(--team-primary) 68%,#26384a)!important}
.nfl-elite-card:before{background:linear-gradient(180deg,var(--team-primary),var(--team-secondary))!important}
.nfl-weather-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;margin-top:5px}
.nfl-weather-grid span{display:block;padding:4px 5px;border:1px solid #172b40;border-radius:7px;background:rgba(2,8,14,.52);font-size:7px;color:#7f94ad;text-transform:uppercase}
.nfl-weather-grid b{display:block;color:#eef5ff;font-size:10px;margin-top:1px;text-transform:none}

.ml-elite-card{
  --away-primary:#3267A8;--away-secondary:#8EA3BA;--home-primary:#7851A9;--home-secondary:#A9B3C1;--winner-primary:#ffcb45;
  border:1px solid color-mix(in srgb,var(--winner-primary) 65%,#24364c)!important;
  background:
    radial-gradient(circle at 0% 15%,color-mix(in srgb,var(--away-primary) 24%,transparent),transparent 28%),
    radial-gradient(circle at 100% 15%,color-mix(in srgb,var(--home-primary) 24%,transparent),transparent 28%),
    linear-gradient(145deg,#07101b,#03070d 70%)!important;
}
.ml-elite-card:before{background:linear-gradient(180deg,var(--away-primary),var(--winner-primary),var(--home-primary))!important;width:4px!important}
.ml-topline{background:linear-gradient(90deg,color-mix(in srgb,var(--away-primary) 13%,transparent),transparent 45%,color-mix(in srgb,var(--home-primary) 13%,transparent))}
.ml-team-panel:first-child{background:linear-gradient(90deg,color-mix(in srgb,var(--away-primary) 16%,transparent),transparent)}
.ml-team-panel.right{background:linear-gradient(270deg,color-mix(in srgb,var(--home-primary) 16%,transparent),transparent)}
.ml-winner-big{color:var(--winner-primary);text-shadow:0 0 14px color-mix(in srgb,var(--winner-primary) 26%,transparent)}
.ml-winner-tag{color:var(--winner-primary)}
.ml-market-panel{background:rgba(2,8,15,.35)}
.ml-tier-pill{display:inline-flex;align-items:center;justify-content:center;padding:5px 9px;border:1px solid color-mix(in srgb,var(--winner-primary) 55%,#26384c);border-radius:999px;background:color-mix(in srgb,var(--winner-primary) 10%,#06101a);font-size:10px;font-weight:1000;color:var(--winner-primary);margin-top:5px}
.ml-weather-strip{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px;padding:0 10px 8px}
.ml-weather-cell{background:rgba(3,9,16,.68);border:1px solid #172b40;border-radius:9px;padding:6px;min-width:0}
.ml-weather-cell .ml-label{font-size:7px}.ml-weather-cell .ml-val{font-size:11px;overflow-wrap:anywhere}
.ml-weather-source{font-size:7px;color:#6f849d;margin-top:2px;text-transform:uppercase}
@media(max-width:760px){
  .compact-prop-row{border-radius:10px!important;margin-bottom:6px!important;border:1px solid color-mix(in srgb,var(--team-primary) 45%,#1c2c40)!important;border-left:3px solid var(--team-primary)!important;padding:9px 5px!important}
  .nfl-elite-card{padding:8px!important;border-radius:12px!important}
  .nfl-readiness{margin-top:5px}.nfl-mini-box,.nfl-stat-box,.nfl-opp-box,.nfl-risk-box{padding:5px}
  .nfl-card-grid{gap:5px!important}.nfl-context-row{gap:5px;margin-top:5px}.nfl-risk-strip{gap:4px;margin-top:4px}
  .nfl-weather-grid{grid-template-columns:repeat(4,1fr)}
  .ml-elite-board{gap:8px!important}.ml-elite-card{border-radius:14px!important}
  .ml-topline{padding:6px 8px!important;font-size:8px!important;line-height:1.25;flex-wrap:wrap}
  .ml-main-grid{grid-template-columns:1fr 1fr 1fr!important}
  .ml-center-panel{grid-column:2!important;grid-row:1!important;border-left:1px solid #172b40!important;border-right:1px solid #172b40!important;border-bottom:0!important;padding:7px 4px!important}
  .ml-team-panel{grid-row:1!important;padding:7px 5px!important;gap:4px!important;flex-direction:column!important;text-align:center!important}
  .ml-team-panel.right{grid-column:3!important}
  .ml-team-panel img{width:48px!important;height:48px!important}.ml-team-code{font-size:17px!important}.ml-team-score{font-size:9px!important;margin-top:1px!important}
  .ml-winner-tag{font-size:7px!important}.ml-winner-big{font-size:23px!important}.ml-cal{font-size:9px!important}
  .ml-scoreline{grid-column:1/-1;margin:0 7px 6px!important;padding:5px!important}.ml-scoreline b{font-size:14px!important}
  .ml-market-panel{grid-column:1/-1!important;display:grid!important;grid-template-columns:82px 1fr!important;gap:5px!important;padding:6px 8px!important;border-top:1px solid #172b40;border-right:0!important}
  .ml-market-grid{margin:0!important;grid-template-columns:repeat(4,1fr)!important}.ml-mini{padding:5px!important}.ml-val{font-size:12px!important}
  .ml-gauge-panel{grid-column:1/-1!important;padding:5px 8px!important;display:grid!important;grid-template-columns:auto 1fr auto auto!important;align-items:center;gap:7px;border-top:1px solid #172b40!important}
  .ml-gauge{width:58px!important;height:29px!important;margin:0!important}.ml-gauge:after{left:9px!important;right:9px!important;height:20px!important}
  .ml-gauge-num{font-size:19px!important;margin:0!important}.ml-tier-pill{margin:0!important;font-size:9px!important}
  .ml-battle{grid-template-columns:repeat(4,1fr)!important}.ml-battle-cell{padding:5px 2px!important}.ml-battle-name{font-size:6px!important}.ml-battle-val{font-size:8px!important}
  .ml-detail-grid{grid-template-columns:repeat(3,1fr)!important;padding:6px 8px!important;gap:4px!important}
  .ml-weather-strip{grid-template-columns:repeat(4,1fr)!important;padding:0 8px 6px!important;gap:4px!important}
  .ml-weather-cell{padding:5px!important}.ml-weather-cell .ml-val{font-size:9px!important}
  .ml-why{margin:0 8px 7px!important;font-size:8px!important;white-space:normal!important;line-height:1.3}
}
</style>
""", unsafe_allow_html=True)

# ---------- helpers ----------
def now_iso(): return datetime.now().isoformat(timespec="seconds")
def safe_float(x, default=None):
    try:
        if x is None or x == "": return default
        value=float(x)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default

def _usable_context_value(value):
    """True when a CSV/JSON value can safely override an existing context value."""
    if value is None:
        return False
    if isinstance(value,str):
        return value.strip().lower() not in {"", "nan", "none", "null", "n/a"}
    if isinstance(value,(float,np.floating)):
        return math.isfinite(float(value))
    return True

def clamp(x, lo, hi): return max(lo, min(hi, x))
_JSON_RUNTIME_CACHE = {}
_CSV_RUNTIME_CACHE = {}
_RECORD_BANK_RUNTIME_CACHE = {}
_ROW_BANK_RUNTIME_CACHE = {}
_PHASE6_PLAYER_LOOKUP_CACHE = {"sig": None, "data": None}
_PROJECTION_READINESS_CACHE = {"sig": None, "data": None}

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

NFL_DISPLAY_TIMEZONE = os.getenv("NFL_DISPLAY_TIMEZONE", "America/Los_Angeles")

def _parse_aware_datetime(value):
    """Parse feed timestamps without silently dropping UTC/offset information."""
    if value in [None, ""]:
        return None
    if isinstance(value, datetime):
        dt=value
    else:
        txt=str(value).strip()
        if txt.endswith(("Z","z")):
            txt=txt[:-1] + "+00:00"
        try:
            dt=datetime.fromisoformat(txt)
        except Exception:
            dt=None
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"]:
                try:
                    dt=datetime.strptime(txt,fmt); break
                except Exception:
                    continue
            if dt is None:
                return None
    # Underdog/league scheduled_at timestamps are UTC when no offset survives the feed.
    if dt.tzinfo is None:
        dt=dt.replace(tzinfo=timezone.utc)
    return dt

def _display_datetime(value):
    dt=_parse_aware_datetime(value)
    if not dt:
        return None
    try:
        return dt.astimezone(ZoneInfo(NFL_DISPLAY_TIMEZONE))
    except Exception:
        return dt.astimezone(timezone.utc)

def _local_game_date(value):
    dt=_display_datetime(value)
    return dt.strftime("%Y-%m-%d") if dt else ""

def _hours_old(value):
    dt=_parse_aware_datetime(value)
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()/3600.0)

def nfl_game_phase(row):
    """Classify preseason rows before a regular-season workload model can run."""
    row=row or {}
    explicit_values=[str(row.get(key) or "").strip().upper() for key in [
        "season_type","game_type","event_type","season_phase","week_type","line_title",
    ]]
    explicit=" ".join(explicit_values)
    if any(value in ["PRE","P","PRESEASON"] for value in explicit_values):
        return "PRESEASON"
    if any(term in explicit for term in ["PRESEASON","PRE-SEASON","PRE SEASON","PRE1","PRE2","PRE3"]):
        return "PRESEASON"
    if any(term in explicit for term in ["POSTSEASON","PLAYOFF","WILD CARD","DIVISIONAL","CHAMPIONSHIP","SUPER BOWL"]):
        return "POSTSEASON"
    if any(term in explicit for term in ["REGULAR","REG SEASON"]):
        return "REGULAR"
    for key in ["scheduled_at","starts_at","start_time","event_time","game_time","game_date"]:
        dt=_parse_any_datetime(row.get(key))
        if dt:
            if dt.month in [7,8]:
                return "PRESEASON"
            if dt.month in [9,10,11,12]:
                return "REGULAR"
            if dt.month in [1,2]:
                return "REGULAR_OR_POSTSEASON"
    return "UNKNOWN"

def normalized_season_mode(value):
    text=str(value or "").strip().upper().replace("-","_").replace(" ","_")
    if text in {"PRE","P","PRESEASON","PRE_SEASON"}:
        return "PRESEASON"
    if text in {"REGULAR","REG","REGULAR_SEASON","POSTSEASON","PLAYOFF","REGULAR_OR_POSTSEASON"}:
        return "REGULAR"
    return ""

def season_mode_for_row(row, default="REGULAR"):
    explicit=normalized_season_mode((row or {}).get("season_mode"))
    if explicit:
        return explicit
    phase=nfl_game_phase(row or {})
    if phase=="PRESEASON":
        return "PRESEASON"
    if phase in {"REGULAR","POSTSEASON","REGULAR_OR_POSTSEASON"}:
        return "REGULAR"
    return normalized_season_mode(default) or "REGULAR"

def row_matches_season_mode(row, mode):
    mode=normalized_season_mode(mode) or "REGULAR"
    explicit=normalized_season_mode((row or {}).get("season_mode"))
    if explicit:
        return explicit==mode
    phase=nfl_game_phase(row or {})
    if phase=="PRESEASON":
        return mode=="PRESEASON"
    if phase in {"REGULAR","POSTSEASON","REGULAR_OR_POSTSEASON"}:
        return mode=="REGULAR"
    return False

def graded_row_season_mode(row):
    explicit=normalized_season_mode((row or {}).get("season_mode"))
    if explicit:
        return explicit
    return "PRESEASON" if nfl_game_phase(row or {})=="PRESEASON" else "REGULAR"

def rows_for_season_mode(rows, mode):
    mode=normalized_season_mode(mode) or "REGULAR"
    return [row for row in (rows or []) if graded_row_season_mode(row)==mode]

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

def edge_requirement_for_row(row):
    prop=str((row or {}).get("prop") or "")
    regular=edge_requirement(prop)
    if season_mode_for_row(row) != "PRESEASON":
        return regular
    line=abs(safe_float((row or {}).get("line"),0) or 0)
    floor=safe_float(PRESEASON_EDGE_MIN.get(prop),regular) or regular
    relative=line*(0.12 if prop in {"Passing Yards","Receiving Yards","Rushing Yards"} else 0.10)
    return float(max(floor,relative))

def decimal_odds(odds):
    odds=safe_float(odds)
    if odds is None: return None
    return 1 + odds/100 if odds > 0 else 1 + 100/abs(odds)

def expected_value(prob, odds=-110, loss_prob=None):
    """Expected profit per unit, treating pushes as returned stakes."""
    dec=decimal_odds(odds)
    if prob is None or dec is None: return None
    loss_prob=(1-prob) if loss_prob is None else max(0.0, float(loss_prob))
    return (prob*(dec-1)) - loss_prob

def kelly_fraction(prob, odds=-110, loss_prob=None):
    """Kelly fraction for win/loss/push outcomes, capped for app safety."""
    dec=decimal_odds(odds)
    if prob is None or dec is None: return 0.0
    b=dec-1
    q=(1-prob) if loss_prob is None else max(0.0, float(loss_prob))
    decision_prob=max(1e-9, float(prob)+q)
    if b <= 0: return 0.0
    return float(clamp(((b*prob)-q)/(b*decision_prob), 0, MAX_RECOMMENDED_KELLY))

def selected_side_price(row, side, default=-110):
    """Return the American price for the model's selected side."""
    row=row or {}; side=str(side or "").upper()
    side_key="over_price" if side == "OVER" else "under_price" if side == "UNDER" else ""
    price=safe_float(row.get(side_key)) if side_key else None
    if price is None:
        price=safe_float(row.get("odds"), safe_float(row.get("price"), default))
    return float(price if price is not None else default)

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

def calibration_scale(player, prop, phase="REGULAR"):
    results=load_json(RESULT_LOG,[])
    phase=normalized_season_mode(phase) or "REGULAR"
    rows=[r for r in results if norm(r.get("player"))==norm(player) and r.get("prop")==prop and r.get("actual") is not None and r.get("projection") is not None and graded_row_season_mode(r)==phase]
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

def calibration_readiness(prop=None, phase="REGULAR"):
    results=load_json(RESULT_LOG,[])
    phase=normalized_season_mode(phase) or "REGULAR"
    rows=[]
    for r in results:
        if prop and r.get("prop") != prop:
            continue
        if graded_row_season_mode(r) != phase:
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
    phase=season_mode_for_row(row)
    clean=[r for r in results if r.get("actual") is not None and r.get("projection") is not None and graded_row_season_mode(r)==phase]
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

def nfl_projection_reliability_score(row, usage_quality, stability, audit, model_fallback_used=False, injury_risk="LOW", game_script_risk="LOW", volatility="LOW"):
    """0-100 input trust score, intentionally independent from pick probability.

    Adapted from the MLB app's strongest transferable idea: confidence answers which
    side the simulation likes; reliability answers whether the inputs are trustworthy
    enough to make that confidence actionable.
    """
    row=row or {}; audit=audit or {}
    usage=float(clamp(safe_float(usage_quality,0) or 0,0,100))
    stable=float(clamp(safe_float(stability,0) or 0,0,100))
    audit_score=safe_float(audit.get("score"),0) or 0
    audit_max=max(1.0,safe_float(audit.get("max_score"),1) or 1)
    audit_pct=float(clamp((audit_score/audit_max)*100.0,0,100))
    score=0.35*usage + 0.22*stable + 0.18*audit_pct
    readiness=row.get("database_readiness") or projection_database_readiness()
    score += 10.0 if readiness.get("ready") else 1.0
    score += 8.0 if not model_fallback_used else 0.0
    if row.get("model_match_status")=="MATCHED" or row.get("model_match",True): score+=4.0
    if str(injury_risk).upper()=="HIGH": score-=9.0
    elif str(injury_risk).upper()=="EXTREME": score-=18.0
    if str(game_script_risk).upper()=="HIGH": score-=4.0
    if str(volatility).upper()=="HIGH": score-=7.0
    elif str(volatility).upper()=="MED": score-=2.0
    if audit.get("hard_blocks"): score-=12.0
    score=float(clamp(score,0,99))
    label="ELITE" if score>=90 else "STRONG" if score>=82 else "SOLID" if score>=72 else "RISKY" if score>=62 else "FADE"
    return round(score,1),label

def calibrate_nfl_decision_probabilities(over, under, push, reliability_score, volatility="LOW"):
    """Reliability/noise shrink for regular-season decision probabilities.

    Raw simulation probabilities remain available for audit. The actionable probabilities
    are pulled toward 50% when input reliability is weak or simulation dispersion is high.
    This changes selection confidence, not the raw projection mean.
    """
    over=float(clamp(safe_float(over,0.5) or 0.5,0,1))
    under=float(clamp(safe_float(under,0.5) or 0.5,0,1))
    push=float(clamp(safe_float(push,0.0) or 0.0,0,1))
    rel=float(clamp(safe_float(reliability_score,65) or 65,0,100))
    strength=float(clamp(0.68 + (rel-70.0)*0.0085,0.55,0.97))
    if str(volatility).upper()=="HIGH": strength*=0.88
    elif str(volatility).upper()=="MED": strength*=0.95
    strength=float(clamp(strength,0.48,0.97))
    cal_over=0.5+(over-0.5)*strength
    cal_under=0.5+(under-0.5)*strength
    total=cal_over+cal_under
    cal_push=max(0.0,1.0-total)
    if total>1.0:
        cal_over/=total; cal_under/=total; cal_push=0.0
    return float(cal_over),float(cal_under),float(cal_push),round(strength,3)

def _hours_to_kickoff(row):
    row=row or {}
    for key in ["starts_at","start_time","event_time","scheduled_at","game_time","game_date"]:
        dt=_parse_aware_datetime(row.get(key))
        if dt:
            return (dt.astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()/3600.0
    return None

def official_inactives_safety_gate(row, prop=None):
    """Fail-safe status gate for player, QB, role and game-day inactive uncertainty."""
    row=row or {}; prop=prop or row.get("prop")
    hard=[]; review=[]
    if nfl_game_phase(row) == "PRESEASON":
        hard.append("Preseason props require a separate workload model and are disabled")
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
    if require and not confirmed_bool:
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
    if label == "Stale":
        blocks.append("Projection context stale")
    safety=official_inactives_safety_gate(row, row.get("prop"))
    blocks.extend(safety.get("hard_blocks") or [])
    blocks.extend(safety.get("review_blocks") or [])
    market_intel=market_intelligence_engine(row, line=line)
    blocks.extend(market_intel.get("blocks") or [])
    database_readiness=row.get("database_readiness") or projection_database_readiness()
    if not database_readiness.get("ready"):
        missing=", ".join(database_readiness.get("missing",[])[:3])
        blocks.append(f"Game-day database incomplete: {missing}")
    # Preserve order while removing duplicate wording from overlapping safety checks.
    blocks=list(dict.fromkeys(str(x) for x in blocks if x))
    return {"label":label, "score":score, "max_score":len(layers), "layers":layers, "hard_blocks":blocks, "inactives_gate":safety, "market_intelligence":market_intel}

def official_rejection_reasons(p):
    reasons=[]
    prop=p.get("prop")
    if p.get("data_integrity_block"):
        reasons.append(str(p.get("data_integrity_block")))
    prob=safe_float(p.get("fair_prob"),0) or 0
    edge_abs=abs(safe_float(p.get("edge"),0) or 0)
    score=safe_float(p.get("data_score"),0) or 0
    stability=safe_float(p.get("stability_score"),0) or 0
    if season_mode_for_row(p)=="PRESEASON":
        reliability=safe_float(p.get("reliability_score"),0) or 0
        workload_conf=str(p.get("preseason_workload_confidence") or "UNKNOWN").upper()
        if safe_float(p.get("line")) is None: reasons.append("No real line")
        if safe_float(p.get("projection")) is None: reasons.append("No projection")
        if prop not in PRESEASON_SUPPORTED_MARKETS: reasons.append("Unsupported preseason market")
        if prob < MIN_PRESEASON_BETTABLE_PROB: reasons.append(f"Prob below {MIN_PRESEASON_BETTABLE_PROB:.0%}")
        required=edge_requirement_for_row(p)
        if edge_abs < required: reasons.append(f"Edge below {required:g} for preseason {prop}")
        if score < MIN_PRESEASON_DATA_SCORE: reasons.append(f"Data score below {MIN_PRESEASON_DATA_SCORE}")
        if reliability < MIN_PRESEASON_RELIABILITY: reasons.append(f"Reliability below {MIN_PRESEASON_RELIABILITY}")
        if workload_conf in {"LOW","UNKNOWN"}: reasons.append("Preseason workload is not confirmed")
        if stability < NFL_PROJECTION_STABILITY_MIN: reasons.append("Projection too unstable")
        if (p.get("distribution_conflict") or {}).get("conflict"): reasons.append("Mean/P50 distribution conflict")
        reasons.extend((p.get("projection_audit") or {}).get("hard_blocks") or [])
        return list(dict.fromkeys(str(reason) for reason in reasons if reason))
    if safe_float(p.get("line")) is None: reasons.append("No real line")
    if safe_float(p.get("projection")) is None: reasons.append("No projection")
    if prob < MIN_NFL_BETTABLE_PROB: reasons.append(f"Prob below {MIN_NFL_BETTABLE_PROB:.0%}")
    if edge_abs < edge_requirement(prop): reasons.append(f"Edge below {edge_requirement(prop)} for {prop}")
    if score < MIN_NFL_DATA_SCORE: reasons.append(f"Data score below {MIN_NFL_DATA_SCORE}")
    if stability < NFL_PROJECTION_STABILITY_MIN: reasons.append("Projection too unstable")
    if str(p.get("volatility")) == "HIGH": reasons.append("High volatility tax")
    if p.get("injury_risk") in ["HIGH", "EXTREME"]: reasons.append(f"Injury/role risk: {p.get('injury_risk')}")
    if safe_float(p.get("usage_quality"),100) < 68: reasons.append("Usage data/role quality too weak")
    if p.get("model_fallback_used"): reasons.append("Player/stat model data fallback used")
    if (p.get("distribution_conflict") or {}).get("conflict"):
        reasons.append("Mean/P50 distribution conflict")
    if p.get("defense_risk") == "HIGH" and prob < 0.66: reasons.append("Tough defensive role matchup")
    if safe_float(p.get("collapse_prob"),0) >= 0.24 and prob < 0.69: reasons.append("High collapse-branch risk")
    if p.get("game_script_risk") == "HIGH" and prob < 0.67: reasons.append("Game-script risk on non-elite edge")
    cal=p.get("calibration_status") or calibration_readiness(prop)
    if cal.get("label") == "WARMING" and prob < 0.66:
        reasons.append(f"Calibration sample warming up ({cal.get('graded_rows',0)}/{cal.get('min_rows',25)})")
    audit=p.get("projection_audit") or {}
    reasons.extend(audit.get("hard_blocks") or [])
    database_readiness=p.get("database_readiness") or projection_database_readiness()
    if not database_readiness.get("ready"):
        reasons.append("Game-day database is not ready")
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
    req=edge_requirement_for_row(p)

    if side == "PASS":
        return "🚫 PASS", "PASS", reasons
    if side == "NO LINE" or safe_float(p.get("line")) is None:
        return "🚫 NO LINE", "PASS", reasons

    preseason=season_mode_for_row(p)=="PRESEASON"
    elite_prob=MIN_PRESEASON_ELITE_PROB if preseason else MIN_NFL_ELITE_PROB
    bet_prob=MIN_PRESEASON_BETTABLE_PROB if preseason else MIN_NFL_BETTABLE_PROB
    elite_score=MIN_PRESEASON_ELITE_SCORE if preseason else MIN_NFL_ELITE_SCORE
    data_score=MIN_PRESEASON_DATA_SCORE if preseason else MIN_NFL_DATA_SCORE
    elite=(not reasons and prob>=elite_prob and score>=elite_score and edge_abs>=req*1.35)
    strong=(not reasons and prob>=bet_prob and score>=data_score and edge_abs>=req)

    if elite:
        return f"🔥 {side}", "BET", reasons
    if strong:
        return f"✅ {side}", "BET", reasons

    # Data-integrity and structural distribution disagreements are no-bet diagnostics.
    if any("Market integrity conflict" in str(r) or "DATA MAPPING" in str(r) for r in reasons):
        return "🚫 PASS · DATA INTEGRITY", "PASS", reasons
    if any("Mean/P50 distribution conflict" in str(r) for r in reasons):
        return "🚫 PASS · DISTRIBUTION CONFLICT", "PASS", reasons

    # LEAN means there is enough direction to track, but not enough for official.
    if prob >= 0.57 or edge_abs >= req*0.55:
        return f"⚠️ LEAN {side}", "LEAN", reasons

    # Thin/no-edge spots should not be forced into OVER/UNDER.
    return "🚫 PASS", "PASS", reasons

def get_secret(key, default=""):
    try: return st.secrets[key]
    except Exception: return os.getenv(key, default)

@st.cache_data(ttl=120, show_spinner=False)
def safe_get_json(url, cache_bust=0):
    try:
        headers={
            "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept":"application/json,text/plain,*/*",
            "Cache-Control":"no-cache",
            "Pragma":"no-cache",
            "Referer":"https://underdogfantasy.com/",
            "Origin":"https://underdogfantasy.com",
        }
        # cache_bust is part of the Streamlit cache key, so a manual refresh forces
        # a real request without changing Underdog's endpoint URL.
        r=requests.get(
            url,
            headers=headers,
            timeout=(UNDERDOG_CONNECT_TIMEOUT_SECONDS, UNDERDOG_READ_TIMEOUT_SECONDS),
        )
        if r.status_code!=200:
            request_log(url,f"HTTP {r.status_code}",r.text[:240]); return None
        data=r.json()
        request_log(url,"HTTP 200",f"bytes={len(r.content)} refresh={cache_bust}")
        return data
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

def _normalized_market_text(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return " ".join(text.split())

def _is_full_game_market_label(value):
    """Reject quarter, half, drive and season markets before stat projection."""
    text=_normalized_market_text(value)
    if not text:
        return True
    blocked_patterns=[
        r"\b(?:1q|2q|3q|4q|first quarter|second quarter|third quarter|fourth quarter|quarter)\b",
        r"\b(?:1h|2h|first half|second half|half)\b",
        r"\b(?:season|regular season|postseason|playoffs?)\b",
        r"\b(?:first drive|opening drive|next drive|drive result)\b",
    ]
    return not any(re.search(pattern,text) for pattern in blocked_patterns)

def _market_alias_matches(text, alias):
    text = _normalized_market_text(text)
    alias = _normalized_market_text(alias)
    if not text or not alias:
        return False
    # Very short aliases such as rec/int must be whole tokens.  Substring matching
    # used to turn unrelated labels into the wrong market.
    if len(alias) <= 4 and " " not in alias:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))

def prop_name_from_blob(blob):
    b = _normalized_market_text(blob)
    # Match the longest/specific aliases first.  This prevents e.g.
    # 'Longest Reception' from being routed to plain 'Receptions'.
    candidates=[]
    for prop, aliases in NFL_PROP_ALIASES.items():
        for alias in aliases:
            candidates.append((len(_normalized_market_text(alias)), prop, alias))
    for _, prop, alias in sorted(candidates, key=lambda x: x[0], reverse=True):
        if _market_alias_matches(b, alias):
            return prop
    return None

# v10.9 live-board safety: only accept realistic single-game NFL prop lines.
# This blocks season-long totals like 3249.5 passing yards from being treated as a game prop line.
REGULAR_MARKET_LINE_RANGES = {
    "Passing Yards": (70.0, 430.0),
    "Passing TDs": (0.5, 5.5),
    "Interceptions": (0.5, 3.5),
    "Pass Attempts": (8.5, 58.5),
    "Completions": (4.5, 42.5),
    "Rushing Yards": (2.5, 175.0),
    "Rush Attempts": (0.5, 35.5),
    "Receiving Yards": (0.5, 190.0),
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
PRESEASON_MARKET_LINE_RANGES = {
    "Passing Yards": (5.0, 260.0),
    "Pass Attempts": (0.5, 42.5),
    "Completions": (0.5, 30.5),
    "Rushing Yards": (0.5, 125.0),
    "Rush Attempts": (0.5, 28.5),
    "Receiving Yards": (0.5, 125.0),
    "Receptions": (0.5, 10.5),
}
# Intake uses the union so low preseason full-game lines are not discarded before
# season type is known. project_row/project_row_preseason apply strict mode ranges.
MARKET_LINE_RANGES = {
    prop: (
        min(REGULAR_MARKET_LINE_RANGES.get(prop, (999, -999))[0], PRESEASON_MARKET_LINE_RANGES.get(prop, (999, -999))[0]),
        max(REGULAR_MARKET_LINE_RANGES.get(prop, (-999, -999))[1], PRESEASON_MARKET_LINE_RANGES.get(prop, (-999, -999))[1]),
    )
    for prop in set(REGULAR_MARKET_LINE_RANGES) | set(PRESEASON_MARKET_LINE_RANGES)
}

def _valid_market_line(prop, line, season_mode=None):
    v = safe_float(line)
    if v is None:
        return False
    mode=str(season_mode or "").upper().strip()
    if mode=="PRESEASON":
        ranges=PRESEASON_MARKET_LINE_RANGES
    elif mode in {"REGULAR","POSTSEASON"}:
        ranges=REGULAR_MARKET_LINE_RANGES
    else:
        ranges=MARKET_LINE_RANGES
    lo, hi = ranges.get(str(prop or ""), (0.01, 999.0))
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


def _infer_home_away_from_matchup(row):
    """Infer HOME/AWAY from an oriented ``AWAY @ HOME`` matchup when the feed omits it."""
    ha=str((row or {}).get("home_away") or "").strip().upper()
    if ha in {"HOME","H"}:
        return "HOME"
    if ha in {"AWAY","A"}:
        return "AWAY"
    matchup=str((row or {}).get("matchup") or "")
    if "@" not in matchup:
        return ""
    away, home=_teams_from_matchup_text(matchup)
    team=_normalize_nfl_team((row or {}).get("team"))
    if team and away and team==away:
        return "AWAY"
    if team and home and team==home:
        return "HOME"
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
    has_nfl=any(x in b for x in ["nfl", "football", "national football", "nfl_player"])
    if any(x in b for x in NON_NFL_BLOCK_TERMS) and not has_nfl:
        return False
    if has_nfl:
        return True
    # Without an explicit league tag, require a football market plus an NFL-style position/team.
    if prop_name_from_blob(b):
        for o in objs:
            if not isinstance(o, dict):
                continue
            pos=str(o.get("position") or o.get("pos") or "").upper().strip()
            team=_normalize_nfl_team(o.get("team") or o.get("team_abbr") or o.get("team_code"))
            if pos in ["QB","RB","FB","WR","TE","K","PK"] or team:
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

def _extract_underdog_v6_flat_rows(data, source_url):
    """Parse Underdog's current v6 flat board shape directly.

    Current boards expose over_under_lines plus top-level players, appearances and
    games.  Parsing that native shape first is much more reliable than recursively
    guessing relationships from the entire payload.
    """
    if not isinstance(data, dict) or not isinstance(data.get("over_under_lines"), list):
        return [], {"native_v6": False}
    players={str(x.get("id")):x for x in data.get("players",[]) if isinstance(x,dict) and x.get("id") is not None}
    appearances={str(x.get("id")):x for x in data.get("appearances",[]) if isinstance(x,dict) and x.get("id") is not None}
    games={str(x.get("id")):x for x in data.get("games",[]) if isinstance(x,dict) and x.get("id") is not None}
    teams={str(x.get("id")):x for x in data.get("teams",[]) if isinstance(x,dict) and x.get("id") is not None}
    rows=[]; dropped=0
    for line_obj in data.get("over_under_lines",[]):
        if not isinstance(line_obj,dict):
            continue
        ou=line_obj.get("over_under") if isinstance(line_obj.get("over_under"),dict) else {}
        stat=ou.get("appearance_stat") if isinstance(ou.get("appearance_stat"),dict) else {}
        raw_label=stat.get("display_stat") or stat.get("stat") or stat.get("name") or ou.get("title") or line_obj.get("title") or ""
        prop=_canon_prop_label(raw_label)
        line=safe_float(line_obj.get("stat_value"))
        if not _is_full_game_market_label(raw_label) or prop not in ACTIVE_NFL_MARKETS or line is None or not _valid_market_line(prop,line):
            dropped+=1; continue
        app_id=stat.get("appearance_id") or ou.get("appearance_id") or line_obj.get("appearance_id")
        app=appearances.get(str(app_id),{}) if app_id is not None else {}
        player_id=app.get("player_id") or stat.get("player_id") or ou.get("player_id")
        player=players.get(str(player_id),{}) if player_id is not None else {}
        name=str(player.get("display_name") or player.get("full_name") or player.get("name") or "").strip()
        if not name:
            first=str(player.get("first_name") or "").strip(); last=str(player.get("last_name") or "").strip()
            name=f"{first} {last}".strip()
        if not name:
            # Modern line options usually include the selection header as a final fallback.
            for opt in line_obj.get("options",[]) if isinstance(line_obj.get("options"),list) else []:
                if isinstance(opt,dict) and str(opt.get("selection_header") or "").strip():
                    name=str(opt.get("selection_header")).strip(); break
        if not name:
            dropped+=1; continue
        match_id=app.get("match_id") or stat.get("match_id") or ou.get("match_id")
        game=games.get(str(match_id),{}) if match_id is not None else {}
        team_id=app.get("team_id") or player.get("team_id")
        team_obj=teams.get(str(team_id),{}) if team_id is not None else {}
        team=_normalize_nfl_team(
            app.get("team_abbr") or player.get("team_abbr") or team_obj.get("abbr") or
            team_obj.get("abbreviation") or app.get("team") or player.get("team")
        )
        position=str(app.get("position") or player.get("position") or player.get("position_abbr") or "").upper().strip()
        matchup=_canonical_matchup(game.get("title") or game.get("matchup") or "")
        mt1,mt2=_teams_from_matchup_text(matchup)
        if not team:
            # Phase 6 lookup below can recover team/position from the player name.
            resolved=_resolve_model_player_strict(name,None,position)
            if resolved:
                team=_normalize_nfl_team(resolved.get("team")); position=position or str(resolved.get("position") or "").upper()
                name=resolved.get("player") or name
        opp=""
        if team and mt1 and mt2:
            opp=mt2 if mt1==team else mt1 if mt2==team else ""
        options=line_obj.get("options",[]) if isinstance(line_obj.get("options"),list) else []
        higher=next((o for o in options if isinstance(o,dict) and str(o.get("choice") or "").lower() in {"higher","over"}),{})
        lower=next((o for o in options if isinstance(o,dict) and str(o.get("choice") or "").lower() in {"lower","under"}),{})
        over_multiplier=safe_float(higher.get("payout_multiplier"))
        under_multiplier=safe_float(lower.get("payout_multiplier"))
        even_multipliers=(
            over_multiplier is not None and under_multiplier is not None
            and abs(over_multiplier-1.0)<1e-9 and abs(under_multiplier-1.0)<1e-9
        )
        rows.append({
            "player":name,"team":team,"opp":opp,"position":position,"prop":prop,"raw_prop_label":str(raw_label),
            "line":float(line),"source":"Underdog","source_url":source_url,"matchup":matchup,
            "event_id":str(match_id or game.get("id") or ""),"underdog_id":str(line_obj.get("id") or ""),
            "odds":safe_float(higher.get("american_price"),-110) or -110,
            "over_price":safe_float(higher.get("american_price")),"under_price":safe_float(lower.get("american_price")),
            "over_multiplier":over_multiplier,"under_multiplier":under_multiplier,
            "line_status":line_obj.get("status") or ou.get("status") or "",
            "line_type":line_obj.get("line_type") or line_obj.get("type") or ou.get("type") or "",
            "line_variant":line_obj.get("variant") or line_obj.get("line_variant") or ou.get("variant") or "",
            "line_title":line_obj.get("title") or ou.get("title") or "",
            "is_standard_line":bool(line_obj.get("is_standard") is True or line_obj.get("standard") is True or even_multipliers),
            "is_alt_line":bool(line_obj.get("is_alternate") is True or line_obj.get("alternate") is True),
            "scheduled_at":game.get("scheduled_at"),
            "season_type":game.get("season_type") or game.get("game_type") or game.get("event_type") or "",
            "week":game.get("week") or game.get("week_number") or "",
        })
    return rows,{"native_v6":True,"native_rows":len(rows),"native_dropped":dropped,"players":len(players),"appearances":len(appearances),"games":len(games)}

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
        explicit_market_label=_pick_first_string([app_stat,ou,line_obj],["display_stat","stat_type_name","stat","title","label","name"])
        if not _is_full_game_market_label(explicit_market_label):
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
            "scheduled_at":_pick_first_string(combo,["scheduled_at","starts_at","start_time","event_time"]),
            "season_type":_pick_first_string(combo,["season_type","game_type","event_type","season_phase"]),
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
    request_started=time.monotonic()
    for url in UNDERDOG_URLS:
        if time.monotonic()-request_started >= UNDERDOG_REQUEST_BUDGET_SECONDS:
            endpoint_debug.append({"url":url,"status":"REQUEST_BUDGET_EXHAUSTED","rows":0})
            break
        data=safe_get_json(url, cache_bust)
        if not data:
            endpoint_debug.append({"url":url,"status":"NO_DATA","rows":0})
            continue

        # Parse the current native v6 shape first, then keep the relationship-aware
        # and recursive fallbacks for older/alternate endpoint versions.
        native_rows=[]; native_diag={}
        try:
            native_rows,native_diag=_extract_underdog_v6_flat_rows(data,url)
        except Exception as e:
            native_diag={"native_parser_error":str(e)[:220]}; native_rows=[]
        rows.extend(native_rows)

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
            explicit_market_label=_first_existing(o,["display_stat","stat_type_name","stat","title","label","name","description"])
            if not _is_full_game_market_label(explicit_market_label):
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
                "scheduled_at":o.get("scheduled_at") or o.get("starts_at") or o.get("start_time"),
                "season_type":o.get("season_type") or o.get("game_type") or o.get("event_type") or "",
            })
        rows.extend(flat_rows)

        url_rows=len(native_rows)+len(rel_rows)+len(flat_rows)
        endpoint_debug.append({"url":url,"status":"OK","rows":url_rows,"native_rows":len(native_rows),"relationship_rows":len(rel_rows),"flat_rows":len(flat_rows),**native_diag,**rel_diag})
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
    # Preserve the feed timestamp, but also store the display-local date/time so a
    # late UTC kickoff cannot be saved/graded under the wrong calendar day.
    for r in clean:
        local_dt=_display_datetime(r.get("scheduled_at"))
        if local_dt:
            r["local_game_date"]=local_dt.strftime("%Y-%m-%d")
            r["local_start_time"]=local_dt.isoformat()
            r["display_timezone"]=NFL_DISPLAY_TIMEZONE
    request_log("UNDERDOG_NFL_LIVE_PULL", "FOUND" if clean else "NO_NFL_ROWS", endpoint_debug)
    return clean[:1000]

@st.cache_data(ttl=120, show_spinner=False)
def fetch_underdog_nfl_moneylines(cache_bust=0):
    """Scan Underdog feeds for NFL moneyline/winner markets when Underdog posts them.

    Some Underdog endpoints only expose player over/under props. This function is intentionally
    defensive: it returns an empty list if moneyline-style markets are not present instead of
    creating fake prices.
    """
    rows=[]; endpoint_debug=[]; request_started=time.monotonic()
    money_terms=["moneyline", "money line", "match winner", "game winner", "winner", "to win"]
    for url in UNDERDOG_URLS:
        if time.monotonic()-request_started >= UNDERDOG_REQUEST_BUDGET_SECONDS:
            endpoint_debug.append({"url":url,"status":"REQUEST_BUDGET_EXHAUSTED","rows":0})
            break
        data=safe_get_json(url, cache_bust)
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
            american_price=safe_float(_first_existing(o,["american_price","american_odds"]))
            decimal_price=safe_float(_first_existing(o,["decimal_price","decimal_odds"]))
            payout_multiplier=safe_float(_first_existing(o,["payout_multiplier","payout"]))
            price=american_price if american_price is not None else decimal_price if decimal_price is not None else payout_multiplier
            # Preserve each price format separately. A DFS payout multiplier is not
            # presented as sportsbook moneyline odds.
            rows.append({
                "team_or_side": str(team),
                "matchup": matchup,
                "market": "Money Line",
                "price_or_payout": price if price is not None else _first_existing(o,["payout","payout_multiplier","odds","price"]),
                "american_price": american_price,
                "decimal_price": decimal_price,
                "payout_multiplier": payout_multiplier,
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
    b = _normalized_market_text(raw)
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
    """Build a current-first player identity lookup.

    Current usage/depth chart wins over old 2025 metadata.  The lookup also creates
    first-initial + last-name aliases so a live full name such as ``Bijan Robinson``
    can match a historical row stored as ``B.Robinson`` without changing the live
    player identity shown on the card.
    """
    sources = [
        (100, CURRENT_USAGE_FILE),
        (95, DEPTH_CHART_FILE),
        (85, PHASE6_PLAYER_SUMMARY_FILE),
        (80, USAGE_FILE),
        (60, PHASE6_PLAYER_LOG_FILE),
    ]
    source_sig=tuple(_path_signature(path) for _,path in sources)
    if _PHASE6_PLAYER_LOOKUP_CACHE.get("sig") == source_sig and isinstance(_PHASE6_PLAYER_LOOKUP_CACHE.get("data"),dict):
        return _PHASE6_PLAYER_LOOKUP_CACHE["data"]
    exact, by_last, by_initial_last = {}, {}, {}

    def _initial_last_key(player):
        parts=norm(player).split()
        if len(parts)<2:
            return ""
        first=re.sub(r"[^a-z]", "", parts[0])
        last=re.sub(r"[^a-z0-9]", "", parts[-1])
        return f"{first[:1]}|{last}" if first and last else ""

    def _merge_meta(old, new):
        if not old:
            return dict(new)
        # Higher priority controls team/position, but never replace useful current
        # metadata with blanks from an older file.
        if new.get("priority",0) >= old.get("priority",0):
            if new.get("priority",0) == old.get("priority",0) and new.get("record_quality",0) < old.get("record_quality",0):
                return dict(old)
            merged=dict(old)
            for k,v in new.items():
                if v not in [None, "", "NAN", "NA"]:
                    merged[k]=v
            if new.get("priority",0) == old.get("priority",0) and new.get("record_quality",0) > old.get("record_quality",0) and not new.get("position"):
                merged["position"]=""
            return merged
        merged=dict(old)
        for k in ["team","position"]:
            if merged.get(k) in [None, "", "NAN", "NA"] and new.get(k) not in [None, "", "NAN", "NA"]:
                merged[k]=new.get(k)
        return merged

    for priority, path in sources:
        try:
            if not Path(path).exists():
                continue
            df=pd.read_csv(path, usecols=lambda c: str(c).lower() in [
                "player","name","team","position","pos","recent_team","player_display_name",
                "games_played","current_games","pass_attempts_pg","rush_attempts_pg","targets_pg",
                "passing_yards_pg","rushing_yards_pg","receiving_yards_pg"
            ])
            df.columns=[str(c).strip() for c in df.columns]
            if "player_display_name" in df.columns and "player" not in df.columns:
                df["player"]=df["player_display_name"]
            if "name" in df.columns and "player" not in df.columns:
                df["player"]=df["name"]
            if "recent_team" in df.columns and "team" not in df.columns:
                df["team"]=df["recent_team"]
            if "pos" in df.columns and "position" not in df.columns:
                df["position"]=df["pos"]
            for _,r in df.iterrows():
                player=str(r.get("player") or "").strip()
                if not player or player.lower()=="nan":
                    continue
                team=_normalize_nfl_team(r.get("team"))
                pos=str(r.get("position") or "").upper().strip()
                if pos in {"NAN","NONE"}: pos=""
                meta={"player":player,"team":team,"position":pos,"priority":priority,"source":Path(path).name,"record_quality":_player_record_quality(r.to_dict())}
                key=norm(player)
                exact[key]=_merge_meta(exact.get(key),meta)
                parts=key.split()
                if parts:
                    by_last.setdefault(parts[-1],[]).append(meta)
                il=_initial_last_key(player)
                if il:
                    by_initial_last.setdefault(il,[]).append(meta)
        except Exception as e:
            request_log("PLAYER_LOOKUP", "READ_ERROR", f"{Path(path).name}: {str(e)[:160]}")

    # Deduplicate candidate banks with current/high-priority metadata first.
    for bank in [by_last, by_initial_last]:
        for k,items in list(bank.items()):
            seen=set(); clean=[]
            for item in sorted(items,key=lambda x:x.get("priority",0),reverse=True):
                sig=(norm(item.get("player")),item.get("team"),item.get("position"))
                if sig in seen: continue
                seen.add(sig); clean.append(item)
            bank[k]=clean
    result={"exact":exact,"by_last":by_last,"by_initial_last":by_initial_last}
    _PHASE6_PLAYER_LOOKUP_CACHE.update({"sig":source_sig,"data":result})
    return result

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
    """Resolve a live NFL player against current-first model metadata.

    Full-name ↔ initial/last-name matching is supported, but only within the same
    team/position when those identifiers are available.  This avoids silently
    attaching another player's historical usage to a live Underdog line.
    """
    lookup=_phase6_player_lookup()
    if not lookup.get("exact") and not lookup.get("by_last"):
        return {"player":str(player or "").strip(),"team":team or "NFL","position":position or "","model_match":False,"model_filter_disabled":True}
    raw=str(player or "").strip()
    if not raw: return None
    n=norm(raw); parts=n.split(); team_u=_normalize_nfl_team(team); pos_u=str(position or "").upper().strip()
    candidates=[]
    exact=lookup.get("exact",{}).get(n)
    if exact: candidates=[exact]
    if not candidates and len(parts)>=2:
        first=re.sub(r"[^a-z]","",parts[0]); last=re.sub(r"[^a-z0-9]","",parts[-1])
        il=f"{first[:1]}|{last}" if first and last else ""
        candidates=list(lookup.get("by_initial_last",{}).get(il,[]))
        if not candidates:
            candidates=list(lookup.get("by_last",{}).get(last,[]))
    if candidates:
        if team_u:
            tm=[m for m in candidates if _normalize_nfl_team(m.get("team"))==team_u]
            if tm: candidates=tm
        if pos_u:
            pm=[m for m in candidates if str(m.get("position") or "").upper()==pos_u]
            if pm: candidates=pm
        # Require initial agreement when more than one same-last-name player remains.
        if len(candidates)>1 and parts:
            ini=parts[0][:1]
            same=[m for m in candidates if norm(m.get("player")).split() and norm(m.get("player")).split()[0][:1]==ini]
            if same: candidates=same
        candidates=sorted(candidates,key=lambda x:x.get("priority",0),reverse=True)
        meta=candidates[0] if candidates else None
    else:
        meta=None
    if not meta:
        # Conservative near-exact fallback for punctuation/spacing differences only.
        names=list(lookup.get("exact",{}).keys())
        close=difflib.get_close_matches(n,names,n=1,cutoff=0.965)
        meta=lookup.get("exact",{}).get(close[0]) if close else None
    if not meta: return None
    meta_team=_normalize_nfl_team(meta.get("team"))
    meta_pos=str(meta.get("position") or "").upper().strip()
    # Never overwrite a reliable live team/position with conflicting historical
    # metadata. A no-match is safer and still lets the live row project at a
    # reduced data score.
    if team_u and meta_team and meta_team != team_u:
        return None
    if pos_u and meta_pos and meta_pos != pos_u:
        return None
    resolved_team=meta_team or team_u
    resolved_pos=meta_pos or pos_u
    return {"player":meta.get("player") or raw,"team":resolved_team or team or "NFL","position":resolved_pos,"model_match":True,"model_filter_disabled":False,"model_identity_source":meta.get("source")}

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

        # Live/current event identity always outranks a historical Phase 6 team label.
        # The historical model may normalize the player name/position, but it must never
        # move a player to an old franchise and create a false matchup.
        live_team = _normalize_nfl_team(row.get("team"))
        live_matchup_a, live_matchup_b = _teams_from_matchup_text(row.get("matchup"))
        meta = _resolve_model_player_strict(row.get("player"), row.get("team"), row.get("position")) if model_filter_available else None
        if meta:
            row["player"] = meta.get("player") or row.get("player")
            row["position"] = str(meta.get("position") or row.get("position") or "").upper().strip()
            meta_team = _normalize_nfl_team(meta.get("team"))
            if live_team in NFL_TEAM_ABBRS:
                row["team"] = live_team
                if meta_team and meta_team != live_team:
                    row["historical_team_mismatch"] = f"{meta_team}->{live_team}"
            elif meta_team in NFL_TEAM_ABBRS:
                row["team"] = meta_team
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
        match_a, match_b = _teams_from_matchup_text(row.get("matchup"))
        if match_a and match_b:
            if team not in {match_a, match_b}:
                dropped["bad_team"] += 1
                request_log("NFL_MAPPING_GUARD","BLOCKED",f"{row.get('player')} team={team} matchup={row.get('matchup')}")
                continue
            expected_opp = match_b if team == match_a else match_a
            if opp != expected_opp:
                row["opp"] = expected_opp
                row["opponent_corrected_from_matchup"] = True
        row["matchup_status"] = "VALID" if match_a and match_b and row.get("team") in {match_a,match_b} else "OPPONENT_PENDING"

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



def apply_market_integrity_guards(rows):
    """Attach no-bet warnings for suspicious cross-market line reuse.

    Legitimate equal numbers can happen, so rows are not deleted.  Instead a repeated
    yardage line for the same player/game across different yardage markets is marked for
    manual review and the final action layer forces PASS.  This catches feed/cache joins
    such as an RB rushing line accidentally appearing under Receiving Yards.
    """
    rows=[dict(r or {}) for r in (rows or [])]
    groups={}
    yardage={"Passing Yards","Rushing Yards","Receiving Yards"}
    for i,row in enumerate(rows):
        prop=_canon_prop_label(row.get("prop")) or row.get("prop")
        if prop not in yardage:
            continue
        line=safe_float(row.get("line"))
        if line is None:
            continue
        event=str(row.get("event_id") or row.get("game_id") or row.get("match_id") or row.get("matchup") or "")
        key=(event,norm(row.get("player")),round(float(line),3))
        groups.setdefault(key,[]).append((i,prop))
    # Same player/game duplicated as Receiving Yards with wildly separated lines is usually a feed market-map collision.
    rec_groups={}
    for i,row in enumerate(rows):
        if (_canon_prop_label(row.get("prop")) or row.get("prop"))!="Receiving Yards": continue
        event=str(row.get("event_id") or row.get("game_id") or row.get("match_id") or row.get("matchup") or "")
        rec_groups.setdefault((event,norm(row.get("player"))),[]).append((i,safe_float(row.get("line"))))
    for _,vals in rec_groups.items():
        vals=[x for x in vals if x[1] is not None]
        if len(vals)>=2:
            lo=min(v for _,v in vals); hi=max(v for _,v in vals)
            if hi-lo>=25 and hi/max(1,lo)>=1.65:
                for idx,v in vals:
                    if v==hi:
                        rows[idx]["data_integrity_block"]=f"Market mapping conflict: duplicate Receiving Yards lines {lo:g} and {hi:g}; higher line blocked"
                        rows[idx]["market_integrity_status"]="FEED_CONFLICT"
    for key,items in groups.items():
        props={p for _,p in items}
        if len(props)<2:
            continue
        # Passing/Rushing can legitimately share numbers for a QB at tiny lines, and
        # rushing/receiving can occasionally match.  Only force a review when the line
        # is large enough that cross-market cache contamination is materially plausible.
        line=key[2]
        if line < 12.5:
            continue
        reason="Market integrity conflict: same player/game line reused across " + ", ".join(sorted(props))
        for idx,_ in items:
            rows[idx]["data_integrity_block"]=reason
            rows[idx]["market_integrity_status"]="REVIEW"
    return rows


def _line_variant_text(row):
    vals=[row.get(k) for k in ["line_type","line_variant","line_title","line_status","raw_prop_label"]]
    return " ".join(str(v or "") for v in vals).lower()

def _line_is_explicit_alt(row):
    txt=_line_variant_text(row)
    return any(x in txt for x in ["alternate"," alt ","alt-line","alt line","goblin","demon","promo","boost","special","discount"])

def _primary_line_score(row, median):
    """Lower score = more likely to be Underdog's standard/main line."""
    line=safe_float(row.get("line"),0) or 0
    status=str(row.get("line_status") or "").lower()
    closed=1 if any(x in status for x in ["closed","suspend","inactive","settled"]) else 0
    explicit_alt=1 if _line_is_explicit_alt(row) or row.get("is_alt_line") is True else 0
    explicit_standard=0 if row.get("is_standard_line") is True else 1
    om=safe_float(row.get("over_multiplier")); um=safe_float(row.get("under_multiplier"))
    if om is not None or um is not None:
        mult_dev=sum(abs(x-1.0) for x in [om,um] if x is not None)
    else:
        mult_dev=0.35
    op=safe_float(row.get("over_price")); up=safe_float(row.get("under_price"))
    price_dev=(abs(op+110)+abs(up+110))/400 if op is not None and up is not None else 0.25
    central=abs(line-median)/max(1.0,abs(median))
    return (closed, explicit_alt, explicit_standard, round(mult_dev,5), round(price_dev,5), round(central,5), str(row.get("underdog_id") or ""))


def _select_primary_market_lines(rows):
    """Keep Underdog's most likely standard/main line per player + prop + event.

    Standard/default option metadata is preferred over alternate/promo lines. Median
    is only a final tie-breaker, so an alt ladder can no longer silently become the
    app's official line merely because it sits in the middle numerically.
    """
    groups={}
    for row in rows or []:
        r=dict(row or {})
        event=str(r.get("event_id") or r.get("game_id") or r.get("match_id") or r.get("matchup") or r.get("team") or "")
        key=(event,norm(r.get("player")),str(r.get("prop") or ""))
        groups.setdefault(key,[]).append(r)
    selected=[]
    for group in groups.values():
        valid=[r for r in group if safe_float(r.get("line")) is not None]
        if not valid: continue
        lines=sorted(float(r.get("line")) for r in valid)
        median=float(np.median(lines))
        non_alt=[r for r in valid if not _line_is_explicit_alt(r) and r.get("is_alt_line") is not True]
        pool=non_alt or valid
        best=min(pool,key=lambda r:_primary_line_score(r,median))
        best=dict(best); best["primary_line_selected"]=True
        best["line_selection_reason"]="standard/default metadata" if pool is non_alt and len(valid)>1 else "single/central valid line"
        best["alt_line_count"]=max(0,len(valid)-1)
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
        SAVANT_MANIFEST_FILE, PRESEASON_ROTATION_FILE, PRESEASON_PRIOR_FILE,
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
        "savant_production": bool(SAVANT_PRODUCTION_ENABLED),
        "context": _projection_context_signature(),
    }
    payload = {"rows": rows, "settings": settings}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def clear_projection_result_cache():
    """Clear section projection results after the board or model settings change."""
    for key in ["nfl_projection_cache","nfl_projection_cache_key","nfl_projection_cache_rows","nfl_projection_cache_seconds"]:
        st.session_state.pop(key,None)
    clear_savant_runtime_cache()


def _manual_row_to_board_row(row, default_prop=None):
    # Flexible column names from CSV upload or pasted tables.
    get = lambda *keys: next((row.get(k) for k in keys if k in row and _usable_context_value(row.get(k))), None)
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
    """True only when the saved database has game-usable coverage."""
    return (
        _fast_csv_data_rows(PHASE6_PLAYER_LOG_FILE,1000) >= 1000
        and _fast_csv_data_rows(PHASE6_PLAYER_SUMMARY_FILE,100) >= 100
        and _fast_csv_data_rows(PHASE6_DEFENSE_RANK_FILE,28) >= 28
        and _fast_json_items(PHASE6_TEAM_CONTEXT_FILE) >= 28
    )

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
        nflverse_url("stats_player", f"stats_player_week_{season}.csv"),
        nflverse_url("stats_player", f"stats_player_reg_{season}.csv"),
        nflverse_url("stats_player", f"stats_player_week_{season}.csv.gz"),
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
        "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
        nflverse_url("schedules", "schedules.csv"),
        nflverse_url("schedules", f"schedules_{season}.csv"),
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
    """Low-memory PBP pull.

    Only the columns required for team identity, defense, red-zone, overtime, and trench
    context are loaded. This avoids loading the several-hundred-column full PBP table into
    Railway memory while still populating the advanced Phase 6 CSV files.
    """
    season = int(season)
    cache_path = PHASE6_RAW_DIR / f"play_by_play_reduced_{season}.csv"
    if cache_path.exists() and not force_refresh:
        cached = _read_csv_cached(cache_path)
        if not cached.empty:
            request_log("NFLVERSE_PBP_REDUCED", "LOCAL_CACHE", f"rows={len(cached)}")
            return cached
    keep_cols = {
        "season","season_type","game_type","game_id","week","posteam","defteam",
        "pass_attempt","rush_attempt","penalty","penalty_yards","fumble_lost","fumble","sack","qb_hit",
        "touchdown","pass_touchdown","rush_touchdown","interception","epa","success","yards_gained","air_yards","complete_pass","down",
        "yardline_100","qtr","rusher_player_name","receiver_player_name","passer_player_name",
        "first_down","third_down_converted","third_down_failed","fourth_down_converted","fourth_down_failed",
        "field_goal_attempt","field_goal_result","punt_attempt","extra_point_attempt","two_point_attempt",
        "drive","drive_result","fixed_drive","fixed_drive_result","drive_ended_with_score","drive_first_downs","drive_play_count","drive_yards_penalized",
        "series","series_result","series_success","ydsnet","goal_to_go","shotgun","no_huddle","cp","cpoe","xpass","pass_oe",
        "home_team","away_team","total_home_score","total_away_score"
    }
    urls = [
        nflverse_url("pbp", f"play_by_play_{season}.csv.gz"),
        nflverse_url("pbp", f"play_by_play_{season}.csv"),
    ]
    last_error = ""
    for url in urls:
        try:
            df = pd.read_csv(url, usecols=lambda c: c in keep_cols, low_memory=False)
            if not df.empty:
                if "season" in df.columns:
                    df = df[df["season"].astype(str).eq(str(season))].copy()
                if "week" in df.columns:
                    df = df[pd.to_numeric(df["week"], errors="coerce").between(1, 18)].copy()
                for tcol in ["season_type","game_type"]:
                    if tcol in df.columns:
                        vals = df[tcol].astype(str).str.upper()
                        mask = vals.isin(["REG","REGULAR","REGULAR_SEASON","R"])
                        if mask.any():
                            df = df[mask].copy()
                        break
                df.to_csv(cache_path, index=False)
                request_log("NFLVERSE_PBP_REDUCED", "DOWNLOADED", f"{url} rows={len(df)}")
                return df
        except Exception as e:
            last_error = str(e)[:400]
            request_log("NFLVERSE_PBP_REDUCED", "URL_FAILED", f"{url} :: {last_error}")
    cached = _read_csv_cached(cache_path)
    if not cached.empty:
        return cached
    request_log("NFLVERSE_PBP_REDUCED", "NO_DATA", last_error)
    return pd.DataFrame()


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
    for c in ["pass_attempt","rush_attempt","penalty","penalty_yards","fumble_lost","fumble","sack","qb_hit","touchdown","pass_touchdown","rush_touchdown","interception","epa","success","yards_gained","air_yards","complete_pass","first_down","third_down_converted","third_down_failed","fourth_down_converted","fourth_down_failed","field_goal_attempt","punt_attempt","series_success","goal_to_go","shotgun","no_huddle"]:
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
    # nflfastR recommends fixed_drive/fixed_drive_result because the raw NFL drive
    # fields can be inconsistent. Prefer the fixed versions whenever available.
    if "fixed_drive" in df.columns:
        df["model_drive"] = df["fixed_drive"]
    elif "drive" in df.columns:
        df["model_drive"] = df["drive"]
    else:
        df["model_drive"] = np.nan
    if "fixed_drive_result" in df.columns:
        df["model_drive_result"] = df["fixed_drive_result"]
    elif "drive_result" in df.columns:
        df["model_drive_result"] = df["drive_result"]
    elif "series_result" in df.columns:
        df["model_drive_result"] = df["series_result"]
    else:
        df["model_drive_result"] = ""

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
            # Deep football outcome features.  Each family is later capped in the
            # Moneyline model so correlated box-score statistics cannot stack freely.
            first_downs=float(g.get("first_down", pd.Series(dtype=float)).sum()) if "first_down" in g.columns else 0.0
            third_conv=float(g.get("third_down_converted", pd.Series(dtype=float)).sum()) if "third_down_converted" in g.columns else 0.0
            third_fail=float(g.get("third_down_failed", pd.Series(dtype=float)).sum()) if "third_down_failed" in g.columns else 0.0
            fourth_conv=float(g.get("fourth_down_converted", pd.Series(dtype=float)).sum()) if "fourth_down_converted" in g.columns else 0.0
            fourth_fail=float(g.get("fourth_down_failed", pd.Series(dtype=float)).sum()) if "fourth_down_failed" in g.columns else 0.0
            rz_tds=float(rz.get("touchdown", pd.Series(dtype=float)).sum()) if not rz.empty and "touchdown" in rz.columns else 0.0
            rz_drives=max(1.0, float(rz["model_drive"].nunique()) if not rz.empty and "model_drive" in rz.columns else float(len(rz))/3.0 if len(rz) else 1.0)
            fg_att=float(g.get("field_goal_attempt", pd.Series(dtype=float)).sum()) if "field_goal_attempt" in g.columns else 0.0
            fg_made=0.0
            if "field_goal_result" in g.columns:
                fg_made=float(g["field_goal_result"].astype(str).str.upper().isin(["MADE","GOOD"]).sum())
            turnovers=(float(g.get("fumble_lost", pd.Series(dtype=float)).sum()) if "fumble_lost" in g.columns else 0.0) + (float(g.get("interception", pd.Series(dtype=float)).sum()) if "interception" in g.columns else 0.0)
            drives=max(1, int(g["model_drive"].nunique()) if "model_drive" in g.columns else int(round(gp*10.5)))
            drive_points=0.0
            if "model_drive_result" in g.columns and "model_drive" in g.columns:
                dr=g.dropna(subset=["model_drive"]).drop_duplicates(["game_id","model_drive"],keep="last") if "game_id" in g.columns else g.dropna(subset=["model_drive"]).drop_duplicates(["model_drive"],keep="last")
                if not dr.empty:
                    results=dr["model_drive_result"].astype(str).str.upper()
                    drive_points=float(results.map(lambda x: 7.0 if "TOUCHDOWN" in x or x=="TD" else 3.0 if "FIELD GOAL" in x or x in {"FG","FIELD_GOAL"} else 2.0 if "SAFETY" in x else 0.0).sum())
            # Fallback if drive-result fields are unavailable: actual offensive scoring events.
            if drive_points <= 0:
                tds=float(g.get("touchdown",pd.Series(dtype=float)).sum()) if "touchdown" in g.columns else 0.0
                drive_points=tds*7.0+fg_made*3.0
            # Deep possession / field-position / hidden-yards / game-state features.
            drive_first = pd.DataFrame()
            if "model_drive" in g.columns:
                drive_first = g.dropna(subset=["model_drive"]).drop_duplicates(["game_id","model_drive"],keep="first") if "game_id" in g.columns else g.dropna(subset=["model_drive"]).drop_duplicates(["model_drive"],keep="first")
            start_field=[]
            if not drive_first.empty and "yardline_100_num" in drive_first.columns:
                start_field=[100-float(x) for x in pd.to_numeric(drive_first["yardline_100_num"],errors="coerce").dropna().tolist()]
            avg_start_own=float(np.mean(start_field)) if start_field else None
            yards_per_drive=float(g.loc[plays_mask,"yards_gained"].sum())/max(1.0,drives) if "yards_gained" in g.columns else None
            plays_per_drive=float(plays)/max(1.0,drives)
            rz_trip_count=float(rz["model_drive"].nunique()) if not rz.empty and "model_drive" in rz.columns else float(len(rz))/3.0 if len(rz) else 0.0
            gl_drives=float(gl["model_drive"].nunique()) if not gl.empty and "model_drive" in gl.columns else float(len(gl))/2.0 if len(gl) else 0.0
            gl_tds=float(gl.get("touchdown",pd.Series(dtype=float)).sum()) if not gl.empty and "touchdown" in gl.columns else 0.0
            fg_settle=0.0
            if rz_trip_count>0 and "model_drive_result" in rz.columns:
                rz_last=rz.dropna(subset=["model_drive"]).drop_duplicates(["game_id","model_drive"],keep="last") if "game_id" in rz.columns else rz.dropna(subset=["model_drive"]).drop_duplicates(["model_drive"],keep="last")
                fg_settle=float(rz_last["model_drive_result"].astype(str).str.upper().str.contains("FIELD GOAL|FIELD_GOAL|\bFG\b",regex=True,na=False).sum())
            pressure_events=(float(g.get("qb_hit",pd.Series(dtype=float)).sum()) if "qb_hit" in g.columns else 0.0)+(float(g.get("sack",pd.Series(dtype=float)).sum()) if "sack" in g.columns else 0.0)
            pressure_allowed_rate=100*pressure_events/max(1.0,pass_att)
            sack_given_pressure=100*(float(g.get("sack",pd.Series(dtype=float)).sum()) if "sack" in g.columns else 0.0)/max(1.0,pressure_events)
            # Neutral situation = regulation, within one score before Q4 when possible.
            neutral=g
            if "score_differential" in g.columns:
                sd=pd.to_numeric(g["score_differential"],errors="coerce")
                neutral=g[sd.abs()<=8]
            if "qtr_num" in neutral.columns:
                neutral=neutral[neutral["qtr_num"]<=3]
            neutral_mask=((neutral.get("pass_attempt",0)==1)|(neutral.get("rush_attempt",0)==1)) if not neutral.empty and "pass_attempt" in neutral.columns and "rush_attempt" in neutral.columns else pd.Series(False,index=neutral.index)
            neutral_pass=float(neutral.loc[neutral_mask,"pass_attempt"].sum()) if len(neutral) and "pass_attempt" in neutral.columns else 0.0
            neutral_rush=float(neutral.loc[neutral_mask,"rush_attempt"].sum()) if len(neutral) and "rush_attempt" in neutral.columns else 0.0
            neutral_epa=float(neutral.loc[neutral_mask,"epa"].mean()) if len(neutral) and "epa" in neutral.columns and neutral_mask.any() else None
            neutral_success=100*float(neutral.loc[neutral_mask,"success"].mean()) if len(neutral) and "success" in neutral.columns and neutral_mask.any() else None
            # Hidden yards and kicking. nflverse exposes return_yards/punt_distance on many seasons.
            # Return production belongs to return_team, not posteam (the kicking
            # team is usually posteam on punt/kickoff plays).
            return_g=df.iloc[0:0]
            if "return_team" in df.columns:
                return_g=df[df["return_team"].astype(str).eq(str(team))]
            ret_yards=float(return_g.get("return_yards",pd.Series(dtype=float)).sum()) if not return_g.empty and "return_yards" in return_g.columns else 0.0
            punt_distance=float(g.loc[g.get("punt_attempt",pd.Series(0,index=g.index))==1,"punt_distance"].sum()) if "punt_distance" in g.columns and "punt_attempt" in g.columns else 0.0
            punt_ret_g=return_g[return_g.get("punt_attempt",pd.Series(0,index=return_g.index))==1] if not return_g.empty and "punt_attempt" in return_g.columns else return_g.iloc[0:0]
            punt_returns=float((pd.to_numeric(punt_ret_g.get("return_yards",pd.Series(0,index=punt_ret_g.index)),errors="coerce").fillna(0)>0).sum()) if not punt_ret_g.empty else 0.0
            punt_return_yards=float(punt_ret_g.get("return_yards",pd.Series(dtype=float)).sum()) if not punt_ret_g.empty and "return_yards" in punt_ret_g.columns else 0.0
            # Net punt is computed from the punting team's plays and therefore uses
            # opponent return yards on those same punt plays.
            opponent_punt_return_yards=float(g.loc[g.get("punt_attempt",pd.Series(0,index=g.index))==1,"return_yards"].sum()) if "punt_attempt" in g.columns and "return_yards" in g.columns else 0.0
            net_punt=(punt_distance-opponent_punt_return_yards)/max(1.0,float(g.get("punt_attempt",pd.Series(dtype=float)).sum())) if "punt_attempt" in g.columns and float(g.get("punt_attempt",pd.Series(dtype=float)).sum())>0 else None
            # Expected field-goal make rate from distance bands; used only as a special-teams residual.
            fg_xm=0.0; fg_obs=0.0
            if "field_goal_attempt" in g.columns:
                fg_rows=g[g["field_goal_attempt"]==1]
                if not fg_rows.empty:
                    fg_obs=fg_made
                    if "kick_distance" in fg_rows.columns:
                        for d in pd.to_numeric(fg_rows["kick_distance"],errors="coerce").dropna():
                            fg_xm += 0.97 if d<=29 else 0.92 if d<=39 else 0.84 if d<=49 else 0.68 if d<=59 else 0.48
                    else:
                        fg_xm=0.84*len(fg_rows)
            fg_oe=(fg_obs-fg_xm)/max(1.0,len(g[g["field_goal_attempt"]==1]) if "field_goal_attempt" in g.columns else 0)
            # Turnover regression proxy: fumbles are assumed 50/50 recovery; interceptions
            # are shrunk toward league-average rather than treated as fully repeatable.
            fumbles=float(g.get("fumble",pd.Series(dtype=float)).sum()) if "fumble" in g.columns else 0.0
            ints=float(g.get("interception",pd.Series(dtype=float)).sum()) if "interception" in g.columns else 0.0
            turnover_regression=(0.50*fumbles+0.68*ints)/gp
            fourth_go=fourth_conv+fourth_fail
            fourth_punts=float(g.loc[g["down_num"].eq(4),"punt_attempt"].sum()) if "punt_attempt" in g.columns and "down_num" in g.columns else 0.0
            fourth_fgs=float(g.loc[g["down_num"].eq(4),"field_goal_attempt"].sum()) if "field_goal_attempt" in g.columns and "down_num" in g.columns else 0.0
            fourth_decisions=fourth_go+fourth_punts+fourth_fgs
            two_pt=float(pd.to_numeric(g.get("two_point_attempt",pd.Series(dtype=float)),errors="coerce").fillna(0).sum()) if "two_point_attempt" in g.columns else 0.0
            row.update({
                "avg_drive_start_own_yardline": None if avg_start_own is None else round(avg_start_own,2),
                "yards_per_drive": None if yards_per_drive is None else round(yards_per_drive,3),
                "plays_per_drive": round(plays_per_drive,3),
                "red_zone_trips_pg": round(rz_trip_count/gp,3),
                "goal_to_go_td_rate": round(100*gl_tds/max(1.0,gl_drives),2) if gl_drives else 0,
                "red_zone_fg_settlement_rate": round(100*fg_settle/max(1.0,rz_trip_count),2) if rz_trip_count else 0,
                "pressure_allowed_rate": round(pressure_allowed_rate,2),
                "sack_given_pressure_rate": round(sack_given_pressure,2),
                "neutral_epa_per_play": None if neutral_epa is None else round(neutral_epa,4),
                "neutral_success_rate": None if neutral_success is None else round(neutral_success,2),
                "neutral_pass_rate": round(100*neutral_pass/max(1.0,neutral_pass+neutral_rush),2),
                "return_yards_pg": round(ret_yards/gp,2),
                "punt_return_yards_pg": round(punt_return_yards/gp,2),
                "punt_return_attempts_pg": round(punt_returns/gp,2),
                "net_punt_yards": None if net_punt is None else round(net_punt,2),
                "fg_over_expected_per_attempt": round(fg_oe,4),
                "turnover_regression_proxy_pg": round(turnover_regression,3),
                "fumble_lost_share": round(100*float(g.get("fumble_lost",pd.Series(dtype=float)).sum())/max(1.0,fumbles),2) if fumbles else 0,
                "explosive_40_rate": round(100*float((pd.to_numeric(g.get("yards_gained",pd.Series(0,index=g.index)),errors="coerce").fillna(0)>=40).mean()),2) if "yards_gained" in g.columns else 0,
                "fourth_down_go_rate": round(100*fourth_go/max(1.0,fourth_decisions),2) if fourth_decisions else 0,
                "two_point_attempts_pg": round(two_pt/gp,3),
            })
            row.update({
                "first_downs_pg": round(first_downs/gp,2),
                "first_down_rate": round(100*first_downs/max(1.0,float(plays)),2),
                "third_down_conversion_rate": round(100*third_conv/max(1.0,third_conv+third_fail),2),
                "fourth_down_conversion_rate": round(100*fourth_conv/max(1.0,fourth_conv+fourth_fail),2),
                "red_zone_td_rate": round(100*rz_tds/max(1.0,rz_drives),2),
                "turnovers_pg": round(turnovers/gp,2),
                "fumbles_lost_pg": round(float(g.get("fumble_lost", pd.Series(dtype=float)).sum())/gp,2) if "fumble_lost" in g.columns else 0,
                "interceptions_pg": round(float(g.get("interception", pd.Series(dtype=float)).sum())/gp,2) if "interception" in g.columns else 0,
                "penalty_yards_pg": round(float(g.get("penalty_yards", pd.Series(dtype=float)).sum())/gp,2) if "penalty_yards" in g.columns else 0,
                "field_goal_attempts_pg": round(fg_att/gp,2),
                "field_goal_made_pg": round(fg_made/gp,2),
                "field_goal_pct": round(100*fg_made/max(1.0,fg_att),2) if fg_att else 0,
                "punts_pg": round(float(g.get("punt_attempt", pd.Series(dtype=float)).sum())/gp,2) if "punt_attempt" in g.columns else 0,
                "drives_pg": round(drives/gp,2),
                "points_per_drive": round(drive_points/max(1.0,drives),3),
                "yards_per_play": round(float(g.loc[plays_mask,"yards_gained"].mean()),3) if "yards_gained" in g.columns and plays else 0,
                "series_success_rate": round(100*float(g.get("series_success",pd.Series(dtype=float)).mean()),2) if "series_success" in g.columns else 0,
                "shotgun_rate": round(100*float(g.get("shotgun",pd.Series(dtype=float)).mean()),2) if "shotgun" in g.columns else 0,
                "no_huddle_rate": round(100*float(g.get("no_huddle",pd.Series(dtype=float)).mean()),2) if "no_huddle" in g.columns else row.get("no_huddle_rate",0),
            })
            if "model_drive_result" in g.columns and "model_drive" in g.columns:
                drive_last=g.dropna(subset=["model_drive"]).drop_duplicates(["game_id","model_drive"],keep="last") if "game_id" in g.columns else g.dropna(subset=["model_drive"]).drop_duplicates(["model_drive"],keep="last")
                results=drive_last["model_drive_result"].astype(str).str.upper()
                punts=results.str.contains("PUNT",na=False)
                # A true three-and-out requires drive play count/first-down detail. Use it when present.
                if "drive_play_count" in drive_last.columns and "drive_first_downs" in drive_last.columns:
                    pc=pd.to_numeric(drive_last["drive_play_count"],errors="coerce")
                    fd=pd.to_numeric(drive_last["drive_first_downs"],errors="coerce").fillna(0)
                    three_out_like=punts & (pc<=3) & (fd<=0)
                else:
                    three_out_like=punts
                row["three_and_out_proxy_rate"]=round(100*float(three_out_like.mean()),2) if len(results) else 0
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
            first_allowed=float(g.get("first_down", pd.Series(dtype=float)).sum()) if "first_down" in g.columns else 0.0
            third_conv=float(g.get("third_down_converted", pd.Series(dtype=float)).sum()) if "third_down_converted" in g.columns else 0.0
            third_fail=float(g.get("third_down_failed", pd.Series(dtype=float)).sum()) if "third_down_failed" in g.columns else 0.0
            fourth_conv=float(g.get("fourth_down_converted", pd.Series(dtype=float)).sum()) if "fourth_down_converted" in g.columns else 0.0
            fourth_fail=float(g.get("fourth_down_failed", pd.Series(dtype=float)).sum()) if "fourth_down_failed" in g.columns else 0.0
            rz=g[g["yardline_100_num"].between(1,20,inclusive="both")] if "yardline_100_num" in g.columns else g.iloc[0:0]
            rz_tds=float(rz.get("touchdown",pd.Series(dtype=float)).sum()) if not rz.empty and "touchdown" in rz.columns else 0.0
            rz_drives=max(1.0,float(rz["model_drive"].nunique()) if not rz.empty and "model_drive" in rz.columns else float(len(rz))/3.0 if len(rz) else 1.0)
            takeaways=(float(g.get("fumble_lost",pd.Series(dtype=float)).sum()) if "fumble_lost" in g.columns else 0.0)+(float(g.get("interception",pd.Series(dtype=float)).sum()) if "interception" in g.columns else 0.0)
            def_pass_att=float(pass_mask.sum())
            def_pressure_events=(float(g.get("qb_hit",pd.Series(dtype=float)).sum()) if "qb_hit" in g.columns else 0.0)+(float(g.get("sack",pd.Series(dtype=float)).sum()) if "sack" in g.columns else 0.0)
            def_pressure_rate=100*def_pressure_events/max(1.0,def_pass_att)
            def_sack_given_pressure=100*(float(g.get("sack",pd.Series(dtype=float)).sum()) if "sack" in g.columns else 0.0)/max(1.0,def_pressure_events)
            def_rz_trips=float(rz["model_drive"].nunique()) if not rz.empty and "model_drive" in rz.columns else float(len(rz))/3.0 if len(rz) else 0.0
            def_neutral=g
            if "score_differential" in g.columns:
                sd=pd.to_numeric(g["score_differential"],errors="coerce")
                def_neutral=g[sd.abs()<=8]
            if "qtr_num" in def_neutral.columns:
                def_neutral=def_neutral[def_neutral["qtr_num"]<=3]
            dn_mask=((def_neutral.get("pass_attempt",0)==1)|(def_neutral.get("rush_attempt",0)==1)) if not def_neutral.empty and "pass_attempt" in def_neutral.columns and "rush_attempt" in def_neutral.columns else pd.Series(False,index=def_neutral.index)
            def_neutral_epa=float(def_neutral.loc[dn_mask,"epa"].mean()) if len(def_neutral) and "epa" in def_neutral.columns and dn_mask.any() else None
            def_neutral_success=100*float(def_neutral.loc[dn_mask,"success"].mean()) if len(def_neutral) and "success" in def_neutral.columns and dn_mask.any() else None
            row.update({
                "def_pressure_rate":round(def_pressure_rate,2),
                "def_sack_given_pressure_rate":round(def_sack_given_pressure,2),
                "def_red_zone_trips_allowed_pg":round(def_rz_trips/gp,3),
                "def_neutral_epa_allowed":None if def_neutral_epa is None else round(def_neutral_epa,4),
                "def_neutral_success_allowed_rate":None if def_neutral_success is None else round(def_neutral_success,2),
                "def_explosive_40_allowed_rate":round(100*float((pd.to_numeric(g.get("yards_gained",pd.Series(0,index=g.index)),errors="coerce").fillna(0)>=40).mean()),2) if "yards_gained" in g.columns else 0,
            })
            row.update({
                "def_first_downs_allowed_pg":round(first_allowed/gp,2),
                "def_first_down_rate_allowed":round(100*first_allowed/max(1.0,float(len(g))),2),
                "def_third_down_allowed_rate":round(100*third_conv/max(1.0,third_conv+third_fail),2),
                "def_fourth_down_allowed_rate":round(100*fourth_conv/max(1.0,fourth_conv+fourth_fail),2),
                "def_red_zone_td_allowed_rate":round(100*rz_tds/max(1.0,rz_drives),2),
                "def_takeaways_pg":round(takeaways/gp,2),
                "def_penalty_first_down_proxy_pg":round(float(g.get("penalty",pd.Series(dtype=float)).sum())/gp,2) if "penalty" in g.columns else 0,
                "def_yards_per_play_allowed":round(float(g.get("yards_gained",pd.Series(dtype=float)).mean()),3) if "yards_gained" in g.columns else 0,
            })
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
    enable_full_pbp = str(os.getenv("NFL_ENABLE_FULL_PBP_BUILD", "1")).strip().lower() in {"1","true","yes","on"}
    pbp = fetch_nflverse_pbp(season, force_refresh=force_refresh) if enable_full_pbp else pd.DataFrame()
    if not enable_full_pbp:
        request_log("PHASE6_PBP", "SKIPPED", "Low-memory PBP build disabled by NFL_ENABLE_FULL_PBP_BUILD=0.")

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

    # Build one player/team summary. Weekly feeds can assign a one-game QB label
    # to a non-QB and previously that split the same player into duplicate rows.
    group_cols = ["player","team"]
    sum_cols = _phase6_sum_cols(logs, numeric_cols + ["offense_snaps","defense_snaps","st_snaps"])
    summary = logs.groupby(group_cols, dropna=False)[sum_cols].sum(numeric_only=True).reset_index() if sum_cols else logs[group_cols].drop_duplicates()
    games = logs.groupby(group_cols, dropna=False)["week"].nunique().reset_index(name="games_played") if "week" in logs.columns else summary[group_cols].assign(games_played=17)
    summary = summary.merge(games, on=group_cols, how="left")
    position_rows=[]
    for keys,g in logs.groupby(group_cols,dropna=False):
        player,team=keys if isinstance(keys,tuple) else (keys,"")
        def _group_total(column):
            return float(pd.to_numeric(g[column],errors="coerce").fillna(0).sum()) if column in g.columns else 0.0
        attempts=_group_total("attempts")
        carries=_group_total("carries")
        targets=_group_total("targets")
        positions=[str(x).upper().strip() for x in g.get("position",pd.Series(dtype=str)).tolist() if str(x).upper().strip() in {"QB","RB","FB","WR","TE","K"}]
        non_qb=[x for x in positions if x != "QB"]
        if attempts >= 20:
            position="QB"
        elif non_qb:
            position=max(set(non_qb),key=non_qb.count)
        elif carries >= 10 and carries >= targets:
            position="RB"
        elif targets >= 3:
            position="WR"
        else:
            position=positions[0] if positions else ""
        position_rows.append({"player":player,"team":team,"position":position})
    if position_rows:
        summary=summary.merge(pd.DataFrame(position_rows),on=group_cols,how="left")
    elif "position" not in summary.columns:
        summary["position"]=""
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
    defense_path = db_dir / "nfl_defense_ranks_last_season.csv"
    team_advanced_path = db_dir / "nfl_team_advanced_last_season.csv"
    if not _phase6_file_has_rows(summary_path, 100):
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
        "player_log_rows": int(_fast_csv_data_rows(logs_path,1000)),
        "team_context_teams": int(len(team_context)) if isinstance(team_context, dict) else 0,
        "defense_rows": int(_fast_csv_data_rows(defense_path,28)),
        "team_advanced_rows": int(_fast_csv_data_rows(team_advanced_path,28)),
    }
    quality["minimums"]={"non_zero_offensive_rows":100,"player_log_rows":1000,"team_context_teams":28,"defense_rows":28,"team_advanced_rows":28}
    ok = (
        quality["non_zero_offensive_rows"] >= 100
        and quality["player_log_rows"] >= 1000
        and quality["team_context_teams"] >= 28
        and quality["defense_rows"] >= 28
        and quality["team_advanced_rows"] >= 28
    )
    if not ok:
        quality["reason"] = "database_present_but_low_quality_or_mostly_zero"
    return ok, quality


def projection_database_readiness():
    """Audit blocking model data separately from advisory injury coverage."""
    paths=[
        PHASE6_PLAYER_LOG_FILE,PHASE6_PLAYER_SUMMARY_FILE,PHASE6_DEFENSE_RANK_FILE,
        PHASE6_TEAM_ADVANCED_FILE,PHASE6_TEAM_CONTEXT_FILE,USAGE_FILE,
        DEPTH_CHART_FILE,INJURY_FILE,
    ]
    sig=tuple(_path_signature(path) for path in paths)
    if _PROJECTION_READINESS_CACHE.get("sig")==sig and isinstance(_PROJECTION_READINESS_CACHE.get("data"),dict):
        return _PROJECTION_READINESS_CACHE["data"]
    counts={
        "player_logs":_fast_csv_data_rows(PHASE6_PLAYER_LOG_FILE,1000),
        "player_summary":_fast_csv_data_rows(PHASE6_PLAYER_SUMMARY_FILE,100),
        "defense_teams":_fast_csv_data_rows(PHASE6_DEFENSE_RANK_FILE,28),
        "team_advanced":_fast_csv_data_rows(PHASE6_TEAM_ADVANCED_FILE,28),
        "team_context":_fast_json_items(PHASE6_TEAM_CONTEXT_FILE),
        "player_usage":_fast_csv_data_rows(USAGE_FILE,100),
        "depth_chart":_fast_csv_data_rows(DEPTH_CHART_FILE,100),
        "injury_players":_fast_json_items(INJURY_FILE),
    }
    minimums={
        "player_logs":1000,"player_summary":100,"defense_teams":28,
        "team_advanced":28,"team_context":28,"player_usage":100,
        "depth_chart":100,
    }
    missing=[f"{key} {counts[key]}/{minimum}" for key,minimum in minimums.items() if counts[key]<minimum]
    advisory_minimums={"injury_players":100}
    warnings=[
        f"{key} {counts[key]}/{minimum} (advisory only)"
        for key,minimum in advisory_minimums.items() if counts[key]<minimum
    ]
    data={
        "ready":not missing,
        "status":"GAME READY" if not missing else "BLOCKED",
        "counts":counts,
        "minimums":minimums,
        "advisory_minimums":advisory_minimums,
        "missing":missing,
        "warnings":warnings,
        "checked_at":now_iso(),
    }
    _PROJECTION_READINESS_CACHE.update({"sig":sig,"data":data})
    return data


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
    """Export the complete app-ready nfl_engine data folder."""
    PHASE6_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = PHASE6_DIR / "nfl_full_data_pack_2026.zip"
    root_files = [
        USAGE_FILE, TEAM_CONTEXT_FILE, INJURY_FILE, DEPTH_CHART_FILE, WEATHER_FILE,
        MARKET_CONTEXT_FILE, CURRENT_USAGE_FILE, CURRENT_TEAM_CONTEXT_FILE,
        TRAVEL_CONTEXT_FILE, MATCHUP_CONTEXT_FILE, QB_CONTEXT_FILE, DEF_INJURY_FILE,
        SPLITS_CONTEXT_FILE, PERSONNEL_CONTEXT_FILE, API_CONFIG_FILE,
        FINAL_INACTIVES_FILE, MANUAL_OVERRIDE_FILE,
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in PHASE6_DIR.rglob("*"):
            if file.is_file() and file.resolve() != zip_path.resolve():
                rel = file.relative_to(PHASE6_DIR)
                z.write(file, arcname=str(Path("nfl_engine") / "phase6_nfl_database" / rel))
        for file in root_files:
            if Path(file).exists():
                z.write(file, arcname=str(Path("nfl_engine") / Path(file).name))
        readme = (
            "NFL full data pack\n"
            f"Created: {now_iso()}\n"
            f"Historical baseline season: {NFL_LAST_SEASON}\n"
            f"Current roster/depth season: {NFL_CURRENT_SEASON}\n\n"
            "Unzip at the repository root so the nfl_engine folder is preserved.\n"
            "Market, weather, defensive-injury, and final-inactive hooks must update on slate day.\n"
        )
        z.writestr("README_DATA_PACK.txt", readme)
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



# ---------- Current-season full data-pack builder ----------
def _first_existing_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


def _series_from_candidates(df, names, default=""):
    col = _first_existing_col(df, names)
    if col is None:
        return pd.Series([default] * len(df), index=df.index)
    return df[col]


def fetch_nflverse_rosters(season=NFL_CURRENT_SEASON, force_refresh=False):
    season = int(season)
    urls = [
        nflverse_url("rosters", f"roster_{season}.csv"),
        nflverse_url("rosters", f"roster_{season}.csv.gz"),
    ]
    return _download_csv_with_persistent_cache(
        "NFLVERSE_ROSTERS", urls, f"roster_{season}.csv", force_refresh
    )


def fetch_nflverse_depth_charts(season=NFL_CURRENT_SEASON, force_refresh=False):
    season = int(season)
    urls = [
        nflverse_url("depth_charts", f"depth_charts_{season}.csv"),
        nflverse_url("depth_charts", f"depth_charts_{season}.csv.gz"),
    ]
    return _download_csv_with_persistent_cache(
        "NFLVERSE_DEPTH_CHARTS", urls, f"depth_charts_{season}.csv", force_refresh
    )


def _normalize_roster_frame(roster):
    if roster is None or roster.empty:
        return pd.DataFrame(columns=[
            "player","team","position","status","injury_status","practice_status",
            "years_exp","rookie_flag","player_id"
        ])
    out = pd.DataFrame(index=roster.index)
    out["player"] = _series_from_candidates(
        roster, ["full_name","player_name","display_name","football_name","name","gsis_name"]
    ).fillna("").astype(str).str.strip()
    first = _series_from_candidates(roster, ["first_name"], "").fillna("").astype(str).str.strip()
    last = _series_from_candidates(roster, ["last_name"], "").fillna("").astype(str).str.strip()
    missing_name = out["player"].eq("")
    out.loc[missing_name, "player"] = (first + " " + last).str.strip()[missing_name]
    out["team"] = _series_from_candidates(
        roster, ["team","recent_team","club_code","club","team_abbr"]
    ).fillna("").astype(str).str.upper().str.strip()
    out["position"] = _series_from_candidates(
        roster, ["position","position_group","depth_chart_position","pos_abb"]
    ).fillna("").astype(str).str.upper().str.strip()
    out["status"] = _series_from_candidates(
        roster, ["status","roster_status","active_status"]
    ).fillna("").astype(str).str.upper().str.strip()
    out["injury_status"] = _series_from_candidates(
        roster, ["injury_status","injury_designation","injury"]
    ).fillna("").astype(str).str.upper().str.strip()
    out["practice_status"] = _series_from_candidates(
        roster, ["practice_status","practice_participation"]
    ).fillna("").astype(str).str.upper().str.strip()
    out["years_exp"] = pd.to_numeric(
        _series_from_candidates(roster, ["years_exp","years_of_experience","experience"], np.nan),
        errors="coerce",
    )
    out["rookie_flag"] = (out["years_exp"].fillna(1) <= 0).astype(int)
    out["player_id"] = _series_from_candidates(
        roster, ["gsis_id","player_id","sleeper_id","espn_id","pfr_id"]
    ).fillna("").astype(str)
    out = out[out["player"].ne("") & out["team"].isin(NFL_TEAM_ABBRS)].copy()
    out["_name_key"] = out["player"].map(norm)
    out = out.drop_duplicates(["_name_key","team","position"], keep="last")
    return out.reset_index(drop=True)


def _normalize_depth_frame(depth, roster_norm):
    if depth is None or depth.empty:
        base = roster_norm.copy()
        if base.empty:
            return pd.DataFrame(columns=[
                "player","team","position","depth_rank","starter","role","slot_role",
                "expected_routes","expected_targets","expected_attempts","expected_carries",
                "qb_change_risk","role_note","updated_at"
            ])
        base["depth_rank"] = np.nan
    else:
        base = pd.DataFrame(index=depth.index)
        base["player"] = _series_from_candidates(
            depth, ["full_name","player_name","display_name","football_name","name","gsis_name"]
        ).fillna("").astype(str).str.strip()
        first = _series_from_candidates(depth, ["first_name"], "").fillna("").astype(str).str.strip()
        last = _series_from_candidates(depth, ["last_name"], "").fillna("").astype(str).str.strip()
        missing = base["player"].eq("")
        base.loc[missing, "player"] = (first + " " + last).str.strip()[missing]
        base["team"] = _series_from_candidates(
            depth, ["club_code","team","recent_team","club","team_abbr"]
        ).fillna("").astype(str).str.upper().str.strip()
        base["position"] = _series_from_candidates(
            depth, ["position","pos_abb","depth_position","position_group"]
        ).fillna("").astype(str).str.upper().str.strip()
        rank_raw = _series_from_candidates(
            depth, ["depth_team","depth_rank","depth_chart_order","depth_chart_position","rank"], np.nan
        )
        base["depth_rank"] = pd.to_numeric(rank_raw, errors="coerce")
        base = base[base["player"].ne("") & base["team"].isin(NFL_TEAM_ABBRS)].copy()

    # Fill missing depth ranks within team/position. This is ordering metadata, not a projection.
    base["_name_key"] = base["player"].map(norm)
    if not roster_norm.empty:
        roster_small = roster_norm[["_name_key","team","position"]].drop_duplicates("_name_key")
        base = base.merge(roster_small, on="_name_key", how="left", suffixes=("","_roster"))
        base["team"] = base["team"].replace("", np.nan).fillna(base.get("team_roster"))
        base["position"] = base["position"].replace("", np.nan).fillna(base.get("position_roster"))
        base = base.drop(columns=[c for c in ["team_roster","position_roster"] if c in base.columns])
    base["depth_rank"] = base.groupby(["team","position"])["depth_rank"].transform(
        lambda x: x.fillna(pd.Series(range(1, len(x)+1), index=x.index))
    )
    base["depth_rank"] = pd.to_numeric(base["depth_rank"], errors="coerce").fillna(9).astype(int)
    base["starter"] = (base["depth_rank"] == 1).astype(int)
    base["role"] = np.where(base["starter"].eq(1), "STARTER", "BACKUP")
    base["slot_role"] = ""
    for c in ["expected_routes","expected_targets","expected_attempts","expected_carries"]:
        base[c] = np.nan
    base["qb_change_risk"] = np.where(
        base["position"].eq("QB") & base["depth_rank"].gt(1), "HIGH", "LOW"
    )
    base["role_note"] = "Current-season depth chart; verify official game-day inactives."
    base["updated_at"] = now_iso()
    cols = [
        "player","team","position","depth_rank","starter","role","slot_role",
        "expected_routes","expected_targets","expected_attempts","expected_carries",
        "qb_change_risk","role_note","updated_at"
    ]
    return base[cols].drop_duplicates(["player","team","position"], keep="first").reset_index(drop=True)


def _last_n_player_means(logs, n=5):
    if logs is None or logs.empty or "player" not in logs.columns:
        return pd.DataFrame()
    work = logs.copy()
    work["_name_key"] = work["player"].map(norm)
    sort_cols = [c for c in ["season","week"] if c in work.columns]
    if sort_cols:
        work = work.sort_values(sort_cols)
    work = work.groupby("_name_key", group_keys=False).tail(n)
    numeric = work.select_dtypes(include=[np.number]).columns.tolist()
    numeric = [c for c in numeric if c not in ["season","week"]]
    if not numeric:
        return pd.DataFrame()
    return work.groupby("_name_key")[numeric].mean().reset_index()


def _write_csv_template(path, columns):
    path = Path(path)
    if path.exists() and path.stat().st_size > 10:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=columns).to_csv(path, index=False)


def _write_json_template(path, payload=None):
    path = Path(path)
    if path.exists() and path.stat().st_size > 2:
        return
    save_json(path, payload if payload is not None else {})


def build_current_season_data_files(current_season=NFL_CURRENT_SEASON, force_refresh=False):
    """Create every root-level CSV/JSON used by the projection engine.

    Historical performance comes from the completed prior season. Current roster and depth
    metadata come from the selected current season. Live markets, weather, injuries, and final
    inactives are created as safe empty hooks because they must be refreshed on slate day.
    """
    current_season = int(current_season)
    roster_raw = fetch_nflverse_rosters(current_season, force_refresh=force_refresh)
    depth_raw = fetch_nflverse_depth_charts(current_season, force_refresh=force_refresh)
    schedules = fetch_nflverse_schedules(current_season, force_refresh=force_refresh)
    roster = _normalize_roster_frame(roster_raw)
    depth = _normalize_depth_frame(depth_raw, roster)
    depth.to_csv(DEPTH_CHART_FILE, index=False)

    summary = _read_optional_csv(PHASE6_PLAYER_SUMMARY_FILE)
    logs = _read_optional_csv(PHASE6_PLAYER_LOG_FILE)
    if summary.empty and USAGE_FILE.exists():
        summary = _read_optional_csv(USAGE_FILE)
    if not summary.empty:
        summary = summary.copy()
        summary["_name_key"] = summary["player"].map(norm)
        summary["_context_quality"] = summary.apply(lambda r:_player_record_quality(r.to_dict()),axis=1)
        summary = summary.sort_values("_context_quality",ascending=False).drop_duplicates("_name_key",keep="first")
    last5 = _last_n_player_means(logs, 5)

    offensive_positions = {"QB","RB","WR","TE","FB","K"}
    player_base = roster[roster["position"].isin(offensive_positions)].copy()
    if player_base.empty and not summary.empty:
        player_base = summary[[c for c in ["player","team","position"] if c in summary.columns]].copy()
        player_base["_name_key"] = player_base["player"].map(norm)
        player_base["rookie_flag"] = 0
        player_base["status"] = "LAST_SEASON_BASELINE"
        player_base["injury_status"] = ""
        player_base["practice_status"] = ""
    if not summary.empty and not player_base.empty:
        keep = [c for c in summary.columns if c != "team" and c != "position"]
        player_base = player_base.merge(summary[keep], on="_name_key", how="left", suffixes=("","_last"))
    if not last5.empty and not player_base.empty:
        player_base = player_base.merge(last5, on="_name_key", how="left", suffixes=("","_last5"))

    current_columns = [
        "player","team","position","snap_share","route_participation","target_share",
        "air_yards_share","red_zone_touch_share","targets_pg","receptions_pg",
        "pass_attempts_pg","completions_pg","receiving_yards_pg","passing_yards_pg",
        "rush_attempts_pg","rushing_yards_pg","yards_per_carry","current_games",
        "last5_targets_pg","last5_receptions_pg","last5_pass_attempts_pg",
        "last5_completions_pg","last5_rush_attempts_pg","source_season","updated_at"
    ]
    if player_base.empty:
        current_usage = pd.DataFrame(columns=current_columns)
    else:
        current_usage = pd.DataFrame(index=player_base.index)
        for c in ["player","team","position"]:
            current_usage[c] = player_base.get(c, "")
        for c in [
            "snap_share","route_participation","target_share","air_yards_share",
            "red_zone_touch_share","targets_pg","receptions_pg","pass_attempts_pg",
            "completions_pg","receiving_yards_pg","passing_yards_pg","rush_attempts_pg",
            "rushing_yards_pg","yards_per_carry"
        ]:
            current_usage[c] = pd.to_numeric(player_base.get(c, np.nan), errors="coerce")
        current_usage["current_games"] = 0
        for src, dst in [
            ("targets","last5_targets_pg"),("receptions","last5_receptions_pg"),
            ("attempts","last5_pass_attempts_pg"),("completions","last5_completions_pg"),
            ("carries","last5_rush_attempts_pg")
        ]:
            candidate = f"{src}_last5" if f"{src}_last5" in player_base.columns else src
            current_usage[dst] = pd.to_numeric(player_base.get(candidate, np.nan), errors="coerce")
        current_usage["source_season"] = NFL_LAST_SEASON
        current_usage["updated_at"] = now_iso()
        current_usage = current_usage[current_columns].drop_duplicates(["player","team"], keep="first")
    current_usage.to_csv(CURRENT_USAGE_FILE, index=False)

    # Keep the main usage file as the completed-season baseline created by Phase 6.
    if not USAGE_FILE.exists() and not summary.empty:
        app_usage_cols = [c for c in [
            "player","team","position","snap_share","route_participation","target_share",
            "air_yards_share","red_zone_touch_share","red_zone_carries","red_zone_targets",
            "goal_line_touches","targets_pg","receptions_pg","rush_attempts_pg","carries_share",
            "pass_attempts_pg","passing_yards_pg","rushing_yards_pg","receiving_yards_pg",
            "fantasy_points_pg"
        ] if c in summary.columns]
        if app_usage_cols:
            summary[app_usage_cols].to_csv(USAGE_FILE, index=False)

    # Current injury hook from roster metadata; live game-week updates may overwrite it.
    injuries = {}
    for _, r in roster.iterrows():
        status = str(r.get("injury_status") or "").strip()
        practice = str(r.get("practice_status") or "").strip()
        roster_status = str(r.get("status") or "").strip()
        if status or practice or (roster_status and roster_status not in {"ACTIVE","ACT"}):
            injuries[norm(r.get("player"))] = {
                "player": r.get("player"), "team": r.get("team"),
                "status": status or roster_status, "practice_status": practice,
                "updated_at": now_iso(), "source": "current_roster_metadata"
            }
    save_json(INJURY_FILE, injuries)

    # Quarterback context.
    qb_rows = []
    for _, r in depth[depth["position"].eq("QB")].iterrows():
        qb_rows.append({
            "team": r.get("team"), "player": r.get("player"),
            "qb_status": "STARTER" if int(r.get("depth_rank") or 9) == 1 else "BACKUP",
            "qb_name": r.get("player"),
            "qb_change_risk": "LOW" if int(r.get("depth_rank") or 9) == 1 else "HIGH",
            "qb_injury_status": injuries.get(norm(r.get("player")), {}).get("status", ""),
            "qb_blitz_grade": np.nan, "qb_pressure_grade": np.nan,
            "qb_deep_accuracy": np.nan,
            "qb_receiver_quality_note": "Verify starter and game-day status.",
            "updated_at": now_iso(),
        })
    pd.DataFrame(qb_rows, columns=[
        "team","player","qb_status","qb_name","qb_change_risk","qb_injury_status",
        "qb_blitz_grade","qb_pressure_grade","qb_deep_accuracy",
        "qb_receiver_quality_note","updated_at"
    ]).to_csv(QB_CONTEXT_FILE, index=False)

    # Player splits start neutral; they can be learned from graded history without inventing effects.
    split_rows = []
    for _, r in player_base.iterrows() if not player_base.empty else []:
        split_rows.append({
            "player": r.get("player"), "prop": "ALL", "team": r.get("team"),
            "indoor_factor": 1.0, "dome_factor": 1.0, "turf_factor": 1.0,
            "grass_factor": 1.0, "home_factor": 1.0, "away_factor": 1.0,
            "rookie_flag": int(safe_float(r.get("rookie_flag"), 0) or 0),
            "updated_at": now_iso(), "data_status": "NEUTRAL_UNTIL_GRADED"
        })
    pd.DataFrame(split_rows, columns=[
        "player","prop","team","indoor_factor","dome_factor","turf_factor",
        "grass_factor","home_factor","away_factor","rookie_flag","updated_at","data_status"
    ]).to_csv(SPLITS_CONTEXT_FILE, index=False)

    # Current team context starts with the real completed-season team baseline.
    team_context = load_json(PHASE6_TEAM_CONTEXT_FILE, {}) or load_json(TEAM_CONTEXT_FILE, {})
    if isinstance(team_context, dict):
        current_team_context = {}
        for team, ctx in team_context.items():
            if team not in NFL_TEAM_ABBRS:
                continue
            item = dict(ctx) if isinstance(ctx, dict) else {}
            item.update({"team": team, "source_season": NFL_LAST_SEASON, "current_season": current_season, "updated_at": now_iso()})
            current_team_context[team] = item
        save_json(CURRENT_TEAM_CONTEXT_FILE, current_team_context)
        if not TEAM_CONTEXT_FILE.exists() or not load_json(TEAM_CONTEXT_FILE, {}):
            save_json(TEAM_CONTEXT_FILE, team_context)

    defense = _read_optional_csv(PHASE6_DEFENSE_RANK_FILE)
    defense_map = {}
    if not defense.empty and "team" in defense.columns:
        defense_map = {str(r.get("team")): r.to_dict() for _, r in defense.iterrows()}

    # 2026 schedule-based travel and matchup rows.
    travel_rows, matchup_rows, personnel_rows = [], [], []
    if schedules is not None and not schedules.empty:
        if "season" in schedules.columns:
            schedules = schedules[schedules["season"].astype(str).eq(str(current_season))].copy()
        for _, g in schedules.iterrows():
            away = str(g.get("away_team") or "").upper().strip()
            home = str(g.get("home_team") or "").upper().strip()
            if away not in NFL_TEAM_ABBRS or home not in NFL_TEAM_ABBRS:
                continue
            matchup = f"{away} @ {home}"
            miles = great_circle_miles(away, home)
            stadium_text = str(g.get("stadium") or "")
            location_text = str(g.get("location") or "")
            international = int(any(x in stadium_text.lower() for x in ["london","wembley","tottenham","munich","frankfurt","madrid","dublin","mexico","sao paulo","berlin"]))
            neutral = int(location_text.lower() not in {"home","","nan"})
            for team, opp, is_away in [(away, home, True), (home, away, False)]:
                team_miles = miles if is_away else 0.0
                rest = g.get("away_rest") if is_away else g.get("home_rest")
                opp_rest = g.get("home_rest") if is_away else g.get("away_rest")
                travel_rows.append({
                    "team": team, "opp": opp, "matchup": matchup,
                    "season": current_season, "week": g.get("week"),
                    "game_date": g.get("gameday"), "travel_miles": team_miles,
                    "rest_days": rest, "opp_rest_days": opp_rest,
                    "timezone_shift": np.nan, "consecutive_road_games": np.nan,
                    "international_game": international, "neutral_site": neutral,
                    "body_clock_risk": "HIGH" if (team_miles or 0) >= 2000 else "MEDIUM" if (team_miles or 0) >= 1000 else "LOW",
                    "divisional_game": g.get("div_game"), "rematch_game": np.nan,
                    "stadium": stadium_text, "roof": g.get("roof"), "surface": g.get("surface"),
                    "updated_at": now_iso(),
                })
                opp_def = defense_map.get(opp, {})
                match_row = {
                    "team": team, "opp": opp, "matchup": matchup,
                    "season": current_season, "week": g.get("week"),
                    "pass_funnel": np.nan, "run_funnel": np.nan, "slot_weakness": np.nan,
                    "te_weakness": np.nan, "rb_receiving_weakness": np.nan,
                    "shadow_corner": "", "shadow_corner_grade": np.nan,
                    "blitz_rate": opp_def.get("blitz_rate"), "man_rate": opp_def.get("man_rate"),
                    "zone_rate": opp_def.get("zone_rate"),
                    "qb_pass_protection_rank": np.nan,
                    "opp_def_pressure_rank": opp_def.get("def_pressure_rank"),
                    "def_pass_rank": opp_def.get("def_pass_rank"),
                    "def_run_rank": opp_def.get("def_run_rank"),
                    "def_role_rank": opp_def.get("def_role_rank"),
                    "def_run_stop_rank": opp_def.get("def_run_stop_rank"),
                    "updated_at": now_iso(),
                }
                matchup_rows.append(match_row)
                personnel_rows.append({
                    "team": team, "opp": opp, "matchup": matchup,
                    "shadow_corner": "", "shadow_corner_grade": np.nan,
                    "slot_weakness": np.nan, "te_weakness": np.nan,
                    "rb_receiving_weakness": np.nan, "edge_rush_advantage": np.nan,
                    "interior_run_stuffer_missing": np.nan,
                    "updated_at": now_iso(), "data_status": "AWAITING_GAME_WEEK_PERSONNEL"
                })
    pd.DataFrame(travel_rows, columns=[
        "team","opp","matchup","season","week","game_date","travel_miles","rest_days",
        "opp_rest_days","timezone_shift","consecutive_road_games","international_game",
        "neutral_site","body_clock_risk","divisional_game","rematch_game","stadium","roof",
        "surface","updated_at"
    ]).to_csv(TRAVEL_CONTEXT_FILE, index=False)
    pd.DataFrame(matchup_rows, columns=[
        "team","opp","matchup","season","week","pass_funnel","run_funnel","slot_weakness",
        "te_weakness","rb_receiving_weakness","shadow_corner","shadow_corner_grade","blitz_rate",
        "man_rate","zone_rate","qb_pass_protection_rank","opp_def_pressure_rank","def_pass_rank",
        "def_run_rank","def_role_rank","def_run_stop_rank","updated_at"
    ]).to_csv(MATCHUP_CONTEXT_FILE, index=False)
    pd.DataFrame(personnel_rows, columns=[
        "team","opp","matchup","shadow_corner","shadow_corner_grade","slot_weakness",
        "te_weakness","rb_receiving_weakness","edge_rush_advantage",
        "interior_run_stuffer_missing","updated_at","data_status"
    ]).to_csv(PERSONNEL_CONTEXT_FILE, index=False)

    # Live/slate-day hooks: exact schemas, intentionally not filled with guessed values.
    _write_csv_template(MARKET_CONTEXT_FILE, [
        "player","team","prop","consensus_line","best_line","open_line","close_line",
        "market_prob_over","market_prob_under","market_books","line_move","updated_at"
    ])
    _write_json_template(WEATHER_FILE, {})
    _write_json_template(DEF_INJURY_FILE, {})
    _write_json_template(FINAL_INACTIVES_FILE, {})
    _write_json_template(MANUAL_OVERRIDE_FILE, {})
    _write_json_template(API_CONFIG_FILE, {
        "updated_at": now_iso(),
        "note": "Add licensed/live injury, weather, and market endpoints here when available.",
        "nflverse_player_stats": "stats_player release",
        "nflverse_rosters": "rosters release",
        "nflverse_depth_charts": "depth_charts release",
    })

    # Ensure optional Phase 6 files have visible schemas even when full PBP is disabled.
    _write_csv_template(PHASE6_TEAM_ADVANCED_FILE, [
        "team","plays_pg","pass_rate","rush_rate","pass_attempts_pg","rush_attempts_pg","source_season"
    ])
    _write_csv_template(PHASE6_TRENCH_FILE, [
        "team","pressure_rate","sack_rate","stuff_rate","yards_before_contact","source_season"
    ])
    _write_csv_template(PHASE6_RED_ZONE_FILE, [
        "team","player","red_zone_touch_share","red_zone_carries","red_zone_targets",
        "red_zone_pass_attempts","goal_line_touches","source_season"
    ])
    _write_csv_template(PHASE6_OT_FILE, ["team","games","overtime_games","overtime_rate","source_season"])

    result = {
        "status": "CURRENT_DATA_FILES_READY",
        "current_season": current_season,
        "baseline_season": NFL_LAST_SEASON,
        "roster_rows": int(len(roster)),
        "depth_rows": int(len(depth)),
        "current_usage_rows": int(len(current_usage)),
        "schedule_rows": int(len(schedules)) if schedules is not None else 0,
        "travel_rows": int(len(travel_rows)),
        "matchup_rows": int(len(matchup_rows)),
        "injury_rows": int(len(injuries)),
        "live_hooks": [
            MARKET_CONTEXT_FILE.name, WEATHER_FILE.name, DEF_INJURY_FILE.name,
            FINAL_INACTIVES_FILE.name, MANUAL_OVERRIDE_FILE.name
        ],
        "updated_at": now_iso(),
    }
    request_log("CURRENT_DATA_BUILDER", "READY", result)
    return result


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

def _load_prior_red_zone_bank():
    df=_read_optional_csv(PHASE6_RED_ZONE_FILE)
    if df.empty: return {}
    bank={}
    for _,r in df.iterrows():
        d={k:r.get(k) for k in df.columns}; pkey=norm(d.get("player"))
        if not pkey: continue
        d["red_zone_data_quality"]="PRIOR_PBP"
        d["red_zone_source_season"]=int(safe_float(d.get("source_season"),NFL_LAST_SEASON) or NFL_LAST_SEASON)
        bank[pkey]=d
    return bank

def load_usage_bank():
    base=_records_by_player(USAGE_FILE); rz=_load_prior_red_zone_bank()
    if not rz: return base
    out={k:dict(v or {}) for k,v in base.items()}
    for pkey,r in rz.items():
        rec=out.setdefault(pkey,{})
        for k in ["red_zone_touch_share","red_zone_carries","red_zone_targets","red_zone_pass_attempts","goal_line_touches","red_zone_data_quality","red_zone_source_season"]:
            if _usable_context_value(r.get(k)): rec[k]=r.get(k)
    return out

def _player_record_quality(row):
    """Prefer a player's full-season/high-volume row over duplicate tiny samples."""
    row=row or {}
    games=safe_float(row.get("games_played"),safe_float(row.get("current_games"),0)) or 0
    volume=max(
        safe_float(row.get("pass_attempts_pg"),0) or 0,
        safe_float(row.get("rush_attempts_pg"),0) or 0,
        safe_float(row.get("targets_pg"),0) or 0,
    )
    production=max(
        safe_float(row.get("passing_yards_pg"),0) or 0,
        safe_float(row.get("rushing_yards_pg"),0) or 0,
        safe_float(row.get("receiving_yards_pg"),0) or 0,
    )
    filled=sum(1 for value in row.values() if _usable_context_value(value))
    return games*100.0 + volume*3.0 + production*0.05 + filled*0.01

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
        if pkey and (pkey not in bank or _player_record_quality(d) > _player_record_quality(bank[pkey])):
            bank[pkey]=d
    _RECORD_BANK_RUNTIME_CACHE[key] = bank
    return bank

def load_current_usage_bank():
    return _records_by_player(CURRENT_USAGE_FILE)


def _lookup_player_record(bank, player, team=None, position=None):
    """Lookup exact or first-initial/last-name record from a player-keyed bank."""
    if not isinstance(bank,dict) or not bank:
        return {}
    n=norm(player)
    if n in bank:
        return dict(bank[n] or {})
    parts=n.split()
    if len(parts)<2:
        return {}
    wanted_last=parts[-1]; wanted_initial=parts[0][:1]
    team_u=_normalize_nfl_team(team); pos_u=str(position or "").upper().strip()
    candidates=[]
    for k,v in bank.items():
        kp=str(k).split()
        if len(kp)<2 or kp[-1] != wanted_last or kp[0][:1] != wanted_initial:
            continue
        d=dict(v or {})
        score=0
        dteam=_normalize_nfl_team(d.get("team") or d.get("recent_team"))
        dpos=str(d.get("position") or d.get("pos") or "").upper().strip()
        if team_u and dteam==team_u: score+=3
        if pos_u and dpos==pos_u: score+=2
        candidates.append((score,d))
    if not candidates:
        return {}
    candidates.sort(key=lambda x:x[0],reverse=True)
    # If ambiguous and no team/position tie-breaker, don't guess.
    if len(candidates)>1 and candidates[0][0]==candidates[1][0] and candidates[0][0]==0:
        return {}
    return candidates[0][1]

def load_role_override_bank():
    return _records_by_player(ROLE_OVERRIDE_CSV)

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
    out=dict(data) if isinstance(data,dict) else {}
    df=_read_optional_csv(DEF_INJURY_OVERRIDE_CSV)
    if not df.empty and "team" in df.columns:
        for team,g in df.groupby("team"):
            tm=_normalize_nfl_team(team)
            if not tm: continue
            rows=[{k:r.get(k) for k in df.columns} for _,r in g.iterrows()]
            rec=dict(out.get(tm) or {}); rec["players"]=rows
            rec["verified_missing_starters"]=sum(1 for d in rows if str(d.get("status") or "").upper() in {"OUT","INACTIVE","IR","DOUBTFUL"})
            rec["source"]="verified_manual_defensive_injury_override"; out[tm]=rec
    return out

def load_final_inactives_context():
    data=load_json(FINAL_INACTIVES_FILE,{})
    out=dict(data) if isinstance(data,dict) else {}
    out.setdefault("players",{}); out.setdefault("teams",{}); out.setdefault("confirmed_matchups",{})
    df=_read_optional_csv(FINAL_INACTIVES_CSV)
    if not df.empty:
        for _,r in df.iterrows():
            d={k:r.get(k) for k in df.columns}; player=norm(d.get("player")); team=_normalize_nfl_team(d.get("team")); matchup=str(d.get("matchup") or "").upper().strip()
            confirmed=str(d.get("confirmed") or "").strip().lower() in {"1","true","yes","y"}
            if player: out["players"][player]={"status":d.get("status") or "","note":d.get("note") or "","team":team,"updated_at":d.get("updated_at") or now_iso(),"source":"verified_manual_final_inactives"}
            if team and confirmed: out["teams"][team]={"confirmed":True,"updated_at":d.get("updated_at") or now_iso(),"source":"verified_manual_final_inactives"}
            if matchup and confirmed: out["confirmed_matchups"][matchup]={"confirmed":True,"updated_at":d.get("updated_at") or now_iso(),"source":"verified_manual_final_inactives"}
    return out

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

def _weather_condition_label(code):
    try: code=int(code)
    except Exception: return "Unknown"
    if code==0: return "Clear"
    if code in {1,2}: return "Partly cloudy"
    if code==3: return "Overcast"
    if code in {45,48}: return "Fog"
    if code in {51,53,55,56,57}: return "Drizzle"
    if code in {61,63,65,66,67}: return "Rain"
    if code in {71,73,75,77}: return "Snow"
    if code in {80,81,82}: return "Rain showers"
    if code in {85,86}: return "Snow showers"
    if code in {95,96,99}: return "Thunderstorm"
    return "Mixed"

def _weather_home_team(row):
    row=row or {}
    explicit=_normalize_nfl_team(row.get("home_team") or row.get("home"))
    if explicit in TEAM_STADIUM_COORDS:
        return explicit
    team=_normalize_nfl_team(row.get("team"))
    opp=_normalize_nfl_team(row.get("opp"))
    hoa=str(row.get("home_away") or row.get("location") or "").upper()
    if "HOME" in hoa and team in TEAM_STADIUM_COORDS: return team
    if "AWAY" in hoa and opp in TEAM_STADIUM_COORDS: return opp
    matchup=str(row.get("matchup") or "")
    if "@" in matchup:
        h=_normalize_nfl_team(matchup.split("@")[-1].strip().split()[0])
        if h in TEAM_STADIUM_COORDS: return h
    return opp if opp in TEAM_STADIUM_COORDS else team

def _weather_game_time(row):
    for key in ["scheduled_at","game_time","start_time","commence_time","event_time","date"]:
        val=(row or {}).get(key)
        if val not in [None,""]:
            try:
                ts=pd.to_datetime(val,utc=True,errors="coerce")
                if pd.notna(ts): return ts
            except Exception:
                pass
    return None

@st.cache_data(ttl=1800, show_spinner=False)
def _open_meteo_game_weather(home_team, game_time_iso):
    """Game-hour stadium forecast. Never fabricates a forecast when unavailable."""
    home=_normalize_nfl_team(home_team)
    coords=TEAM_STADIUM_COORDS.get(home)
    if not coords or not game_time_iso: return {}
    stadium=STADIUM_ENV.get(home,{})
    roof=str(stadium.get("roof") or "Outdoor")
    if roof.upper() in {"DOME","CLOSED"}:
        return {"roof":roof,"stadium":stadium.get("stadium"),"weather_risk":"LOW",
                "weather_note":"Protected indoor game","condition":"Indoor / protected",
                "source":"stadium_roof","updated_at":now_iso()}
    try:
        game_ts=pd.to_datetime(game_time_iso,utc=True)
        now=pd.Timestamp.now(tz="UTC")
        if game_ts < now-pd.Timedelta(hours=6) or game_ts > now+pd.Timedelta(days=16):
            return {"roof":roof,"stadium":stadium.get("stadium"),"source":"stadium_only",
                    "weather_note":"Game-hour forecast outside live forecast window"}
        lat,lon=coords
        params={
            "latitude":lat,"longitude":lon,"timezone":"UTC","temperature_unit":"fahrenheit",
            "wind_speed_unit":"mph","precipitation_unit":"inch","forecast_days":16,
            "hourly":"temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_gusts_10m"
        }
        r=requests.get("https://api.open-meteo.com/v1/forecast",params=params,timeout=8,
                       headers={"User-Agent":"NFLProjectionApp/7.49"})
        r.raise_for_status()
        payload=r.json() if r.content else {}
        hourly=payload.get("hourly") or {}; times=hourly.get("time") or []
        if not times: return {}
        parsed=pd.to_datetime(times,utc=True,errors="coerce")
        valid=[(i,abs((ts-game_ts).total_seconds())) for i,ts in enumerate(parsed) if pd.notna(ts)]
        if not valid: return {}
        idx=min(valid,key=lambda x:x[1])[0]
        def at(name):
            arr=hourly.get(name) or []
            return arr[idx] if idx < len(arr) else None
        code=at("weather_code")
        out={
            "temperature":at("temperature_2m"),"feels_like_f":at("apparent_temperature"),
            "humidity_pct":at("relative_humidity_2m"),"precipitation_pct":at("precipitation_probability"),
            "precipitation_in":at("precipitation"),"weather_code":code,
            "condition":_weather_condition_label(code),"wind_mph":at("wind_speed_10m"),
            "gust_mph":at("wind_gusts_10m"),"roof":roof,"stadium":stadium.get("stadium"),
            "forecast_time":str(parsed[idx]),"source":"open-meteo","updated_at":now_iso(),
        }
        if roof.upper()=="RETRACTABLE":
            out["weather_note"]="Outdoor forecast shown; roof status must be confirmed near kickoff"
        elif roof.upper()=="CANOPY":
            out["weather_note"]="Open-air/canopy stadium forecast"
        return {k:v for k,v in out.items() if v is not None}
    except Exception as exc:
        return {"roof":roof,"stadium":stadium.get("stadium"),"source":"weather_unavailable",
                "weather_note":f"Live forecast unavailable: {type(exc).__name__}"}

def _lookup_weather_for_row(row):
    row=row or {}; weather=load_weather_context()
    team=str(row.get("team") or "").upper().strip(); opp=str(row.get("opp") or "").upper().strip()
    matchup=str(row.get("matchup") or "").upper().strip()
    manual={}
    for k in [matchup,f"{team}|{opp}",f"{opp}|{team}",team,opp]:
        if k and isinstance(weather.get(k),dict):
            manual=dict(weather.get(k) or {}); break
    home=_weather_home_team(row); game_ts=_weather_game_time(row)
    auto=_open_meteo_game_weather(home,game_ts.isoformat() if game_ts is not None else "")
    merged=dict(manual)
    physical={"temperature","temp_f","feels_like_f","humidity_pct","precipitation_pct","precip_pct",
              "precipitation_in","weather_code","condition","wind_mph","wind","gust_mph","gust",
              "forecast_time","source","updated_at","stadium"}
    for k,v in (auto or {}).items():
        if k in physical and _usable_context_value(v): merged[k]=v
        elif k not in merged and _usable_context_value(v): merged[k]=v
    if _usable_context_value(manual.get("roof_status")): merged["roof"]=manual.get("roof_status")
    elif _usable_context_value(manual.get("roof")): merged["roof"]=manual.get("roof")
    elif _usable_context_value((auto or {}).get("roof")): merged["roof"]=auto.get("roof")
    return merged

def _local_nflverse_player_weekly(season):
    """Load saved nflverse weekly data without making an implicit web request."""
    season=int(season)
    for path in [
        PHASE6_RAW_DIR / f"stats_player_week_{season}.csv",
        PHASE6_RAW_DIR / f"player_weekly_from_pbp_{season}.csv",
    ]:
        df=_read_optional_csv(path)
        if not df.empty:
            return df.copy()
    return pd.DataFrame()

@st.cache_data(ttl=21600, show_spinner=False)
def _current_season_context_bank(season=NFL_CURRENT_SEASON):
    """Build rolling current-season context from saved nflverse data.

    Before current-season weekly data exists, this returns empty and saved Phase 6 remains the historical source.
    The explicit Auto Refresh action is responsible for downloading fresh files.
    """
    ctx={"players":{}, "teams":{}, "source":"none", "rows":0}
    try:
        weekly=_local_nflverse_player_weekly(int(season))
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
            weekly = _local_nflverse_player_weekly(int(season))
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
                ctx["source"] = "saved_nflverse_player_weekly"
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
                # Some nflverse summary rows carry a one-week QB label for a
                # non-QB. Require real passing volume so that row cannot enter the
                # quarterback context bank merely because of the label.
                if attempts < 8 or (ypg or 0) < 40:
                    continue
                player=str(r.get("player") or r.get("player_display_name") or "").strip()
                if not player:
                    continue
                team=str(r.get("team") or r.get("recent_team") or "").strip()
                comps=safe_float(r.get("completions_pg"), None)
                games=safe_float(r.get("games_played"), 17) or 17
                ypa=(safe_float(r.get("passing_yards"), 0) or 0) / max(1.0, safe_float(r.get("attempts"), 0) or 0)
                candidate = {
                    "player": player, "team": team, "position": "QB" if pos == "QB" or attempts >= 10 else pos,
                    "passing_yards_pg": round(float(ypg or 0),3),
                    "pass_attempts_pg": round(float(attempts or 0),3),
                    "completions_pg": None if comps is None else round(float(comps),3),
                    "yards_per_attempt": round(float(clamp(ypa if ypa else (ypg or 0)/max(1, attempts), 3.5, 10.5)),3),
                    "games_played": int(games),
                    "passing_context_source": ctx["source"],
                }
                pkey=norm(player)
                if pkey not in ctx["players"] or _player_record_quality(candidate) > _player_record_quality(ctx["players"][pkey]):
                    ctx["players"][pkey] = candidate
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
            weekly = _local_nflverse_player_weekly(int(season))
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
                ctx["source"] = "saved_nflverse_player_weekly"
        if not df.empty:
            ctx["rows"] = int(len(df))
            for _, r in df.iterrows():
                pos=str(r.get("position") or "").upper()
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
                pass_attempts=safe_float(r.get("pass_attempts_pg"),safe_float(r.get("attempts"),0)) or 0
                if pos == "QB" and pass_attempts >= 8:
                    continue
                # Live Underdog supplies the exact WR/TE/RB position. Historical
                # rows with a blank position are still valid receiving samples.
                inferred_pos=pos if pos in ["WR","TE","RB"] else "WR"
                candidate = {
                    "player": player, "team": team, "position": inferred_pos,
                    "receiving_yards_pg": round(float(yards_pg or 0),3),
                    "targets_pg": round(float(targets_pg or 0),3),
                    "receptions_pg": None if receptions_pg is None else round(float(receptions_pg),3),
                    "air_yards_pg": None if air_pg is None else round(float(air_pg),3),
                    "yards_per_target": round(float(clamp(ypt if ypt else (yards_pg or 0)/max(1, targets_pg or 1), 3.0, 14.5)),3),
                    "games_played": int(games),
                    "receiving_context_source": ctx["source"],
                }
                pkey=norm(player)
                if pkey not in ctx["players"] or _player_record_quality(candidate) > _player_record_quality(ctx["players"][pkey]):
                    ctx["players"][pkey] = candidate
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

def _american_implied_probability(odds):
    odds=safe_float(odds)
    if odds is None or odds == 0:
        return None
    if odds < 0:
        return abs(odds)/(abs(odds)+100.0)
    return 100.0/(odds+100.0)

def _probability_to_american(probability):
    probability=safe_float(probability)
    if probability is None:
        return None
    probability=clamp(probability,0.01,0.99)
    if probability >= 0.5:
        return int(round(-100.0*probability/(1.0-probability)))
    return int(round(100.0*(1.0-probability)/probability))

def _moneyline_team_context_bank(team_bank=None):
    """Merge completed-season offense/defense data with current team context."""
    if isinstance(team_bank,dict):
        return {
            _normalize_nfl_team(team):dict(ctx)
            for team,ctx in team_bank.items()
            if _normalize_nfl_team(team) and isinstance(ctx,dict)
        }
    bank={}

    def merge_mapping(mapping, source, overwrite=True):
        if not isinstance(mapping,dict):
            return
        for raw_team,raw_ctx in mapping.items():
            team=_normalize_nfl_team(raw_team)
            if not team or not isinstance(raw_ctx,dict):
                continue
            dest=bank.setdefault(team,{})
            for k,v in raw_ctx.items():
                if not _usable_context_value(v):
                    continue
                # Source priority matters for regular season.  Historical/Savant
                # data may fill holes, but it must never overwrite fresher weekly
                # team context once games begin.
                if overwrite or not _usable_context_value(dest.get(k)):
                    dest[k]=v
            dest.setdefault("moneyline_context_sources",[])
            if source not in dest["moneyline_context_sources"]:
                dest["moneyline_context_sources"].append(source)

    merge_mapping(load_json(PHASE6_TEAM_CONTEXT_FILE,{}),"phase6_team_context",overwrite=True)
    merge_mapping(load_team_context(),"team_context",overwrite=True)
    for path,source in [
        (PHASE6_TEAM_ADVANCED_FILE,"phase6_team_advanced"),
        (PHASE6_DEFENSE_RANK_FILE,"phase6_defense"),
    ]:
        df=_read_optional_csv(path)
        if df.empty or "team" not in df.columns:
            continue
        mapping={str(row.get("team")):row.to_dict() for _,row in df.iterrows()}
        merge_mapping(mapping,source)
    # NFL Savant is a complementary historical/advanced source.  Fill missing
    # fields only; do not let last-season data overwrite current-season football.
    try:
        savant_teams=_savant_banks(SAVANT_DIR,NFL_LAST_SEASON).get("teams",{})
        merge_mapping(savant_teams,"nfl_savant",overwrite=False)
    except Exception as exc:
        request_log("MONEYLINE_SAVANT","FALLBACK",str(exc)[:180])
    # Current weekly/team context is authoritative and therefore merged last.
    merge_mapping(load_current_team_context(),"current_team_context",overwrite=True)
    return bank

def _moneyline_rank_strength(value):
    rank=safe_float(value)
    if rank is None or not 1 <= rank <= 32:
        return None
    return clamp((16.5-rank)/15.5,-1.0,1.0)

def _ml_num(ctx, *keys, default=None):
    for key in keys:
        v=safe_float((ctx or {}).get(key))
        if v is not None:
            return v
    return default


def _ml_pct(value):
    v=safe_float(value)
    if v is None:
        return None
    return v*100.0 if abs(v) <= 1.5 else v


def _ml_center(value, center, scale, invert=False, lo=-1.5, hi=1.5):
    v=safe_float(value)
    if v is None:
        return None
    score=(v-center)/max(1e-6,scale)
    if invert:
        score=-score
    return clamp(score,lo,hi)


def _ml_rank_score(ctx, *keys):
    for key in keys:
        score=_moneyline_rank_strength((ctx or {}).get(key))
        if score is not None:
            return score
    return None


def _ml_mean(values, default=0.0):
    clean=[float(v) for v in values if v is not None and math.isfinite(float(v))]
    return float(np.mean(clean)) if clean else float(default)


def _ml_shrink(value, league_mean, weight=0.55):
    """Shrink unstable football rates (especially turnovers) toward league average."""
    v=safe_float(value)
    if v is None:
        return None
    return float(weight)*v+(1.0-float(weight))*float(league_mean)


def _moneyline_team_profile(ctx):
    """Family-capped football profile.  Correlated statistics are averaged inside
    one family before any family is allowed to influence a game simulation."""
    ctx=ctx or {}
    off_epa=_ml_num(ctx,"epa_per_play","league__off_epa","league__epa")
    off_success=_ml_pct(_ml_num(ctx,"success_rate","league__off_success","league__success"))
    first_rate=_ml_pct(_ml_num(ctx,"first_down_rate","league__first_down_rate"))
    early_success=_ml_pct(_ml_num(ctx,"early_down_success_rate","league__early_down_success"))
    ypp=_ml_num(ctx,"yards_per_play","league__yards_per_play")
    ppd=_ml_num(ctx,"points_per_drive","league__points_per_drive")
    third=_ml_pct(_ml_num(ctx,"third_down_conversion_rate","league__third_down_pct"))
    fourth=_ml_pct(_ml_num(ctx,"fourth_down_conversion_rate","league__fourth_down_pct"))
    rz=_ml_pct(_ml_num(ctx,"red_zone_td_rate","league__red_zone_td_rate","league__rz_td_pct"))
    explosive_pass=_ml_pct(_ml_num(ctx,"explosive_pass_rate","league__explosive_pass"))
    explosive_rush=_ml_pct(_ml_num(ctx,"explosive_rush_rate","league__explosive_rush"))
    three_out=_ml_pct(_ml_num(ctx,"three_and_out_proxy_rate","league__three_and_out_rate"))
    sacks_allowed=_ml_num(ctx,"sacks_allowed_pg")
    hits_allowed=_ml_num(ctx,"qb_hits_allowed_pg")
    pass_pro_rank=_ml_rank_score(ctx,"ol_pass_pro_rank","qb_pass_protection_rank")
    run_block_rank=_ml_rank_score(ctx,"ol_run_block_rank","ol_run_block_proxy_rank")
    savant_pb=_ml_center(_ml_num(ctx,"league__pass_block_grade","league__pass_block"),100,15)
    savant_rb=_ml_center(_ml_num(ctx,"league__run_block_grade","league__run_block"),100,15)
    qb_rank=_ml_rank_score(ctx,"qb_unit_rank","qb_rank","off_qb_rank")
    qb_epa=_ml_center(_ml_num(ctx,"qb_epa_per_play","passing_epa_per_play","league__passing_epa","league__epa_play"),0.0,0.10)
    qb_cpoe=_ml_center(_ml_num(ctx,"cpoe","qb_cpoe","league__cpoe"),0.0,4.0)
    qb_sack_rate=_ml_center(_ml_pct(_ml_num(ctx,"qb_sack_rate","sack_rate_allowed")),6.5,2.0,invert=True)

    def_epa=_ml_num(ctx,"def_epa_allowed_per_play","league__def_epa")
    def_success=_ml_pct(_ml_num(ctx,"def_success_allowed_rate","league__def_success"))
    def_ypp=_ml_num(ctx,"def_yards_per_play_allowed","league__def_yards_per_play")
    def_third=_ml_pct(_ml_num(ctx,"def_third_down_allowed_rate","league__def_third_down_pct"))
    def_fourth=_ml_pct(_ml_num(ctx,"def_fourth_down_allowed_rate","league__def_fourth_down_pct"))
    def_rz=_ml_pct(_ml_num(ctx,"def_red_zone_td_allowed_rate","league__def_rz_td_pct"))
    def_pressure=_ml_pct(_ml_num(ctx,"league__pressure","def_pressure_rate","pressure_top4_rate"))
    def_sacks=_ml_num(ctx,"def_sacks_pg","pass_rush_sacks")
    def_takeaways=_ml_num(ctx,"def_takeaways_pg","league__takeaways")
    exp_pass_allowed=_ml_pct(_ml_num(ctx,"explosive_pass_allowed_rate","league__explosive_pass_allowed"))
    exp_rush_allowed=_ml_pct(_ml_num(ctx,"explosive_rush_allowed_rate","league__explosive_rush_allowed"))

    # Turnovers matter, but raw year-to-year turnover and fumble recovery results are noisy.
    # Regress them before the possession model so lucky recovery/return sequences cannot dominate.
    turnovers=_ml_shrink(_ml_num(ctx,"turnovers_pg"),1.35,0.58)
    fumbles_lost=_ml_shrink(_ml_num(ctx,"fumbles_lost_pg"),0.55,0.52)
    interceptions=_ml_shrink(_ml_num(ctx,"interceptions_pg"),0.80,0.68)
    def_takeaways=_ml_shrink(def_takeaways,1.35,0.50)
    penalties=_ml_num(ctx,"penalties_pg","penalties")
    penalty_yards=_ml_num(ctx,"penalty_yards_pg","penalty_yards")
    presnap=_ml_num(ctx,"league__pre_snap_flags_pg","league__presnap_penalties_pg")
    auto_first=_ml_num(ctx,"league__automatic_first_downs","league__auto_1st")
    fg_pct=_ml_pct(_ml_num(ctx,"field_goal_pct","league__fg_pct"))
    punts=_ml_num(ctx,"punts_pg")
    special_epa=_ml_num(ctx,"league__special_teams_epa","league__st_epa")
    # v7.47 deep football modules.
    yards_drive=_ml_num(ctx,"yards_per_drive")
    plays_drive=_ml_num(ctx,"plays_per_drive")
    rz_trips=_ml_num(ctx,"red_zone_trips_pg")
    avg_start=_ml_num(ctx,"avg_drive_start_own_yardline")
    neutral_epa=_ml_num(ctx,"neutral_epa_per_play")
    neutral_success=_ml_pct(_ml_num(ctx,"neutral_success_rate"))
    neutral_pass=_ml_pct(_ml_num(ctx,"neutral_pass_rate"))
    pressure_allowed=_ml_pct(_ml_num(ctx,"pressure_allowed_rate"))
    sack_given_pressure=_ml_pct(_ml_num(ctx,"sack_given_pressure_rate"))
    return_yards=_ml_num(ctx,"return_yards_pg")
    punt_return_yards=_ml_num(ctx,"punt_return_yards_pg")
    net_punt=_ml_num(ctx,"net_punt_yards")
    fg_oe=_ml_num(ctx,"fg_over_expected_per_attempt")
    turnover_reg=_ml_num(ctx,"turnover_regression_proxy_pg")
    fumble_lost_share=_ml_pct(_ml_num(ctx,"fumble_lost_share"))
    explosive_40=_ml_pct(_ml_num(ctx,"explosive_40_rate"))
    def_pressure_real=_ml_pct(_ml_num(ctx,"def_pressure_rate"))
    def_sack_given_pressure=_ml_pct(_ml_num(ctx,"def_sack_given_pressure_rate"))
    def_neutral_epa=_ml_num(ctx,"def_neutral_epa_allowed")
    def_neutral_success=_ml_pct(_ml_num(ctx,"def_neutral_success_allowed_rate"))
    def_explosive_40=_ml_pct(_ml_num(ctx,"def_explosive_40_allowed_rate"))
    fourth_go_rate=_ml_pct(_ml_num(ctx,"fourth_down_go_rate"))
    two_point_attempts=_ml_num(ctx,"two_point_attempts_pg")
    ol_continuity=_ml_pct(_ml_num(ctx,"ol_returning_starters_pct"))

    families={
        "offense": _ml_mean([
            _ml_center(off_epa,0.0,0.10), _ml_center(off_success,44.5,5.5),
            _ml_center(first_rate,33.0,4.5), _ml_center(early_success,44.5,5.5),
            _ml_center(ypp,5.45,0.65), _ml_center(ppd,2.05,0.45),
        ]),
        "situational": _ml_mean([
            _ml_center(third,39.5,7.0), _ml_center(fourth,53.0,12.0),
            _ml_center(rz,56.0,10.0), _ml_center(three_out,35.0,8.0,invert=True),
            _ml_center(fourth_go_rate,18.0,10.0),
        ]),
        "explosive": _ml_mean([_ml_center(explosive_pass,7.0,2.0),_ml_center(explosive_rush,6.5,2.0)]),
        "trench": _ml_mean([
            pass_pro_rank,run_block_rank,savant_pb,savant_rb,
            _ml_center(sacks_allowed,2.4,0.8,invert=True),_ml_center(hits_allowed,5.0,1.8,invert=True),
            _ml_center(ol_continuity,70.0,20.0),
        ]),
        "quarterback": _ml_mean([qb_rank,qb_epa,qb_cpoe,qb_sack_rate]),
        "defense": _ml_mean([
            _ml_center(def_epa,0.0,0.10,invert=True),_ml_center(def_success,44.5,5.5,invert=True),
            _ml_center(def_ypp,5.45,0.65,invert=True),_ml_center(def_third,39.5,7.0,invert=True),
            _ml_center(def_rz,56.0,10.0,invert=True),
        ]),
        "pass_rush": _ml_mean([
            _ml_center(def_pressure,25.0,5.0),_ml_center(def_sacks,2.4,0.8),
        ]),
        "def_explosive": _ml_mean([
            _ml_center(exp_pass_allowed,7.0,2.0,invert=True),_ml_center(exp_rush_allowed,6.5,2.0,invert=True),
        ]),
        "ball_security": _ml_mean([
            _ml_center(turnovers,1.35,0.55,invert=True),_ml_center(fumbles_lost,0.55,0.30,invert=True),
            _ml_center(interceptions,0.80,0.40,invert=True),
        ]),
        "takeaways": _ml_mean([_ml_center(def_takeaways,1.35,0.55)]),
        "discipline": _ml_mean([
            _ml_center(penalties,6.0,1.5,invert=True),_ml_center(penalty_yards,50.0,15.0,invert=True),
            _ml_center(presnap,1.5,0.7,invert=True),_ml_center(auto_first,2.0,1.0,invert=True),
        ]),
        "special_teams": _ml_mean([
            _ml_center(fg_pct,84.0,7.0),_ml_center(punts,4.0,1.2,invert=True),_ml_center(special_epa,0.0,0.08),
            _ml_center(net_punt,42.0,4.0),_ml_center(fg_oe,0.0,0.10),
        ]),
        "possessions": _ml_mean([
            _ml_center(yards_drive,31.0,5.5),_ml_center(plays_drive,5.8,0.8),
            _ml_center(rz_trips,3.2,0.8),_ml_center(avg_start,28.0,4.0),
        ]),
        "game_state": _ml_mean([
            _ml_center(neutral_epa,0.0,0.10),_ml_center(neutral_success,44.5,5.5),
            _ml_center(neutral_pass,56.0,8.0),
        ]),
        "hidden_yards": _ml_mean([
            _ml_center(return_yards,45.0,25.0),_ml_center(punt_return_yards,15.0,12.0),
            _ml_center(net_punt,42.0,4.0),_ml_center(fg_oe,0.0,0.10),
        ]),
        "pressure_response": _ml_mean([
            _ml_center(pressure_allowed,28.0,6.0,invert=True),
            _ml_center(sack_given_pressure,22.0,7.0,invert=True),
            _ml_center(def_pressure_real,28.0,6.0),
            _ml_center(def_sack_given_pressure,22.0,7.0),
        ]),
        "turnover_quality": _ml_mean([
            _ml_center(turnover_reg,1.35,0.55,invert=True),
            _ml_center(fumble_lost_share,50.0,18.0,invert=True),
        ]),
        "neutral_defense": _ml_mean([
            _ml_center(def_neutral_epa,0.0,0.10,invert=True),
            _ml_center(def_neutral_success,44.5,5.5,invert=True),
            _ml_center(def_explosive_40,1.5,0.8,invert=True),
        ]),
        "coaching": _ml_mean([
            _ml_center(fourth_go_rate,18.0,10.0),_ml_center(two_point_attempts,0.12,0.12),
            _ml_center(_ml_pct(_ml_num(ctx,"no_huddle_rate","league__no_huddle_rate")),8.0,6.0),
        ]),
        "ol_continuity": _ml_mean([_ml_center(ol_continuity,70.0,20.0)]),
    }
    # Strength-of-schedule is a small independent family, not another offense multiplier.
    sos_rank=_ml_rank_score(ctx,"sos_rank","strength_of_schedule_rank","schedule_strength_rank")
    sos_value=_ml_center(_ml_num(ctx,"strength_of_schedule","sos","schedule_strength"),0.0,0.12)
    opp_quality=_ml_center(_ml_num(ctx,"opponent_avg_epa","avg_opponent_epa"),0.0,0.08)
    families["schedule"]=_ml_mean([sos_rank,sos_value,opp_quality],0.0)
    # Cap every family.  This is the primary anti-double-counting control.
    families={k:float(clamp(v,-1.25,1.25)) for k,v in families.items()}
    pace=_ml_num(ctx,"current_plays_pg","pbp_plays_pg","plays_pg",default=63.0)
    drives=_ml_num(ctx,"drives_pg",default=10.5)
    no_huddle=_ml_pct(_ml_num(ctx,"no_huddle_rate","league__no_huddle_rate"))
    coverage=sum(1 for v in [off_epa,off_success,first_rate,third,rz,def_epa,def_success,turnovers,penalties,fg_pct] if v is not None)
    qb_coverage=sum(1 for v in [qb_rank,qb_epa,qb_cpoe,qb_sack_rate] if v is not None)
    trench_coverage=sum(1 for v in [pass_pro_rank,run_block_rank,savant_pb,savant_rb,sacks_allowed,hits_allowed,pressure_allowed,sack_given_pressure] if v is not None)
    module_coverage={
        "possessions":sum(1 for v in [yards_drive,plays_drive,rz_trips,avg_start,third,fourth] if v is not None),
        "trenches":sum(1 for v in [pass_pro_rank,run_block_rank,pressure_allowed,sack_given_pressure,def_pressure_real,def_sack_given_pressure] if v is not None),
        "hidden_yards":sum(1 for v in [return_yards,punt_return_yards,net_punt,fg_pct,fg_oe,penalties] if v is not None),
        "game_state":sum(1 for v in [neutral_epa,neutral_success,neutral_pass,def_neutral_epa,def_neutral_success] if v is not None),
        "skill_role":sum(1 for v in [qb_rank,qb_epa,qb_cpoe,explosive_pass,explosive_rush,explosive_40] if v is not None),
    }
    return {"families":families,"pace":pace,"drives":drives,"no_huddle":no_huddle,"coverage":coverage,"qb_coverage":qb_coverage,
            "trench_coverage":trench_coverage,"module_coverage":module_coverage,"off_epa":off_epa}


def _moneyline_injury_context(team):
    """Team availability penalty with final-inactive confirmation.

    Injury reports and final inactives are de-duplicated by player.  QB/OL remain
    the largest offensive weights; EDGE/CB/S carry the largest defensive weights.
    """
    team=_normalize_nfl_team(team)
    bank=load_injury_bank()
    offense=0.0; defense=0.0; names=[]; counted=set(); final_count=0
    pos_weights={"QB":0.48,"LT":0.16,"RT":0.14,"C":0.10,"G":0.08,"LG":0.08,"RG":0.08,"WR":0.10,"TE":0.07,"RB":0.06,
                 "EDGE":0.13,"DE":0.12,"DT":0.09,"LB":0.08,"CB":0.11,"S":0.08,"FS":0.08,"SS":0.08}
    offense_pos={"QB","LT","RT","C","G","LG","RG","WR","TE","RB","FB"}

    def add_player(raw_name,rec,force_inactive=False):
        nonlocal offense,defense,final_count
        if not isinstance(rec,dict) or _normalize_nfl_team(rec.get("team"))!=team:
            return
        key=norm(raw_name)
        status=str(rec.get("status") or rec.get("injury_status") or rec.get("inactive_status") or "").upper()
        practice=str(rec.get("practice_status") or "").upper()
        inactive=force_inactive or "INACTIVE" in status or any(x in status for x in ["OUT","IR","PUP"])
        if not inactive and not any(x in status for x in ["DOUBTFUL","QUESTION"]) and practice not in ["DNP","LIMITED"]:
            return
        if key in counted:
            return
        counted.add(key)
        pos=str(rec.get("position") or rec.get("pos") or "").upper()
        weight=pos_weights.get(pos,0.05)
        severity=1.0 if inactive else 0.65 if "DOUBTFUL" in status else 0.35
        value=weight*severity
        if pos in offense_pos: offense+=value
        else: defense+=value
        if force_inactive or "INACTIVE" in status: final_count+=1
        names.append(f"{raw_name}:{status or practice or 'LIMITED'}")

    if isinstance(bank,dict):
        for raw_name,rec in bank.items():
            add_player(raw_name,rec,False)

    final_data=load_final_inactives_context()
    players=final_data.get("players",{}) if isinstance(final_data,dict) and isinstance(final_data.get("players"),dict) else {}
    for raw_name,rec in players.items():
        if isinstance(rec,dict):
            status=str(rec.get("status") or rec.get("inactive_status") or "").upper()
            if "INACTIVE" in status or status in {"OUT","IR","PUP"}:
                add_player(raw_name,rec,True)

    def_ctx=load_defensive_injury_context().get(team,{}) if isinstance(load_defensive_injury_context(),dict) else {}
    if isinstance(def_ctx,dict):
        # Team-level injury feeds can know a missing starter even when player-level
        # rows are unavailable.  Keep these nudges small to avoid double counting.
        missing_cb=safe_float(def_ctx.get("missing_cb_starters"),0) or 0
        missing_s=safe_float(def_ctx.get("missing_safety_starters"),0) or 0
        missing_edge=safe_float(def_ctx.get("missing_edge_starters"),0) or 0
        defense+=min(0.18,0.055*missing_cb+0.045*missing_s+0.060*missing_edge)

    confirmed=False
    teams=final_data.get("teams",{}) if isinstance(final_data,dict) and isinstance(final_data.get("teams"),dict) else {}
    tctx=teams.get(team,{}) if isinstance(teams.get(team,{}),dict) else {}
    confirmed=bool(tctx.get("confirmed"))
    return {"offense":float(clamp(offense,0,0.72)),"defense":float(clamp(defense,0,0.62)),"notes":names[:7],
            "final_inactives_confirmed":confirmed,"final_inactive_count":final_count}


def _moneyline_game_environment(away,home,matchup,scheduled_at=None):
    env=STADIUM_ENV.get(home,{})
    away_row={"team":away,"opp":home,"matchup":matchup,"home_away":"AWAY","scheduled_at":scheduled_at}
    home_row={"team":home,"opp":away,"matchup":matchup,"home_away":"HOME","scheduled_at":scheduled_at}
    travel=_lookup_pair_context(load_travel_context_bank(),away_row) or {}
    if travel:
        away_row.update(travel)
    travel_score=travel_difficulty_score(away_row)
    weather=_lookup_weather_for_row(home_row) or _lookup_weather_for_row(away_row) or {}
    risk,pass_factor,notes=_weather_risk_from_detail(weather) if weather else ("LOW",1.0,[])
    wind=safe_float(weather.get("wind_mph"),safe_float(weather.get("wind"))) if weather else None
    precip=safe_float(weather.get("precipitation_pct"),safe_float(weather.get("precip_pct"))) if weather else None
    home_edge=0.34
    crowd=str(env.get("crowd") or "").upper()
    if crowd=="LOUD": home_edge+=0.08
    elif crowd=="EXTREME": home_edge+=0.13
    if safe_float(env.get("altitude"),0)>=4000: home_edge+=0.06
    if travel_score.get("label")=="HIGH": home_edge+=0.10
    elif travel_score.get("label")=="MED": home_edge+=0.05
    return {"home_edge":float(clamp(home_edge,0.20,0.65)),"weather_risk":risk,"weather_pass_factor":pass_factor,
            "weather_notes":notes,"wind":wind,"precip":precip,"travel":travel_score,"stadium":env,
            "weather_detail":weather,
            "temperature":safe_float(weather.get("temperature"),safe_float(weather.get("temp_f"))) if weather else None,
            "feels_like":safe_float(weather.get("feels_like_f")) if weather else None,
            "humidity":safe_float(weather.get("humidity_pct")) if weather else None,
            "gust":safe_float(weather.get("gust_mph"),safe_float(weather.get("gust"))) if weather else None,
            "roof":str(weather.get("roof") or env.get("roof") or "") if weather or env else "",
            "condition":str(weather.get("condition") or weather.get("summary") or "") if weather else "",
            "weather_source":str(weather.get("source") or "") if weather else ""}


def _preseason_moneyline_rotation(team, game_rows):
    rotations=load_preseason_rotations()
    records=[]
    for rec in (rotations.get("players",{}) or {}).values():
        if isinstance(rec,dict) and _normalize_nfl_team(rec.get("team"))==team:
            records.append(rec)
    qb=[r for r in records if str(r.get("position") or "").upper()=="QB"]
    score=0.0; confidence=[]; notes=[]
    for r in qb:
        conf=safe_float(r.get("confidence"),0.5) or 0.5; confidence.append(conf)
        status=str(r.get("status") or "").upper()
        drives=safe_float(r.get("preseason_expected_drives"))
        snap=safe_float(r.get("preseason_snap_share"))
        if status=="RESTING": score-=0.18*conf
        elif status=="EXTENDED_WORK": score+=0.16*conf
        if drives is not None: score+=(drives-3.0)*0.025*conf
        elif snap is not None: score+=(snap-0.30)*0.35*conf
        notes.append(f"{r.get('player')} {status or 'ACTIVE'}")
    # Raw preseason props provide a small secondary clue about how much QB room
    # opportunity is posted, but never determine a side on their own.
    qb_lines=[]
    for row in game_rows or []:
        if _normalize_nfl_team(row.get("team"))==team and (_canon_prop_label(row.get("prop"))=="Passing Yards"):
            v=safe_float(row.get("line"))
            if v is not None: qb_lines.append(v)
    if qb_lines:
        score+=clamp((max(qb_lines)-75.0)/160.0,-0.10,0.12)
    conf=float(np.mean(confidence)) if confidence else 0.35
    return {"score":float(clamp(score,-0.45,0.45)),"confidence":conf,"records":len(records),"notes":notes[:4]}




def _moneyline_regular_readiness(away_profile,home_profile,away_inj,home_inj,env,away_ctx,home_ctx,market_odds):
    """REGULAR-season readiness audit with required football data modules."""
    rows=[]
    def component(name,score,weight,detail):
        rows.append({"name":name,"score":round(float(clamp(score,0,1))*100,1),"weight":weight,"detail":detail})
    core=min(1.0,(away_profile.get("coverage",0)+home_profile.get("coverage",0))/16.0)
    qb=min(1.0,(away_profile.get("qb_coverage",0)+home_profile.get("qb_coverage",0))/4.0)
    trench=min(1.0,(away_profile.get("trench_coverage",0)+home_profile.get("trench_coverage",0))/10.0)
    am=away_profile.get("module_coverage",{}) or {}; hm=home_profile.get("module_coverage",{}) or {}
    def mod_score(key,denom):
        return min(1.0,(safe_float(am.get(key),0)+safe_float(hm.get(key),0))/float(denom))
    possessions=mod_score("possessions",8)
    hidden=mod_score("hidden_yards",8)
    game_state=mod_score("game_state",7)
    skill_role=mod_score("skill_role",8)
    current=float(any(src=="current_team_context" for src in (away_ctx.get("moneyline_context_sources") or [])))
    current+=float(any(src=="current_team_context" for src in (home_ctx.get("moneyline_context_sources") or []))); current/=2.0
    # Current PBP is the gold-standard signal once games exist.
    current_pbp=(float(bool(away_ctx.get("current_pbp_rows") or away_ctx.get("source","").startswith("nflverse_current_pbp")))+
                 float(bool(home_ctx.get("current_pbp_rows") or home_ctx.get("source","").startswith("nflverse_current_pbp"))))/2.0
    injuries=0.45+0.275*bool(away_inj.get("notes") or away_inj.get("final_inactives_confirmed"))+0.275*bool(home_inj.get("notes") or home_inj.get("final_inactives_confirmed"))
    finals=(float(bool(away_inj.get("final_inactives_confirmed")))+float(bool(home_inj.get("final_inactives_confirmed"))))/2.0
    weather=1.0 if env.get("weather_pass_factor") is not None else 0.6
    travel=1.0 if isinstance(env.get("travel"),dict) else 0.6
    market=1.0 if len(market_odds)>=2 else 0.65
    component("Core offense/defense",core,0.14,f"coverage {away_profile.get('coverage',0)} + {home_profile.get('coverage',0)}")
    component("Possession/drive module",possessions,0.12,f"fields {am.get('possessions',0)} + {hm.get('possessions',0)}")
    component("QB / skill-role",min(1.0,0.55*qb+0.45*skill_role),0.12,f"QB {away_profile.get('qb_coverage',0)}+{home_profile.get('qb_coverage',0)}; skill {am.get('skill_role',0)}+{hm.get('skill_role',0)}")
    component("OL/DL + pressure",trench,0.12,f"trench fields {away_profile.get('trench_coverage',0)} + {home_profile.get('trench_coverage',0)}")
    component("Hidden yards / special teams",hidden,0.08,f"fields {am.get('hidden_yards',0)} + {hm.get('hidden_yards',0)}")
    component("Neutral game-state",game_state,0.08,f"fields {am.get('game_state',0)} + {hm.get('game_state',0)}")
    component("Current-season context",max(current,current_pbp),0.10,"weekly + current PBP override prior when real")
    component("Injury availability",injuries,0.08,"injury report + defensive starter context")
    component("Final inactives",finals,0.06,"confirmed team inactive lists")
    component("Weather/stadium",weather,0.04,str(env.get("weather_risk") or "LOW"))
    component("Travel/rest",travel,0.03,str((env.get("travel") or {}).get("label") or "LOW"))
    component("Live market audit",market,0.03,"two-sided price audit" if len(market_odds)>=2 else "model-only")
    total=sum((r["score"]/100.0)*r["weight"] for r in rows)/sum(r["weight"] for r in rows)
    score=int(round(total*100))
    label="ELITE" if score>=92 else "STRONG" if score>=84 else "USABLE" if score>=72 else "THIN"
    module_floor=min(possessions,trench,hidden,game_state)
    tracking=(score>=72 and module_floor>=0.35)
    official=(score>=82 and module_floor>=0.50)
    return {"score":score,"label":label,"components":rows,"tracking_ready":tracking,"official_ready":official,
            "module_floor":round(module_floor,3),"module_coverage":{"possessions":possessions,"trenches":trench,"hidden_yards":hidden,"game_state":game_state,"skill_role":skill_role}}

def _moneyline_possession_probs(own,opp,env_side=0.0,injury_off=0.0,injury_def_opp=0.0,preseason_rotation=0.0,weather_pass_factor=1.0):
    f=own["families"]; d=opp["families"]
    offense=(0.34*f["offense"]+0.12*f["situational"]+0.06*f["explosive"]+0.08*f["trench"]+
             0.10*f["quarterback"]+0.08*f["possessions"]+0.06*f["game_state"]+
             0.04*f["hidden_yards"]+0.04*f["pressure_response"]+0.03*f["turnover_quality"]+0.05*f.get("schedule",0.0))
    defense_opp=(0.45*d["defense"]+0.12*d["pass_rush"]+0.09*d["def_explosive"]+0.07*d["takeaways"]+
                 0.11*d["neutral_defense"]+0.09*d["pressure_response"]+0.07*d["turnover_quality"])
    field=0.05*f["special_teams"]+0.05*f["hidden_yards"]+0.05*f["discipline"]
    availability=-0.70*injury_off+0.38*injury_def_opp
    weather_adj=(safe_float(weather_pass_factor,1.0)-1.0)*1.8
    strength=offense-defense_opp+field+env_side+availability+preseason_rotation+weather_adj
    td=clamp(0.215+0.045*strength+0.008*f["situational"],0.105,0.37)
    fg=clamp(0.155+0.018*f["special_teams"]+0.007*f["offense"]-0.005*f["situational"],0.085,0.245)
    turnover=clamp(0.112-0.022*f["ball_security"]+0.020*d["takeaways"]+0.006*d["pass_rush"],0.055,0.205)
    downs=clamp(0.022-0.005*f["situational"]+0.004*d["defense"],0.008,0.055)
    safety=clamp(0.0022+0.0008*d["pass_rush"],0.0005,0.006)
    used=td+fg+turnover+downs+safety
    punt=max(0.08,1.0-used)
    z=td+fg+turnover+downs+safety+punt
    return {"td":td/z,"fg":fg/z,"turnover":turnover/z,"downs":downs/z,"safety":safety/z,"punt":punt/z,"strength":strength}


def _moneyline_team_rating(ctx):
    profile=_moneyline_team_profile(ctx)
    fam=profile["families"]
    offense=(0.34*fam["offense"]+0.14*fam["situational"]+0.07*fam["explosive"]+0.10*fam["trench"]+
             0.12*fam["quarterback"]+0.09*fam["possessions"]+0.06*fam["game_state"]+
             0.04*fam["hidden_yards"]+0.04*fam["pressure_response"])
    defense=(0.46*fam["defense"]+0.13*fam["pass_rush"]+0.10*fam["def_explosive"]+0.07*fam["takeaways"]+
             0.10*fam["neutral_defense"]+0.08*fam["pressure_response"]+0.06*fam["turnover_quality"])
    return {"ready":profile["coverage"]>=4,"offense":offense,"defense":defense,"pace":profile["pace"],"inputs":profile["coverage"],"labels":list(fam),"off_epa":profile.get("off_epa"),"profile":profile}


def _moneyline_side_team(value, away, home):
    raw=str(value or "").upper().strip()
    direct=_normalize_nfl_team(raw)
    if direct in [away,home]: return direct
    for full,abbr in NFL_TEAM_NAME_ALIASES.items():
        if full in raw and abbr in [away,home]: return abbr
    for token in re.findall(r"[A-Z]{2,3}",raw):
        team=_normalize_nfl_team(token)
        if team in [away,home]: return team
    return ""


def _moneyline_american_price(row):
    row=row or {}
    price=safe_float(row.get("american_price"),safe_float(row.get("american_odds")))
    if price is not None: return int(round(price))
    decimal=safe_float(row.get("decimal_price"),safe_float(row.get("decimal_odds")))
    if decimal is not None and decimal>1.0:
        return int(round((decimal-1.0)*100.0)) if decimal>=2.0 else int(round(-100.0/(decimal-1.0)))
    ambiguous=safe_float(row.get("price_or_payout"))
    if ambiguous is not None and (ambiguous<=-100 or ambiguous>=100): return int(round(ambiguous))
    return None


def _moneyline_games_from_rows(moneyline_rows, prop_rows):
    games={}; all_rows=[(row,False) for row in (prop_rows or [])]+[(row,True) for row in (moneyline_rows or [])]
    for row,is_market in all_rows:
        row=dict(row or {}); matchup=_canonical_matchup(row.get("matchup"),row.get("team"),row.get("opp"),row.get("home_away"))
        if "@" not in matchup: continue
        away,home=_teams_from_matchup_text(matchup)
        if away not in NFL_TEAM_ABBRS or home not in NFL_TEAM_ABBRS or away==home: continue
        key=f"{away} @ {home}"
        game=games.setdefault(key,{"matchup":key,"away":away,"home":home,"rows":[],"market_rows":[],"scheduled_at":"","season_type":"","event_id":""})
        game["market_rows" if is_market else "rows"].append(row)
        for target,keys in [("scheduled_at",["scheduled_at","starts_at","start_time","event_time","game_time"]),("season_type",["season_type","game_type","event_type"]),("event_id",["event_id","game_id","match_id","underdog_id"])]:
            if game.get(target): continue
            for source_key in keys:
                if row.get(source_key) not in [None,""]:
                    game[target]=row.get(source_key); break
    return list(games.values())


def _moneyline_market_calibration(football_prob, market_prob, readiness_score, phase="REGULAR"):
    """Second probability layer. Never changes the football side; only calibrates confidence."""
    p=clamp(safe_float(football_prob,0.5) or 0.5,0.01,0.99)
    m=safe_float(market_prob)
    if phase!="REGULAR" or m is None:
        return p,{"market_weight":0.0,"note":"football-only probability"}
    readiness=clamp((safe_float(readiness_score,72) or 72)/100.0,0.0,1.0)
    # Strong data earns more independence from market; thin data gets more shrinkage.
    market_weight=clamp(0.30-(readiness-0.70)*0.45,0.08,0.30)
    calibrated=p*(1-market_weight)+m*market_weight
    # Preserve football side. Calibration can reduce conviction to near coin flip, not reverse it.
    if p>=0.5: calibrated=max(0.5001,calibrated)
    else: calibrated=min(0.4999,calibrated)
    return float(clamp(calibrated,0.01,0.99)),{"market_weight":round(market_weight,3),"note":"market used for calibration only; football side preserved"}


def _moneyline_clv_update(matchup, implied, phase="REGULAR"):
    data=load_json(MONEYLINE_CLV_FILE,{})
    if not isinstance(data,dict): data={}
    key=f"{phase}|{matchup}"
    rec=data.setdefault(key,{"matchup":matchup,"phase":phase,"history":[]})
    stamp=now_iso()
    clean={k:round(float(v),5) for k,v in (implied or {}).items() if safe_float(v) is not None}
    if clean:
        hist=rec.setdefault("history",[])
        if not hist or hist[-1].get("probabilities")!=clean:
            hist.append({"at":stamp,"probabilities":clean})
            rec["history"]=hist[-80:]
            rec["updated_at"]=stamp
            save_json(MONEYLINE_CLV_FILE,data)
    hist=rec.get("history",[])
    opening=hist[0].get("probabilities",{}) if hist else {}
    latest=hist[-1].get("probabilities",{}) if hist else clean
    return {"opening":opening,"latest":latest,"snapshots":len(hist)}


def save_moneyline_snapshot(cards, label="BEFORE"):
    rows=load_json(MONEYLINE_SNAPSHOT_FILE,[])
    if not isinstance(rows,list): rows=[]
    stamp=now_iso(); slate_id=f"NFL_ML_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    saved=[]
    for card in cards or []:
        if card.get("phase")!="REGULAR": continue
        r=_clean_snapshot_row(card); r.update({"saved_at":stamp,"snapshot_type":label,"slate_id":slate_id})
        rows.append(r); saved.append(r)
    save_json(MONEYLINE_SNAPSHOT_FILE,rows[-4000:])
    return len(saved),slate_id


def grade_moneyline_results_csv(uploaded_file):
    """Grade saved REGULAR moneylines from away/home/away_score/home_score CSV."""
    if uploaded_file is None: return []
    try:
        raw=uploaded_file.read(); uploaded_file.seek(0); df=pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        request_log("ML_GRADE","CSV_ERROR",str(exc)[:180]); return []
    snapshots=load_json(MONEYLINE_SNAPSHOT_FILE,[]); results=load_json(MONEYLINE_RESULT_LOG,[])
    if not isinstance(results,list): results=[]
    graded=[]
    for _,rr in df.iterrows():
        away=_normalize_nfl_team(rr.get("away") or rr.get("Away")); home=_normalize_nfl_team(rr.get("home") or rr.get("Home"))
        away_score=safe_float(rr.get("away_score"),safe_float(rr.get("Away Score"))); home_score=safe_float(rr.get("home_score"),safe_float(rr.get("Home Score")))
        if not away or not home or away_score is None or home_score is None: continue
        matchup=f"{away} @ {home}"
        candidates=[s for s in snapshots if s.get("phase")=="REGULAR" and s.get("matchup")==matchup]
        if not candidates: continue
        s=sorted(candidates,key=lambda x:str(x.get("saved_at") or ""))[-1]
        winner=home if home_score>away_score else away if away_score>home_score else "TIE"
        model=s.get("favorite"); win=(model==winner) if winner!="TIE" else None
        p=safe_float(s.get("calibrated_favorite_prob"),safe_float(s.get("favorite_prob"),0.5)) or 0.5
        y=1.0 if win else 0.0 if win is False else 0.5
        brier=(p-y)**2; logloss=-(y*math.log(max(p,1e-9))+(1-y)*math.log(max(1-p,1e-9)))
        margin_actual=home_score-away_score; margin_proj=(safe_float(s.get("home_projection"),0) or 0)-(safe_float(s.get("away_projection"),0) or 0)
        clv=load_json(MONEYLINE_CLV_FILE,{}).get(f"REGULAR|{matchup}",{}) if isinstance(load_json(MONEYLINE_CLV_FILE,{}),dict) else {}
        hist=clv.get("history",[]) if isinstance(clv,dict) else []
        close_probs=hist[-1].get("probabilities",{}) if hist else {}
        close_model_prob=safe_float(close_probs.get(model))
        row={**_clean_snapshot_row(s),"graded_at":now_iso(),"actual_away_score":away_score,"actual_home_score":home_score,"actual_winner":winner,"model_win":win,"brier":round(brier,5),"log_loss":round(logloss,5),"actual_margin":round(margin_actual,2),"projected_margin":round(margin_proj,2),"margin_error":round(margin_actual-margin_proj,2),"closing_market_prob":close_model_prob,"clv_probability":None if close_model_prob is None else round(p-close_model_prob,5)}
        results.append(row); graded.append(row)
    save_json(MONEYLINE_RESULT_LOG,results[-5000:])
    return graded


def moneyline_calibration_summary():
    rows=[r for r in load_json(MONEYLINE_RESULT_LOG,[]) if r.get("phase")=="REGULAR" and r.get("model_win") is not None]
    if not rows: return {}
    wins=[1.0 if r.get("model_win") else 0.0 for r in rows]
    b=[safe_float(r.get("brier")) for r in rows if safe_float(r.get("brier")) is not None]
    ll=[safe_float(r.get("log_loss")) for r in rows if safe_float(r.get("log_loss")) is not None]
    mae=[abs(safe_float(r.get("margin_error"),0) or 0) for r in rows]
    return {"games":len(rows),"win_rate":float(np.mean(wins)),"brier":float(np.mean(b)) if b else None,"log_loss":float(np.mean(ll)) if ll else None,"margin_mae":float(np.mean(mae)) if mae else None}


def build_moneyline_game_cards(moneyline_rows, prop_rows, team_bank=None, sims=18000, rng_seed=7717):
    """Deep possession-based NFL Moneyline model.

    Market odds are attached only after the football model is built.  Correlated
    metrics are grouped into capped families to reduce double counting.  Preseason
    uses the same possession engine with stronger rotation uncertainty and heavy
    shrinkage toward neutral rather than regular-season starter assumptions.
    """
    bank=_moneyline_team_context_bank(team_bank); cards=[]
    for game in _moneyline_games_from_rows(moneyline_rows,prop_rows):
        away,home=game["away"],game["home"]
        away_ctx=bank.get(away,{}) if isinstance(bank.get(away),dict) else {}
        home_ctx=bank.get(home,{}) if isinstance(bank.get(home),dict) else {}
        away_rating=_moneyline_team_rating(away_ctx); home_rating=_moneyline_team_rating(home_ctx)
        phase=nfl_game_phase(game)
        blocks=[]
        if not away_rating.get("ready"): blocks.append(f"{away} football database incomplete")
        if not home_rating.get("ready"): blocks.append(f"{home} football database incomplete")

        market_odds={}; payouts={}
        for row in game.get("market_rows",[]):
            side=_moneyline_side_team(row.get("team_or_side") or row.get("team") or row.get("raw_label"),away,home)
            if not side: continue
            odds=_moneyline_american_price(row)
            if odds is not None: market_odds[side]=odds
            payout=safe_float(row.get("payout_multiplier"))
            if payout is not None: payouts[side]=payout
        implied={team:_american_implied_probability(odds) for team,odds in market_odds.items()}
        if away in implied and home in implied:
            z=implied[away]+implied[home]
            if z>0: implied={away:implied[away]/z,home:implied[home]/z}

        game_rows=game.get("rows",[])+game.get("market_rows",[])
        market_total=None; home_spread=None
        for row in game_rows:
            if market_total is None: market_total=safe_float(row.get("game_total"),safe_float(row.get("market_game_total")))
            if home_spread is None:
                spread=safe_float(row.get("spread"),safe_float(row.get("market_spread"))); team=_normalize_nfl_team(row.get("team"))
                if spread is not None and team in [away,home]: home_spread=spread if team==home else -spread
        base={**game,"phase":phase,"blocked":bool(blocks),"blocks":blocks,"away_market_odds":market_odds.get(away),"home_market_odds":market_odds.get(home),
              "away_market_prob":implied.get(away),"home_market_prob":implied.get(home),"away_payout":payouts.get(away),"home_payout":payouts.get(home),
              "market_total":market_total,"home_market_spread":home_spread,"price_status":"LIVE MARKET" if market_odds else "MODEL ONLY"}
        if blocks:
            cards.append(base); continue

        away_profile=away_rating["profile"]; home_profile=home_rating["profile"]
        env=_moneyline_game_environment(away,home,game["matchup"],game.get("scheduled_at"))
        away_inj=_moneyline_injury_context(away); home_inj=_moneyline_injury_context(home)
        away_rot=_preseason_moneyline_rotation(away,game_rows) if phase=="PRESEASON" else {"score":0.0,"confidence":1.0,"records":0,"notes":[]}
        home_rot=_preseason_moneyline_rotation(home,game_rows) if phase=="PRESEASON" else {"score":0.0,"confidence":1.0,"records":0,"notes":[]}
        regular_readiness=_moneyline_regular_readiness(away_profile,home_profile,away_inj,home_inj,env,away_ctx,home_ctx,market_odds) if phase=="REGULAR" else None
        weather_factor=safe_float(env.get("weather_pass_factor"),1.0) or 1.0
        away_probs=_moneyline_possession_probs(away_profile,home_profile,env_side=-0.02-env["home_edge"]*0.16,injury_off=away_inj["offense"],injury_def_opp=home_inj["defense"],preseason_rotation=away_rot["score"],weather_pass_factor=weather_factor)
        home_probs=_moneyline_possession_probs(home_profile,away_profile,env_side=env["home_edge"]*0.22,injury_off=home_inj["offense"],injury_def_opp=away_inj["defense"],preseason_rotation=home_rot["score"],weather_pass_factor=weather_factor)
        pace_vals=[x for x in [away_profile.get("pace"),home_profile.get("pace")] if x is not None]
        projected_plays=float(np.mean(pace_vals)) if pace_vals else 63.0
        drive_vals=[x for x in [away_profile.get("drives"),home_profile.get("drives")] if x is not None]
        drives=float(np.mean(drive_vals)) if drive_vals else 10.5
        drives=clamp(drives+(projected_plays-63.0)*0.055,8.4,13.0)
        if phase=="PRESEASON": drives=clamp(drives,9.0,12.0)

        seed=int(hashlib.sha256(f"{rng_seed}|{game['matchup']}|{phase}".encode()).hexdigest()[:8],16); rng=np.random.default_rng(seed)
        sim_n=max(5000,int(sims))
        outcomes=("td","fg","turnover","downs","safety","punt")
        pa=np.array([away_probs[k] for k in outcomes],dtype=float); pa=pa/pa.sum()
        ph=np.array([home_probs[k] for k in outcomes],dtype=float); ph=ph/ph.sum()
        # Use a common core drive count plus a Bernoulli extra possession. This preserves
        # realistic possession variance without Python loops across 18k simulations/game.
        base_drives=max(7,int(math.floor(drives)))
        extra_prob=clamp(drives-base_drives,0.0,1.0)
        away_counts=rng.multinomial(base_drives,pa,size=sim_n)
        home_counts=rng.multinomial(base_drives,ph,size=sim_n)
        if extra_prob>0:
            # NFL possessions are strongly correlated: when game pace creates an
            # extra drive, both teams usually gain one.  Keep a small asymmetric
            # tail for end-of-half/end-game possession differences.
            common_extra=rng.random(sim_n)<extra_prob
            away_extra=common_extra.copy(); home_extra=common_extra.copy()
            asym=rng.random(sim_n)<0.10
            if asym.any():
                coin=rng.random(int(asym.sum()))<0.5
                idx=np.where(asym)[0]
                away_extra[idx]=coin
                home_extra[idx]=~coin
            if away_extra.any():
                idx=np.where(away_extra)[0]; pick=rng.choice(len(outcomes),size=len(idx),p=pa); away_counts[idx,pick]+=1
            if home_extra.any():
                idx=np.where(home_extra)[0]; pick=rng.choice(len(outcomes),size=len(idx),p=ph); home_counts[idx,pick]+=1
        # Small independent scoring noise captures missed PATs/2pt tries and drive-level variance.
        away_scores=away_counts[:,0]*7.0+away_counts[:,1]*3.0+home_counts[:,4]*2.0
        home_scores=home_counts[:,0]*7.0+home_counts[:,1]*3.0+away_counts[:,4]*2.0
        away_tos=away_counts[:,2].astype(float); home_tos=home_counts[:,2].astype(float)
        ties=away_scores==home_scores
        if ties.any():
            base_home=clamp(0.50+0.035*(home_probs["strength"]-away_probs["strength"])+0.015,0.40,0.60)
            home_ot=rng.random(int(ties.sum()))<base_home
            home_scores[ties]+=home_ot.astype(float)*3.0
            away_scores[ties]+=(~home_ot).astype(float)*3.0
        away_wins=float(np.mean(away_scores>home_scores)); home_wins=1.0-away_wins
        # Preseason uncertainty shrink: unknown rotations should not manufacture 70% winners.
        rotation_conf=min(away_rot.get("confidence",1.0),home_rot.get("confidence",1.0))
        if phase=="PRESEASON":
            shrink=clamp(0.52+0.36*rotation_conf,0.52,0.86)
            home_wins=0.5+(home_wins-0.5)*shrink; away_wins=1.0-home_wins
        football_home_wins=float(home_wins); football_away_wins=float(away_wins)
        readiness_score=(regular_readiness or {}).get("score",72) if phase=="REGULAR" else 70
        calibrated_home,home_cal_meta=_moneyline_market_calibration(football_home_wins,implied.get(home),readiness_score,phase)
        calibrated_away=1.0-calibrated_home
        clv_state=_moneyline_clv_update(game["matchup"],implied,phase)
        totals=away_scores+home_scores; margins=home_scores-away_scores
        away_mean=float(np.mean(away_scores)); home_mean=float(np.mean(home_scores)); model_total=float(np.mean(totals)); blowout=float(np.mean(np.abs(margins)>=14))
        favorite=home if football_home_wins>=football_away_wins else away; favorite_prob=max(football_home_wins,football_away_wins)
        calibrated_favorite_prob=calibrated_home if favorite==home else calibrated_away
        total_pick="NO MARKET TOTAL"; total_prob=None; total_edge=None; total_over_prob=None
        if market_total is not None:
            over_prob=float(np.mean(totals>market_total)+0.5*np.mean(totals==market_total)); total_over_prob=over_prob
            total_pick="OVER" if model_total>market_total else "UNDER"; total_prob=over_prob if total_pick=="OVER" else 1-over_prob; total_edge=model_total-market_total
            if abs(total_edge)<1.5 or total_prob<0.56: total_pick="PASS"

        fam_weights={"offense":0.20,"quarterback":0.12,"situational":0.11,"explosive":0.06,"trench":0.11,"schedule":0.05,"defense":0.17,"pass_rush":0.07,"def_explosive":0.04,"ball_security":0.03,"takeaways":0.02,"discipline":0.01,"special_teams":0.01}
        contributions={}
        for k,w in fam_weights.items():
            # positive = home advantage
            h=home_profile["families"].get(k,0); a=away_profile["families"].get(k,0)
            contributions[k]=round((h-a)*w,3)
        contributions.update({"home_stadium":round(env["home_edge"]*0.18,3),"travel_rest":round((env.get("travel",{}).get("score",0) or 0)/100*0.10,3),
                              "injury_personnel":round((away_inj["offense"]+away_inj["defense"]-home_inj["offense"]-home_inj["defense"])*0.12,3),
                              "preseason_rotation":round((home_rot["score"]-away_rot["score"])*0.22,3)})
        top=sorted(contributions.items(),key=lambda kv:abs(kv[1]),reverse=True)
        total_inputs=away_profile["coverage"]+home_profile["coverage"]
        data_score=int(clamp(58+min(27,total_inputs*1.6)+(5 if len(market_odds)>=2 else 0)+(4 if market_total is not None else 0)+(4 if phase=="REGULAR" else int(4*rotation_conf)),0,99))
        if phase=="REGULAR" and regular_readiness:
            # Data score cannot outrun actual game-day readiness.
            data_score=min(data_score,int(clamp(regular_readiness["score"]+7,0,99)))
        reliability="HIGH" if data_score>=88 and favorite_prob>=0.60 else "MED" if data_score>=75 else "LOW"
        market_edge=None
        market_disagreement=False
        if favorite in implied and implied.get(favorite) is not None:
            market_edge=favorite_prob-implied[favorite]
            market_disagreement=abs(market_edge)>=0.08
            if abs(market_edge)>=0.12:
                reliability="LOW"
            elif abs(market_edge)>=0.08 and reliability=="HIGH":
                reliability="MED"
        if phase=="REGULAR" and regular_readiness and not regular_readiness.get("official_ready"):
            reliability="LOW"
        base.update({"blocked":False,"status":(("REGULAR READY" if regular_readiness and regular_readiness.get("official_ready") else "REGULAR DATA LIMITED") if phase=="REGULAR" else "PRESEASON MODEL READY"),"away_projection":round(away_mean,1),"home_projection":round(home_mean,1),"away_win_prob":round(football_away_wins,4),"home_win_prob":round(football_home_wins,4),
                     "football_away_win_prob":round(football_away_wins,4),"football_home_win_prob":round(football_home_wins,4),
                     "calibrated_away_win_prob":round(calibrated_away,4),"calibrated_home_win_prob":round(calibrated_home,4),
                     "away_model_odds":_probability_to_american(calibrated_away),"home_model_odds":_probability_to_american(calibrated_home),"favorite":favorite,"favorite_prob":round(favorite_prob,4),"calibrated_favorite_prob":round(calibrated_favorite_prob,4),"probability_calibration":home_cal_meta,
                     "model_total":round(model_total,1),"total_pick":total_pick,"total_prob":None if total_prob is None else round(total_prob,4),"total_over_prob":None if total_over_prob is None else round(total_over_prob,4),
                     "total_edge":None if total_edge is None else round(total_edge,1),"projected_plays":round(projected_plays,1),"projected_drives":round(drives,1),
                     "away_offense":round(away_rating["offense"],3),"home_offense":round(home_rating["offense"],3),"away_off_epa":away_rating.get("off_epa"),"home_off_epa":home_rating.get("off_epa"),
                     "blowout_prob":round(blowout,4),"data_score":data_score,"reliability":reliability,"sim_samples":sim_n,"weather_risk":env.get("weather_risk") or "LOW",
                     "expected_turnovers_away":round(float(np.mean(away_tos)),2),"expected_turnovers_home":round(float(np.mean(home_tos)),2),
                     "away_possession_probs":{k:round(v,4) for k,v in away_probs.items() if k!="strength"},"home_possession_probs":{k:round(v,4) for k,v in home_probs.items() if k!="strength"},
                     "football_contributions":contributions,"top_factors":top[:6],"market_model_edge":None if market_edge is None else round(market_edge,4),"calibrated_market_edge":None if favorite not in implied else round(calibrated_favorite_prob-implied[favorite],4),"market_disagreement":market_disagreement,"moneyline_clv":clv_state,
                     "away_injuries":away_inj,"home_injuries":home_inj,"away_preseason_rotation":away_rot,"home_preseason_rotation":home_rot,
                     "away_family_scores":{k:round(v,3) for k,v in (away_profile.get("families") or {}).items()},
                     "home_family_scores":{k:round(v,3) for k,v in (home_profile.get("families") or {}).items()},
                     "away_module_coverage":dict(away_profile.get("module_coverage") or {}),"home_module_coverage":dict(home_profile.get("module_coverage") or {}),
                     "away_profile_drives":round(safe_float(away_profile.get("drives"),10.5) or 10.5,2),"home_profile_drives":round(safe_float(home_profile.get("drives"),10.5) or 10.5,2),
                     "regular_season_readiness":regular_readiness,"official_moneyline_ready":bool(phase=="PRESEASON" or (regular_readiness and regular_readiness.get("official_ready") and reliability!="LOW")),
                     "game_environment":env,"model_note":"Possession simulation: current-priority offense/defense, QB quality, first downs, situational downs, red zone, OL/DL trenches, regressed turnovers/fumbles, penalties, special teams, injuries/final inactives, weather/stadium and travel/rest. Market is calibration/audit-only and can reduce conviction but never flip the football side. SOS-adjusted strength, current opponent defense and dual football/calibrated probabilities are active."})
        cards.append(base)
    return sorted(cards,key=lambda card:(str(card.get("scheduled_at") or "9999"),card.get("matchup","")))

def load_injury_bank():
    data=load_json(INJURY_FILE,{})
    bank=dict(data) if isinstance(data,dict) else {}
    df=_read_optional_csv(INJURY_OVERRIDE_CSV)
    if not df.empty:
        for _,r in df.iterrows():
            d={k:r.get(k) for k in df.columns}; pkey=norm(d.get("player"))
            if not pkey: continue
            rec=dict(bank.get(pkey) or {})
            for k,v in d.items():
                if _usable_context_value(v): rec[k]=v
            rec["source"]="verified_manual_injury_override"; bank[pkey]=rec
    return bank

def merge_nfl_context(row):
    """Attach real usage/team/injury context without ever changing the market identity.

    Supplemental files may contain generic rows such as prop=ALL.  Those values are
    context only and must never overwrite the live Underdog prop/line.
    """
    row=dict(row or {})
    original_identity={k:row.get(k) for k in [
        "player","prop","line","source","underdog_id","event_id","game_id","match_id",
        "team","opp","position","matchup","home_away"
    ]}
    original_prop=_canon_prop_label(original_identity.get("prop"))
    if original_prop not in ACTIVE_NFL_MARKETS:
        raise ValueError(f"Unsupported/unmapped NFL prop: {original_identity.get('prop')!r}")
    original_line=safe_float(original_identity.get("line"))
    if original_line is None or not _valid_market_line(original_prop, original_line, season_mode_for_row(row)):
        raise ValueError(f"Invalid {original_prop} line: {original_identity.get('line')!r}")
    row["prop"]=original_prop
    row["line"]=float(original_line)
    usage_bank = load_usage_bank()
    current_usage_bank = load_current_usage_bank()
    usage=_lookup_player_record(usage_bank,row.get("player"),row.get("team"),row.get("position"))
    current_usage=_lookup_player_record(current_usage_bank,row.get("player"),row.get("team"),row.get("position"))
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
        if not k or not _usable_context_value(v):
            continue
        if k not in row or not _usable_context_value(row.get(k)) or row.get(k) == "NFL":
            row[k]=v
    # v7.50 DATA IDENTITY: a prior-season fallback must never masquerade as current-season usage.
    # Preserve explicit prior/current/projected namespaces so early-season blending can work correctly.
    for k,v in usage.items():
        if k and _usable_context_value(v) and k not in {"player","team","position"}:
            row.setdefault("prior_"+str(k), v)
    _cu_games=safe_float(current_usage.get("current_games"),0) or 0
    _cu_season=safe_float(current_usage.get("source_season"),None)
    _true_current=bool(_cu_games >= 1 and (_cu_season is None or int(_cu_season)==int(NFL_CURRENT_SEASON)))
    row["current_usage_is_true_current"]=_true_current
    row["current_usage_games"]=int(_cu_games)
    row["current_usage_source_season"]=None if _cu_season is None else int(_cu_season)
    row["current_usage_freshness"]="CURRENT" if _true_current else "PRIOR_FALLBACK"
    for k,v in current_usage.items():
        if not k or not _usable_context_value(v):
            continue
        if _true_current:
            row["current_"+str(k) if not str(k).startswith("current_") and k not in {"player","team","position"} else str(k)]=v
            # Only true current-season samples may replace same-name workload fields.
            if k not in {"player","team","position","source_season"}: row[k]=v
            row["has_current_usage"] = True
        else:
            # Keep stale/fallback values as priors only; do not overwrite the 2025 historical baseline.
            if k not in {"player","team","position"}: row.setdefault("prior_"+str(k),v)
    # Fix generic live-feed labels after usage/model context is attached.
    if row.get("team") in [None, "", "NFL"]:
        if _usable_context_value(current_usage.get("team")):
            row["team"] = current_usage.get("team")
        elif _usable_context_value(usage.get("team")):
            row["team"] = usage.get("team")
    if row.get("position") in [None, ""]:
        if _usable_context_value(current_usage.get("position")):
            row["position"] = current_usage.get("position")
        elif _usable_context_value(usage.get("position")):
            row["position"] = usage.get("position")

    # If current-season nflverse has games, blend its rolling values into the row.
    current_bank=_current_season_context_bank(int(NFL_CURRENT_SEASON))
    current_player=_fuzzy_player_context(row.get("player"), current_bank.get("players",{}), team=row.get("team"), min_score=0.88)
    if current_player:
        for k,v in current_player.items():
            if _usable_context_value(v):
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
    current_opp_ctx=current_teams.get(opp,{}) if isinstance(current_teams.get(opp,{}),dict) else {}
    if not current_opp_ctx and opp in current_bank.get("teams",{}):
        current_opp_ctx=current_bank["teams"].get(opp,{})
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
        "ol_pass_pro_rank","ol_run_block_rank","wr_unit_rank","rb_unit_rank","qb_unit_rank",
        "drives_pg","points_per_drive","yards_per_drive","plays_per_drive","avg_drive_start_own_yardline",
        "red_zone_trips_pg","goal_to_go_td_rate","red_zone_fg_settlement_rate","pressure_allowed_rate",
        "sack_given_pressure_rate","neutral_epa_per_play","neutral_success_rate","neutral_pass_rate",
        "return_yards_pg","punt_return_yards_pg","punt_return_attempts_pg","net_punt_yards",
        "fg_over_expected_per_attempt","turnover_regression_proxy_pg","fumble_lost_share","explosive_40_rate",
        "ol_returning_starters_pct","ol_returning_core_count","ol_prior_core_count"
    ]
    for k in team_keys:
        if not _usable_context_value(row.get(k)) and _usable_context_value(team_ctx.get(k)):
            row[k]=team_ctx.get(k)
    for src, dst in [
        ("current_plays_pg", "plays_pg"),
        ("current_pass_rate", "pass_rate"),
        ("current_rush_rate", "rush_rate"),
        ("pbp_plays_pg", "pbp_plays_pg"),
        ("pbp_pass_rate", "pbp_pass_rate"),
        ("pbp_rush_rate", "pbp_rush_rate"),
    ]:
        if _usable_context_value(current_team_ctx.get(src)):
            row[dst]=current_team_ctx.get(src)
            row["has_current_team_context"] = True
    for k in team_keys:
        if _usable_context_value(current_team_ctx.get(k)):
            row[k]=current_team_ctx.get(k)
            row["has_current_team_context"] = True

    # Opponent defensive context.  Prefix advanced fields with opp_ so we never
    # accidentally confuse offense and defense.
    opp_keys = [
        "def_pass_rank","def_run_rank","def_slot_rank","def_te_rank","def_rb_rec_rank",
        "def_role_rank","coverage_grade","pressure_rate","def_pressure_rate",
        "pass_yards_allowed_pg","rush_yards_allowed_pg","rec_yards_allowed_pg","receptions_allowed_pg",
        "def_epa_allowed_per_play","def_success_allowed_rate","def_sacks_pg",
        "def_qb_hits_pg","def_fumbles_forced_pg","explosive_pass_allowed_rate",
        "explosive_rush_allowed_rate","def_explosive_pass_rank","def_explosive_run_rank",
        "def_pressure_rank","def_run_stop_rank","def_pressure_rate","def_sack_given_pressure_rate",
        "def_red_zone_trips_allowed_pg","def_neutral_epa_allowed","def_neutral_success_allowed_rate",
        "def_explosive_40_allowed_rate","def_first_downs_allowed_pg","def_first_down_rate_allowed",
        "def_third_down_allowed_rate","def_fourth_down_allowed_rate","def_red_zone_td_allowed_rate",
        "def_takeaways_pg","def_yards_per_play_allowed"
    ]
    for k in opp_keys:
        if _usable_context_value(opp_ctx.get(k)):
            if not _usable_context_value(row.get(k)):
                row[k]=opp_ctx.get(k)
            if not _usable_context_value(row.get("opp_"+k)):
                row["opp_"+k]=opp_ctx.get(k)
    # Current-season opponent defense must be allowed to replace the historical prior.
    # This fixes the asymmetry where the offense became current while its matchup stayed stale.
    for k in opp_keys:
        if _usable_context_value(current_opp_ctx.get(k)):
            row[k]=current_opp_ctx.get(k)
            row["opp_"+k]=current_opp_ctx.get(k)
            row["has_current_opponent_context"] = True
    injuries=load_injury_bank()
    inj=injuries.get(norm(row.get("player"))) or injuries.get(str(row.get("player") or ""))
    row["has_injury_context"]=bool(inj)
    row["injury_context_status"]="MATCHED" if inj else "NOT_LISTED"
    if inj and row.get("injury_status") in [None, ""]:
        row["injury_status"] = inj.get("status") if isinstance(inj,dict) else inj
    if isinstance(inj, dict):
        for k in ["practice_status","injury_note","body_part","limited_snap_risk","expected_snap_share"]:
            if _usable_context_value(inj.get(k)):
                row[k]=inj.get(k)

    final_inactives=_lookup_final_inactives(row)
    for k,v in final_inactives.items():
        if _usable_context_value(v):
            row[k]=v

    depth=_lookup_player_record(load_depth_chart_bank(),row.get("player"),row.get("team"),row.get("position"))
    for k,v in depth.items():
        if _usable_context_value(v):
            row[k]=v
            row["has_depth_chart_context"] = True
    role_override=_lookup_player_record(load_role_override_bank(),row.get("player"),row.get("team"),row.get("position"))
    if role_override:
        for k,v in role_override.items():
            if k not in {"player","team","position"} and _usable_context_value(v): row[k]=v
        row["has_verified_role_override"]=True

    weather_ctx=_lookup_weather_for_row(row)
    if weather_ctx:
        risk, pass_factor, weather_notes = _weather_risk_from_detail(weather_ctx)
        for k,v in weather_ctx.items():
            if _usable_context_value(v):
                row[f"weather_{k}"]=v
        if risk and risk != "LOW":
            row["weather_risk"]=risk
        row["weather_pass_factor"]=pass_factor
        row["weather_notes"]=weather_notes

    market_bank=load_market_context_bank()
    market=market_bank.get((norm(row.get("player")), str(row.get("prop") or ""), team)) or market_bank.get((norm(row.get("player")), str(row.get("prop") or ""), ""))
    if market:
        for k,v in market.items():
            if _usable_context_value(v):
                row[f"market_{k}"]=v
        row["has_market_context"] = True

    travel_ctx=_lookup_pair_context(load_travel_context_bank(), row)
    if travel_ctx:
        for k,v in travel_ctx.items():
            if _usable_context_value(v):
                row[k]=v
                row["has_travel_context"] = True
    elif team and opp:
        miles=great_circle_miles(team, opp)
        if miles:
            row["travel_miles"]=miles
            row["has_travel_context"] = True

    matchup_ctx=_lookup_pair_context(load_matchup_context_bank(), row)
    for k,v in matchup_ctx.items():
        if _usable_context_value(v):
            row[k]=v
            row["has_matchup_context"] = True

    qb_ctx=_lookup_qb_context(row)
    for k,v in qb_ctx.items():
        if _usable_context_value(v):
            row[k if str(k).startswith("qb_") else f"qb_{k}"]=v
            row["has_qb_context"] = True

    def_inj=load_defensive_injury_context()
    opp_def=def_inj.get(opp) if isinstance(def_inj, dict) else None
    if isinstance(opp_def, dict):
        for k,v in opp_def.items():
            if _usable_context_value(v):
                row[f"opp_def_injury_{k}"]=v
                row["has_defensive_injury_context"] = True

    splits=_lookup_player_prop_context(load_splits_context_bank(), row)
    for k,v in splits.items():
        if _usable_context_value(v):
            # player/prop/team are lookup identifiers, not projection inputs.
            # In particular the generic split file uses prop=ALL.
            if k in ["player","prop","team"]:
                continue
            row[f"split_{k}"]=v
            row["has_splits_context"] = True

    personnel=_lookup_pair_context(load_personnel_context_bank(), row)
    for k,v in personnel.items():
        if _usable_context_value(v):
            # Never let supplemental context replace live team/opponent/matchup identity.
            row[f"personnel_{k}"]=v
            row["has_personnel_context"] = True
    manual_override=_lookup_manual_override(row)
    for k,v in manual_override.items():
        if _usable_context_value(v):
            row[k]=v

    # FINAL MARKET-ID LOCK. Context is allowed to fill missing team/position/opponent,
    # but never to mutate player, prop, line, source, or feed IDs.
    for k in ["player","source","underdog_id","event_id","game_id","match_id"]:
        if original_identity.get(k) not in [None, ""]:
            row[k]=original_identity.get(k)
    row["prop"]=original_prop
    row["line"]=float(original_line)
    original_team=_normalize_nfl_team(original_identity.get("team"))
    if original_team:
        row["team"]=original_team
    original_opp=_normalize_nfl_team(original_identity.get("opp"))
    if original_opp:
        row["opp"]=original_opp
    if str(original_identity.get("position") or "").strip():
        row["position"]=str(original_identity.get("position") or "").upper().strip()
    original_matchup=_canonical_matchup(original_identity.get("matchup"), row.get("team"), row.get("opp"), original_identity.get("home_away"))
    if original_matchup:
        row["matchup"]=original_matchup
    # Modern Underdog rows often omit home_away even when matchup is oriented.
    # Infer it here so stadium, crowd-noise, travel, home/road splits, and weather
    # all use the correct venue instead of assuming every unknown row is away.
    inferred_ha=_infer_home_away_from_matchup(row)
    if inferred_ha:
        row["home_away"]=inferred_ha
    row["market_identity_locked"]=True
    # Savant is supplemental and fail-soft. It never replaces market identity,
    # workload, or the existing Phase 6/current-week context.
    try:
        row=attach_best_savant_context(row,SAVANT_DIR)
    except Exception as exc:
        row.update({
            "savant_status":"MISSING","savant_player_match":False,
            "savant_team_match":False,"savant_sample_size":0,
            "savant_reliability":0,"savant_error":str(exc)[:160],
        })
    # v7.50 semantic validity gates. Missing/contradictory opportunity forces PASS instead of fake confidence.
    if row.get("prop") in {"Receiving Yards","Receptions"}:
        _ro=shared_receiver_opportunity(row, {}) if "shared_receiver_opportunity" in globals() else {}
        if _ro.get("role_conflict"):
            row["data_integrity_block"]=_ro.get("role_conflict")
            row["market_integrity_status"]="ROLE_CONFLICT"
    if row.get("prop")=="Anytime TD":
        _rz=safe_float(row.get("red_zone_touch_share"),0) or 0
        _rzc=safe_float(row.get("red_zone_carries"),0) or 0; _rzt=safe_float(row.get("red_zone_targets"),0) or 0
        _games=safe_float(row.get("current_games"),0) or 0
        if _rz>0 or _rzc>0 or _rzt>0:
            quality="CURRENT_PBP" if _games>=1 and str(row.get("red_zone_data_quality") or "").upper()=="CURRENT_PBP" else "PRIOR_PBP"
            row["td_role_data_quality"]=quality; row["td_role_data_ready"]=True
        else:
            row["data_integrity_block"]="TD ROLE DATA MISSING: no verified current/prior red-zone carry/target/share evidence"
            row["td_role_data_quality"]="MISSING"; row["td_role_data_ready"]=False
    row["data_freshness_state"]="CURRENT" if bool(row.get("current_usage_is_true_current")) else "PRIOR_BASED_PRE_WEEK1"
    row["route_data_state"]="CURRENT_ACTUAL" if safe_float(row.get("current_route_participation")) is not None and (safe_float(row.get("current_games"),0) or 0)>=1 else ("PROXY" if _usable_context_value(row.get("route_participation_proxy")) else "PRIOR")
    row["injury_data_state"]="MATCHED" if row.get("has_injury_context") else "NO_PLAYER_REPORT"
    row["inactive_data_state"]="CONFIRMED" if row.get("final_inactives_confirmed") is True else "PENDING"
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

def _regular_offseason_transition(row, prop=None):
    """Describe how aggressively history should be trusted entering a new season.

    Returning starters retain a stronger prior.  Rookies, team changes, coordinator/head
    coach changes and clear depth-chart promotions lower the old-team prior without
    pretending we know the new role precisely before regular-season snaps exist.
    """
    row=row or {}; notes=[]; risk="LOW"; prior_mult=1.0
    rookie=str(row.get("rookie_flag") or row.get("split_rookie_flag") or "").upper() in {"1","TRUE","YES"}
    team_change=str(row.get("team_change") or row.get("new_team") or row.get("changed_team") or "").upper() in {"1","TRUE","YES","NEW"}
    scheme_change=str(row.get("new_offensive_coordinator") or row.get("new_head_coach") or row.get("scheme_change") or row.get("coaching_change") or "").upper() in {"1","TRUE","YES","NEW","HIGH"}
    depth=safe_float(row.get("depth_rank")); prev=safe_float(row.get("previous_depth_rank"))
    promoted=depth is not None and prev is not None and depth < prev
    if rookie:
        prior_mult*=0.50; risk="HIGH"; notes.append("Rookie transition: last-season player prior heavily reduced")
    if team_change:
        prior_mult*=0.62; risk="HIGH"; notes.append("New-team transition: old environment prior reduced")
    if scheme_change:
        prior_mult*=0.78; risk="MED" if risk!="HIGH" else risk; notes.append("Coaching/scheme transition prior reduction")
    if promoted:
        prior_mult*=0.72; risk="MED" if risk!="HIGH" else risk; notes.append("Depth-chart promotion: old role prior reduced")
    ol_cont=safe_float(row.get("ol_continuity"),safe_float(row.get("offensive_line_continuity")))
    if ol_cont is not None and ol_cont < 60 and prop in ["Passing Yards","Pass Attempts","Completions","Rushing Yards","Rush Attempts"]:
        prior_mult*=0.88; risk="MED" if risk!="HIGH" else risk; notes.append("Low OL continuity increases Week 1-3 uncertainty")
    return {"prior_multiplier":float(clamp(prior_mult,0.35,1.0)),"risk":risk,"notes":notes,"rookie":rookie,"team_change":team_change,"scheme_change":scheme_change,"promoted":promoted}


def _early_season_current_weight(games, transition=None):
    """Current-season evidence weight without counting L3/L5 as independent samples."""
    games=max(0,int(safe_float(games,0) or 0)); transition=transition or {}
    # One game is evidence, not three samples just because season/L3/L5 all contain it.
    table={0:0.00,1:0.24,2:0.39,3:0.53,4:0.64,5:0.72}
    weight=table.get(games,min(0.90,0.72+0.045*(games-5)))
    prior_mult=safe_float(transition.get("prior_multiplier"),1.0) or 1.0
    # If the old prior is structurally less relevant, current snaps take over faster.
    weight=1.0-(1.0-weight)*prior_mult
    return float(clamp(weight,0.0,0.92))


def _weighted_current_metric(row, season_keys, last5_keys, last3_keys):
    """Estimate the current-season metric without triple-counting the same early games."""
    season=_first_numeric(row, season_keys)
    l5=_first_numeric(row, last5_keys)
    l3=_first_numeric(row, last3_keys)
    games=int(safe_float((row or {}).get("current_games"),0) or 0)
    if season is None and l5 is None and l3 is None:
        return None
    # Season average is the anchor; L5/L3 only add recency once enough distinct games exist.
    if games <= 3:
        return season if season is not None else l3 if l3 is not None else l5
    vals=[]
    if season is not None: vals.append((season,0.68 if games<6 else 0.52))
    if l5 is not None: vals.append((l5,0.22 if games<6 else 0.30))
    if l3 is not None: vals.append((l3,0.10 if games<6 else 0.18))
    total=sum(w for _,w in vals)
    return sum(v*w for v,w in vals)/max(total,1e-9)

def current_week_role_engine(row, role, prop):
    """Blend season role with L5/L3 usage, depth chart, QB and OL changes."""
    row=row or {}; role=dict(role or {}); notes=[]; factor=1.0; risk="LOW"
    metrics={}
    transition=_regular_offseason_transition(row,prop)
    current_games=int(safe_float(row.get("current_games"),0) or 0)
    current_weight=_early_season_current_weight(current_games,transition)
    notes.extend(transition.get("notes") or [])
    if transition.get("risk")=="HIGH": risk="HIGH"
    elif transition.get("risk")=="MED" and risk!="HIGH": risk="MED"
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
            blended=old*(1.0-current_weight)+val*current_weight
            role[dst]=float(clamp(blended,0,100)); metrics[dst]=round(blended,2)
            metrics[f"{dst}_current_raw"]=round(val,2)
            if abs(val-old)>=8: notes.append(f"Current-week {dst} role moved {val-old:+.1f} pts; sample-weight {current_weight:.0%}")

    volume_specs={
        "pass":(["current_pass_attempts_pg","pass_attempts_pg"],["last5_pass_attempts_pg"],["last3_pass_attempts_pg"]),
        "targets":(["current_targets_pg","targets_pg"],["last5_targets_pg"],["last3_targets_pg"]),
        "carries":(["current_rush_attempts_pg","rush_attempts_pg"],["last5_rush_attempts_pg"],["last3_rush_attempts_pg"]),
    }
    volumes={k:_weighted_current_metric(row,*spec) for k,spec in volume_specs.items()}
    metrics.update({k:round(v,2) for k,v in volumes.items() if v is not None})
    if prop in ["Passing Yards","Passing TDs","Interceptions","Pass Attempts","Completions"]:
        baseline=_first_numeric(row,["pass_attempts_pg"])
        if volumes.get("pass") is not None and baseline and baseline>0:
            ratio=clamp(volumes["pass"]/baseline,0.82,1.18)
            factor*=1.0+(ratio-1.0)*current_weight
    elif prop in ["Receiving Yards","Receptions","Longest Reception"]:
        baseline=_first_numeric(row,["targets_pg"])
        if volumes.get("targets") is not None and baseline and baseline>0:
            ratio=clamp(volumes["targets"]/baseline,0.78,1.22)
            factor*=1.0+(ratio-1.0)*current_weight
    elif prop in ["Rushing Yards","Rush Attempts","Longest Rush"]:
        baseline=_first_numeric(row,["rush_attempts_pg","carries_pg"])
        if volumes.get("carries") is not None and baseline and baseline>0:
            ratio=clamp(volumes["carries"]/baseline,0.74,1.26)
            factor*=1.0+(ratio-1.0)*current_weight
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
    metrics["current_sample_weight"]=round(current_weight,3)
    metrics["current_games"]=current_games
    return role, {"factor":float(clamp(factor,0.84,1.16)),"risk":risk,"notes":notes,"metrics":metrics,"role_bucket":role_bucket,"active":bool(metrics or notes),"offseason_transition":transition,"current_sample_weight":round(current_weight,3)}

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
    team=_normalize_nfl_team(row.get("team")); opp=_normalize_nfl_team(row.get("opp"))
    home_away=_infer_home_away_from_matchup(row)
    # Prefer the oriented matchup itself for venue identity when available.
    away_team, matchup_home=_teams_from_matchup_text(row.get("matchup")) if "@" in str(row.get("matchup") or "") else ("","")
    home_team = matchup_home or (team if home_away=="HOME" else opp if home_away=="AWAY" else team)
    env=dict(STADIUM_ENV.get(home_team, {"stadium":"Unknown Stadium","crowd":"MODERATE","noise":1.0,"surface":"Unknown","roof":"Unknown","altitude":0}))
    # Current schedule/travel context is more authoritative for stadium/roof/surface.
    for key in ["stadium","roof","surface"]:
        if row.get(key) not in [None,"","nan","NaN"]:
            env[key]=row.get(key)
    env["home_team"]=home_team
    env["home_away"]=home_away
    return env

def apply_environment(base, row, prop):
    env=environment_for(row)
    factor=1.0
    notes=[]
    away=env.get("home_away")=="AWAY"
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

def learning_scale(player, prop, season_mode="REGULAR"):
    data=load_json(LEARN_FILE,{})
    mode=normalized_season_mode(season_mode) or "REGULAR"
    scoped=f"{mode}|{norm(player)}|{prop}"
    legacy=f"{norm(player)}|{prop}"
    value=data.get(scoped, data.get(legacy,1.0) if mode=="REGULAR" else 1.0)
    return safe_float(value,1.0) or 1.0

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
                    if prop == "Anytime TD":
                        td_cols=[c for c in ["receiving_tds","rushing_tds"] if c in g.columns]
                        if td_cols:
                            vals=g[td_cols].apply(pd.to_numeric,errors="coerce").fillna(0).sum(axis=1).tolist()
                            if len(vals)>=3: bank[(pkey,prop)]=[float(x) for x in vals[-20:]]
                        continue
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
    # Keep the assist independent of sportsbook pricing. Projection is the rule-model
    # output; line/edge/fair_prob are intentionally excluded to prevent market leakage.
    "projection", "data_score", "stability_score", "usage_quality",
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


def _graded_training_rows(prop=None, season_mode="REGULAR"):
    rows = load_json(RESULT_LOG, [])
    mode=normalized_season_mode(season_mode) or "REGULAR"
    out = []
    for r in rows:
        if safe_float(r.get("actual")) is None or safe_float(r.get("projection")) is None:
            continue
        if prop and r.get("prop") != prop:
            continue
        if graded_row_season_mode(r) != mode:
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
        vals=np.asarray(_historical_player_values(player,prop,max_games=20),dtype=float)
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


def _historical_player_values(player, prop, max_games=18):
    """Return recent values from the signature-cached empirical log bank."""
    bank=_empirical_prop_bank()
    key=(norm(player),str(prop))
    vals=bank.get(key)
    if vals is None:
        parts=key[0].split()
        if len(parts)>=2:
            initial,last=parts[0][:1],parts[-1]
            matches=[v for (name,market),v in bank.items() if market==str(prop) and len(name.split())>=2 and name.split()[0][:1]==initial and name.split()[-1]==last]
            if len(matches)==1:
                vals=matches[0]
    return [float(x) for x in (vals or [])[-int(max_games):]]

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
        baseline=recent_mean
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



def shared_game_opportunity_context(row):
    """One football volume budget shared by QB/RB/receiver yardage models.

    It is intentionally market-independent.  It estimates plays, drives, dropbacks and
    rushes from team identity/current context, then each player model allocates its role.
    """
    row=row or {}; team=_normalize_nfl_team(row.get("team")); opp=_normalize_nfl_team(row.get("opp"))
    teams=load_team_context(); current=load_current_team_context()
    tctx={**(teams.get(team,{}) if isinstance(teams.get(team),dict) else {}),**(current.get(team,{}) if isinstance(current.get(team),dict) else {})}
    octx={**(teams.get(opp,{}) if isinstance(teams.get(opp),dict) else {}),**(current.get(opp,{}) if isinstance(current.get(opp),dict) else {})}
    team_plays=safe_float(row.get("pbp_plays_pg"),safe_float(row.get("plays_pg"),safe_float(tctx.get("current_plays_pg"),safe_float(tctx.get("pbp_plays_pg"),63.0)))) or 63.0
    opp_plays=safe_float(octx.get("current_plays_pg"),safe_float(octx.get("pbp_plays_pg"),team_plays)) or team_plays
    plays=clamp(team_plays*0.72+opp_plays*0.28,54.0,73.0)
    pass_rate=safe_float(row.get("pbp_pass_rate"),safe_float(row.get("pass_rate"),safe_float(tctx.get("current_pass_rate"),safe_float(tctx.get("pbp_pass_rate"),57.0)))) or 57.0
    neutral=safe_float(row.get("neutral_pass_rate"),safe_float(tctx.get("neutral_pass_rate"),safe_float(tctx.get("early_down_pass_rate"))))
    proe=safe_float(row.get("proe"),safe_float(tctx.get("proe"),safe_float(tctx.get("league__proe"))))
    if neutral is not None: pass_rate=0.72*pass_rate+0.28*neutral
    if proe is not None: pass_rate+=clamp(proe,-8,8)*0.35
    pass_rate=clamp(pass_rate,43.0,72.0)
    sacks=safe_float(row.get("sacks_allowed_pg"),safe_float(tctx.get("sacks_allowed_pg"),2.4)) or 2.4
    dropbacks=plays*pass_rate/100.0
    sack_rate=clamp(sacks/max(15.0,dropbacks),0.025,0.115)
    attempts=dropbacks*(1.0-sack_rate)
    rushes=max(8.0,plays-dropbacks)
    drives=safe_float(tctx.get("drives_pg"),10.5) or 10.5
    drives=clamp(drives+(plays-63.0)*0.045,8.5,12.8)
    return {"plays":round(plays,2),"drives":round(drives,2),"pass_rate":round(pass_rate,2),"dropbacks":round(dropbacks,2),"pass_attempts":round(attempts,2),"rushes":round(rushes,2),"sack_rate":round(sack_rate,4)}


def passing_yards_stat_projection(row, role, cfg):
    """Passing Yards projection built from QB history + opportunity + matchup.

    Uses the saved Phase 6/nflverse database when available:
    - last season passing yards per game
    - pass attempts per game
    - estimated yards per attempt
    - team pass rate / expected plays
    - opponent pass defense rank
    - spread / total / stadium/weather

    The sportsbook line is not used to create the raw projection. It is compared only after simulation to determine OVER/UNDER.
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
        player_ypg = cfg.get("base", 235)
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

    # Shared team opportunity budget keeps QB/RB/receivers internally consistent.
    shared_opp=shared_game_opportunity_context(row)
    team_plays=shared_opp.get("plays",team_plays); pass_rate=shared_opp.get("pass_rate",pass_rate)
    pace_attempts=shared_opp.get("pass_attempts",team_plays*pass_rate/100.0)
    expected_attempts=(attempts_pg*0.58)+(pace_attempts*0.42)

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

    # Opponent pass defense: rank 1 is tough, rank 32 is weak. Raw yards
    # allowed is a fallback when a current feed has not supplied the rank yet.
    opp_rank = safe_float(row.get("def_pass_rank"), safe_float(row.get("opp_def_pass_rank")))
    pass_allowed = safe_float(row.get("pass_yards_allowed_pg"), safe_float(row.get("opp_pass_yards_allowed_pg")))
    if opp_rank is None:
        if pass_allowed is None:
            matchup_factor = 1.0
            notes.append("Opponent pass defense rank/yards allowed missing")
        else:
            matchup_factor = clamp(1 + (pass_allowed - 220.0) * 0.0015, 0.94, 1.06)
            notes.append(f"Pass defense fallback: {pass_allowed:.1f} yards allowed/game")
    else:
        matchup_factor = clamp(1 + (opp_rank - 16.5) * 0.0065, 0.90, 1.105)
        if opp_rank <= 8:
            notes.append("Top pass defense tax")
        elif opp_rank >= 25:
            notes.append("Weak pass defense boost")

    pressure = safe_float(
        row.get("opp_def_pressure_rate"),
        safe_float(row.get("def_pressure_rate"), safe_float(row.get("pressure_rate"))),
    )
    pressure_factor = 1.0
    if pressure is not None:
        pressure_factor = clamp(1 - (pressure - 24) * 0.0035, 0.94, 1.04)
    # Explicit OL vs pass-rush interaction.  Ranks are blended inside one capped
    # trench factor so sack/hit/pressure signals do not each tax the QB separately.
    ol_rank=safe_float(row.get("ol_pass_pro_rank"),safe_float(row.get("qb_pass_protection_rank")))
    opp_pressure_rank=safe_float(row.get("opp_def_pressure_rank"),safe_float(row.get("def_pressure_rank")))
    ol_parts=[]
    if ol_rank is not None: ol_parts.append(clamp((16.5-ol_rank)/15.5,-1,1))
    if opp_pressure_rank is not None: ol_parts.append(clamp((opp_pressure_rank-16.5)/15.5,-1,1))
    quick_pressure=safe_float(row.get("savant_quick_pressure_allowed"),safe_float(row.get("league__quick_pressure_allowed")))
    if quick_pressure is not None: ol_parts.append(clamp((20.0-quick_pressure)/8.0,-1,1))
    trench_score=float(np.mean(ol_parts)) if ol_parts else 0.0
    trench_factor=clamp(1.0+0.025*trench_score,0.965,1.035)
    projected_attempts = expected_attempts * script_attempt_factor * pass_rate_factor
    attempt_model = projected_attempts * ypa * total_factor * stadium_factor * matchup_factor * pressure_factor * trench_factor
    history_model = player_ypg * total_factor * stadium_factor * matchup_factor * pressure_factor
    projection = (history_model * 0.55) + (attempt_model * 0.45)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))

    # A final realism guard before market sanity. Passing yards game projections
    # generally should not be extreme unless the live line/team context justifies it.
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
        "opponent_pass_yards_allowed_pg": None if pass_allowed is None else round(pass_allowed,2),
        "game_total": round(total, 2),
        "spread": round(spread, 2),
        "matchup_factor": round(matchup_factor, 3),
        "stadium_factor": round(stadium_factor, 3),
        "total_factor": round(total_factor, 3),
        "pressure_factor": round(pressure_factor, 3),
        "trench_factor": round(trench_factor, 3),
        "shared_game_opportunity": shared_opp,
        "final_pre_market": round(projection, 2),
        "context_source": row.get("passing_context_bank_source"),
        "model_match_status": row.get("model_match_status"),
        "model_player_match": row.get("model_player_match"),
    }
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}


def shared_receiver_opportunity(row, role=None):
    """Single authoritative route -> target opportunity object for Receiving Yards + Receptions."""
    row=dict(row or {}); role=dict(role or {})
    shared=shared_game_opportunity_context(row)
    attempts=safe_float(shared.get("pass_attempts"),None)
    if attempts is None:
        plays=safe_float(row.get("pbp_plays_pg"),safe_float(row.get("plays_pg"),62)) or 62
        pr=safe_float(row.get("pbp_pass_rate"),safe_float(row.get("pass_rate"),56)) or 56
        attempts=plays*pr/100.0
    games=safe_float(row.get("current_games"),0) or 0
    route=safe_float(row.get("current_route_participation") if games>=1 else None,
                     safe_float(row.get("route_participation"),safe_float(row.get("route_participation_proxy"),safe_float(role.get("route"),65))))
    route_source="CURRENT_ACTUAL" if games>=1 and safe_float(row.get("current_route_participation")) is not None else "PRIOR_ACTUAL"
    depth_rank=int(safe_float(row.get("depth_rank"),9) or 9)
    starter=bool(row.get("starter")) or str(row.get("role") or "").upper()=="STARTER" or depth_rank==1
    pre_target_share=safe_float(row.get("current_target_share") if games>=1 else None,safe_float(row.get("target_share"),0)) or 0
    if route is None or (route < 20 and starter and pre_target_share >= 10):
        pos=str(row.get("position") or "").upper()
        if pos=="WR": route=90 if depth_rank==1 else 78 if depth_rank==2 else 64
        elif pos=="TE": route=80 if depth_rank==1 else 58
        elif pos in {"RB","FB"}: route=52 if depth_rank==1 else 32
        route_source="DEPTH_ROLE_PROXY"
    route=clamp(route,5,100)
    routes=attempts*route/100.0
    prior_targets=safe_float(row.get("prior_targets_pg"),safe_float(row.get("targets_pg")))
    cur_targets=safe_float(row.get("current_targets_pg")) if games>=1 else None
    transition=_regular_offseason_transition(row,"Receiving Yards")
    w=_early_season_current_weight(games,transition)
    targets_pg=prior_targets
    if cur_targets is not None and cur_targets>0 and games>=1:
        targets_pg=(prior_targets or cur_targets)*(1-w)+cur_targets*w
    target_share=safe_float(row.get("current_target_share") if games>=1 else None,safe_float(row.get("target_share"),safe_float(role.get("target"),15)))
    tprr=safe_float(row.get("current_targets_per_route") if games>=1 else None,safe_float(row.get("targets_per_route"),safe_float(row.get("target_per_route_run"))))
    if tprr is not None and tprr>1.5: tprr/=100.0
    if tprr is None or tprr<=0:
        tprr=(targets_pg/max(1.0,routes)) if targets_pg is not None else clamp((target_share/100.0)/max(.15,route/100.0),.08,.34)
    tprr=clamp(tprr,.06,.38)
    route_targets=routes*tprr
    share_targets=attempts*(target_share/100.0)
    if targets_pg is None: targets_pg=share_targets
    projected_targets=0.56*route_targets+0.24*share_targets+0.20*targets_pg
    # Semantic role guard: conflicting route/share inputs should not create an official play.
    conflict=None
    snap=safe_float(row.get("snap_share"))
    pos=str(row.get("position") or "").upper()
    if route<20 and target_share>=12 and pos in {"WR","TE"} and route_source!="DEPTH_ROLE_PROXY":
        conflict=f"ROLE DATA CONFLICT: route participation {route:.1f}% vs target share {target_share:.1f}%"
    if snap is not None and snap>=65 and route<20 and pos in {"WR","TE"}: conflict=f"ROLE DATA CONFLICT: snap share {snap:.1f}% vs route participation {route:.1f}%"
    return {"pass_attempts":attempts,"route_participation":route,"projected_routes":routes,"target_share":target_share,
            "targets_per_route":tprr,"projected_targets":projected_targets,"prior_targets_pg":prior_targets,
            "current_targets_pg":cur_targets,"current_weight":w,"role_conflict":conflict,"route_source":route_source,"shared_game_opportunity":shared}

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
    route_part=safe_float(row.get("current_route_participation") if (safe_float(row.get("current_games"),0) or 0)>=1 else None, safe_float(row.get("route_participation"), safe_float(row.get("route_participation_proxy"), None)))
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
        rec_ypg = cfg.get("base", 52)
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
    receiver_opp=shared_receiver_opportunity(row,role)
    shared_opp=receiver_opp.get("shared_game_opportunity",{})
    team_plays=shared_opp.get("plays",team_plays); pass_rate=shared_opp.get("pass_rate",pass_rate)
    implied_team_attempts=receiver_opp.get("pass_attempts",team_plays*pass_rate/100.0)
    # v7.50 shared receiver opportunity: identical routes/targets feed Receiving Yards and Receptions.
    route_part=receiver_opp.get("route_participation")
    route_part_frac=None if route_part is None else clamp(route_part/100.0 if route_part>1.5 else route_part,0.0,1.0)
    expected_routes=implied_team_attempts*(route_part_frac if route_part_frac is not None else clamp(safe_float(role.get("route"),65)/100.0,0.20,0.98))
    tprr=safe_float(row.get("targets_per_route"),safe_float(row.get("target_per_route_run")))
    if tprr is not None and tprr>1.5: tprr/=100.0
    if tprr is None or tprr<=0:
        if expected_routes>0 and targets_pg is not None:
            tprr=targets_pg/max(1.0,expected_routes)
        elif target_share is not None and route_part_frac and route_part_frac>0:
            ts=target_share/100.0 if target_share>1.5 else target_share
            tprr=ts/max(0.15,route_part_frac)
        else:
            tprr=0.20 if pos=="WR" else 0.18 if pos=="TE" else 0.16
    tprr=clamp(tprr,0.06,0.38)
    route_targets=expected_routes*tprr
    share_targets=implied_team_attempts*(target_share/100.0 if target_share is not None and target_share>1.5 else (target_share or 0)) if target_share is not None else targets_pg
    expected_targets=receiver_opp.get("projected_targets", route_targets*0.56+(share_targets if share_targets is not None else targets_pg)*0.24+targets_pg*0.20)
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
    # Opponent role/pass defense: higher rank = easier. Raw receiving yards
    # allowed is a fallback when rank generation has not completed yet.
    role_rank=safe_float(row.get("def_role_rank"), safe_float(row.get("opp_def_role_rank")))
    pass_rank=safe_float(row.get("def_pass_rank"), safe_float(row.get("opp_def_pass_rank")))
    rec_allowed=safe_float(row.get("rec_yards_allowed_pg"), safe_float(row.get("opp_rec_yards_allowed_pg")))
    opp_rank=role_rank if role_rank is not None else pass_rank
    if opp_rank is None:
        if rec_allowed is None:
            matchup_factor=1.0; notes.append("Opponent receiving/pass defense rank/yards allowed missing")
        else:
            matchup_factor=clamp(1 + (rec_allowed-215.0)*0.0014,0.94,1.06)
            notes.append(f"Receiving defense fallback: {rec_allowed:.1f} yards allowed/game")
    else:
        matchup_factor=clamp(1 + (opp_rank-16.5)*0.0058, 0.90, 1.10)
        if opp_rank <= 8: notes.append("Top receiving/pass defense tax")
        elif opp_rank >= 25: notes.append("Weak receiving/pass defense boost")
    coverage=safe_float(row.get("coverage_grade"), safe_float(row.get("opp_coverage_grade")))
    coverage_factor=1.0
    if coverage is not None:
        coverage_factor=clamp(1 - (coverage-60)*0.0025, 0.94, 1.04)
    # NGS/route skill context. These are small efficiency modifiers only;
    # they never replace route/target opportunity.
    separation=safe_float(row.get("avg_separation"),safe_float(row.get("target_separation")))
    cushion=safe_float(row.get("avg_cushion"))
    cpoe_rec=safe_float(row.get("catch_pct_above_expectation"))
    man_rate=safe_float(row.get("man_rate"),safe_float(row.get("opp_man_rate")))
    zone_rate=safe_float(row.get("zone_rate"),safe_float(row.get("opp_zone_rate")))
    yprr_man=safe_float(row.get("yprr_vs_man"),safe_float(row.get("yards_per_route_vs_man")))
    yprr_zone=safe_float(row.get("yprr_vs_zone"),safe_float(row.get("yards_per_route_vs_zone")))
    skill_factor=1.0
    if separation is not None:
        skill_factor*=clamp(1+(separation-3.0)*0.018,0.96,1.04)
    if cpoe_rec is not None:
        skill_factor*=clamp(1+cpoe_rec*0.0025,0.97,1.03)
    if man_rate is not None and zone_rate is not None and yprr_man is not None and yprr_zone is not None:
        mr=man_rate/100.0 if man_rate>1.5 else man_rate
        zr=zone_rate/100.0 if zone_rate>1.5 else zone_rate
        denom=max(1e-6,mr+zr)
        matchup_yprr=(mr*yprr_man+zr*yprr_zone)/denom
        skill_factor*=clamp(1+(matchup_yprr-1.65)*0.025,0.96,1.04)
        notes.append("Man/zone receiver efficiency blend active")
    catch_rate=safe_float(row.get("catch_rate"),safe_float(row.get("catch_pct")))
    if catch_rate is None and receptions_pg is not None and targets_pg:
        catch_rate=receptions_pg/max(1.0,targets_pg)
    if catch_rate is None: catch_rate=0.66 if pos=="WR" else 0.69 if pos=="TE" else 0.76
    if catch_rate>1.5: catch_rate/=100.0
    catch_rate=clamp(catch_rate,0.40,0.92)
    ypr=safe_float(row.get("yards_per_reception"))
    if ypr is None or ypr<=0: ypr=ypt/max(0.35,catch_rate)
    ypr=clamp(ypr,5.0,22.0)
    caught_yard_model=expected_targets*catch_rate*ypr
    target_model=(expected_targets*ypt*0.50+caught_yard_model*0.50)*script_factor*pass_rate_factor*total_factor*stadium_factor*matchup_factor*coverage_factor*skill_factor
    history_model=rec_ypg*script_factor*total_factor*stadium_factor*matchup_factor*coverage_factor*skill_factor
    # Opportunity should lead; history remains a stabilizer, especially early season.
    projection=(history_model*0.40)+(target_model*0.60)
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    projection=clamp(projection, 4, 185)
    breakdown={
        "player_rec_ypg": round(rec_ypg,2),
        "targets_pg": round(targets_pg,2),
        "projected_routes": round(expected_routes,2),
        "targets_per_route": round(tprr,3),
        "projected_targets": round(expected_targets,2),
        "catch_rate": round(catch_rate,3),
        "yards_per_reception": round(ypr,3),
        "yards_per_target": round(ypt,3),
        "target_model": round(target_model,2),
        "history_model": round(history_model,2),
        "team_pass_rate": round(pass_rate,2),
        "team_plays_pg": round(team_plays,2),
        "opponent_receiving_def_rank": None if opp_rank is None else int(round(opp_rank)),
        "opponent_receiving_yards_allowed_pg": None if rec_allowed is None else round(rec_allowed,2),
        "game_total": round(total,2),
        "spread": round(spread,2),
        "matchup_factor": round(matchup_factor,3),
        "stadium_factor": round(stadium_factor,3),
        "total_factor": round(total_factor,3),
        "coverage_factor": round(coverage_factor,3),
        "skill_factor": round(skill_factor,3),
        "avg_separation": None if separation is None else round(separation,3),
        "avg_cushion": None if cushion is None else round(cushion,3),
        "receiver_cpoe": None if cpoe_rec is None else round(cpoe_rec,3),
        "shared_game_opportunity": shared_opp,
        "final_pre_market": round(projection,2),
        "context_source": row.get("receiving_context_bank_source"),
        "model_match_status": row.get("model_match_status"),
        "model_player_match": row.get("model_player_match"),
    }
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def rushing_yards_stat_projection(row, role, cfg):
    """Position-aware rushing model.

    RB/FB: team rush volume -> carry share -> YPC -> script/trenches.
    QB: designed runs + pressure-created scrambles - kneels -> QB rush efficiency.
    """
    row=dict(row or {}); notes=[]; line=safe_float(row.get("line")); pos=str(row.get("position") or "").upper()
    rush_ypg=safe_float(row.get("rushing_yards_pg")); current_games=safe_float(row.get("current_games"),0) or 0
    cur_ypg=safe_float(row.get("current_rushing_yards_pg")); last3_ypg=safe_float(row.get("last3_rushing_yards_pg"))
    team_plays=safe_float(row.get("pbp_plays_pg"),safe_float(row.get("plays_pg"),62)) or 62
    rush_rate=safe_float(row.get("pbp_rush_rate"),safe_float(row.get("rush_rate"),44)) or 44
    spread=safe_float(row.get("spread"),0) or 0; total=safe_float(row.get("game_total"),44) or 44
    shared_opp=shared_game_opportunity_context(row); team_plays=shared_opp.get("plays",team_plays)
    team_rushes=shared_opp.get("rushes",team_plays*rush_rate/100.0); rush_rate=100.0*team_rushes/max(1.0,team_plays)
    run_rank=safe_float(row.get("def_run_rank"),safe_float(row.get("opp_def_run_rank")))
    rush_allowed=safe_float(row.get("rush_yards_allowed_pg"),safe_float(row.get("opp_rush_yards_allowed_pg")))
    if run_rank is None:
        matchup_factor=1.0 if rush_allowed is None else clamp(1+(rush_allowed-115.0)*0.0020,0.94,1.06)
    else:
        matchup_factor=clamp(1+(run_rank-16.5)*0.006,0.90,1.10)
    run_block=safe_float(row.get("ol_run_block_rank"),safe_float(row.get("ol_run_block_proxy_rank"),safe_float(row.get("run_block_rank"))))
    run_stop=safe_float(row.get("def_run_stop_rank"),safe_float(row.get("opp_def_run_stop_rank")))
    trench_factor=1.0
    if run_block is not None and run_stop is not None:
        if run_block<=8 and run_stop>=24: trench_factor*=1.018; notes.append("Run-blocking trench edge")
        elif run_block>=24 and run_stop<=8: trench_factor*=0.972; notes.append("Run-blocking trench mismatch")
    weather=str(row.get("weather_risk") or "").upper(); weather_factor=1.01 if weather in ["WIND","RAIN","SNOW","SEVERE"] else 1.0
    transition=_regular_offseason_transition(row,"Rushing Yards"); sample_w=_early_season_current_weight(current_games,transition)

    if pos=="QB":
        # QB carries are not RB carry-share events. Separate designed runs and scrambles.
        designed=safe_float(row.get("designed_rush_attempts_pg"),safe_float(row.get("qb_designed_runs_pg")))
        scrambles=safe_float(row.get("scrambles_pg"),safe_float(row.get("qb_scrambles_pg")))
        kneels=safe_float(row.get("kneels_pg"),safe_float(row.get("qb_kneels_pg"),0)) or 0
        raw_qb_carries=safe_float(row.get("qb_rush_attempts_pg"),safe_float(row.get("rush_attempts_pg"),safe_float(row.get("carries_pg"))))
        dropbacks=shared_opp.get("pass_attempts",team_plays*(safe_float(row.get("pass_rate"),56) or 56)/100.0)
        pressure_rate=safe_float(row.get("opp_def_pressure_rate"),safe_float(row.get("def_pressure_rate")))
        scramble_rate=safe_float(row.get("scramble_rate"),safe_float(row.get("qb_scramble_rate")))
        if scramble_rate is not None:
            scramble_rate=scramble_rate/100.0 if scramble_rate>1.5 else scramble_rate
        if scrambles is None:
            if scramble_rate is not None:
                scrambles=dropbacks*scramble_rate
            elif raw_qb_carries is not None:
                scrambles=max(0.0,raw_qb_carries-max(0.0,safe_float(designed,0) or 0)-kneels)
            else:
                scrambles=2.4
                notes.append("QB scramble fallback used")
        if designed is None:
            designed=max(0.5,(raw_qb_carries or 4.2)-scrambles-kneels)
        if pressure_rate is not None:
            pr=pressure_rate/100.0 if pressure_rate>1.5 else pressure_rate
            scrambles*=clamp(0.88+pr*0.55,0.90,1.10)
        expected_carries=max(0.0,designed+scrambles-kneels)
        cur_carries=safe_float(row.get("current_rush_attempts_pg")); last3_carries=safe_float(row.get("last3_rush_attempts_pg"))
        if cur_carries is not None and current_games>=1:
            recent=cur_carries if last3_carries is None else 0.72*cur_carries+0.28*last3_carries
            expected_carries=expected_carries*(1-sample_w)+recent*sample_w
        if rush_ypg is None or rush_ypg<=0: rush_ypg=cfg.get("base",28)
        if cur_ypg is not None and current_games>=1:
            recent_y=cur_ypg if last3_ypg is None else 0.72*cur_ypg+0.28*last3_ypg
            rush_ypg=rush_ypg*(1-sample_w)+recent_y*sample_w
        ypc=safe_float(row.get("qb_yards_per_carry"),safe_float(row.get("yards_per_carry")))
        if ypc is None or ypc<=0: ypc=rush_ypg/max(1.0,raw_qb_carries or expected_carries)
        ypc=clamp(ypc,3.0,8.5)
        # Trailing can create more scrambles; big leads reduce dropback-created rushes.
        script_factor=1.0
        if spread>=6: script_factor+=0.035
        elif spread<=-8: script_factor-=0.030
        projection=(expected_carries*ypc*0.68+rush_ypg*0.32)*script_factor*matchup_factor*weather_factor
        projection=clamp(projection,0,125)
        breakdown={"rush_model_type":"QB_DESIGNED_PLUS_SCRAMBLE","player_rush_ypg":round(rush_ypg,2),"designed_runs":round(designed,2),"projected_scrambles":round(scrambles,2),"kneels":round(kneels,2),"projected_carries":round(expected_carries,2),"yards_per_carry":round(ypc,3),"sample_weight":round(sample_w,3),"matchup_factor":round(matchup_factor,3),"weather_factor":round(weather_factor,3),"shared_game_opportunity":shared_opp,"final_pre_market":round(projection,2)}
        notes.append("QB rushing model: designed runs + scrambles - kneels")
        return float(projection),{"active":True,"breakdown":breakdown,"notes":notes}

    carries_pg=safe_float(row.get("rush_attempts_pg"),safe_float(row.get("carries_pg")))
    carry_share=safe_float(row.get("carries_share"),role.get("carry",40)) or role.get("carry",40)
    if carries_pg is None or carries_pg<=0:
        carries_pg=(team_plays*rush_rate/100.0)*(carry_share/100.0); notes.append("Carries estimated from team rush rate and carry share")
    cur_carries=safe_float(row.get("current_rush_attempts_pg")); last3_carries=safe_float(row.get("last3_rush_attempts_pg"))
    if cur_carries is not None and current_games>=1:
        recent=cur_carries if last3_carries is None else 0.72*cur_carries+0.28*last3_carries
        carries_pg=carries_pg*(1-sample_w)+recent*sample_w
    if rush_ypg is None or rush_ypg<=0: rush_ypg=cfg.get("base",49); notes.append("Rushing yards fallback used")
    if cur_ypg is not None and current_games>=1:
        recent_y=cur_ypg if last3_ypg is None else 0.72*cur_ypg+0.28*last3_ypg
        rush_ypg=rush_ypg*(1-sample_w)+recent_y*sample_w
    ypc=safe_float(row.get("yards_per_carry")); ypc=ypc if ypc and ypc>0 else rush_ypg/max(1.0,carries_pg); ypc=clamp(ypc,2.8,6.2)
    expected_carries=0.70*carries_pg+0.30*(team_rushes*carry_share/100.0)
    script_factor=1.0
    if spread<=-6: script_factor+=0.055; notes.append("Rushing volume boost: favorite/lead script")
    elif spread>=6: script_factor-=0.070; notes.append("Rushing volume tax: trailing script")
    elif abs(spread)<=3: script_factor+=0.012
    total_factor=clamp(1+(total-44)*0.003,0.96,1.04)
    if str(row.get("run_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]: matchup_factor*=1.016
    if str(row.get("pass_funnel") or "").upper() in ["TRUE","YES","1","HIGH"]: matchup_factor*=0.992
    projection=(rush_ypg*0.48+expected_carries*ypc*0.52)*script_factor*total_factor*matchup_factor*trench_factor*weather_factor
    projection=clamp(projection,1,175)
    breakdown={"rush_model_type":"RB_CARRY_SHARE","player_rush_ypg":round(rush_ypg,2),"rush_attempts_pg":round(carries_pg,2),"projected_carries":round(expected_carries,2),"yards_per_carry":round(ypc,3),"team_rush_rate":round(rush_rate,2),"team_plays_pg":round(team_plays,2),"opponent_run_def_rank":None if run_rank is None else int(round(run_rank)),"opponent_rush_yards_allowed_pg":None if rush_allowed is None else round(rush_allowed,2),"sample_weight":round(sample_w,3),"script_factor":round(script_factor,3),"matchup_factor":round(matchup_factor,3),"trench_factor":round(trench_factor,3),"weather_factor":round(weather_factor,3),"shared_game_opportunity":shared_opp,"final_pre_market":round(projection,2)}
    return float(projection),{"active":True,"breakdown":breakdown,"notes":notes}

def pass_attempts_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    att_pg=safe_float(row.get("current_pass_attempts_pg"), safe_float(row.get("last5_pass_attempts_pg"), safe_float(row.get("pass_attempts_pg"))))
    team_plays=safe_float(row.get("pbp_plays_pg"), safe_float(row.get("plays_pg"), 62)) or 62
    pass_rate=safe_float(row.get("pbp_pass_rate"), safe_float(row.get("pass_rate"), 56)) or 56
    if att_pg is None or att_pg <= 0:
        att_pg=cfg.get("base", 33.5)
        notes.append("Pass attempts model-data fallback used")
    projected_dropbacks=team_plays*pass_rate/100.0
    spread=safe_float(row.get("spread"),0) or 0
    total=safe_float(row.get("game_total"),44) or 44
    script_factor=1.0 + clamp(spread, -10, 10)*0.012
    total_factor=clamp(1+(total-44)*0.006,0.94,1.06)
    pace_factor=1.0  # v7.50: pace is already represented by projected team plays; do not double-count it
    pressure=safe_float(row.get("opp_def_pressure_rank"), safe_float(row.get("def_pressure_rank")))
    pressure_factor=1.0
    if pressure is not None and pressure <= 8:
        pressure_factor*=0.985; notes.append("Strong pressure can reduce attempts/completion rhythm")
    projection=(att_pg*0.52 + projected_dropbacks*0.48)*script_factor*total_factor*pace_factor*pressure_factor
    consensus=safe_float(row.get("market_consensus_line"), safe_float(row.get("market_consensus"), safe_float(row.get("market_best_line"))))
    projection=clamp(projection,5,62)
    breakdown={"pass_attempts_pg":round(att_pg,2),"projected_dropbacks":round(projected_dropbacks,2),"team_pass_rate":round(pass_rate,2),"team_plays_pg":round(team_plays,2),"script_factor":round(script_factor,3),"total_factor":round(total_factor,3),"pace_factor":round(pace_factor,3),"pressure_factor":round(pressure_factor,3)}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def completions_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    comp_pg=safe_float(row.get("current_completions_pg"), safe_float(row.get("completions_pg")))
    attempts=safe_float(row.get("current_pass_attempts_pg"), safe_float(row.get("last5_pass_attempts_pg"), safe_float(row.get("pass_attempts_pg"))))
    if attempts is None or attempts <= 0:
        attempts=33.5
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
    projection=clamp(projection,2,45)
    breakdown={"projected_attempts":round(pass_att_base,2),"completion_rate":round(completion_rate,2),"rate_factor":round(rate_factor,3),"completions_pg":None if comp_pg is None else round(comp_pg,2),"attempt_model":pass_att_info.get("breakdown",{})}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}

def receptions_stat_projection(row, role, cfg):
    row=dict(row or {}); notes=[]
    opp=shared_receiver_opportunity(row,role)
    targets=float(opp.get("projected_targets") or 0)
    rec_pg=safe_float(row.get("receptions_pg")); games=safe_float(row.get("current_games"),0) or 0
    cur_rec=safe_float(row.get("current_receptions_pg")) if games>=1 else None
    catch_rate=safe_float(row.get("current_catch_rate") if games>=1 else None,safe_float(row.get("catch_rate"),safe_float(row.get("reception_rate"))))
    if catch_rate is None:
        base_targets=safe_float(row.get("targets_pg"))
        catch_rate=(rec_pg/max(1,base_targets)) if rec_pg is not None and base_targets else (0.69 if str(row.get("position") or "").upper()=="TE" else 0.64)
    if catch_rate>1.5: catch_rate/=100.0
    catch_rate=clamp(catch_rate,.40,.92)
    coverage=safe_float(row.get("coverage_grade")); matchup=1.0
    if coverage is not None: matchup=clamp(1-(coverage-60)*.0018,.965,1.025)
    qb_status=str(row.get("qb_status") or row.get("qb_injury_status") or "").upper()
    qb=0.94 if any(x in qb_status for x in ["OUT","BACKUP","DOUBTFUL"]) else .975 if any(x in qb_status for x in ["QUESTION","LIMIT"]) else 1.0
    projection=targets*catch_rate*matchup*qb
    prior_rec=rec_pg
    if games>=1 and cur_rec is not None:
        w=opp.get("current_weight",0); prior_rec=prior_rec or cur_rec; projection=projection*(1-.20*w)+(prior_rec*(1-w)+cur_rec*w)*(.20*w)
    projection=clamp(projection,0,16)
    if opp.get("role_conflict"): notes.append(opp["role_conflict"])
    return float(projection),{"active":True,"breakdown":{"projected_routes":round(opp.get("projected_routes",0),2),"targets_pg":round(targets,2),"projected_targets":round(targets,2),"catch_rate":round(catch_rate*100,2),"route_participation":round(opp.get("route_participation",0),2),"target_share":round(opp.get("target_share",0),2),"targets_per_route":round(opp.get("targets_per_route",0),3),"current_weight":round(opp.get("current_weight",0),3),"qb_factor":round(qb,3),"matchup_factor":round(matchup,3)},"notes":notes}

def rush_attempts_stat_projection(row, role, cfg):
    notes=[]
    line=safe_float(row.get("line"))
    carries_pg=safe_float(row.get("current_rush_attempts_pg"), safe_float(row.get("last5_rush_attempts_pg"), safe_float(row.get("rush_attempts_pg"))))
    if carries_pg is None or carries_pg <= 0:
        carries_pg=cfg.get("base",13.5)
        notes.append("Rush attempts model-data fallback used")
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
    projection=clamp(projection,0,38)
    breakdown={"rush_attempts_pg":round(carries_pg,2),"expected_team_rushes":round(expected_team_rushes,2),"carry_share":round(carry_share,2),"script_factor":round(script_factor,3)}
    return float(projection), {"active": True, "breakdown": breakdown, "notes": notes}


def _internal_team_scoring_environment(row):
    """Market-independent team scoring environment for TD/FG props.

    Uses football production (EPA, success, drive efficiency, red-zone efficiency), never the
    sportsbook team total.  Returns a bounded multiplier centered around 1.0.
    """
    row=row or {}
    ppd=safe_float(row.get("points_per_drive"))
    epa=safe_float(row.get("epa_per_play"))
    success=safe_float(row.get("success_rate"))
    rz=safe_float(row.get("red_zone_td_rate"))
    first=safe_float(row.get("first_down_rate"))
    parts=[]
    if ppd is not None: parts.append(clamp(1+(ppd-2.05)*0.16,0.86,1.16))
    if epa is not None: parts.append(clamp(1+epa*0.38,0.90,1.11))
    if success is not None: parts.append(clamp(1+(success-44.5)*0.006,0.92,1.08))
    if rz is not None: parts.append(clamp(1+(rz-56.0)*0.004,0.93,1.08))
    if first is not None: parts.append(clamp(1+(first-33.0)*0.008,0.93,1.08))
    factor=float(np.mean(parts)) if parts else 1.0
    return float(clamp(factor,0.86,1.16)), {"points_per_drive":ppd,"epa_per_play":epa,"success_rate":success,"red_zone_td_rate":rz,"first_down_rate":first}


def passing_tds_stat_projection(row, role, cfg):
    row=dict(row or {}); notes=[]; shared=shared_game_opportunity_context(row)
    attempts=safe_float(row.get("pass_attempts_pg"),shared.get("pass_attempts",33.0)) or shared.get("pass_attempts",33.0)
    current=safe_float(row.get("current_pass_attempts_pg")); games=safe_float(row.get("current_games"),0) or 0
    if games>=2 and current: attempts=0.68*attempts+0.32*current
    td_pg=safe_float(row.get("passing_tds_pg")); td_rate=safe_float(row.get("pass_td_rate"))
    if td_rate is None and td_pg is not None: td_rate=td_pg/max(8.0,attempts)
    if td_rate is None: td_rate=0.045
    td_rate=td_rate/100.0 if td_rate>0.30 else td_rate
    td_rate=0.65*clamp(td_rate,0.018,0.085)+0.35*0.045
    rz_pass=safe_float(row.get("red_zone_pass_rate"),55.0) or 55.0
    def_rank=safe_float(row.get("opp_def_pass_rank"),safe_float(row.get("def_pass_rank")))
    matchup=1.0 if def_rank is None else clamp(1+(def_rank-16.5)*0.008,0.88,1.12)
    rz_allowed=safe_float(row.get("opp_def_red_zone_td_allowed_rate"),safe_float(row.get("def_red_zone_td_allowed_rate")))
    if rz_allowed is not None: matchup*=clamp(1+(rz_allowed-56)*0.004,0.92,1.08)
    pressure=safe_float(row.get("opp_def_pressure_rate"),safe_float(row.get("def_pressure_rate")))
    pressure_factor=1.0 if pressure is None else clamp(1-(pressure-25)*0.004,0.92,1.05)
    weather=safe_float(row.get("weather_pass_factor"),1.0) or 1.0
    score_env,score_detail=_internal_team_scoring_environment(row)
    expected=attempts*td_rate*clamp(1+(rz_pass-55)*0.003,0.94,1.07)*matchup*pressure_factor*clamp(weather,0.88,1.03)*score_env
    expected=clamp(expected,0.15,4.2)
    return float(expected),{"active":True,"breakdown":{"projected_attempts":round(attempts,2),"td_rate":round(td_rate,4),"red_zone_pass_rate":round(rz_pass,2),"matchup_factor":round(matchup,3),"pressure_factor":round(pressure_factor,3),"team_scoring_factor":round(score_env,3),"team_scoring_detail":score_detail,"shared_game_opportunity":shared},"notes":notes}


def anytime_td_probability_projection(row, role, cfg):
    row=dict(row or {}); pos=str(row.get("position") or "").upper(); notes=[]
    rz_share=safe_float(row.get("red_zone_touch_share"),role.get("rz",10)) or role.get("rz",10)
    goal=safe_float(row.get("goal_line_touches"),0) or 0
    rush_tds=safe_float(row.get("rushing_tds"),0) or 0; rec_tds=safe_float(row.get("receiving_tds"),0) or 0
    games=max(1.0,safe_float(row.get("games_played"),safe_float(row.get("current_games"),17)) or 17)
    observed=(rush_tds+rec_tds)/games
    role_base=0.34 if pos in {"RB","FB"} else 0.28 if pos in {"WR","TE"} else 0.18 if pos=="QB" else 0.20
    opportunity=role_base*(0.55+rz_share/20.0)+min(0.18,goal/games*0.30)
    scoring=0.60*opportunity+0.40*clamp(observed,0.02,0.85)
    score_env,score_detail=_internal_team_scoring_environment(row)
    scoring*=score_env
    def_rz=safe_float(row.get("opp_def_red_zone_td_allowed_rate"),safe_float(row.get("def_red_zone_td_allowed_rate")))
    if def_rz is not None: scoring*=clamp(1+(def_rz-56)*0.006,0.88,1.12)
    p=clamp(scoring,0.03,0.88)
    return float(p),{"active":True,"breakdown":{"td_probability":round(p,4),"red_zone_touch_share":round(rz_share,2),"goal_line_touches":round(goal,1),"observed_td_pg":round(observed,3),"team_scoring_factor":round(score_env,3),"team_scoring_detail":score_detail},"notes":notes}


def field_goals_made_stat_projection(row, role, cfg):
    row=dict(row or {}); notes=[]
    fg_pg=safe_float(row.get("field_goal_made_pg"),safe_float(row.get("fg_made_pg"),1.65)) or 1.65
    fg_att=safe_float(row.get("field_goal_attempts_pg"),safe_float(row.get("fg_attempts_pg"),2.0)) or 2.0
    fg_pct=safe_float(row.get("field_goal_pct"),84.0) or 84.0
    if fg_pct<=1.5: fg_pct*=100
    shared=shared_game_opportunity_context(row)
    drives=safe_float(row.get("drives_pg"),shared.get("drives",10.5)) or shared.get("drives",10.5)
    rz_td=safe_float(row.get("red_zone_td_rate"),56.0) or 56.0
    stall=clamp(1+(58-rz_td)*0.012,0.82,1.20)
    score_env,score_detail=_internal_team_scoring_environment(row)
    # Strong offenses create more scoring-range trips, but elite RZ TD teams may reduce FG attempts.
    volume=(fg_pg*0.62)+(fg_att*(fg_pct/100.0)*0.38)
    volume*=clamp(drives/10.5,0.88,1.12)*clamp(score_env,0.90,1.11)*stall
    weather=_lookup_weather_for_row(row) or {}
    wind=safe_float(weather.get("wind_mph"),safe_float(weather.get("wind")))
    if wind is not None and wind>=18: volume*=0.88; notes.append("High wind field-goal tax")
    elif wind is not None and wind>=12: volume*=0.95
    env=environment_for(row)
    if safe_float(env.get("altitude"),0)>=4000: volume*=1.035
    volume=clamp(volume,0.25,4.5)
    return float(volume),{"active":True,"breakdown":{"fg_made_pg":round(fg_pg,2),"fg_attempts_pg":round(fg_att,2),"fg_pct":round(fg_pct,1),"drives_pg":round(drives,2),"red_zone_td_rate":round(rz_td,1),"team_scoring_factor":round(score_env,3),"team_scoring_detail":score_detail},"notes":notes}


def _market_line_sanity_projection(base, line, prop, source=None):
    """Projection-integrity guard.

    The sportsbook line is *not* an input to the raw projection.  It is used only
    after simulation to decide OVER/UNDER and for market diagnostics.  This guard
    only applies absolute football-realism bounds, never line-relative anchoring.
    """
    base=safe_float(base,0.0) or 0.0
    bounds={
        "Passing Yards":(45,430),"Passing TDs":(0.02,4.8),"Interceptions":(0.02,2.8),
        "Pass Attempts":(4,62),"Completions":(1,45),"Rushing Yards":(0,190),
        "Rush Attempts":(0,40),"Receiving Yards":(0,205),"Receptions":(0,17),
        "Fantasy Points":(0,55),"Anytime TD":(0.01,0.95),"Longest Reception":(1,100),
        "Longest Rush":(1,85),"Kicking Points":(0,24),"Field Goals Made":(0,5.5),
        "Tackles + Assists":(0,20),"Sacks":(0,3.5),
    }
    lo,hi=bounds.get(prop,(0,999))
    capped=float(clamp(base,lo,hi))
    return capped,{"active":abs(capped-base)>=0.01,"raw_before_cap":round(base,3),"cap_bounds":[lo,hi],"note":f"{prop} absolute realism guard"}

def load_preseason_rotations():
    """Dedicated preseason rotation/news file.

    Expected shape:
      {"players": {"KC|patrick mahomes": {...}}, "teams": {"KC": {...}}}
    Player keys may also be just the normalized player name for backwards compatibility.
    """
    data=load_json(PRESEASON_ROTATION_FILE,{})
    return data if isinstance(data,dict) else {}


def load_preseason_prior_bank():
    """Optional player efficiency priors (manual/vendor/current preseason/college-adjusted)."""
    df=_read_optional_csv(PRESEASON_PRIOR_FILE)
    if df.empty:
        return {}
    bank={}
    for _,r in df.iterrows():
        d={k:r.get(k) for k in df.columns}
        player=norm(d.get("player") or d.get("name"))
        team=_normalize_nfl_team(d.get("team"))
        if not player:
            continue
        bank[(player,team or "")]=d
        bank.setdefault((player,""),d)
    return bank


def _parse_preseason_rotation_note(text):
    """Turn plain coach/beat-writer rotation language into workload hints.

    This is deliberately conservative: it only parses explicit playing-time wording and
    never tries to infer talent or a betting side from prose.
    """
    raw=str(text or "").strip()
    t=raw.upper()
    if not t:
        return {}
    out={}
    # Strong no-play phrases first.
    if any(x in t for x in ["WON'T PLAY","WILL NOT PLAY","NOT PLAYING","RESTING","REST STARTERS","SIT OUT","SITTING OUT"]):
        out.update({"status":"RESTING","preseason_snap_share":0.0,"rotation_parse_confidence":0.98})
        return out
    # Explicit drives / series.
    m=re.search(r"\b(\d+(?:\.\d+)?)\s*(?:DRIVES?|SERIES)\b",t)
    if m:
        out["preseason_expected_drives"]=float(m.group(1))
        out["rotation_parse_confidence"]=0.94
    word_map={"ONE":1,"TWO":2,"THREE":3,"FOUR":4,"FIVE":5,"SIX":6}
    if "preseason_expected_drives" not in out:
        for word,num in word_map.items():
            if re.search(rf"\b{word}\s+(?:DRIVES?|SERIES)\b",t):
                out["preseason_expected_drives"]=float(num); out["rotation_parse_confidence"]=0.92; break
    # Quarters / halves.
    if any(x in t for x in ["FIRST HALF","ONE HALF","THROUGH HALFTIME","UNTIL HALFTIME"]):
        out["preseason_expected_quarters"]=2.0; out["rotation_parse_confidence"]=max(out.get("rotation_parse_confidence",0),0.92)
    elif any(x in t for x in ["FIRST QUARTER","ONE QUARTER"]):
        out["preseason_expected_quarters"]=1.0; out["rotation_parse_confidence"]=max(out.get("rotation_parse_confidence",0),0.92)
    elif "THREE QUARTERS" in t:
        out["preseason_expected_quarters"]=3.0; out["rotation_parse_confidence"]=max(out.get("rotation_parse_confidence",0),0.90)
    # Generic limited wording should lower certainty, not invent a precise share.
    if any(x in t for x in ["LIMITED","BRIEF APPEARANCE","SHORT STINT","FEW SNAPS"]):
        out["status"]="LIMITED_WORKLOAD"
        out.setdefault("limited_snap_risk",0.45)
        out["rotation_parse_confidence"]=max(out.get("rotation_parse_confidence",0),0.78)
    if any(x in t for x in ["FULL GAME","MOST OF THE GAME","EXTENDED WORK","EXTENDED RUN"]):
        out["preseason_snap_floor"] = 0.58
        out["rotation_parse_confidence"]=max(out.get("rotation_parse_confidence",0),0.82)
    return out


def _lookup_preseason_rotation(row):
    data=load_preseason_rotations()
    if not data:
        return {}
    player=norm((row or {}).get("player"))
    team=_normalize_nfl_team((row or {}).get("team"))
    prop=str((row or {}).get("prop") or "")
    out={}
    teams=data.get("teams") if isinstance(data.get("teams"),dict) else {}
    team_ctx=teams.get(team) if team else None
    if isinstance(team_ctx,dict):
        out.update(team_ctx)
    players=data.get("players") if isinstance(data.get("players"),dict) else {}
    for key in [f"{team}|{player}" if team else "", player, str((row or {}).get("player") or "")]:
        if key and isinstance(players.get(key),dict):
            out.update(players.get(key))
    props=data.get("player_props") if isinstance(data.get("player_props"),dict) else {}
    for key in [f"{team}|{player}|{prop}" if team else "", f"{player}|{prop}"]:
        if key and isinstance(props.get(key),dict):
            out.update(props.get(key))
    if not out:
        return {}
    parsed=_parse_preseason_rotation_note(out.get("note") or out.get("news") or out.get("coach_note"))
    for k,v in parsed.items():
        out.setdefault(k,v)
    allowed={
        "status","note","news","coach_note","confidence","depth_rank","preseason_depth_rank",
        "preseason_snap_share","expected_snap_share","preseason_snap_floor",
        "preseason_expected_drives","preseason_expected_quarters","preseason_expected_pass_attempts",
        "preseason_expected_carries","preseason_expected_routes","preseason_expected_targets",
        "limited_snap_risk","preseason_team_plays","preseason_pass_rate",
        "preseason_prior_completion_rate","preseason_prior_ypa","preseason_prior_ypc",
        "preseason_prior_catch_rate","preseason_prior_ypt","prior_sample_attempts",
        "prior_sample_carries","prior_sample_targets","prior_source","prior_confidence",
        "rotation_parse_confidence",
    }
    result={k:v for k,v in out.items() if k in allowed and v not in [None,""]}
    if result:
        result["has_preseason_rotation_context"]=True
        result["preseason_rotation_updated_at"]=out.get("updated_at") or data.get("updated_at") or now_iso()
    return result

PRESEASON_ROOM_BUDGETS = {
    # Player-snap equivalents per offensive snap.  QB is exactly one player; the
    # skill-room budgets sum to ~5 eligible skill positions without pretending every
    # WR/RB/TE room should sum to 100% individually.
    "QB": 1.00,
    "BACKFIELD": 1.15,
    "WR": 2.65,
    "TE": 1.20,
}


def _preseason_room_group(position):
    pos=str(position or "").upper().strip()
    if pos=="QB": return "QB"
    if pos in {"RB","FB"}: return "BACKFIELD"
    if pos=="WR": return "WR"
    if pos=="TE": return "TE"
    return "OTHER"


def _preseason_default_room_weight(position, depth_rank=None, rookie=False, starter=False):
    """Relative rotation weight only; final shares are zero-sum inside the room."""
    pos=str(position or "").upper().strip()
    d=int(max(1, safe_float(depth_rank, 9) or 9))
    if pos=="QB":
        table={1:0.22,2:0.44,3:0.38,4:0.28,5:0.18}
        w=table.get(d,0.12)
    elif pos in {"RB","FB"}:
        table={1:0.20,2:0.31,3:0.40,4:0.43,5:0.37,6:0.29,7:0.20}
        w=table.get(d,0.14)
    elif pos=="WR":
        table={1:0.18,2:0.27,3:0.37,4:0.46,5:0.49,6:0.45,7:0.37,8:0.29,9:0.20}
        w=table.get(d,0.15)
    elif pos=="TE":
        table={1:0.20,2:0.35,3:0.43,4:0.38,5:0.27,6:0.18}
        w=table.get(d,0.13)
    else:
        w=0.20
    # Rookies/deep developmental players generally receive more preseason evaluation
    # work, but keep the nudge small because coach news must remain the dominant input.
    if rookie and d>=2:
        w*=1.10
    if starter and d<=1:
        w*=0.96
    return float(max(0.02,w))


def _preseason_rotation_explicit_share(ctx, position):
    """Return (share, locked, source) from explicit rotation/news context."""
    ctx=dict(ctx or {})
    text=" ".join(str(ctx.get(k) or "") for k in ["status","note","news","coach_note","manual_override_status","manual_note","role_note"])
    parsed=_parse_preseason_rotation_note(text)
    for k,v in parsed.items():
        ctx.setdefault(k,v)
    status=str(ctx.get("status") or ctx.get("manual_override_status") or "").upper()
    if any(x in status for x in ["OUT","INACTIVE","RESTING","NOT_PLAYING"]):
        return 0.0, True, "inactive/rest"
    for key in ["preseason_snap_share","expected_snap_share","manual_expected_snap_share","projected_snap_share"]:
        if ctx.get(key) not in [None,""]:
            share=_as_fraction(ctx.get(key))
            if share is not None:
                return float(clamp(share,0.0,0.90)), True, key
    if str(position or "").upper()=="QB":
        drives=_preseason_first_num(ctx,["preseason_expected_drives","expected_drives","qb_expected_drives"],None)
        quarters=_preseason_first_num(ctx,["preseason_expected_quarters","expected_quarters"],None)
        if drives is not None and drives>=0:
            return float(clamp(drives/10.0,0.0,0.90)), True, "expected_drives"
        if quarters is not None and quarters>=0:
            return float(clamp(quarters/4.0,0.0,0.90)), True, "expected_quarters"
    return None, False, ""


def _preseason_roster_metadata_bank():
    """Small player metadata bank used by the rotation allocator."""
    bank={}
    cur=_read_optional_csv(CURRENT_USAGE_FILE)
    if not cur.empty:
        for _,r in cur.iterrows():
            player=norm(r.get("player")); team=_normalize_nfl_team(r.get("team"))
            if player:
                bank[(player,team or "")]={k:r.get(k) for k in cur.columns}
                bank.setdefault((player,""),bank[(player,team or "")])
    return bank


def apply_preseason_team_rotation_context(rows):
    """Allocate preseason playing time jointly across each team position room.

    Explicit coach/manual inputs are locked first.  The remaining room budget is shared
    across the rest of the current depth chart, so QB2/QB3/RB/WR projections cannot all
    independently assume large playing-time shares.  Only the returned live rows are
    projected, but omitted depth-chart teammates still consume their share of the room.
    """
    rows=[dict(r) for r in (rows or [])]
    if not rows:
        return rows
    active_teams={_normalize_nfl_team(r.get("team")) for r in rows if _normalize_nfl_team(r.get("team"))}
    depth_df=_read_optional_csv(DEPTH_CHART_FILE)
    meta_bank=_preseason_roster_metadata_bank()
    candidates={}

    def add_candidate(player,team,pos,depth=None,starter=None,base_row=None):
        player_name=str(player or "").strip(); team_u=_normalize_nfl_team(team); pos_u=str(pos or "").upper().strip()
        room=_preseason_room_group(pos_u)
        if not player_name or not team_u or room=="OTHER" or (active_teams and team_u not in active_teams):
            return
        key=(team_u,room,norm(player_name))
        d=candidates.setdefault(key,{"player":player_name,"team":team_u,"position":pos_u,"depth_rank":depth,"starter":starter,"row":{}})
        if base_row:
            d["row"].update(dict(base_row))
        if d.get("depth_rank") in [None,""] and depth not in [None,""]:
            d["depth_rank"]=depth
        if d.get("starter") in [None,""] and starter not in [None,""]:
            d["starter"]=starter

    # Full depth chart first so players without an Underdog line still consume rotation.
    if not depth_df.empty:
        for _,d in depth_df.iterrows():
            add_candidate(d.get("player"),d.get("team"),d.get("position"),d.get("depth_rank"),d.get("starter"),d.to_dict())
    # Live-board rows win for identity/context.
    for r in rows:
        add_candidate(r.get("player"),r.get("team"),r.get("position"),r.get("depth_rank"),r.get("starter"),r)

    groups={}
    for key,c in candidates.items():
        groups.setdefault((c["team"],_preseason_room_group(c["position"])),[]).append(c)

    alloc_bank={}
    for (team,room),members in groups.items():
        budget=float(PRESEASON_ROOM_BUDGETS.get(room,1.0))
        locked_total=0.0; open_members=[]
        for c in members:
            pseudo=dict(c.get("row") or {})
            pseudo.update({"player":c["player"],"team":team,"position":c["position"],"depth_rank":c.get("depth_rank"),"starter":c.get("starter")})
            # Dedicated preseason rotation plus generic manual news are both honored.
            rot=_lookup_preseason_rotation(pseudo)
            man=_lookup_manual_override(pseudo)
            ctx={**pseudo,**rot,**man}
            explicit_share,locked,source=_preseason_rotation_explicit_share(ctx,c["position"])
            meta=meta_bank.get((norm(c["player"]),team)) or meta_bank.get((norm(c["player"]),"")) or {}
            rookie=bool(int(safe_float(ctx.get("rookie_flag"), safe_float(ctx.get("split_rookie_flag"), safe_float(meta.get("rookie_flag"),0))) or 0))
            depth=safe_float(ctx.get("preseason_depth_rank"), safe_float(ctx.get("depth_rank"), safe_float(c.get("depth_rank"),9)))
            starter=bool(str(ctx.get("starter") or c.get("starter") or "").upper() in {"1","TRUE","YES"} or (depth is not None and depth==1))
            c.update({"ctx":ctx,"rookie":rookie,"depth_rank":depth,"starter":starter,"locked":locked,"lock_source":source})
            if locked:
                c["allocated_share"]=float(max(0.0,explicit_share or 0.0)); locked_total+=c["allocated_share"]
            else:
                c["weight"]=_preseason_default_room_weight(c["position"],depth,rookie,starter)
                open_members.append(c)

        overbooked=locked_total>budget+1e-9
        if overbooked and locked_total>0:
            scale=budget/locked_total
            for c in members:
                if c.get("locked"):
                    c["allocated_share"]*=scale
            locked_total=budget
        remaining=max(0.0,budget-locked_total)
        weight_total=sum(c.get("weight",0.0) for c in open_members)
        if open_members and remaining>0 and weight_total>0:
            # First proportional allocation, with a realistic single-player cap.  Any
            # tiny leftover after caps remains reserve instead of being forced onto a player.
            for c in open_members:
                share=remaining*c.get("weight",0.0)/weight_total
                c["allocated_share"]=float(clamp(share,0.01,0.85))
        group_total=sum(c.get("allocated_share",0.0) for c in members)
        reserve=max(0.0,budget-group_total)
        depth_known=sum(1 for c in members if safe_float(c.get("depth_rank")) is not None)
        completeness=depth_known/max(1,len(members))
        for c in members:
            confidence="HIGH" if c.get("locked") else "MEDIUM" if completeness>=0.75 and len(members)>=2 else "LOW"
            alloc_bank[(team,room,norm(c["player"]))]={
                "preseason_room_snap_share":round(float(c.get("allocated_share",0.0)),4),
                "preseason_room_group":room,
                "preseason_room_budget":round(budget,3),
                "preseason_room_total":round(group_total,3),
                "preseason_room_reserve":round(reserve,3),
                "preseason_room_locked":bool(c.get("locked")),
                "preseason_room_source":c.get("lock_source") or "zero-sum depth allocation",
                "preseason_room_confidence":confidence,
                "preseason_room_members":len(members),
                "preseason_room_overbooked":bool(overbooked),
            }

    out=[]
    for r in rows:
        rr=dict(r)
        team=_normalize_nfl_team(rr.get("team")); room=_preseason_room_group(rr.get("position"))
        ctx=alloc_bank.get((team,room,norm(rr.get("player"))))
        if ctx:
            rr.update(ctx)
        out.append(rr)
    return out


def _preseason_first_num(row, keys, default=None):
    for key in keys:
        val=safe_float((row or {}).get(key))
        if val is not None:
            return val
    return default

def _as_fraction(value, default=None):
    v=safe_float(value)
    if v is None:
        return default
    if v > 1.0:
        v/=100.0
    return float(clamp(v,0.0,1.0))

def _preseason_workload_model(row, prop, role):
    """Estimate only *playing time/opportunity* for preseason.

    Talent/efficiency comes later. This intentionally does not scale a full-game
    projection by a generic constant. Explicit coach/manual workload information wins,
    then depth-chart role, then conservative position defaults.
    """
    row=dict(row or {})
    # Parse explicit coach/rotation wording from either the dedicated rotation file or
    # the generic manual-news override before any default workload is considered.
    rotation_text=" ".join(str(row.get(k) or "") for k in ["note","news","coach_note","manual_note","role_note","manual_override_status","injury_note"])
    for k,v in _parse_preseason_rotation_note(rotation_text).items():
        row.setdefault(k,v)
    pos=str(row.get("position") or "").upper().strip()
    depth=_preseason_first_num(row,["preseason_depth_rank","depth_rank","qb_depth_rank"],None)
    starter_txt=" ".join(str(row.get(k) or "") for k in ["starter","role","role_note","manual_override_status","manual_note","injury_note","status","note","news","coach_note"]).upper()
    notes=[]
    explicit=False
    source="DEPTH/ROLE DEFAULT"

    # Explicit preseason opportunity inputs. expected_snap_share may be supplied through
    # the existing Manual News Override panel, which is useful for coach rotation news.
    snap_share=None
    for key in ["preseason_snap_share","expected_snap_share","manual_expected_snap_share","projected_snap_share"]:
        if row.get(key) not in [None,""]:
            snap_share=_as_fraction(row.get(key))
            if snap_share is not None:
                explicit=True; source=key; notes.append(f"Explicit preseason snap share: {snap_share:.0%}")
                break

    expected_drives=_preseason_first_num(row,["preseason_expected_drives","expected_drives","qb_expected_drives"],None)
    expected_quarters=_preseason_first_num(row,["preseason_expected_quarters","expected_quarters"],None)
    if pos=="QB" and snap_share is None:
        if expected_drives is not None and expected_drives>0:
            # Roughly 10 offensive drives in a normal game; used only as a workload map.
            snap_share=float(clamp(expected_drives/10.0,0.06,0.85))
            explicit=True; source="expected_drives"; notes.append(f"Explicit QB drives: {expected_drives:g}")
        elif expected_quarters is not None and expected_quarters>0:
            snap_share=float(clamp(expected_quarters/4.0,0.06,0.85))
            explicit=True; source="expected_quarters"; notes.append(f"Explicit QB quarters: {expected_quarters:g}")

    # Joint team-room allocation is preferred over independent depth defaults.  It has
    # already accounted for the other QBs/RBs/WRs/TEs on the roster.
    if snap_share is None and row.get("preseason_room_snap_share") not in [None,""]:
        snap_share=_as_fraction(row.get("preseason_room_snap_share"))
        if snap_share is not None:
            source="zero-sum team room"
            notes.append(f"Zero-sum {row.get('preseason_room_group') or pos} room share: {snap_share:.0%}")
            if row.get("preseason_room_overbooked"):
                notes.append("Rotation room had explicit shares above budget; locked shares were normalized")

    if snap_share is None:
        is_starter=("STARTER" in starter_txt or str(row.get("starter") or "").upper() in {"TRUE","YES","1"})
        if pos=="QB":
            if is_starter or depth==1:
                snap_share=0.24
            elif depth==2:
                snap_share=0.42
            elif depth is not None and depth>=3:
                snap_share=0.34
            else:
                snap_share=0.32
        elif pos in {"RB","FB"}:
            if is_starter or depth==1:
                snap_share=0.22
            elif depth==2:
                snap_share=0.38
            elif depth is not None and depth>=3:
                snap_share=0.44
            else:
                snap_share=0.30
        elif pos in {"WR","TE"}:
            if is_starter or depth==1:
                snap_share=0.24
            elif depth==2:
                snap_share=0.38
            elif depth is not None and depth>=3:
                snap_share=0.44
            else:
                snap_share=0.30
        else:
            snap_share=0.30

    # Coach/role language can override the generic depth assumption.
    if any(x in starter_txt for x in ["NOT PLAY","WON'T PLAY","WILL NOT PLAY","OUT","RESTING","REST STARTERS"]):
        snap_share=min(snap_share,0.02); explicit=True; source="coach/status inactive"; notes.append("Preseason no-play/rest signal")
    elif "ONE DRIVE" in starter_txt:
        snap_share=min(snap_share,0.10); explicit=True; source="coach one-drive"; notes.append("One-drive workload")
    elif "TWO DRIVE" in starter_txt:
        snap_share=min(snap_share,0.18); explicit=True; source="coach two-drive"; notes.append("Two-drive workload")
    elif "FIRST HALF" in starter_txt or "ONE HALF" in starter_txt:
        snap_share=max(snap_share,0.46); explicit=True; source="coach first-half"; notes.append("First-half workload")
    elif any(x in starter_txt for x in ["LIMITED","WORKLOAD LIMIT","LIMITED_WORKLOAD"]):
        snap_share*=0.72; notes.append("Limited-workload tax")

    snap_floor=_as_fraction(row.get("preseason_snap_floor"))
    if snap_floor is not None and snap_floor>snap_share:
        snap_share=min(0.85,snap_floor); explicit=True; source="preseason snap floor"; notes.append(f"Explicit extended-work floor: {snap_floor:.0%}")

    limited_risk=_preseason_first_num(row,["limited_snap_risk","preseason_limited_snap_risk"],0.0) or 0.0
    if limited_risk>0:
        snap_share*=float(clamp(1-limited_risk*0.25,0.65,1.0))
        notes.append(f"Limited snap risk {limited_risk:.0%}")

    snap_share=float(clamp(snap_share,0.01,0.85))
    team_plays=_preseason_first_num(row,["preseason_team_plays","pbp_plays_pg","plays_pg"],62.0) or 62.0
    pass_rate=_preseason_first_num(row,["preseason_pass_rate","pbp_pass_rate","pass_rate"],56.0) or 56.0
    if pass_rate<=1.0: pass_rate*=100.0
    pass_rate=float(clamp(pass_rate,38.0,72.0))
    expected_snaps=team_plays*snap_share
    expected_team_passes=team_plays*(pass_rate/100.0)*snap_share

    # Direct opportunity overrides are more authoritative than inferred snaps.
    exp_pass_attempts=_preseason_first_num(row,["preseason_expected_pass_attempts","expected_pass_attempts","expected_attempts"],None)
    exp_carries=_preseason_first_num(row,["preseason_expected_carries","expected_carries"],None)
    exp_routes=_preseason_first_num(row,["preseason_expected_routes","expected_routes"],None)
    exp_targets=_preseason_first_num(row,["preseason_expected_targets","expected_targets"],None)
    if any(v is not None for v in [exp_pass_attempts,exp_carries,exp_routes,exp_targets]):
        explicit=True
        source="explicit opportunity override"

    if exp_pass_attempts is None and pos=="QB":
        exp_pass_attempts=expected_team_passes*0.98

    room_conf=str(row.get("preseason_room_confidence") or "").upper()
    confidence="HIGH" if explicit else room_conf if room_conf in {"HIGH","MEDIUM","LOW"} else "MEDIUM" if depth is not None else "LOW"
    workload_score={"HIGH":92,"MEDIUM":78,"LOW":58}[confidence]
    if row.get("preseason_room_overbooked"):
        workload_score-=8
    workload_score-=int(clamp(limited_risk*20,0,18))
    if snap_share<=0.03:
        workload_score=min(workload_score,45)

    uncertainty_mult={"HIGH":1.12,"MEDIUM":1.34,"LOW":1.62}[confidence]
    return {
        "snap_share":round(snap_share,4),
        "expected_snaps":round(expected_snaps,2),
        "expected_team_passes_during_role":round(expected_team_passes,2),
        "expected_pass_attempts":None if exp_pass_attempts is None else round(float(exp_pass_attempts),2),
        "expected_carries":None if exp_carries is None else round(float(exp_carries),2),
        "expected_routes":None if exp_routes is None else round(float(exp_routes),2),
        "expected_targets":None if exp_targets is None else round(float(exp_targets),2),
        "team_plays":round(team_plays,2),"pass_rate":round(pass_rate,2),
        "depth_rank":depth,"confidence":confidence,"score":int(clamp(workload_score,0,99)),
        "uncertainty_mult":uncertainty_mult,"source":source,"explicit":explicit,
        "room_group":row.get("preseason_room_group"),"room_budget":row.get("preseason_room_budget"),
        "room_total":row.get("preseason_room_total"),"room_reserve":row.get("preseason_room_reserve"),
        "room_members":row.get("preseason_room_members"),"room_locked":row.get("preseason_room_locked"),
        "inactive":snap_share<=0.03,"notes":notes,
    }


def _preseason_prior_row(row):
    bank=load_preseason_prior_bank()
    player=norm((row or {}).get("player")); team=_normalize_nfl_team((row or {}).get("team"))
    return dict(bank.get((player,team or "")) or bank.get((player,"")) or {})


def _shrink_efficiency(observed, baseline, sample_n, stabilizer, lo, hi):
    obs=safe_float(observed); base=safe_float(baseline)
    if base is None:
        base=(lo+hi)/2.0
    if obs is None:
        return float(clamp(base,lo,hi)),0.0
    n=max(0.0,safe_float(sample_n,0) or 0)
    weight=float(clamp(n/max(1.0,n+stabilizer),0.0,0.88))
    val=base*(1-weight)+obs*weight
    return float(clamp(val,lo,hi)),weight


def _preseason_efficiency_prior(row, workload=None):
    """Hierarchical preseason efficiency prior.

    Priority: explicit/vendor preseason prior -> NFL player evidence shrunk to role prior ->
    conservative rookie/depth-chart prior.  This lets backups and rookies project from
    realistic efficiency without pretending a tiny NFL sample is their true talent.
    """
    row=dict(row or {}); workload=workload or {}
    pos=str(row.get("position") or "").upper().strip()
    depth=int(max(1,safe_float(row.get("preseason_depth_rank"),safe_float(row.get("depth_rank"),9)) or 9))
    rookie_raw=row.get("rookie_flag",row.get("split_rookie_flag",0))
    rookie=str(rookie_raw).upper() in {"TRUE","YES","1"} or (safe_float(rookie_raw,0) or 0)>=1
    years=safe_float(row.get("years_exp"),safe_float(row.get("years_of_experience")))
    prior_file=_preseason_prior_row(row)

    # Conservative role priors.  These are not projections by themselves; they are
    # efficiency anchors that are multiplied by separately estimated opportunities.
    if pos=="QB":
        if depth<=1:
            base_ypa,base_comp=7.15,0.648
        elif depth==2:
            base_ypa,base_comp=6.70,0.625
        else:
            base_ypa,base_comp=6.35,0.605
        if rookie:
            base_ypa-=0.18; base_comp-=0.012
    else:
        base_ypa,base_comp=7.0,0.64
    if pos in {"RB","FB"}:
        base_ypc=4.22 if depth<=2 else 4.08
        base_ypt=6.45 if depth<=2 else 6.15
        base_catch=0.715 if depth<=2 else 0.69
    elif pos=="WR":
        base_ypc=4.25
        base_ypt=8.25 if depth<=3 else 7.75
        base_catch=0.625 if depth<=3 else 0.595
    elif pos=="TE":
        base_ypc=4.15
        base_ypt=7.35 if depth<=2 else 6.95
        base_catch=0.675 if depth<=2 else 0.645
    elif pos=="QB":
        base_ypc=5.0 if depth<=2 else 4.6
        base_ypt=4.0; base_catch=0.50
    else:
        base_ypc=4.15; base_ypt=7.2; base_catch=0.62
    if rookie:
        if pos in {"WR","TE"}: base_ypt-=0.20
        if pos in {"RB","FB"}: base_ypc-=0.08

    # Player NFL evidence.  If exact total samples are unavailable, per-game volume is
    # converted to a deliberately conservative pseudo-sample instead of treated as full certainty.
    games=max(0.0,_preseason_first_num(row,["current_games","games_played","games","player_games"],0.0) or 0.0)
    att_pg=_preseason_first_num(row,["current_pass_attempts_pg","last5_pass_attempts_pg","pass_attempts_pg"],None)
    pass_ypg=_preseason_first_num(row,["current_passing_yards_pg","last5_passing_yards_pg","passing_yards_pg"],None)
    comp_pg=_preseason_first_num(row,["current_completions_pg","last5_completions_pg","completions_pg"],None)
    obs_ypa=_preseason_first_num(row,["yards_per_attempt","passing_yards_per_attempt"],None)
    if obs_ypa is None and pass_ypg is not None and att_pg and att_pg>0:
        obs_ypa=pass_ypg/att_pg
    obs_comp=_as_fraction(_preseason_first_num(row,["completion_rate","qb_completion_rate","current_completion_rate"],None))
    if obs_comp is None and comp_pg is not None and att_pg and att_pg>0:
        obs_comp=comp_pg/att_pg
    att_total=_preseason_first_num(row,["pass_attempts","attempts","career_pass_attempts","prior_sample_attempts"],None)
    if att_total is None and att_pg:
        att_total=att_pg*(games if games>=2 else 4.5)

    rush_pg=_preseason_first_num(row,["current_rush_attempts_pg","last5_rush_attempts_pg","rush_attempts_pg","carries_pg"],None)
    rush_ypg=_preseason_first_num(row,["current_rushing_yards_pg","last5_rushing_yards_pg","rushing_yards_pg"],None)
    obs_ypc=_preseason_first_num(row,["yards_per_carry"],None)
    if obs_ypc is None and rush_ypg is not None and rush_pg and rush_pg>0:
        obs_ypc=rush_ypg/rush_pg
    carry_total=_preseason_first_num(row,["carries","rush_attempts","career_carries","prior_sample_carries"],None)
    if carry_total is None and rush_pg:
        carry_total=rush_pg*(games if games>=2 else 4.5)

    targ_pg=_preseason_first_num(row,["current_targets_pg","last5_targets_pg","targets_pg"],None)
    rec_pg=_preseason_first_num(row,["current_receptions_pg","last5_receptions_pg","receptions_pg"],None)
    rec_ypg=_preseason_first_num(row,["current_receiving_yards_pg","last5_receiving_yards_pg","receiving_yards_pg"],None)
    obs_catch=_as_fraction(_preseason_first_num(row,["catch_rate","reception_rate"],None))
    if obs_catch is None and rec_pg is not None and targ_pg and targ_pg>0:
        obs_catch=rec_pg/targ_pg
    obs_ypt=_preseason_first_num(row,["yards_per_target"],None)
    if obs_ypt is None and rec_ypg is not None and targ_pg and targ_pg>0:
        obs_ypt=rec_ypg/targ_pg
    target_total=_preseason_first_num(row,["targets","career_targets","prior_sample_targets"],None)
    if target_total is None and targ_pg:
        target_total=targ_pg*(games if games>=2 else 4.5)

    # Optional current-preseason/college-adjusted/vendor file overrides observations and
    # provides an explicit sample size/confidence.  Same fields may arrive via API/manual rotation.
    explicit_sources=[]
    def pick_explicit(field, file_field=None):
        for src,label in [(row,"rotation/manual"),(prior_file,"prior file")]:
            key=field if field in src else (file_field or field)
            if src.get(key) not in [None,""]:
                explicit_sources.append(label)
                return safe_float(src.get(key))
        return None
    e_comp=pick_explicit("preseason_prior_completion_rate","completion_rate")
    e_ypa=pick_explicit("preseason_prior_ypa","yards_per_attempt")
    e_ypc=pick_explicit("preseason_prior_ypc","yards_per_carry")
    e_catch=pick_explicit("preseason_prior_catch_rate","catch_rate")
    e_ypt=pick_explicit("preseason_prior_ypt","yards_per_target")
    if e_comp is not None: obs_comp=_as_fraction(e_comp); att_total=max(att_total or 0,_preseason_first_num(prior_file,["sample_attempts","prior_sample_attempts"],80) or 80)
    if e_ypa is not None: obs_ypa=e_ypa; att_total=max(att_total or 0,_preseason_first_num(prior_file,["sample_attempts","prior_sample_attempts"],80) or 80)
    if e_ypc is not None: obs_ypc=e_ypc; carry_total=max(carry_total or 0,_preseason_first_num(prior_file,["sample_carries","prior_sample_carries"],45) or 45)
    if e_catch is not None: obs_catch=_as_fraction(e_catch); target_total=max(target_total or 0,_preseason_first_num(prior_file,["sample_targets","prior_sample_targets"],55) or 55)
    if e_ypt is not None: obs_ypt=e_ypt; target_total=max(target_total or 0,_preseason_first_num(prior_file,["sample_targets","prior_sample_targets"],55) or 55)

    ypa,w_ypa=_shrink_efficiency(obs_ypa,base_ypa,att_total,110,4.8,9.4)
    comp,w_comp=_shrink_efficiency(obs_comp,base_comp,att_total,140,0.48,0.76)
    ypc,w_ypc=_shrink_efficiency(obs_ypc,base_ypc,carry_total,65,2.8,6.8)
    catch,w_catch=_shrink_efficiency(obs_catch,base_catch,target_total,80,0.42,0.84)
    ypt,w_ypt=_shrink_efficiency(obs_ypt,base_ypt,target_total,90,4.2,11.8)

    explicit_conf=_preseason_first_num(row,["prior_confidence"],_preseason_first_num(prior_file,["confidence","prior_confidence"],None))
    if explicit_conf is not None:
        explicit_conf=_as_fraction(explicit_conf,0.75)
    evidence=max(w_ypa,w_comp,w_ypc,w_catch,w_ypt)
    if explicit_sources:
        score=88 if (explicit_conf or 0.75)>=0.8 else 82
        source=" + ".join(sorted(set(explicit_sources)))
    elif evidence>=0.55:
        score=80; source="NFL player evidence + role shrinkage"
    elif evidence>=0.25:
        score=70; source="thin NFL evidence + role shrinkage"
    else:
        score=58 if rookie or depth>=3 else 64; source="role/depth prior"
    if rookie and not explicit_sources and evidence<0.25:
        score-=5
    if years is not None and years>=3 and evidence<0.25:
        score+=3
    score=float(clamp(score,35,94))
    label="STRONG" if score>=82 else "SOLID" if score>=72 else "PARTIAL" if score>=62 else "THIN"
    return {
        "completion_rate":round(comp,4),"yards_per_attempt":round(ypa,3),
        "yards_per_carry":round(ypc,3),"catch_rate":round(catch,4),"yards_per_target":round(ypt,3),
        "sample_attempts":round(att_total or 0,1),"sample_carries":round(carry_total or 0,1),"sample_targets":round(target_total or 0,1),
        "weights":{"ypa":round(w_ypa,3),"completion":round(w_comp,3),"ypc":round(w_ypc,3),"catch":round(w_catch,3),"ypt":round(w_ypt,3)},
        "source":source,"score":round(score,1),"label":label,"rookie":bool(rookie),"depth_rank":depth,
    }


def _preseason_projection_core(row, prop, role, workload):
    """Opportunity-first preseason mean with hierarchical efficiency priors."""
    pos=str(row.get("position") or "").upper().strip()
    team_plays=safe_float(workload.get("team_plays"),62) or 62
    pass_rate=(safe_float(workload.get("pass_rate"),56) or 56)/100.0
    share=safe_float(workload.get("snap_share"),0.30) or 0.30
    expected_snaps=safe_float(workload.get("expected_snaps"),team_plays*share) or team_plays*share
    expected_team_passes=safe_float(workload.get("expected_team_passes_during_role"),team_plays*pass_rate*share) or team_plays*pass_rate*share
    eff=_preseason_efficiency_prior(row,workload)

    attempts=safe_float(workload.get("expected_pass_attempts"))
    comp_rate=float(clamp(safe_float(eff.get("completion_rate"),0.64) or 0.64,0.48,0.78))
    ypa=float(clamp(safe_float(eff.get("yards_per_attempt"),6.7) or 6.7,4.8,9.4))
    if attempts is None and pos=="QB":
        attempts=expected_team_passes*0.98

    carries=safe_float(workload.get("expected_carries"))
    regular_snap=_as_fraction(_preseason_first_num(row,["current_snap_share","last5_snap_share","snap_share"],None))
    rush_pg=_preseason_first_num(row,["current_rush_attempts_pg","last5_rush_attempts_pg","rush_attempts_pg","carries_pg"],None)
    evaluation_carry_share=share
    depth=safe_float(row.get("preseason_depth_rank"),safe_float(row.get("depth_rank")))
    # Preseason RB2/RB3+ backs are frequently evaluation players. A zero-sum room share
    # can understate their individual carry distribution because only one back needs an
    # extended second-half run for his prop to matter. Use a bounded evaluation floor
    # only when there is NO explicit coach/manual carry instruction.
    if carries is None and pos in {"RB","FB"} and not bool(workload.get("explicit")):
        conf=str(workload.get("confidence") or "LOW").upper()
        if depth is not None and depth>=2 and conf in {"LOW","MEDIUM"}:
            eval_floor=0.24 if depth<3 else 0.28
            evaluation_carry_share=max(share,eval_floor)
    evaluation_carry_snaps=team_plays*evaluation_carry_share
    if carries is None:
        if regular_snap and rush_pg is not None and regular_snap>0.08:
            carry_per_snap=rush_pg/max(1.0,team_plays*regular_snap)
        else:
            # Usage priors are role-based, not regular-season full-game carry totals.
            carry_per_snap=0.085 if pos=="QB" else 0.30 if pos in {"RB","FB"} else 0.03
        if pos=="QB": carry_per_snap=float(clamp(carry_per_snap,0.015,0.17))
        elif pos in {"RB","FB"}:
            carry_per_snap=float(clamp(carry_per_snap,0.14,0.46))
            if depth is not None and depth>=2 and not bool(workload.get("explicit")):
                carry_per_snap=max(carry_per_snap,0.27)
        else: carry_per_snap=float(clamp(carry_per_snap,0.0,0.10))
        carries=evaluation_carry_snaps*carry_per_snap
    ypc=float(clamp(safe_float(eff.get("yards_per_carry"),4.1) or 4.1,2.8,6.8))

    targets=safe_float(workload.get("expected_targets"))
    routes=safe_float(workload.get("expected_routes"))
    target_share=_as_fraction(_preseason_first_num(row,["current_target_share","last5_target_share","target_share"],None))
    if target_share is None:
        # Route/opportunity uncertainty is separate from catch/yard efficiency.
        target_share=0.115 if pos in {"RB","FB"} else 0.155 if pos=="TE" else 0.19 if pos=="WR" else 0.02
    # Deep roster players can be featured against backups, but do not let regular-season
    # target-share priors turn limited preseason snaps into unrealistic volume.
    depth=safe_float(row.get("preseason_depth_rank"),safe_float(row.get("depth_rank")))
    if depth is not None and depth>=4 and pos in {"WR","TE"}:
        target_share*=0.94
    target_share=float(clamp(target_share,0.01,0.36))
    tprr=None
    if targets is None and routes is not None and routes>0:
        targets_pg=_preseason_first_num(row,["current_targets_pg","last5_targets_pg","targets_pg"],None)
        route_part=_as_fraction(_preseason_first_num(row,["route_participation","current_route_participation"],None))
        pass_att_pg=_preseason_first_num(row,["current_pass_attempts_pg","last5_pass_attempts_pg","pass_attempts_pg"],None)
        if targets_pg is not None and route_part and pass_att_pg and pass_att_pg>0:
            tprr=targets_pg/max(1.0,pass_att_pg*route_part)
        if tprr is None:
            tprr=0.17 if pos in {"RB","FB"} else 0.18 if pos=="TE" else 0.205 if pos=="WR" else 0.05
        tprr=float(clamp(tprr,0.08,0.34))
        targets=routes*tprr
    elif targets is None:
        targets=expected_team_passes*target_share
    catch_rate=float(clamp(safe_float(eff.get("catch_rate"),0.62) or 0.62,0.42,0.84))
    ypt=float(clamp(safe_float(eff.get("yards_per_target"),7.4) or 7.4,4.2,11.8))

    if prop=="Passing Yards":
        base=(attempts or 0)*ypa
    elif prop=="Pass Attempts":
        base=attempts or 0
    elif prop=="Completions":
        base=(attempts or 0)*comp_rate
    elif prop=="Rushing Yards":
        base=carries*ypc
    elif prop=="Rush Attempts":
        base=carries
    elif prop=="Receiving Yards":
        base=targets*ypt
    elif prop=="Receptions":
        base=targets*catch_rate
    else:
        base=PROP_CONFIG.get(prop,{}).get("base",0)

    return float(max(0.0,base)), {
        "expected_attempts":None if attempts is None else round(attempts,2),
        "completion_rate":round(comp_rate,4),"yards_per_attempt":round(ypa,3),
        "expected_carries":round(carries,2),"yards_per_carry":round(ypc,3),
        "evaluation_carry_snap_share":round(evaluation_carry_share,4),
        "expected_targets":round(targets,2),"catch_rate":round(catch_rate,4),
        "yards_per_target":round(ypt,3),"expected_snaps":round(expected_snaps,2),
        "expected_routes":None if routes is None else round(routes,2),"targets_per_route":None if tprr is None else round(tprr,3),
        "snap_share":round(share,4),"target_share":round(target_share,4),
        "efficiency_prior":eff,"efficiency_prior_score":eff.get("score"),
        "efficiency_prior_label":eff.get("label"),"efficiency_prior_source":eff.get("source"),
    }

def _preseason_market_workload_audit(row, core):
    """Use the posted line only as a *role sanity check*, never to create the projection."""
    prop=str((row or {}).get("prop") or "")
    line=safe_float((row or {}).get("line"))
    if line is None:
        return {"status":"NO LINE","conflict":False,"ratio":None,"note":"No market workload audit"}
    expected=None; implied=None; unit=""
    if prop=="Passing Yards":
        expected=safe_float(core.get("expected_attempts")); ypa=safe_float(core.get("yards_per_attempt"))
        implied=(line/ypa) if ypa and ypa>0 else None; unit="pass attempts"
    elif prop=="Pass Attempts":
        expected=safe_float(core.get("expected_attempts")); implied=line; unit="pass attempts"
    elif prop=="Completions":
        expected=safe_float(core.get("expected_attempts")); cr=safe_float(core.get("completion_rate"))
        implied=(line/cr) if cr and cr>0 else None; unit="pass attempts"
    elif prop=="Rushing Yards":
        expected=safe_float(core.get("expected_carries")); ypc=safe_float(core.get("yards_per_carry"))
        implied=(line/ypc) if ypc and ypc>0 else None; unit="carries"
    elif prop=="Rush Attempts":
        expected=safe_float(core.get("expected_carries")); implied=line; unit="carries"
    elif prop=="Receiving Yards":
        expected=safe_float(core.get("expected_targets")); ypt=safe_float(core.get("yards_per_target"))
        implied=(line/ypt) if ypt and ypt>0 else None; unit="targets"
    elif prop=="Receptions":
        expected=safe_float(core.get("expected_targets")); cr=safe_float(core.get("catch_rate"))
        implied=(line/cr) if cr and cr>0 else None; unit="targets"
    if expected is None or implied is None or expected<=0:
        return {"status":"UNKNOWN","conflict":False,"ratio":None,"note":"Market-implied workload unavailable"}
    ratio=implied/max(expected,0.1)
    if ratio>1.80 or ratio<0.45:
        status="CONFLICT"; conflict=True
    elif ratio>1.50 or ratio<0.60:
        status="WATCH"; conflict=False
    else:
        status="ALIGNED"; conflict=False
    return {
        "status":status,"conflict":conflict,"ratio":round(ratio,3),
        "expected_opportunity":round(expected,2),"market_implied_opportunity":round(implied,2),
        "unit":unit,
        "note":f"Market workload {status}: model {expected:.1f} vs line-implied {implied:.1f} {unit}",
    }

def _preseason_reliability_score(row, workload, market_workload, usage_quality, volatility=None, efficiency_score=None):
    """Independent 0-100 trust score for preseason information quality.

    This is intentionally separate from projection edge/probability.  It mirrors the
    useful MLB idea of asking "how much should I trust this estimate?" before asking
    which side is favored.  Workload/coach certainty dominates because preseason
    talent is much less important than knowing who will actually be on the field.
    """
    workload=workload or {}; market_workload=market_workload or {}; row=row or {}
    wscore=safe_float(workload.get("score"),0) or 0
    uscore=float(clamp(safe_float(usage_quality,0) or 0,0,100))
    escore=float(clamp(safe_float(efficiency_score,65) or 65,0,100))
    # Playing time remains dominant, but a thin rookie/backup efficiency prior now
    # reduces actionable confidence instead of being treated like veteran evidence.
    score=0.48*wscore + 0.17*uscore + 0.13*escore
    score += 10.0 if row.get("model_match",True) else 2.0
    market_status=str(market_workload.get("status") or "UNKNOWN").upper()
    score += {"ALIGNED":10.0,"WATCH":5.0,"UNKNOWN":3.0,"CONFLICT":0.0}.get(market_status,3.0)
    if workload.get("explicit"):
        score += 8.0
    conf=str(workload.get("confidence") or "LOW").upper()
    if conf=="LOW": score-=8.0
    if str(volatility or "").upper()=="HIGH": score-=8.0
    elif str(volatility or "").upper()=="MED": score-=2.0
    if market_workload.get("conflict"): score-=10.0
    if workload.get("inactive"): score=min(score,28.0)
    score=float(clamp(score,0,99))
    label="ELITE" if score>=88 else "STRONG" if score>=80 else "PARTIAL" if score>=70 else "LOW"
    return round(score,1),label

def _preseason_calibrate_probabilities(over, under, push, reliability_score):
    """Shrink noisy preseason simulation probabilities toward 50%.

    The projection mean is untouched. Only decision confidence is damped when playing-
    time/data reliability is weak, preventing a thin preseason estimate from displaying
    fake 80-90% certainty. Raw simulation probabilities are retained separately.
    """
    over=float(clamp(safe_float(over,0.5) or 0.5,0,1))
    under=float(clamp(safe_float(under,0.5) or 0.5,0,1))
    push=float(clamp(safe_float(push,0.0) or 0.0,0,1))
    rel=float(clamp(safe_float(reliability_score,50) or 50,0,100))
    # 55 reliability => ~45% of raw conviction; 90+ => almost all of it survives.
    strength=float(clamp(0.45 + (rel-55.0)*0.012,0.35,0.92))
    cal_over=0.5 + (over-0.5)*strength
    cal_under=0.5 + (under-0.5)*strength
    # Preserve a coherent probability simplex after shrinkage.
    total=cal_over+cal_under
    cal_push=max(0.0,1.0-total)
    if total>1.0:
        cal_over/=total; cal_under/=total; cal_push=0.0
    return float(cal_over),float(cal_under),float(cal_push),round(strength,3)

def project_row_preseason(row, sims=12000):
    """Dedicated preseason projection engine.

    The raw mean is opportunity-first and independent of the Underdog line. Regular-season
    grades/calibration are isolated. Market-implied workload is used only to PASS on role
    conflicts, which is especially important when coach rotations are uncertain.
    """
    raw_market_labels=" ".join(str((row or {}).get(key) or "") for key in ["raw_prop_label","line_title","raw_label"])
    if raw_market_labels.strip() and not _is_full_game_market_label(raw_market_labels):
        raise ValueError(f"Projection blocked: non-full-game market {raw_market_labels!r}")
    row=dict(row or {})
    row["season_mode"]="PRESEASON"
    row=merge_nfl_context(row)
    row["season_mode"]="PRESEASON"
    prop=_canon_prop_label(row.get("prop"))
    if prop not in ACTIVE_NFL_MARKETS or prop not in PROP_CONFIG:
        raise ValueError(f"Projection blocked: unmapped prop {row.get('prop')!r}")
    if prop not in PRESEASON_SUPPORTED_MARKETS:
        raise ValueError(f"Preseason model not enabled yet for {prop}; row PASS-blocked instead of using a regular-season baseline")
    row["prop"]=prop
    line=safe_float(row.get("line"))
    if line is None or not _valid_market_line(prop,line,"PRESEASON"):
        raise ValueError(f"Projection blocked: invalid preseason {prop} line {row.get('line')!r}")
    if not _prop_allowed_for_model_position(prop,row.get("position")):
        raise ValueError(f"Projection blocked: {prop} invalid for position {row.get('position')!r}")

    cfg=PROP_CONFIG[prop]
    role=apply_real_usage_to_role(row,player_role_defaults(row.get("position"),prop))
    usage_quality,usage_flags=usage_data_quality(row,prop)
    workload=_preseason_workload_model(row,prop,role)
    base,core=_preseason_projection_core(row,prop,role,workload)

    # Separate preseason learning/calibration. With no prior preseason grades these stay neutral.
    learn=learning_scale(row.get("player"),prop,"PRESEASON")
    if bool(st.session_state.get("smart_calibration_enabled",True)):
        cal_scale,cal_note,smart_calibration=smart_calibration_scale(row,role,usage_quality)
    else:
        cal_scale,cal_note=calibration_scale(row.get("player"),prop,"PRESEASON")
        smart_calibration={"active":cal_scale!=1.0,"level":"preseason_player_prop","scale":cal_scale}
    cal_status=calibration_readiness(prop,"PRESEASON")
    base*=learn*cal_scale

    # Savant is allowed to refine per-opportunity efficiency only. It cannot create
    # preseason snaps/drives/targets/carries. The Savant function already downweights
    # preseason effects and caps them tightly; low-reliability rows remain shadow-only.
    legacy_projection_pre_savant=float(base)
    savant_input={
        key:value for key,value in row.items()
        if key not in {"line","odds","price","over_price","under_price","spread","game_total","team_total","implied_team_total"}
        and not str(key).startswith("market_")
    }
    savant_input["prop"]=prop
    savant_input["projection_consumed_factors"]=["preseason_rotation","preseason_efficiency_prior"]
    savant_shadow=savant_shadow_projection(savant_input,legacy_projection_pre_savant,"PRESEASON")
    savant_reliability=safe_float(savant_shadow.get("reliability"),0) or 0
    savant_applied=bool(savant_shadow.get("active") and savant_reliability>=55)
    if savant_applied:
        base*=safe_float(savant_shadow.get("factor"),1.0) or 1.0
        savant_shadow["status"]="PRESEASON_EFFICIENCY_ASSIST"

    bounds={
        "Passing Yards":(1,330),"Pass Attempts":(0.5,48),"Completions":(0.2,34),
        "Receiving Yards":(0,165),"Receptions":(0,12),"Rushing Yards":(0,155),"Rush Attempts":(0,32),
    }
    lo,hi=bounds.get(prop,(0,999))
    base=float(clamp(base,lo,hi))

    market_workload=_preseason_market_workload_audit(row,core)
    sigma_base=safe_float(cfg.get("sigma"),1.0) or 1.0
    share=safe_float(workload.get("snap_share"),0.30) or 0.30
    sigma=max(safe_float(PRESEASON_SIGMA_FLOOR.get(prop),0.5) or 0.5,
              sigma_base*math.sqrt(max(0.12,share))*safe_float(workload.get("uncertainty_mult"),1.4))
    efficiency_score=safe_float(core.get("efficiency_prior_score"),65) or 65
    if efficiency_score < 60:
        sigma*=1.12
    elif efficiency_score < 70:
        sigma*=1.07
    limited=safe_float(row.get("limited_snap_risk"),0) or 0
    thin_eff_tax=0.04 if efficiency_score<60 else 0.02 if efficiency_score<70 else 0.0
    collapse_prob=clamp(0.12 + (0.10 if workload.get("confidence")=="LOW" else 0.04 if workload.get("confidence")=="MEDIUM" else 0) + limited*0.12 + thin_eff_tax,0.10,0.40)
    ceiling_prob=0.09 if workload.get("confidence")!="LOW" else 0.07
    seed=stable_projection_seed(row.get("player","x"),prop,line,row.get("team",""),row.get("opp",""),"PRESEASON")
    sim,distribution_meta=simulate_prop_distribution(base,sigma,prop,sims,seed,collapse_prob,ceiling_prob,empirical_values=None)
    mean=float(np.mean(sim)); p10,p25,p50,p75,p90=[float(np.percentile(sim,q)) for q in [10,25,50,75,90]]
    raw_over=float(np.mean(sim>line)); raw_under=float(np.mean(sim<line)); raw_push=max(0.0,1.0-raw_over-raw_under)

    vol=(p90-p10)/max(1,mean)
    volatility="HIGH" if vol>1.25 else "MED" if vol>0.78 else "LOW"
    stability=projection_stability_score(p10,p90,mean,prop)
    reliability_score,reliability_label=_preseason_reliability_score(row,workload,market_workload,usage_quality,volatility,efficiency_score)
    over,under,push,prob_strength=_preseason_calibrate_probabilities(raw_over,raw_under,raw_push,reliability_score)

    # For volatile preseason distributions, the P50 is the actionable fair line. Keep
    # the simulation mean separately for expected-value/audit work, but do not display a
    # skewed mean as the line projection when most simulations live on the other side.
    decision_projection=float(p50)
    edge=decision_projection-line
    preseason_edge_gate={
        "Passing Yards":6.0,"Pass Attempts":1.25,"Completions":1.0,
        "Receiving Yards":3.5,"Receptions":0.55,"Rushing Yards":3.5,"Rush Attempts":0.85,
    }.get(prop,1.0)
    # Small preseason edges should not carry 60%+ confidence merely because the
    # simulated distribution is lumpy. Shrink conviction again as edge approaches zero.
    edge_strength=float(clamp(abs(edge)/max(0.01,preseason_edge_gate),0.25,1.0))
    over=0.5+(over-0.5)*edge_strength
    under=0.5+(under-0.5)*edge_strength
    decision_total=over+under
    if decision_total>0:
        over/=decision_total; under/=decision_total
    push=0.0
    side="OVER" if over>under else "UNDER" if under>over else "PASS"
    prob=max(over,under)
    raw_prob=max(raw_over,raw_under)
    conflict=distribution_conflict_audit(mean,p50,line,side)
    # A mean/median/probability disagreement is a structural uncertainty signal,
    # not a betting side. Tiny P50 edges are also PASS in preseason.
    small_edge=abs(edge) < preseason_edge_gate*0.55
    if conflict.get("conflict") or small_edge:
        side="PASS"
    selected_price=selected_side_price(row,side) if side in {"OVER","UNDER"} else None
    loss_prob=under if side=="OVER" else over if side=="UNDER" else None
    ev=None if loss_prob is None else expected_value(prob,selected_price,loss_prob=loss_prob)
    kelly=0.0 if loss_prob is None else kelly_fraction(prob,selected_price,loss_prob=loss_prob)
    score=int(clamp(12 + 0.48*(safe_float(workload.get("score"),0) or 0) + 0.18*usage_quality + 0.16*efficiency_score,0,94))
    if market_workload.get("status")=="WATCH": score-=7
    if market_workload.get("conflict"): score-=18
    if not row.get("model_match",True): score-=8
    if workload.get("inactive"): score=min(score,35)
    score=int(clamp(score,0,94))

    model_fallback_used=(not row.get("model_match",True)) or workload.get("confidence")=="LOW"
    notes=list(workload.get("notes") or [])
    notes.extend(["Usage data: "+x for x in usage_flags[:3]])
    notes.append(cal_note)
    notes.append(market_workload.get("note",""))
    notes.append(f"Efficiency prior: {core.get('efficiency_prior_label')} {efficiency_score:.0f}/100 · {core.get('efficiency_prior_source')}")
    notes.append(f"Preseason reliability: {reliability_score:.1f}/100 ({reliability_label}); raw probability conviction shrunk {prob_strength:.0%} toward 50%.")
    notes.append("Preseason engine: opportunity first; regular-season volume not reused as full-game workload.")
    notes.append(f"NFL Savant preseason efficiency: {savant_shadow.get('status','MISSING')} · reliability {savant_reliability:.0f}")
    notes.append(f"Preseason decision projection uses P50 fair line {decision_projection:.2f}; simulation expected mean {mean:.2f} is retained for audit.")
    if small_edge:
        notes.append(f"SMALL_EDGE_PASS: |P50-line| {abs(edge):.2f} below preseason action floor {preseason_edge_gate*0.55:.2f}.")
    if conflict.get("conflict"):
        notes.append("DISTRIBUTION_CONFLICT: expected mean and P50/pick direction disagree; forced PASS")
    audit_label="Preseason Ready" if workload.get("confidence")=="HIGH" and not market_workload.get("conflict") else "Preseason Partial"
    audit_preview={
        "label":audit_label,"score":score,
        "hard_blocks":(["Preseason workload conflict with market"] if market_workload.get("conflict") else []) +
                      (["Player expected to rest/not play"] if workload.get("inactive") else []),
        "layers":{"preseason_workload":workload.get("confidence"),"market_workload":market_workload.get("status"),"efficiency_prior":core.get("efficiency_prior_label"),"reliability":reliability_label},
    }
    model_meta={"model_version":MODEL_VERSION,"app_version":APP_VERSION,"generated_at":now_iso(),"prop":prop,"source":row.get("source"),"season_mode":"PRESEASON","calibration_status":cal_status}
    out={**row,
        "season_mode":"PRESEASON","game_phase":"PRESEASON",
        "projection":round(decision_projection,2),"expected_mean":round(mean,2),"edge":round(edge,2),"pick":side,
        "fair_prob":round(prob,3),"over_prob":round(over,3),"under_prob":round(under,3),"push_prob":round(push,3),
        "raw_fair_prob":round(raw_prob,3),"raw_over_prob":round(raw_over,3),"raw_under_prob":round(raw_under,3),"raw_push_prob":round(raw_push,3),
        "reliability_score":reliability_score,"reliability_label":reliability_label,
        "probability_calibration":{"preseason_reliability_shrink":prob_strength,"edge_strength":round(edge_strength,3),"small_edge_pass":small_edge,"raw_fair_prob":round(raw_prob,3),"calibrated_fair_prob":round(prob,3)},
        "selected_price":selected_price,"ev":None if ev is None else round(ev,4),"kelly":round(kelly,4),
        "p10":round(p10,2),"p25":round(p25,2),"p50":round(p50,2),"p50_fair_line":conflict.get("p50_fair_line"),"median_edge":conflict.get("median_edge"),"distribution_conflict":conflict,"p75":round(p75,2),"p90":round(p90,2),
        "pure_upside":"GOOD" if p90-line>sigma*0.55 else "NORMAL",
        "volatility":volatility,"stability_score":stability,"usage_quality":usage_quality,
        "opportunity_score":safe_float(workload.get("score"),0) or 0,
        "expected_opportunity":core,"preseason_workload":workload,"preseason_market_workload":market_workload,
        "projection_breakdown":core,"factor_stack":{"preseason_workload_share":share,"efficiency_prior_score":efficiency_score,"learning":learn,"calibration":cal_scale,"sigma":round(sigma,3),"probability_reliability_shrink":prob_strength},
        "model_meta":model_meta,"model_version":MODEL_VERSION,"calibration_status":cal_status,
        "smart_calibration":smart_calibration,"role_bucket":projection_role_bucket(row,role),
        "data_quality_bucket":projection_data_quality_bucket(row,usage_quality),
        "market_intelligence":market_intelligence_engine(row,projection=base,line=line),
        "distribution_meta":distribution_meta,"projection_audit":audit_preview,"audit_label":audit_label,"audit_score":score,
        "model_fallback_used":model_fallback_used,"collapse_prob":round(collapse_prob,3),"ceiling_prob":round(ceiling_prob,3),
        "data_score":score,"injury_risk":"LOW","game_script_risk":"LOW","defense_risk":"LOW",
        "savant_shadow":savant_shadow,"legacy_projection_pre_savant":round(legacy_projection_pre_savant,3),
        "savant_shadow_projection":savant_shadow.get("shadow_projection"),
        "savant_projection_mode":"PRESEASON_EFFICIENCY_ASSIST" if savant_applied else "SHADOW_ONLY",
        "line_delta":update_clv_snapshot(row.get("player"),prop,row.get("source"),line),
        "true_line_delta":track_line_delta(row.get("player"),prop,row.get("source"),line),
        "role":role,"notes":notes,"sim_samples":sims,
    }
    out["market_compare"]=_market_compare_text(out)
    out["recent_form"]=_recent_form_text(out)
    signal,action_tier,rejections=build_signal(out)
    out["signal"]=signal; out["action_tier"]=action_tier; out["official_rejections"]=rejections; out["bettable"]=action_tier=="BET"
    return out

def project_row(row, sims=12000):
    row={**dict(row or {}),"season_mode":"REGULAR"}
    raw_market_labels=" ".join(str((row or {}).get(key) or "") for key in ["raw_prop_label","line_title","raw_label"])
    if raw_market_labels.strip() and not _is_full_game_market_label(raw_market_labels):
        raise ValueError(f"Projection blocked: non-full-game market {raw_market_labels!r}")
    row=merge_nfl_context(row)
    row["database_readiness"]=projection_database_readiness()
    market_row=dict(row)
    prop=_canon_prop_label(row.get("prop"))
    if prop not in ACTIVE_NFL_MARKETS or prop not in PROP_CONFIG:
        raise ValueError(f"Projection blocked: unmapped prop {row.get('prop')!r}")
    row["prop"]=prop
    market_row["prop"]=prop
    line_check=safe_float(market_row.get("line"))
    if line_check is None or not _valid_market_line(prop, line_check,"REGULAR"):
        raise ValueError(f"Projection blocked: invalid {prop} line {market_row.get('line')!r}")
    if not _prop_allowed_for_model_position(prop, row.get("position")):
        raise ValueError(f"Projection blocked: {prop} is not valid for position {row.get('position')!r}")
    # Keep sportsbook-derived information out of every raw model component. The
    # untouched market_row returns later for side selection, pricing, CLV and audit.
    market_only_fields={
        "line","odds","price","over_price","under_price","spread","home_spread",
        "away_spread","closing_spread","game_total","total","over_under",
        "team_total","implied_team_total","open_line","best_line","consensus_line",
        "no_vig_over","no_vig_under","has_market_context",
    }
    for key in list(row):
        if key in market_only_fields or str(key).startswith("market_"):
            row.pop(key,None)
    cfg=PROP_CONFIG[prop]
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
    passing_tds_model_info = {"active": False}
    anytime_td_model_info = {"active": False}
    fg_made_model_info = {"active": False}
    qb_tier_info = qb_tier_context(row.get("player"), row.get("position")) if prop == "Passing Yards" else {"tier":"N/A","factor":1.0,"sigma_factor":1.0,"confidence_boost":0,"note":""}
    if prop == "Passing Yards":
        base, pass_yards_model_info = passing_yards_stat_projection(row, role, cfg)
        # QB tiers change uncertainty/confidence only; names never create raw yards.
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
    elif prop == "Passing TDs":
        base, passing_tds_model_info = passing_tds_stat_projection(row, role, cfg)
        env_notes = env_notes + (passing_tds_model_info.get("notes") or [])
    elif prop == "Anytime TD":
        base, anytime_td_model_info = anytime_td_probability_projection(row, role, cfg)
        env_notes = env_notes + (anytime_td_model_info.get("notes") or [])
    elif prop == "Field Goals Made":
        base, fg_made_model_info = field_goals_made_stat_projection(row, role, cfg)
        env_notes = env_notes + (fg_made_model_info.get("notes") or [])

    # Stat-specific models already use recent form, so the role factor is bounded and
    # partially damped for those markets to prevent double counting.
    current_role_factor=safe_float(current_week_role.get("factor"),1.0) or 1.0
    if prop in ["Passing Yards","Receiving Yards","Rushing Yards","Pass Attempts","Completions","Receptions","Rush Attempts","Passing TDs","Anytime TD","Field Goals Made"]:
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
        # The stat model already includes QB history, attempts, pass rate, matchup,
        # and stadium/weather. Keep only small generic risk modifiers.
        base*=clamp(role_factor,0.92,1.04)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.03)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.94,1.05)
    elif prop == "Receiving Yards":
        # The stat model already includes receiving history, targets, team pass rate,
        # matchup, and stadium/weather. Keep generic modifiers small.
        base*=clamp(role_factor,0.90,1.05)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.95,1.05)*clamp(blowout_factor,0.93,1.04)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.94,1.06)
    elif prop == "Rushing Yards":
        base*=clamp(role_factor,0.88,1.06)*clamp(rank_factor,0.96,1.05)*clamp(game_factor,0.95,1.06)*clamp(blowout_factor,0.94,1.05)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.95,1.04)
    elif prop in ["Pass Attempts", "Completions"]:
        base*=clamp(role_factor,0.94,1.03)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.04)*clamp(advanced_factor,0.96,1.04)
    elif prop == "Receptions":
        base*=clamp(role_factor,0.91,1.05)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.04)*clamp(blowout_factor,0.94,1.04)*clamp(advanced_factor,0.96,1.04)*clamp(split_factor,0.96,1.04)
    elif prop == "Rush Attempts":
        base*=clamp(role_factor,0.90,1.05)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.05)*clamp(blowout_factor,0.94,1.05)*clamp(advanced_factor,0.97,1.03)
    elif prop == "Passing TDs":
        base*=clamp(role_factor,0.94,1.04)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.05)*clamp(advanced_factor,0.97,1.04)
    elif prop == "Anytime TD":
        base*=clamp(role_factor,0.92,1.06)*clamp(rank_factor,0.97,1.04)*clamp(game_factor,0.96,1.05)*clamp(advanced_factor,0.97,1.04)
    elif prop == "Field Goals Made":
        base*=clamp(game_factor,0.95,1.05)*clamp(advanced_factor,0.97,1.03)
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
    line=safe_float(market_row.get("line"))
    if line is not None and prop in ACTIVE_NFL_MARKETS and not _valid_market_line(prop, line,"REGULAR"):
        # Never project off a corrupted/season-long line. This row should normally
        # be filtered earlier, but this final guard prevents 3000+ yard cards.
        line = None
        market_row["line"] = None
    # Market information is audit/confidence context only. The raw projection stays
    # independent of the Underdog line so the line only determines OVER/UNDER afterward.
    market_intelligence=market_intelligence_engine(market_row, projection=base, line=line)

    xgb_info = {"enabled": bool(st.session_state.get("xgb_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("xgb_assist_enabled", False):
        base, xgb_info = xgboost_assist_projection({**row, "projection": base, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    bayes_markov_info = {"enabled": bool(st.session_state.get("advanced_sim_assist_enabled", True)), "status": "OFF"}
    if st.session_state.get("advanced_sim_assist_enabled", True):
        base, bayes_markov_info = bayesian_markov_poisson_engine({**row, "projection": base, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "usage_quality": usage_quality, "data_score": 70}, prop, base)

    ensemble_info = {"enabled": bool(st.session_state.get("ensemble_ml_assist_enabled", False)), "status": "OFF"}
    if st.session_state.get("ensemble_ml_assist_enabled", False):
        base, ensemble_info = ensemble_ml_assist_projection({**row, "projection": base, "opportunity_score": opportunity.get("factor",1.0)*100, "pace_factor": pace_factor, "vegas_factor": vegas_factor, "game_script_factor": script_factor, "matchup_factor": defense_factor, "blowout_prob": blowout_prob, "collapse_prob": 0.0, "ceiling_prob": 0.0, "usage_quality": usage_quality, "data_score": 70}, base)

    legacy_projection_pre_savant=float(base)
    # Remove all line/price/market fields before shadow projection so NFL Savant
    # cannot leak the Underdog line into the raw estimate.
    savant_input={
        key:value for key,value in row.items()
        if key not in {"line","odds","price","over_price","under_price","market_consensus_line","market_consensus","market_best_line","market_open_line"}
        and not str(key).startswith("market_")
    }
    savant_input["prop"]=prop
    savant_input["savant_player_context"]=row.get("savant_player_context") or {}
    savant_input["savant_matchup_context"]=row.get("savant_matchup_context") or {}
    savant_input["projection_consumed_factors"]=[
        "legacy_role","legacy_matchup","legacy_pressure","legacy_game_environment"
    ]
    savant_shadow=savant_shadow_projection(savant_input,legacy_projection_pre_savant,season_mode="REGULAR")
    if SAVANT_PRODUCTION_ENABLED and prop in SAVANT_VALIDATED_PRODUCTION_PROPS and savant_shadow.get("active"):
        base=float(savant_shadow.get("shadow_projection",base))
        savant_shadow["status"]="PRODUCTION_VALIDATED"

    base, line_sanity_info = _market_line_sanity_projection(base, line, prop, row.get("source"))

    sigma=cfg["sigma"]
    if prop == "Passing Yards":
        sigma *= safe_float(qb_tier_info.get("sigma_factor"), 1.0) or 1.0
    sigma=calibrated_sigma(prop, sigma, row, usage_quality, injury_risk, game_script_risk, advanced_context)
    collapse_prob, ceiling_prob = simulation_branch_rates(row, prop, injury_risk, game_script_risk)
    collapse_prob = clamp(collapse_prob + (blowout_prob*0.12 if prop in ["Passing Yards","Receiving Yards","Receptions","Rushing Yards","Rush Attempts"] else 0), 0.05, 0.46)
    if script_risk == "HIGH":
        sigma *= 1.04
    seed=stable_projection_seed(row.get("player","x"), prop, row.get("team",""), row.get("opp",""), row.get("source",""),"raw_v732")
    empirical_values=empirical_values_for_row(row,prop)
    sim, distribution_meta=simulate_prop_distribution(base, sigma, prop, sims, seed, collapse_prob, ceiling_prob, empirical_values=empirical_values)

    mean=float(np.mean(sim)); p50=float(np.percentile(sim,50)); p75=float(np.percentile(sim,75)); p90=float(np.percentile(sim,90)); p25=float(np.percentile(sim,25)); p10=float(np.percentile(sim,10))
    if line is None:
        raw_over=None; raw_under=None; raw_push=None; over=None; under=None; push=None; prob=None; side="NO LINE"; edge=None; ev=None; kelly=0.0; selected_price=None
    else:
        raw_over=float(np.mean(sim>line)); raw_under=float(np.mean(sim<line))
        raw_push=max(0.0, 1.0-raw_over-raw_under)
        over,under,push=raw_over,raw_under,raw_push
        # Direction starts from raw simulation. Reliability calibration below changes
        # conviction only; it never moves the projection mean.
        side="OVER" if over>under else "UNDER" if under>over else "PASS"
        prob=max(over,under)
        edge=mean-line
        selected_price=selected_side_price(market_row,side)
        loss_prob=under if side=="OVER" else over if side=="UNDER" else None
        ev=None if loss_prob is None else expected_value(prob,selected_price,loss_prob=loss_prob)
        kelly=0.0 if loss_prob is None else kelly_fraction(prob,selected_price,loss_prob=loss_prob)

    distribution_conflict=distribution_conflict_audit(mean,p50,line,side)

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
    score+=8
    score+=int((stability-60)*0.15)
    score+=int((usage_quality-70)*0.20)
    if injury_risk=="HIGH": score-=14
    if injury_risk=="EXTREME": score-=32
    if game_script_risk=="HIGH": score-=5
    if current_week_role.get("risk") == "HIGH": score-=7
    score += int(market_intelligence.get("confidence_delta",0) or 0)
    if prop == "Passing Yards":
        score += int(qb_tier_info.get("confidence_boost", 0) or 0)
    audit_preview=projection_audit(market_row)
    if market_row.get("data_integrity_block"):
        audit_preview=dict(audit_preview or {})
        audit_preview["hard_blocks"]=list(dict.fromkeys((audit_preview.get("hard_blocks") or [])+[str(market_row.get("data_integrity_block"))]))
        audit_preview["label"]="Data Review"
    if audit_preview.get("label") == "Fresh":
        score+=4
    elif audit_preview.get("label") == "Stale":
        score-=12
    if audit_preview.get("hard_blocks"):
        score-=18
    model_fallback_used = any(
        "fallback" in str(n).lower()
        for n in (env_notes or [])
    ) or row.get("model_match_status") == "NO_MODEL_MATCH"
    if model_fallback_used:
        score=min(score,74)
    if not row["database_readiness"].get("ready"):
        score=min(score,74)
    score=int(clamp(score,0,99))

    # Keep regular-season input reliability separate from simulation direction.
    # This prevents partial/fallback rows from displaying fake 80-90% certainty.
    reliability_score,reliability_label=nfl_projection_reliability_score(
        market_row,usage_quality,stability,audit_preview,model_fallback_used,injury_risk,game_script_risk,volatility
    )
    decision_prob_strength=None
    raw_prob=None if line is None else max(raw_over,raw_under)
    if line is not None:
        over,under,push,decision_prob_strength=calibrate_nfl_decision_probabilities(
            raw_over,raw_under,raw_push,reliability_score,volatility
        )
        side="OVER" if over>under else "UNDER" if under>over else "PASS"
        prob=max(over,under)
        selected_price=selected_side_price(market_row,side)
        loss_prob=under if side=="OVER" else over if side=="UNDER" else None
        ev=None if loss_prob is None else expected_value(prob,selected_price,loss_prob=loss_prob)
        kelly=0.0 if loss_prob is None else kelly_fraction(prob,selected_price,loss_prob=loss_prob)
        distribution_conflict=distribution_conflict_audit(mean,p50,line,side)
        if distribution_conflict.get("conflict"):
            side="PASS"; selected_price=None; ev=None; kelly=0.0
        # Regular-season near-coin-flip decisions are audit rows, not forced plays.
        # The model projection remains intact; only the displayed/action side is gated.
        tiny_edge_req=max(0.10, edge_requirement(prop)*0.22)
        if prob is not None and (prob < 0.535 or abs(edge or 0.0) < tiny_edge_req):
            side="PASS"; selected_price=None; ev=None; kelly=0.0

    line_delta=update_clv_snapshot(market_row.get("player"), prop, market_row.get("source"), line) if line is not None else None
    true_line_delta=track_line_delta(market_row.get("player"), prop, market_row.get("source"), line) if line is not None else None

    notes=[]+env_notes+opportunity.get("notes",[])+pace_notes+risk_notes+defense_notes+rank_notes+game_notes+vegas_notes+script_notes+blowout_notes+advanced_notes+split_notes+current_week_role.get("notes",[])+market_intelligence.get("notes",[])
    if market_row.get("data_integrity_block"):
        notes.append(str(market_row.get("data_integrity_block")))
    if usage_flags:
        notes.extend(["Usage data: "+x for x in usage_flags[:3]])
    notes.append(cal_note)
    notes.append(f"Decision reliability: {reliability_score:.1f}/100 ({reliability_label})" + (f" · probability conviction retained {decision_prob_strength:.0%}" if decision_prob_strength is not None else ""))
    if distribution_conflict.get("conflict"):
        notes.append("DISTRIBUTION_CONFLICT: expected mean and P50/pick direction disagree; forced PASS")
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
    if passing_tds_model_info.get("active"):
        b=passing_tds_model_info.get("breakdown",{}); active_breakdown=b
        notes.append(f"Passing TD model: {b.get('projected_attempts')} attempts × {b.get('td_rate')} TD/att")
    if anytime_td_model_info.get("active"):
        b=anytime_td_model_info.get("breakdown",{}); active_breakdown=b
        notes.append(f"Rush + Rec TD probability: {round((b.get('td_probability') or 0)*100,1)}%")
    if fg_made_model_info.get("active"):
        b=fg_made_model_info.get("breakdown",{}); active_breakdown=b
        notes.append(f"FG Made model: {b.get('fg_attempts_pg')} attempts/game · {b.get('fg_pct')}% make")
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
    if savant_shadow.get("active"):
        notes.append(
            f"NFL Savant shadow: {savant_shadow.get('shadow_projection')} "
            f"vs legacy {savant_shadow.get('legacy_projection')} · sample {savant_shadow.get('sample_size',0)}"
        )
    else:
        notes.append(f"NFL Savant shadow: {savant_shadow.get('status','MISSING')}")
    if distribution_conflict.get("conflict"):
        notes.append("DISTRIBUTION_CONFLICT: expected mean and P50/pick direction disagree")

    factor_stack={"role":round(role_factor,3),"current_week_role":round(current_role_factor,3),"game_env":round(game_factor,3),"defense":round(defense_factor,3),"offense_defense_rank":round(rank_factor,3),"opportunity":round(opportunity.get("factor",1.0),3),"pace":round(pace_factor,3),"vegas":round(vegas_factor,3),"script":round(script_factor,3),"blowout":round(blowout_factor,3),"advanced":round(advanced_factor,3),"splits_personnel":round(split_factor,3),"learning":round(learn,3),"calibration":round(cal_scale,3),"sigma":round(sigma,3),"line_sanity_active":bool(line_sanity_info.get("active"))}
    factor_stack["savant_production_active"]=savant_shadow.get("status")=="PRODUCTION_VALIDATED"
    model_meta={"model_version":MODEL_VERSION,"app_version":APP_VERSION,"generated_at":now_iso(),"active_market_count":len(ACTIVE_NFL_MARKETS),"prop":prop,"source":row.get("source"),"context_layers":audit_preview.get("layers",{}),"staleness":context_staleness(market_row),"calibration_status":cal_status,"projection_consumed_factors":savant_input.get("projection_consumed_factors",[])}
    if market_row.get("data_integrity_block"):
        side="PASS"; selected_price=None; ev=None; kelly=0.0
    out={**market_row,"projection":round(mean,2),"edge":None if edge is None else round(edge,2),"pick":side,"fair_prob":None if prob is None else round(prob,3),"over_prob":None if over is None else round(over,3),"under_prob":None if under is None else round(under,3),"push_prob":None if push is None else round(push,3),"raw_fair_prob":None if raw_prob is None else round(raw_prob,3),"raw_over_prob":None if raw_over is None else round(raw_over,3),"raw_under_prob":None if raw_under is None else round(raw_under,3),"raw_push_prob":None if raw_push is None else round(raw_push,3),"reliability_score":reliability_score,"reliability_label":reliability_label,"probability_calibration":{"decision_probability_strength":decision_prob_strength,"raw_fair_prob":None if raw_prob is None else round(raw_prob,3),"calibrated_fair_prob":None if prob is None else round(prob,3)},"selected_price":selected_price,"ev":None if ev is None else round(ev,4),"kelly":round(kelly,4),"p10":round(p10,2),"p25":round(p25,2),"p50":round(p50,2),"p75":round(p75,2),"p90":round(p90,2),"pure_upside":upside,"volatility":volatility,"stability_score":stability,"usage_quality":usage_quality,"opportunity_score":round(opportunity.get("factor",1.0)*100,1),"expected_opportunity":opportunity.get("expected",{}),"pace_factor":round(pace_factor,3),"vegas_factor":round(vegas_factor,3),"advanced_factor":round(advanced_factor,3),"split_personnel_factor":round(split_factor,3),"split_personnel_context":split_context,"advanced_context":advanced_context,"offense_defense_rank_context":rank_context,"offense_defense_rank_factor":round(rank_factor,3),"passing_yards_model":pass_yards_model_info,"receiving_yards_model":receiving_yards_model_info,"rushing_yards_model":rushing_yards_model_info,"pass_attempts_model":pass_attempts_model_info,"completions_model":completions_model_info,"receptions_model":receptions_model_info,"rush_attempts_model":rush_attempts_model_info,"passing_tds_model":passing_tds_model_info,"anytime_td_model":anytime_td_model_info,"field_goals_made_model":fg_made_model_info,"qb_tier":qb_tier_info,"projection_breakdown":active_breakdown,"factor_stack":factor_stack,"model_meta":model_meta,"model_version":MODEL_VERSION,"calibration_status":cal_status,"smart_calibration":smart_calibration,"role_bucket":current_week_role.get("role_bucket") or projection_role_bucket(row,role),"data_quality_bucket":projection_data_quality_bucket(row,usage_quality),"current_week_role":current_week_role,"market_intelligence":market_intelligence,"distribution_meta":distribution_meta,"projection_audit":audit_preview,"audit_label":audit_preview.get("label"),"audit_score":audit_preview.get("score"),"xgb_assist":xgb_info,"bayes_markov_assist":bayes_markov_info,"ensemble_ml_assist":ensemble_info,"line_sanity":line_sanity_info,"model_fallback_used":model_fallback_used,"game_script_factor":round(script_factor,3),"game_script_branches":script_branches,"blowout_prob":blowout_prob,"matchup_factor":round(defense_factor,3),"collapse_prob":round(collapse_prob,3),"ceiling_prob":round(ceiling_prob,3),"data_score":score,"injury_risk":injury_risk,"game_script_risk":game_script_risk,"defense_risk":defense_risk,"line_delta":line_delta,"true_line_delta":true_line_delta,"role":role,"env":env,"notes":notes,"sim_samples":sims}
    out.update({
        "expected_mean":round(mean,2),
        "p50_fair_line":distribution_conflict.get("p50_fair_line"),
        "median_edge":distribution_conflict.get("median_edge"),
        "distribution_conflict":distribution_conflict,
        "savant_shadow":savant_shadow,
        "legacy_projection_pre_savant":round(legacy_projection_pre_savant,3),
        "savant_shadow_projection":savant_shadow.get("shadow_projection"),
        "savant_projection_mode":"PRODUCTION_VALIDATED" if savant_shadow.get("status")=="PRODUCTION_VALIDATED" else "SHADOW_ONLY",
        "season_mode":"REGULAR",
    })
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
    seed=stable_projection_seed(p.get("player","x"),prop,p.get("team",""),p.get("opp",""),"team_reconcile_v732")
    sim,meta=simulate_prop_distribution(target,sigma*max(0.82,math.sqrt(scale)),prop,sims,seed,safe_float(p.get("collapse_prob"),0.12) or 0.12,safe_float(p.get("ceiling_prob"),0.08) or 0.08,empirical_values=empirical_values_for_row(p,prop))
    mean=float(np.mean(sim)); p10,p25,p50,p75,p90=[float(np.percentile(sim,q)) for q in [10,25,50,75,90]]
    line=safe_float(p.get("line"))
    if line is not None:
        over=float(np.mean(sim>line)); under=float(np.mean(sim<line)); push=max(0.0,1.0-over-under)
        side="OVER" if over>under else "UNDER" if under>over else "PASS"; prob=max(over,under)
        price=selected_side_price(p,side); loss_prob=under if side=="OVER" else over if side=="UNDER" else None
        p.update({"edge":round(mean-line,2),"pick":side,"fair_prob":round(prob,3),"over_prob":round(over,3),"under_prob":round(under,3),"push_prob":round(push,3),"selected_price":price,"ev":None if loss_prob is None else round(expected_value(prob,price,loss_prob=loss_prob),4),"kelly":0.0 if loss_prob is None else round(kelly_fraction(prob,price,loss_prob=loss_prob),4)})
    conflict=distribution_conflict_audit(mean,p50,line,p.get("pick"))
    p.update({"projection":round(mean,2),"expected_mean":round(mean,2),"p10":round(p10,2),"p25":round(p25,2),"p50":round(p50,2),"p50_fair_line":conflict.get("p50_fair_line"),"median_edge":conflict.get("median_edge"),"distribution_conflict":conflict,"p75":round(p75,2),"p90":round(p90,2),"stability_score":projection_stability_score(p10,p90,mean,prop),"distribution_meta":{**(p.get("distribution_meta") or {}),**meta},"team_volume_reconciliation":{"active":True,"scale":round(scale,4),"before":round(old,2),"after":round(mean,2),"reason":reason}})
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

def reconcile_preseason_team_volume(rows):
    """Reuse the downward-only team constraints after rotation-first projections."""
    reconciled=reconcile_team_projection_volume(rows)
    for row in reconciled:
        row["season_mode"]="PRESEASON"
        row.setdefault("preseason_volume_reconciliation",row.get("team_volume_reconciliation"))
    return reconciled

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

def update_learning_from_result(player, prop, projected, actual, season_mode="REGULAR"):
    data=load_json(LEARN_FILE,{})
    mode=normalized_season_mode(season_mode) or "REGULAR"
    key=f"{mode}|{norm(player)}|{prop}"
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
        line=safe_float(r.get("line")); pick=str(r.get("pick") or "").upper(); win=None; outcome="NO_ACTION"
        if line is None:
            outcome="NO_LINE"
        elif actual == line:
            outcome="PUSH"
        elif pick == "OVER":
            win=actual > line; outcome="WIN" if win else "LOSS"
        elif pick == "UNDER":
            win=actual < line; outcome="WIN" if win else "LOSS"
        mode=graded_row_season_mode(r)
        scale=update_learning_from_result(r.get("player"), r.get("prop"), r.get("projection"), actual, mode)
        out=_clean_snapshot_row(r)
        out.update({
            "season_mode":mode,
            "actual":actual,
            "win":win,
            "grade_outcome":outcome,
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


st.markdown("""
<style>
.compact-prop-head,.compact-prop-row{display:grid;grid-template-columns:2.15fr 1.15fr .8fr .8fr 1.05fr 1fr;gap:14px;align-items:center}
.compact-prop-head{padding:10px 14px;color:#72798b;font-size:11px;font-weight:850;letter-spacing:.12em;border-bottom:1px solid rgba(255,255,255,.08);background:#0c111c}
.compact-prop-row{padding:15px 14px;border-bottom:1px solid rgba(255,255,255,.075);background:linear-gradient(180deg,rgba(12,18,29,.96),rgba(6,10,17,.96))}
.compact-prop-row:hover{background:rgba(25,31,45,.96)}
.compact-prop-row strong{display:block;color:#eef3ff;font-size:16px;line-height:1.15}.compact-prop-row span{display:block;color:#737d91;font-size:11px;margin-top:4px}
.cp-player{display:flex;align-items:center;gap:10px}.cp-player-copy{min-width:0}.cp-team-logo{width:34px;height:34px;object-fit:contain;flex:0 0 34px;border-radius:7px;background:#101827;padding:3px;border:1px solid rgba(255,255,255,.08)}.cp-player strong{font-size:18px}.cp-proj strong{color:#57e67b;font-size:25px;font-weight:500}.cp-edge strong{font-size:18px}.edge-pos strong{color:#55e778}.edge-neg strong{color:#ff5c56}
.cp-pick strong{font-size:18px}.pick-over strong{color:#58e779}.pick-under strong{color:#4d8dff}.pick-pass strong{color:#efc759}.cp-over strong{font-size:13px;color:#9aa4b8}
.cp-bar{height:8px;background:#202735;border-radius:999px;overflow:hidden;margin-top:7px}.cp-bar i{display:block;height:100%;background:linear-gradient(90deg,#4377e6,#57e67b);border-radius:999px}
.prop-view-toolbar{margin:.25rem 0 1rem}
@media(max-width:760px){
 /* Keep the same six-column IQ-card language as the reference image on phones. */
 .compact-prop-head,.compact-prop-row{grid-template-columns:1.75fr .92fr .70fr .72fr .92fr .90fr;gap:4px;min-width:0}
 .compact-prop-head{display:grid;padding:8px 5px;font-size:8px;letter-spacing:.08em}
 .compact-prop-row{padding:11px 5px;border-bottom:1px solid rgba(255,255,255,.075);border-radius:0;margin-bottom:0}
 .compact-prop-row>div{min-width:0;overflow:hidden}
 .compact-prop-row strong{font-size:11px;white-space:normal;overflow-wrap:anywhere}.compact-prop-row span{font-size:8px;margin-top:2px;white-space:normal}
 .cp-team-logo{width:27px;height:27px;flex-basis:27px;padding:2px}.cp-player{gap:5px}.cp-player strong{font-size:13px}.cp-proj strong{font-size:19px}.cp-edge strong{font-size:12px}.cp-pick strong{font-size:13px}.cp-over strong{font-size:9px}
 .cp-bar{height:6px;margin-top:4px}
}
</style>
""", unsafe_allow_html=True)

st.markdown('\n<style>\n@media(max-width:760px){\n  .hero-panel .sub-title{display:none}\n  .hero-panel{padding:11px 12px!important}\n  .hero-panel .badge{font-size:9px!important;padding:3px 6px!important}\n  .kpi-sub{display:none}\n  .kpi-box{min-height:58px!important;padding:8px!important}\n  .compact-prop-row{background:#080d16!important}\n}\n</style>\n', unsafe_allow_html=True)

st.markdown("""
<style>
.ml-board{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:12px}
.ml-game-card{background:#080e18;border:1px solid rgba(239,199,89,.52);border-radius:8px;overflow:hidden;box-shadow:0 12px 28px rgba(0,0,0,.25)}
.ml-card-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08);color:#8d97aa;font-size:11px;font-weight:800}
.ml-status{color:#efc759}.ml-status.ready{color:#62e88a}.ml-status.blocked{color:#ff6b70}
.ml-teams{display:grid;grid-template-columns:1fr 1.15fr 1fr;align-items:center;padding:16px 14px 12px}
.ml-team{text-align:center;min-width:0}.ml-team-badge{position:relative;width:54px;height:54px;margin:0 auto 7px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(239,199,89,.42);border-radius:8px;background:#101827;color:#fff;font-size:18px;font-weight:950;overflow:hidden}.ml-team-badge span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}.ml-team-badge img{position:absolute;inset:3px;width:calc(100% - 6px);height:calc(100% - 6px);object-fit:contain;background:#101827}
.ml-team-name{font-size:23px;color:#f1f5ff;font-weight:950}.ml-team-proj{font-size:36px;color:#efc759;font-weight:500;line-height:1.05;margin-top:8px}.ml-team-prob{font-size:12px;color:#8f99ac;margin-top:3px}
.ml-center{text-align:center;min-width:0}.ml-center-label{color:#778196;font-size:11px;text-transform:uppercase;font-weight:850}.ml-winner-lock{display:flex;flex-direction:column;align-items:center;justify-content:center;margin-top:5px}.ml-winner-kicker{font-size:8px;letter-spacing:.14em;color:#7f899c;font-weight:950;margin-bottom:4px}.ml-winner-logo{width:52px;height:52px;object-fit:contain;border-radius:12px;background:radial-gradient(circle at center,rgba(239,199,89,.18),rgba(12,20,33,.85));padding:5px;border:1px solid rgba(239,199,89,.55);box-shadow:0 0 22px rgba(239,199,89,.18)}.ml-winner-fallback{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:#101827;border:1px solid rgba(239,199,89,.55);color:#fff;font-weight:950}.ml-favorite{color:#efc759;font-size:13px;font-weight:950;margin-top:4px}.ml-winbar{display:flex;height:9px;background:#1d2635;border-radius:99px;overflow:hidden;margin:8px 0 5px}.ml-winbar-away{background:#4a84ef}.ml-winbar-home{background:#efc759}.ml-wintext{display:flex;justify-content:space-between;color:#8d97aa;font-size:10px}
.ml-total-band{display:grid;grid-template-columns:1.2fr 1fr;align-items:center;gap:12px;padding:14px;border-top:1px solid rgba(255,255,255,.08);border-bottom:1px solid rgba(255,255,255,.08);background:#0b1320}.ml-total-label{font-size:11px;color:#7d879a;font-weight:850}.ml-total-number{font-size:35px;color:#ff4e85;line-height:1.05;margin-top:3px}.ml-total-edge{color:#57e67b;font-size:13px;font-weight:850}.ml-total-call{text-align:right}.ml-total-pick{font-size:20px;color:#efc759;font-weight:950}.ml-total-pick.pass{color:#9ba5b7}.ml-total-confidence{font-size:11px;color:#8d97aa;margin-top:4px}
.ml-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));padding:12px 8px}.ml-metric{text-align:center;padding:2px 7px;border-right:1px solid rgba(255,255,255,.07);min-width:0}.ml-metric:last-child{border-right:0}.ml-metric-label{font-size:9px;color:#737e92;font-weight:850}.ml-metric-value{font-size:13px;color:#efc759;font-weight:900;margin-top:5px;overflow-wrap:anywhere}.ml-metric-sub{font-size:9px;color:#8e98aa;margin-top:3px;overflow-wrap:anywhere}
.ml-card-foot{padding:0 14px 12px;color:#8d97aa;font-size:10px}.ml-audit{margin:0 12px 12px;border-top:1px solid rgba(255,255,255,.08);padding-top:9px;color:#9aa5b7}.ml-audit summary{cursor:pointer;color:#efc759;font-size:10px;font-weight:900;letter-spacing:.04em}.ml-audit-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:8px}.ml-audit-grid>div{background:#0c1421;border:1px solid rgba(255,255,255,.06);border-radius:6px;padding:7px;font-size:9px}.ml-factor-list{font-size:9px;line-height:1.45;margin-top:7px;color:#8e98aa}.ml-block{padding:18px 14px;color:#ffb7ba;font-size:13px;line-height:1.45}.ml-model-only{color:#7fd6ff}
@media(max-width:900px){.ml-board{grid-template-columns:1fr}}
@media(max-width:520px){.ml-board{gap:10px}.ml-card-head{padding:10px}.ml-teams{padding:13px 8px 10px;grid-template-columns:1fr 1.05fr 1fr}.ml-team-badge{width:44px;height:44px;font-size:15px}.ml-team-name{font-size:19px}.ml-team-proj{font-size:30px}.ml-center-label{font-size:9px}.ml-winner-logo{width:44px;height:44px;padding:4px}.ml-winner-kicker{font-size:7px}.ml-favorite{font-size:11px}.ml-total-band{padding:12px 10px}.ml-total-number{font-size:31px}.ml-total-pick{font-size:17px}.ml-metrics{padding:10px 3px}.ml-metric{padding:2px 3px}.ml-metric-label{font-size:8px}.ml-metric-value{font-size:11px}.ml-metric-sub{font-size:8px}.ml-card-foot{padding:0 10px 10px}}
</style>
""", unsafe_allow_html=True)

# ---------- UI ----------
POSITION_TAB_PROPS = {
    "QBs": ["All", "Passing Yards", "Passing TDs", "Pass Attempts", "Completions", "Rushing Yards"],
    "RBs": ["All", "Rushing Yards", "Rush Attempts", "Receiving Yards", "Receptions", "Anytime TD"],
    "Receivers": ["All", "Receiving Yards", "Receptions", "Anytime TD"],
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
    show_cols=["player","position","team","matchup","prop","line","projection","savant_shadow_projection","p50_fair_line","median_edge","edge","pick","fair_prob","signal","action_tier","distribution_conflict","savant_status","savant_sample_size","savant_reliability","preseason_workload_confidence","audit_label","market_compare","recent_form","data_score","stability_score","opportunity_score","ev","kelly","xgb_status","advanced_factor","game_script_factor","matchup_factor","blowout_prob","pure_upside","volatility","line_delta","source"]
    view = view[[c for c in show_cols if c in view.columns]]
    safe_key = re.sub(r"[^a-z0-9]+", "_", str(title).lower()).strip("_") or "board"
    default_size = int(st.session_state.get("nfl_table_page_size", 50) or 50)
    page_size = st.selectbox("Rows per page", [25, 50, 100], index=[25,50,100].index(default_size if default_size in [25,50,100] else 50), key=f"{safe_key}_table_size")
    pages = max(1, math.ceil(len(view) / page_size))
    page = st.number_input("Page", min_value=1, max_value=pages, value=min(int(st.session_state.get(f"{safe_key}_page", 1) or 1), pages), step=1, key=f"{safe_key}_page")
    lo=(int(page)-1)*page_size; hi=min(len(view), lo+page_size)
    st.caption(f"Showing {lo+1}-{hi} of {len(view)} rows")
    st.dataframe(view.iloc[lo:hi], use_container_width=True, hide_index=True, height=min(720, 42*(hi-lo+1)+38))

def _fmt_num(v, digits=1):
    x=safe_float(v)
    if x is None:
        return "—"
    if abs(x-round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.{digits}f}"

def _render_compact_prop_board(rows, title="Prop Board"):
    """Mobile-first compact IQ board modeled after the clean WNBA row layout."""
    if not rows:
        st.warning(f"No {title} lines are loaded.")
        return
    rows=sorted(rows, key=lambda p: (-(safe_float(p.get("fair_prob"),0) or 0), -(abs(safe_float(p.get("edge"),0) or 0))))
    page_size=20
    safe_key=re.sub(r"[^a-z0-9]+","_",str(title).lower()).strip("_") or "compact"
    pages=max(1, math.ceil(len(rows)/page_size))
    page=1
    if pages>1:
        page=int(st.number_input("Page", min_value=1, max_value=pages, value=1, step=1, key=f"{safe_key}_compact_page"))
    lo=(page-1)*page_size; shown=rows[lo:lo+page_size]
    st.markdown("""
    <div class='compact-prop-head'>
      <div>PLAYER</div><div>PROP / LINE</div><div>PROJ</div><div>EDGE</div><div>PICK</div><div>P(OVER)</div>
    </div>
    """, unsafe_allow_html=True)
    cards=[]
    for p in shown:
        player=html.escape(str(p.get("player") or ""))
        team=html.escape(str(p.get("team") or ""))
        pos=html.escape(str(p.get("position") or ""))
        matchup=html.escape(str(p.get("matchup") or ""))
        prop=html.escape(str(ACTIVE_NFL_MARKET_LABELS.get(p.get("prop"),p.get("prop") or "")))
        line=_fmt_num(p.get("line"),1)
        line_tag="UD MAIN" if p.get("primary_line_selected") else "UD" if str(p.get("source") or "").lower()=="underdog" else str(p.get("source") or "")
        proj=_fmt_num(p.get("projection"),1)
        edge=safe_float(p.get("edge"))
        edge_txt="—" if edge is None else f"{edge:+.1f}"
        pick=str(p.get("pick") or "PASS").upper()
        fair=safe_float(p.get("fair_prob"))
        fair_txt="—" if fair is None else f"{fair*100:.0f}%"
        overp=safe_float(p.get("over_prob"))
        over_txt="—" if overp is None else f"{overp*100:.0f}%"
        bar=max(0,min(100,(overp or 0)*100))
        pick_cls="pick-over" if pick=="OVER" else "pick-under" if pick=="UNDER" else "pick-pass"
        edge_cls="edge-pos" if (edge or 0)>0 else "edge-neg" if (edge or 0)<0 else ""
        audit=html.escape(str(p.get("audit_label") or "Partial"))
        savant=html.escape(str(p.get("savant_status") or "MISSING"))
        logo=html.escape(nfl_team_logo_url(p.get("team")),quote=True)
        logo_html=(f"<img class='cp-team-logo' src='{logo}' alt='{team} logo' loading='lazy' decoding='async' referrerpolicy='no-referrer' onerror=\"this.style.display='none'\">" if logo else "")
        primary,secondary=nfl_team_theme(p.get("team"))
        cards.append(textwrap.dedent(f"""
        <div class='compact-prop-row' style='--team-primary:{primary};--team-secondary:{secondary}'>
          <div class='cp-player'>{logo_html}<div class='cp-player-copy'><strong>{player}</strong><span>{team} · {pos}{(' · '+matchup) if matchup else ''}</span></div></div>
          <div class='cp-prop'><strong>{prop}</strong><span>{line_tag} · vs {line}</span></div>
          <div class='cp-proj'><strong>{proj}</strong><span>projection</span></div>
          <div class='cp-edge {edge_cls}'><strong>{edge_txt}</strong><span>edge</span></div>
          <div class='cp-pick {pick_cls}'><strong>{pick}</strong><span>{fair_txt} · {audit} · SAV {savant}</span></div>
          <div class='cp-over'><strong>{over_txt}</strong><div class='cp-bar'><i style='width:{bar:.0f}%'></i></div></div>
        </div>
        """))
    st.markdown("".join(cards), unsafe_allow_html=True)
    if pages>1:
        st.caption(f"Showing {lo+1}-{min(len(rows),lo+page_size)} of {len(rows)}")

def _format_american_odds(value):
    value=safe_float(value)
    if value is None:
        return "—"
    return f"{int(round(value)):+d}"

def nfl_team_logo_url(team):
    team=_normalize_nfl_team(team)
    logo_id=ESPN_NFL_LOGO_IDS.get(team)
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{logo_id}.png" if logo_id else ""

def _moneyline_team_badge(team):
    team=html.escape(str(_normalize_nfl_team(team) or team or ""))
    logo=html.escape(nfl_team_logo_url(team),quote=True)
    if not logo:
        return f"<div class='ml-team-badge'><span>{team}</span></div>"
    # The text remains underneath the image and becomes visible if the CDN image
    # cannot load, so phone users still get an identifiable team badge.
    return (
        f"<div class='ml-team-badge'><span>{team}</span>"
        f"<img src='{logo}' alt='{team} logo' loading='lazy' decoding='async' "
        f"referrerpolicy='no-referrer' onerror=\"this.remove()\"></div>"
    )

def _moneyline_time_label(value):
    dt=_display_datetime(value)
    if not dt:
        return "START TIME PENDING"
    label=dt.strftime("%a %b %d | %I:%M %p %Z").replace(" 0"," ").upper()
    return label


def _ui_pct(value):
    x=safe_float(value)
    if x is None: return None
    if abs(x)<=1.0: x*=100.0
    return float(clamp(x,0,100))

def _ui_grade_from_factor(value):
    x=safe_float(value)
    if x is None: return "—"
    if x>=1.12: return "A+"
    if x>=1.075: return "A"
    if x>=1.035: return "A-"
    if x>=1.01: return "B+"
    if x>=0.99: return "B"
    if x>=0.965: return "B-"
    if x>=0.925: return "C+"
    return "C"

def _ui_action_label(p):
    tier=str(p.get("action_tier") or p.get("signal") or "").upper()
    pick=str(p.get("pick") or "PASS").upper()
    prob=_ui_pct(p.get("fair_prob"))
    if "PASS" in tier or pick=="PASS": return "PASS"
    if "OFFICIAL" in tier or (prob is not None and prob>=67): return "ELITE PLAY"
    if "STRONG" in tier or (prob is not None and prob>=62): return "STRONG PLAY"
    if "LEAN" in tier or (prob is not None and prob>=56): return "LEAN / TRACK"
    return "TRACK"

def _ui_missing(value):
    return value is None or (isinstance(value,str) and value.strip().lower() in {"","none","nan","n/a","missing"})

def _ui_opp_fields(p):
    role=p.get("role") or {}
    exp=p.get("expected_opportunity") or {}
    prop=str(p.get("prop") or "")
    pos=str(p.get("position") or "").upper()
    def item(label,*vals):
        for v in vals:
            if not _ui_missing(v):
                return (label,_fmt_num(v,1) if safe_float(v) is not None else str(v))
        return (label,"—")
    if prop=="Receiving Yards":
        return [item("ROUTES",exp.get("routes_pg"),role.get("routes")),
                item("TARGETS",exp.get("targets_pg_est"),role.get("targets")),
                item("TGT SHARE",role.get("target"),p.get("target_share")),
                item("1ST READ",p.get("first_read_share"),p.get("current_first_read_share"),p.get("ngs_first_read_share"))]
    if prop=="Rushing Yards":
        if pos=="QB":
            model=p.get("rushing_yards_model") or {}
            return [item("DES RUN",model.get("designed_rushes"),p.get("qb_designed_runs_pg")),
                    item("SCRAMBLE",model.get("scrambles"),p.get("qb_scrambles_pg")),
                    item("KNEELS",model.get("kneels"),p.get("qb_kneels_pg")),
                    item("RUSH ATT",exp.get("carries_pg_est"),p.get("rush_attempts_pg"))]
        return [item("CARRIES",exp.get("carries_pg_est"),p.get("rush_attempts_pg")),
                item("CARRY SHR",role.get("carry"),p.get("carry_share")),
                item("RZ SHARE",role.get("rz"),p.get("red_zone_touch_share")),
                item("PLAYS",exp.get("plays_pg"),p.get("plays_pg"))]
    if prop=="Passing Yards":
        return [item("DROPBACKS",exp.get("dropbacks_pg"),p.get("dropbacks_pg")),
                item("ATTEMPTS",exp.get("pass_attempts_pg"),p.get("pass_attempts_pg")),
                item("OL PASS",p.get("ol_pass_pro_rank")),
                item("PRESSURE",p.get("opp_def_pressure_rate"),p.get("def_pressure_rate"))]
    return [item("SNAP %",role.get("snap")),item("ROLE %",role.get("route"),role.get("carry")),
            item("RZ %",role.get("rz")),item("PLAYS",exp.get("plays_pg"))]

def _ui_player_why(p):
    parts=[]
    for n in (p.get("notes") or []):
        t=str(n).strip()
        if t and t not in parts: parts.append(t)
        if len(parts)>=3: break
    if not parts:
        role=p.get("role") or {}
        if safe_float(role.get("route")) is not None: parts.append(f"Route participation {safe_float(role.get('route')):.0f}%")
        if safe_float(role.get("target")) is not None: parts.append(f"Target share {safe_float(role.get('target')):.0f}%")
        mf=safe_float(p.get("matchup_factor"))
        if mf is not None: parts.append(f"Matchup factor {mf:.3f}")
    return " • ".join(parts[:3]) if parts else "No additional model note available"

def _ml_tier(card):
    if card.get("blocked"): return "PASS"
    prob=100*(safe_float(card.get("favorite_prob"),0) or 0)
    ready=bool(card.get("official_moneyline_ready"))
    rel=str(card.get("reliability") or "").upper()
    if ready and rel=="HIGH" and prob>=64: return "ELITE ML"
    if ready and rel in {"HIGH","MED"} and prob>=59: return "STRONG ML"
    if prob>=54 and rel!="LOW": return "LEAN ML"
    return "TRACK ONLY"

def _ml_advantage_cell(card,key,label):
    af=card.get("away_family_scores") or {}; hf=card.get("home_family_scores") or {}
    a=safe_float(af.get(key)); h=safe_float(hf.get(key))
    away=html.escape(str(card.get("away") or "")); home=html.escape(str(card.get("home") or ""))
    if a is None or h is None:
        return f"<div class='ml-battle-cell'><div class='ml-battle-name'>{label}</div><div class='ml-battle-val ml-even'>—</div></div>"
    d=h-a
    if abs(d)<0.08: txt="EVEN"; cls="ml-even"
    elif d>0: txt=f"{home} ADV"; cls="ml-adv-home"
    else: txt=f"{away} ADV"; cls="ml-adv-away"
    return f"<div class='ml-battle-cell'><div class='ml-battle-name'>{label}</div><div class='ml-battle-val {cls}'>{txt}</div></div>"

def _render_moneyline_cards(cards):
    if not cards:
        st.warning("No validated NFL matchups are available for Money Line cards.")
        return
    markup=["<div class='ml-elite-board'>"]
    for card in cards:
        away=html.escape(str(card.get("away") or ""))
        home=html.escape(str(card.get("home") or ""))
        phase=str(card.get("phase") or "REGULAR").upper()
        start=html.escape(_moneyline_time_label(card.get("scheduled_at")))
        matchup=html.escape(str(card.get("matchup") or f"{away} @ {home}"))
        status=html.escape(str(card.get("status") or "MODEL"))
        price_status=html.escape(str(card.get("price_status") or "MODEL ONLY"))
        if card.get("blocked"):
            reasons=" • ".join(html.escape(str(x)) for x in card.get("blocks",[]) if x)
            apc,asc=nfl_team_theme(away); hpc,hsc=nfl_team_theme(home)
            markup.append(f"<section class='ml-elite-card' style='--away-primary:{apc};--away-secondary:{asc};--home-primary:{hpc};--home-secondary:{hsc};--winner-primary:#ffcb45'><div class='ml-topline'><span>{start} · {matchup}</span><span>PASS · DATA BLOCKED</span></div><div style='padding:14px'><b>NO OFFICIAL MODEL</b><div class='nfl-subline'>{reasons or 'Required team inputs are incomplete.'}</div></div></section>")
            continue

        fav=html.escape(str(card.get("favorite") or ""))
        football=100*(safe_float(card.get("favorite_prob"),0) or 0)
        calibrated=100*(safe_float(card.get("calibrated_favorite_prob"),safe_float(card.get("favorite_prob"),0)) or 0)
        away_prob=100*(safe_float(card.get("away_win_prob"),0) or 0); home_prob=100*(safe_float(card.get("home_win_prob"),0) or 0)
        ap=_fmt_num(card.get("away_projection"),1); hp=_fmt_num(card.get("home_projection"),1)
        margin=(safe_float(card.get("home_projection")) or 0)-(safe_float(card.get("away_projection")) or 0)
        margin_team=home if margin>=0 else away
        margin_txt=f"{margin_team} -{abs(margin):.1f}"
        away_logo=html.escape(nfl_team_logo_url(away),quote=True); home_logo=html.escape(nfl_team_logo_url(home),quote=True)
        al=f"<img src='{away_logo}' alt='{away} logo' loading='lazy' onerror=\"this.style.display='none'\">" if away_logo else ""
        hl=f"<img src='{home_logo}' alt='{home} logo' loading='lazy' onerror=\"this.style.display='none'\">" if home_logo else ""
        away_primary,away_secondary=nfl_team_theme(away); home_primary,home_secondary=nfl_team_theme(home)
        fav_primary=(home_primary if fav==home else away_primary)
        is_live_price=card.get("price_status")=="LIVE MARKET"
        line_label="MARKET LINE" if is_live_price else "MODEL LINE"
        am=_format_american_odds(card.get("away_market_odds") if is_live_price else card.get("away_model_odds"))
        hm=_format_american_odds(card.get("home_market_odds") if is_live_price else card.get("home_model_odds"))
        aip=card.get("away_market_prob"); hip=card.get("home_market_prob")
        market_edge=safe_float(card.get("calibrated_market_edge"),safe_float(card.get("market_model_edge")))
        market_edge_txt="—" if market_edge is None else f"{market_edge*100:+.1f}%"
        total=_fmt_num(card.get("model_total"),1); market_total=_fmt_num(card.get("market_total"),1)
        drives=safe_float(card.get("projected_drives")); apd=(safe_float(card.get("away_projection"))/drives if drives else None); hpd=(safe_float(card.get("home_projection"))/drives if drives else None)
        data=int(safe_float(card.get("data_score"),0) or 0); tier=_ml_tier(card)
        ready=card.get("regular_season_readiness") or {}
        readiness=int(safe_float(ready.get("score"),data) or data) if phase=="REGULAR" else data
        phase_note=(f"<span class='ml-pre-note'>PRESEASON · ROTATION-AWARE</span>" if phase=="PRESEASON" else f"<span class='ml-reg-note'>REGULAR · READINESS {readiness}%</span>")
        rot_a=card.get("away_preseason_rotation") or {}; rot_h=card.get("home_preseason_rotation") or {}
        risk_bits=[]
        if phase=="PRESEASON":
            risk_bits=[f"{away} ROT {100*(safe_float(rot_a.get('confidence'),0) or 0):.0f}%",f"{home} ROT {100*(safe_float(rot_h.get('confidence'),0) or 0):.0f}%"]
        else:
            ai=card.get("away_injuries") or {}; hi=card.get("home_injuries") or {}
            risk_bits=[f"INACT {'✓' if ai.get('final_inactives_confirmed') and hi.get('final_inactives_confirmed') else 'PENDING'}",f"WEATHER {str(card.get('weather_risk') or 'LOW')}",f"RELIABILITY {str(card.get('reliability') or '—')}"]
        why=" • ".join(f"{str(k).replace('_',' ').title()} {v:+.3f}" for k,v in (card.get("top_factors") or [])[:4]) or str(card.get("model_note") or "")
        battle="".join([
            _ml_advantage_cell(card,"quarterback","QB"),
            _ml_advantage_cell(card,"offense","OFFENSE"),
            _ml_advantage_cell(card,"trench","OL / DL"),
            _ml_advantage_cell(card,"defense","DEFENSE"),
            _ml_advantage_cell(card,"possessions","POSSESSIONS"),
            _ml_advantage_cell(card,"situational","RED ZONE / DOWNS"),
            _ml_advantage_cell(card,"explosive","EXPLOSIVES"),
            _ml_advantage_cell(card,"special_teams","SPECIAL TEAMS"),
        ])
        gauge=max(0,min(100,football))
        env=card.get("game_environment") or {}
        wx=env.get("weather_detail") or {}
        wx_temp=_fmt_num(env.get("temperature",wx.get("temperature",wx.get("temp_f"))),0)
        wx_feels=_fmt_num(env.get("feels_like",wx.get("feels_like_f")),0)
        wx_hum=_fmt_num(env.get("humidity",wx.get("humidity_pct")),0)
        wx_wind=_fmt_num(env.get("wind",wx.get("wind_mph",wx.get("wind"))),0)
        wx_gust=_fmt_num(env.get("gust",wx.get("gust_mph",wx.get("gust"))),0)
        wx_precip=_fmt_num(env.get("precip",wx.get("precipitation_pct",wx.get("precip_pct"))),0)
        wx_roof=html.escape(str(env.get("roof") or wx.get("roof") or (env.get("stadium") or {}).get("roof") or "—"))
        wx_cond=html.escape(str(env.get("condition") or wx.get("condition") or card.get("weather_risk") or "—"))
        wx_source=html.escape(str(env.get("weather_source") or wx.get("source") or "—"))
        wx_stadium=html.escape(str(wx.get("stadium") or (env.get("stadium") or {}).get("stadium") or "—"))
        weather_strip="".join([
            f"<div class='ml-weather-cell'><div class='ml-label'>WEATHER</div><div class='ml-val'>{wx_cond}</div><div class='ml-weather-source'>{wx_source}</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>TEMP</div><div class='ml-val'>{wx_temp}°F</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>FEELS</div><div class='ml-val'>{wx_feels}°F</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>WIND / GUST</div><div class='ml-val'>{wx_wind} / {wx_gust} mph</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>PRECIP</div><div class='ml-val'>{wx_precip}%</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>HUMIDITY</div><div class='ml-val'>{wx_hum}%</div></div>",
            f"<div class='ml-weather-cell'><div class='ml-label'>ROOF / STADIUM</div><div class='ml-val'>{wx_roof}</div><div class='ml-weather-source'>{wx_stadium}</div></div>",
        ])
        markup.append(textwrap.dedent(f"""
        <section class='ml-elite-card' style='--away-primary:{away_primary};--away-secondary:{away_secondary};--home-primary:{home_primary};--home-secondary:{home_secondary};--winner-primary:{fav_primary}'>
          <div class='ml-topline'><span>{start} · {matchup}</span><span>{phase_note} · {status} · {price_status}</span></div>
          <div class='ml-main-grid'>
            <div class='ml-team-panel'>{al}<div><div class='ml-team-code'>{away}</div><div class='ml-team-score'>{ap} PTS · {away_prob:.0f}% FOOTBALL</div></div></div>
            <div class='ml-center-panel'>
              <div class='ml-winner-tag'>★ MODEL WINNER ★</div><div class='ml-winner-big'>{fav} {football:.0f}%</div><div class='ml-cal'>CALIBRATED {calibrated:.0f}%</div>
              <div class='ml-scoreline'><span><b>{away} {ap}</b></span><span>PROJECTED<br><small>{margin_txt}</small></span><span><b>{home} {hp}</b></span></div>
            </div>
            <div class='ml-market-panel'><div class='ml-label'>{line_label}</div><div class='ml-market-grid'>
              <div class='ml-mini'><div class='ml-label'>{away}</div><div class='ml-val'>{am}</div></div>
              <div class='ml-mini'><div class='ml-label'>{home}</div><div class='ml-val'>{hm}</div></div>
              <div class='ml-mini'><div class='ml-label'>NO-VIG</div><div class='ml-val'>{'—' if aip is None else f'{100*aip:.0f}%'} / {'—' if hip is None else f'{100*hip:.0f}%'}</div></div>
              <div class='ml-mini'><div class='ml-label'>MODEL EDGE</div><div class='ml-val nfl-good'>{market_edge_txt}</div></div>
            </div></div>
            <div class='ml-gauge-panel'><div class='ml-label'>WIN PROBABILITY</div><div class='ml-gauge' style='--p:{gauge:.0f}'></div><div class='ml-gauge-num'>{football:.0f}%</div><div class='ml-cal'>{calibrated:.0f}% CAL</div><div class='ml-tier-pill'>{tier}</div></div>
            <div class='ml-team-panel right'>{hl}<div><div class='ml-team-code'>{home}</div><div class='ml-team-score'>{hp} PTS · {home_prob:.0f}% FOOTBALL</div></div></div>
          </div>
          <div class='ml-battle'>{battle}</div>
          <div class='ml-detail-grid'>
            <div class='ml-mini'><div class='ml-label'>DRIVES / TEAM</div><div class='ml-val'>{_fmt_num(drives,1)}</div></div>
            <div class='ml-mini'><div class='ml-label'>PTS / DRIVE</div><div class='ml-val'>{away} {_fmt_num(apd,2)} · {home} {_fmt_num(hpd,2)}</div></div>
            <div class='ml-mini'><div class='ml-label'>MODEL TOTAL</div><div class='ml-val'>{total}</div></div>
            <div class='ml-mini'><div class='ml-label'>MARKET TOTAL</div><div class='ml-val'>{market_total}</div></div>
            <div class='ml-mini'><div class='ml-label'>EXPECTED TO</div><div class='ml-val'>{away} {_fmt_num(card.get('expected_turnovers_away'),2)} · {home} {_fmt_num(card.get('expected_turnovers_home'),2)}</div></div>
            <div class='ml-mini'><div class='ml-label'>DATA / STATUS</div><div class='ml-val'>{data}% · {'OFFICIAL' if card.get('official_moneyline_ready') else 'TRACK'}</div></div>
          </div>
          <div class='ml-weather-strip'>{weather_strip}</div>
          <div class='nfl-risk-strip' style='padding:0 10px 6px'>{''.join(f"<span class='nfl-risk-chip'>{html.escape(x)}</span>" for x in risk_bits)}</div>
          <div class='ml-why'><b>WHY {fav}:</b> {html.escape(why)}</div>
        </section>
        """))
    markup.append("</div>")
    st.markdown("".join(markup),unsafe_allow_html=True)

def _render_player_cards(rows, limit=None, header=None):
    if header:
        st.markdown(f"<div class='section-title-pro'>{html.escape(str(header))}</div>", unsafe_allow_html=True)
    if not rows:
        st.warning("No player cards available in this view.")
        return
    page_size=int(limit or st.session_state.get("nfl_card_page_size",12) or 12)
    page_size=max(4,min(page_size,50))
    safe_key=re.sub(r"[^a-z0-9]+","_",str(header or (rows[0].get("prop") if rows else "cards")).lower()).strip("_") or "cards"
    pages=max(1,math.ceil(len(rows)/page_size))
    page=st.number_input("Card page",min_value=1,max_value=pages,value=min(int(st.session_state.get(f"{safe_key}_card_page",1) or 1),pages),step=1,key=f"{safe_key}_card_page")
    lo=(int(page)-1)*page_size; shown=rows[lo:min(len(rows),lo+page_size)]
    st.caption(f"Showing cards {lo+1}-{lo+len(shown)} of {len(rows)}. PRESEASON uses rotation/workload uncertainty; REGULAR uses current-role/readiness context.")
    cards=["<div class='nfl-elite-board'>"]
    for idx,p in enumerate(shown,start=lo+1):
        player=html.escape(str(p.get("player") or ""))
        team=html.escape(str(p.get("team") or ""))
        pos=html.escape(str(p.get("position") or ""))
        opp=html.escape(str(p.get("opp") or ""))
        matchup=html.escape(str(p.get("matchup") or ""))
        phase=season_mode_for_row(p)
        logo=html.escape(nfl_team_logo_url(team),quote=True)
        logo_html=f"<img class='nfl-logo' src='{logo}' alt='{team} logo' loading='lazy' decoding='async' onerror=\"this.style.display='none'\">" if logo else "<div></div>"
        primary,secondary=nfl_team_theme(team)
        rank_cls="top1" if idx==1 else "top2" if idx==2 else "top3" if idx==3 else ""
        prop=html.escape(str(ACTIVE_NFL_MARKET_LABELS.get(p.get("prop"),p.get("prop") or "")))
        pick=str(p.get("pick") or "PASS").upper()
        line=_fmt_num(p.get("line"),1); proj=_fmt_num(p.get("projection"),1)
        edge=safe_float(p.get("edge")); edge_txt="—" if edge is None else f"{edge:+.1f}"
        fair=_ui_pct(p.get("fair_prob")); fair_txt="—" if fair is None else f"{fair:.0f}%"
        data=_ui_pct(p.get("data_score")); data=0 if data is None else data
        stability=_ui_pct(p.get("stability_score")); stability=0 if stability is None else stability
        oppscore=_ui_pct(p.get("opportunity_score")); oppscore=0 if oppscore is None else oppscore
        matchup_factor=safe_float(p.get("matchup_factor")); grade=_ui_grade_from_factor(matchup_factor)
        grade_cls="nfl-good" if grade.startswith("A") else "nfl-warn" if grade.startswith("B") else "nfl-bad"
        action=_ui_action_label(p)
        action_cls="nfl-good" if "ELITE" in action or "STRONG" in action else "nfl-warn" if "LEAN" in action or "TRACK" in action else "nfl-bad"
        env=p.get("env") or {}; exp=p.get("expected_opportunity") or {}
        total=_fmt_num(p.get("game_total",env.get("game_total")),1)
        spread=_fmt_num(p.get("spread",env.get("spread")),1)
        plays=_fmt_num(exp.get("plays_pg",p.get("plays_pg")),1)
        weather=html.escape(str(p.get("weather_risk") or env.get("weather_risk") or env.get("weather") or "—"))
        wx_temp=_fmt_num(p.get("weather_temperature",p.get("weather_temp_f",env.get("temperature"))),0)
        wx_feels=_fmt_num(p.get("weather_feels_like_f",env.get("feels_like")),0)
        wx_hum=_fmt_num(p.get("weather_humidity_pct",env.get("humidity")),0)
        wx_wind=_fmt_num(p.get("weather_wind_mph",env.get("wind")),0)
        wx_gust=_fmt_num(p.get("weather_gust_mph",env.get("gust")),0)
        wx_precip=_fmt_num(p.get("weather_precipitation_pct",p.get("weather_precip_pct",env.get("precip"))),0)
        wx_roof=html.escape(str(p.get("weather_roof") or env.get("roof") or "—"))
        wx_cond=html.escape(str(p.get("weather_condition") or env.get("condition") or weather))
        wx_source=html.escape(str(p.get("weather_source") or env.get("weather_source") or "—"))
        weather_grid=(f"<div class='nfl-weather-grid'><span>COND<b>{wx_cond}</b></span><span>TEMP<b>{wx_temp}°F</b></span><span>FEELS<b>{wx_feels}°F</b></span><span>WIND<b>{wx_wind} mph</b></span><span>GUST<b>{wx_gust} mph</b></span><span>PRECIP<b>{wx_precip}%</b></span><span>HUMID<b>{wx_hum}%</b></span><span>ROOF<b>{wx_roof}</b></span></div>")
        opp_fields=_ui_opp_fields(p)
        opp_html="".join(f"<div class='nfl-opp-box'><div class='nfl-label'>{html.escape(str(lbl))}</div><div class='nfl-val'>{html.escape(str(val))}</div></div>" for lbl,val in opp_fields)
        p25=_fmt_num(p.get("p25"),1); p50=_fmt_num(p.get("p50"),1); p75=_fmt_num(p.get("p75"),1)
        role=p.get("role") or {}
        phase_extra=""
        chips=[]
        if phase=="PRESEASON":
            wsrc=html.escape(str(p.get("preseason_workload_source") or "ROTATION MODEL"))
            rc=p.get("preseason_role_confidence",p.get("preseason_rotation_confidence"))
            rc_txt="—" if safe_float(rc) is None else f"{_ui_pct(rc):.0f}%"
            phase_extra=f"<div class='nfl-context-lines'><b>PRESEASON</b><br>Workload: {wsrc}<br>Rotation confidence: {rc_txt}</div>"
            chips.extend(["PRESEASON","ROTATION-AWARE",f"DATA {data:.0f}%"])
        else:
            curr="CURRENT ✓" if p.get("has_current_usage") or p.get("current_context_source") else "CURRENT PRIOR / FALLBACK"
            phase_extra=f"<div class='nfl-context-lines'><b>REGULAR</b><br>{html.escape(curr)}<br>Role bucket: {html.escape(str(p.get('role_bucket') or '—'))}</div>"
            chips.extend(["REGULAR",curr,f"DATA {data:.0f}%"])
        if p.get("injury_risk"): chips.append(f"INJ {str(p.get('injury_risk')).upper()}")
        if p.get("game_script_risk"): chips.append(f"SCRIPT {str(p.get('game_script_risk')).upper()}")
        if weather!="—": chips.append(f"WX {weather}")
        why=html.escape(_ui_player_why(p))
        cards.append(textwrap.dedent(f"""
        <section class='nfl-elite-card' style='--team-primary:{primary};--team-secondary:{secondary}'>
          <div class='nfl-card-grid'>
            <div>
              <div class='nfl-ident'><div class='nfl-rank {rank_cls}'>#{idx}</div>{logo_html}<div><div class='nfl-player-name'>{player}</div><div class='nfl-subline'>{team} {('VS '+opp) if opp else ''} · {pos} · {matchup}</div></div></div>
              <div class='nfl-readiness'><div class='nfl-mini-box'><div class='nfl-label'>DATA READINESS</div><div class='nfl-val'>{data:.0f}%</div><div class='nfl-meter'><i style='width:{data:.0f}%'></i></div></div>
              <div class='nfl-mini-box'><div class='nfl-label'>ROLE CERTAINTY</div><div class='nfl-val'>{stability:.0f}%</div><div class='nfl-meter'><i style='width:{stability:.0f}%'></i></div></div></div>
            </div>
            <div class='nfl-market-panel'>
              <div class='nfl-market-main'><div class='nfl-market-name'>{prop}</div><div class='nfl-line'>{pick} {line}</div><div class='nfl-dist'><div><div class='nfl-label'>P25</div><b>{p25}</b></div><div><div class='nfl-label'>PROJ</div><b>{proj}</b></div><div><div class='nfl-label'>P75</div><b>{p75}</b></div></div></div>
              <div class='nfl-stat-box'><div class='nfl-label'>EDGE</div><div class='nfl-val {'nfl-good' if (edge or 0)>0 else 'nfl-bad' if (edge or 0)<0 else ''}'>{edge_txt}</div></div>
              <div class='nfl-stat-box'><div class='nfl-label'>OPP SCORE</div><div class='nfl-val'>{oppscore:.0f}</div></div>
              <div class='nfl-stat-box'><div class='nfl-label'>VOLATILITY</div><div class='nfl-val'>{html.escape(str(p.get('volatility') or '—'))}</div></div>
            </div>
            <div>
              <div class='nfl-opportunity'><div class='nfl-opp-title'>OPPORTUNITY — PROJECTED / REAL INPUTS</div>{opp_html}</div>
              <div class='nfl-context-row'><div class='nfl-risk-box'><div class='nfl-label'>MATCHUP GRADE</div><div class='nfl-match-grade {grade_cls}'>{grade}</div><div class='nfl-context-lines'>Factor {_fmt_num(matchup_factor,3)}<br>Defense risk {html.escape(str(p.get('defense_risk') or '—'))}</div></div>
              <div class='nfl-risk-box'><div class='nfl-label'>GAME ENVIRONMENT</div><div class='nfl-context-lines'>Total {total} · Spread {spread}<br>Projected plays {plays}<br>Weather risk {weather}</div>{weather_grid}{phase_extra}</div></div>
            </div>
            <div class='nfl-likely'><div><div class='nfl-label'>LIKELY</div><div class='pct'>{fair_txt}</div><div class='nfl-subline'>MODEL FAIR PROB</div></div><div class='nfl-action {action_cls}'>{action}</div></div>
          </div>
          <div class='nfl-risk-strip'>{''.join(f"<span class='nfl-risk-chip'>{html.escape(str(x))}</span>" for x in chips)}</div>
          <div class='nfl-why'><b>WHY:</b> {why}</div>
        </section>
        """))
    cards.append("</div>")
    st.markdown("".join(cards),unsafe_allow_html=True)

    for p in shown:
        with st.expander(f"Full Audit — {p.get('player','')} {p.get('prop','')}"):
            c1,c2,c3=st.columns(3)
            with c1:
                st.subheader("Usage / Opportunity")
                st.json({"role":p.get("role") or {},"expected_opportunity":p.get("expected_opportunity") or {},"current_week_role":p.get("current_week_role") or {}},expanded=False)
            with c2:
                st.subheader("Environment / Matchup")
                st.json({"env":p.get("env") or {},"matchup_factor":p.get("matchup_factor"),"offense_defense_rank_context":p.get("offense_defense_rank_context") or {},"advanced_context":p.get("advanced_context") or {}},expanded=False)
            with c3:
                st.subheader("Audit / Risk")
                st.write(f"Data Score: **{p.get('data_score','—')}** · Stability: **{p.get('stability_score','—')}** · Action: **{p.get('action_tier','—')}**")
                st.write(f"Season Mode: **{season_mode_for_row(p)}** · Calibration: **{p.get('calibration_status','—')}**")
                for n in p.get("notes",[]): st.write("- "+str(n))
            st.subheader("Distribution / Models")
            st.json({"p10":p.get("p10"),"p50":p.get("p50"),"p75":p.get("p75"),"p90":p.get("p90"),"passing_yards_model":p.get("passing_yards_model"),"receiving_yards_model":p.get("receiving_yards_model"),"rushing_yards_model":p.get("rushing_yards_model"),"projection_breakdown":p.get("projection_breakdown")},expanded=False)
            st.subheader("Alt Ladder")
            st.dataframe(alt_ladder(p),use_container_width=True,hide_index=True)

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
    "injury_overrides.csv": "player,team,status,practice_status,body_part,expected_snap_share,limited_snap_risk,injury_note,updated_at\n",
    "final_inactives.csv": "team,matchup,player,status,confirmed,note,updated_at\n",
    "role_overrides.csv": "player,team,position,snap_share,route_participation,target_share,carries_share,red_zone_touch_share,red_zone_carries,red_zone_targets,goal_line_touches,source,updated_at\n",
    "defensive_injury_overrides.csv": "team,player,position,depth_rank,status,practice_status,unit,impact_weight,note,updated_at\n",

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
    try:
        boot = BUNDLED_REAL_DATA_BOOTSTRAP
        st.caption(f"Verified bundled baseline: {boot.get('valid', 0)}/11 real data files available in active storage · {boot.get('installed', 0)} installed this startup.")
    except Exception:
        pass
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
        ("Verified injury overrides", INJURY_OVERRIDE_CSV, ["csv"]),
        ("Verified final inactives CSV", FINAL_INACTIVES_CSV, ["csv"]),
        ("Verified role overrides", ROLE_OVERRIDE_CSV, ["csv"]),
        ("Verified defensive injury overrides", DEF_INJURY_OVERRIDE_CSV, ["csv"]),
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
    st.caption("One click downloads/builds the completed-season projection baseline plus current roster, depth chart, travel, matchup, QB, injury-hook, market-hook, and all app-ready CSV/JSON files. Saved files load automatically.")
    season_to_build = st.number_input("Last season to build", min_value=1999, max_value=2030, value=NFL_LAST_SEASON, step=1, key="phase6_admin_season")
    existing_ready = _phase6_existing_database_ready()
    st.metric("Saved database", "READY" if existing_ready else "NOT BUILT")
    if PHASE6_MANIFEST_FILE.exists() and st.checkbox("Show last saved Phase 6 build", value=False, key="show_phase6_last_saved_build"):
        st.json(load_json(PHASE6_MANIFEST_FILE, {}))
    if st.button("🛠️ Build / Repair Projection Database", use_container_width=True, key="phase6_use_saved_sidebar"):
        diag = build_phase6_nfl_database(int(season_to_build), force_refresh=False)
        current_diag = build_current_season_data_files(NFL_CURRENT_SEASON, force_refresh=False)
        diag["current_data"] = current_diag
        try:
            diag["export_zip"] = str(_phase6_export_database_zip())
        except Exception as e:
            diag["export_error"] = str(e)
        if diag.get("status") in ["BUILT_AND_SAVED", "USING_SAVED_DATABASE", "PULL_FAILED_USING_SAVED_DATABASE", "USING_SAVED_LOCAL_DATABASE", "USING_GITHUB_HARD_INPUT_DATABASE", "BUILT_AND_SAVED_PHASE6_V3"]:
            st.success(f"Phase 6 ready: {diag.get('status')}.")
        else:
            st.warning(f"Phase 6 database not built: {diag.get('status')}")
        st.json(diag)
    if st.button("🌐 Force Refresh Projection Data", use_container_width=True, key="phase6_force_sidebar"):
        diag = build_phase6_nfl_database(int(season_to_build), force_refresh=True)
        current_diag = build_current_season_data_files(NFL_CURRENT_SEASON, force_refresh=True)
        diag["current_data"] = current_diag
        try:
            diag["export_zip"] = str(_phase6_export_database_zip())
        except Exception as e:
            diag["export_error"] = str(e)
        if diag.get("status") in ["BUILT_AND_SAVED", "PULL_FAILED_USING_SAVED_DATABASE", "BUILT_AND_SAVED_PHASE6_V3", "BUILT_PREVIOUS_SEASON_AND_SAVED_PHASE6_V3"]:
            st.success(f"Phase 6 refreshed/saved: {diag.get('status')}.")
        else:
            st.warning(f"Phase 6 database not built: {diag.get('status')}")
        st.json(diag)
    zip_path = PHASE6_DIR / "nfl_full_data_pack_2026.zip"
    if st.button("Export Saved Database ZIP", use_container_width=True, key="phase6_export_sidebar"):
        try:
            zip_path = _phase6_export_database_zip()
            st.success(f"Export created: {zip_path.name}")
        except Exception as e:
            st.error(f"Export failed: {e}")
    if zip_path.exists():
        try:
            st.download_button("⬇️ Download Complete NFL Data Pack ZIP", data=zip_path.read_bytes(), file_name="nfl_full_data_pack_2026.zip", mime="application/zip", use_container_width=True, key="phase6_download_zip_sidebar")
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

def _render_backtest_dashboard(season_mode="REGULAR"):
    rows=rows_for_season_mode(load_json(RESULT_LOG, []),season_mode)
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
    shadow_rows=[]
    for row in graded:
        actual=safe_float(row.get("actual")); legacy=safe_float(row.get("legacy_projection_pre_savant")); shadow=safe_float(row.get("savant_shadow_projection"))
        line=safe_float(row.get("line"))
        if actual is None or legacy is None or shadow is None:
            continue
        actual_side="PUSH" if line is not None and actual==line else "OVER" if line is not None and actual>line else "UNDER" if line is not None else ""
        shadow_rows.append({"prop":row.get("prop"),"actual":actual,"legacy_abs_error":abs(actual-legacy),"savant_abs_error":abs(actual-shadow),
                            "legacy_hit":None if actual_side in {"","PUSH"} else int(("OVER" if legacy>line else "UNDER")==actual_side),
                            "savant_hit":None if actual_side in {"","PUSH"} else int(("OVER" if shadow>line else "UNDER")==actual_side),
                            "snapshot_time":row.get("generated_at") or row.get("saved_at")})
    st.markdown("### Savant shadow validation")
    if shadow_rows:
        sdf=pd.DataFrame(shadow_rows)
        s1,s2,s3,s4=st.columns(4)
        s1.metric("Legacy MAE",f"{sdf['legacy_abs_error'].mean():.2f}")
        s2.metric("Savant MAE",f"{sdf['savant_abs_error'].mean():.2f}")
        s3.metric("Legacy Median AE",f"{sdf['legacy_abs_error'].median():.2f}")
        s4.metric("Savant Median AE",f"{sdf['savant_abs_error'].median():.2f}")
        summary=sdf.groupby("prop",dropna=False).agg(count=("actual","count"),legacy_mae=("legacy_abs_error","mean"),savant_mae=("savant_abs_error","mean"),legacy_hit=("legacy_hit","mean"),savant_hit=("savant_hit","mean")).reset_index()
        summary["mae_gain"]=(summary["legacy_mae"]-summary["savant_mae"]).round(3)
        summary["legacy_hit_pct"]=(summary["legacy_hit"]*100).round(1); summary["savant_hit_pct"]=(summary["savant_hit"]*100).round(1)
        st.dataframe(summary.drop(columns=["legacy_hit","savant_hit"]),use_container_width=True,hide_index=True)
        st.caption("Only Savant features saved with the pregame projection are scored. Full-season aggregate files are never retroactively joined to earlier weeks.")
    else:
        st.info("No prospectively saved Savant shadow projections have been graded yet. Production promotion remains disabled.")
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
            "ready":bool(r.get("action_tier") == "BET" and not hard),
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
    """Save current-season player + deep team context.

    Player weekly/snap data provide role and volume. Full current-season PBP, when
    available, supplies the five football data modules used by Moneyline:
    possessions, trenches, hidden yards/special teams, game state/coaching, and
    opponent-adjusted defense inputs. Missing PBP never invents values; the saved
    last-season baseline remains the prior until real current-season data exists.
    """
    season=int(season)
    weekly=fetch_nflverse_player_weekly_stats(season, force_refresh=force_refresh)
    if weekly.empty:
        request_log("AUTO_CURRENT_CONTEXT", "NO_DATA", f"season={season}")
        return {"status":"NO_DATA", "season":season, "players":0, "teams":0, "pbp_rows":0}
    logs=weekly.copy()
    if "player_display_name" in logs.columns and "player" not in logs.columns:
        logs["player"]=logs["player_display_name"]
    if "recent_team" in logs.columns and "team" not in logs.columns:
        logs["team"]=logs["recent_team"]
    if "position" not in logs.columns:
        logs["position"]=""
    for c in ["attempts","completions","passing_yards","targets","receptions","receiving_yards","air_yards","carries","rushing_yards"]:
        if c not in logs.columns: logs[c]=0
        logs[c]=pd.to_numeric(logs[c], errors="coerce").fillna(0)
    logs["week_num"]=pd.to_numeric(logs["week"], errors="coerce").fillna(0) if "week" in logs.columns else np.arange(len(logs))

    # Current offensive snap share.
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
                s["_week"]=pd.to_numeric(s["week"],errors="coerce").fillna(0) if "week" in s.columns else np.arange(len(s))
                for (pl,tm),sg in s.groupby([pcol,tcol],dropna=False):
                    sg=sg.sort_values("_week"); vals=sg["_pct"].dropna()
                    if len(vals):
                        snap_bank[(norm(pl),str(tm or ""))]={
                            "snap_share":round(float(vals.mean()),2),
                            "last3_snap_share":round(float(vals.tail(3).mean()),2),
                            "last5_snap_share":round(float(vals.tail(5).mean()),2),
                        }
    except Exception as e:
        request_log("AUTO_CURRENT_SNAPS","ERROR",str(e)[:180])

    # Offensive-line continuity: compare last year's five highest-snap OL with the
    # current roster. This is a real continuity signal, not a generic OL ranking.
    ol_continuity={}
    try:
        cur_roster=_normalize_roster_frame(fetch_nflverse_rosters(season,force_refresh=force_refresh))
        prev_snaps=fetch_nflverse_snap_counts(int(season)-1,force_refresh=False)
        if not cur_roster.empty and not prev_snaps.empty:
            ps=prev_snaps.copy()
            pcol=next((c for c in ["player","player_name","pfr_player_name"] if c in ps.columns),None)
            tcol=next((c for c in ["team","recent_team"] if c in ps.columns),None)
            poscol=next((c for c in ["position","pos"] if c in ps.columns),None)
            snapcol=next((c for c in ["offense_snaps","offense","offense_snap_count"] if c in ps.columns),None)
            pctcol=next((c for c in ["offense_pct","offense_snap_pct","offense_percentage"] if c in ps.columns),None)
            if pcol and tcol:
                if snapcol:
                    ps["_ol_volume"]=pd.to_numeric(ps[snapcol],errors="coerce").fillna(0)
                elif pctcol:
                    ps["_ol_volume"]=pd.to_numeric(ps[pctcol],errors="coerce").fillna(0)
                else:
                    ps["_ol_volume"]=1.0
                if poscol:
                    olmask=ps[poscol].astype(str).str.upper().isin(["C","G","OG","LG","RG","T","OT","LT","RT","OL"])
                    ps=ps[olmask]
                current_names_by_team={}
                for tm,rg in cur_roster.groupby("team") if "team" in cur_roster.columns else []:
                    if "position" in rg.columns:
                        rg=rg[rg["position"].astype(str).str.upper().isin(["C","G","OG","LG","RG","T","OT","LT","RT","OL"])]
                    current_names_by_team[str(tm)]={norm(x) for x in rg["player"].tolist() if norm(x)}
                for tm,sg in ps.groupby(tcol):
                    tm=_normalize_nfl_team(tm)
                    if not tm: continue
                    agg=sg.groupby(pcol)["_ol_volume"].sum().sort_values(ascending=False).head(5)
                    prior=[norm(x) for x in agg.index if norm(x)]
                    retained=sum(1 for x in prior if x in current_names_by_team.get(tm,set()))
                    ol_continuity[tm]={
                        "ol_returning_starters_pct":round(100*retained/max(1,len(prior)),1),
                        "ol_returning_core_count":retained,
                        "ol_prior_core_count":len(prior),
                    }
    except Exception as e:
        request_log("AUTO_OL_CONTINUITY","ERROR",str(e)[:180])

    # Current-season play-by-play provides real QB rush decomposition and the
    # team-level possession/trench/special-teams/game-state modules.
    pbp=pd.DataFrame(); pbp_team_context={}; pbp_player_rush={}; pbp_red_zone_bank={}
    try:
        pbp=fetch_nflverse_pbp(season, force_refresh=force_refresh)
        if not pbp.empty:
            pbp_team_context, _, defense_adv, current_red_zone, _, trench = _build_pbp_context(pbp)
            if isinstance(current_red_zone,pd.DataFrame) and not current_red_zone.empty:
                for _,rzr in current_red_zone.iterrows():
                    pkey=(norm(rzr.get("player")),_normalize_nfl_team(rzr.get("team")))
                    if pkey[0]: pbp_red_zone_bank[pkey]={k:rzr.get(k) for k in current_red_zone.columns}
            # Merge defense rows and trench rows into the same current-team bank.
            if isinstance(pbp_team_context,dict):
                for _,r in defense_adv.iterrows() if isinstance(defense_adv,pd.DataFrame) and not defense_adv.empty else []:
                    tm=_normalize_nfl_team(r.get("team"))
                    if tm: pbp_team_context.setdefault(tm,{}).update({k:v for k,v in r.to_dict().items() if k!="team" and _usable_context_value(v)})
                for _,r in trench.iterrows() if isinstance(trench,pd.DataFrame) and not trench.empty else []:
                    tm=_normalize_nfl_team(r.get("team"))
                    if tm: pbp_team_context.setdefault(tm,{}).update({k:v for k,v in r.to_dict().items() if k!="team" and _usable_context_value(v)})
            # QB designed runs / scrambles / kneels from real PBP.
            p=pbp.copy()
            for c in ["qb_scramble","qb_kneel","rush_attempt","pass_attempt"]:
                if c in p.columns: p[c]=pd.to_numeric(p[c],errors="coerce").fillna(0)
            rusher_col=next((c for c in ["rusher_player_name","rusher"] if c in p.columns),None)
            team_col="posteam" if "posteam" in p.columns else None
            if rusher_col and team_col:
                for (pl,tm),g in p.dropna(subset=[rusher_col,team_col]).groupby([rusher_col,team_col]):
                    games=max(1,g["game_id"].nunique() if "game_id" in g.columns else g["week"].nunique() if "week" in g.columns else 1)
                    kneels=float(g["qb_kneel"].sum()) if "qb_kneel" in g.columns else float(g.get("desc",pd.Series(dtype=str)).astype(str).str.contains("kneel",case=False,na=False).sum()) if "desc" in g.columns else 0.0
                    scr=float(g["qb_scramble"].sum()) if "qb_scramble" in g.columns else float(g.get("desc",pd.Series(dtype=str)).astype(str).str.contains("scrambl",case=False,na=False).sum()) if "desc" in g.columns else 0.0
                    rush=float(g["rush_attempt"].sum()) if "rush_attempt" in g.columns else len(g)
                    designed=max(0.0,rush-scr-kneels)
                    pbp_player_rush[(norm(pl),str(tm))]={
                        "qb_scrambles_pg":round(scr/games,3),
                        "qb_designed_runs_pg":round(designed/games,3),
                        "qb_kneels_pg":round(kneels/games,3),
                    }
    except Exception as e:
        request_log("AUTO_CURRENT_PBP","ERROR",str(e)[:180])

    player_rows=[]
    for (player, team, pos), g in logs.groupby(["player","team","position"], dropna=False):
        player=str(player or "").strip()
        if not player: continue
        g=g.sort_values("week_num"); gp=max(1,len(g)); tail3=g.tail(3); tail5=g.tail(5)
        targets=float(g["targets"].sum()); air=float(g["air_yards"].sum())
        team_slice=logs[logs["team"].astype(str)==str(team)]
        team_targets=float(team_slice["targets"].sum()); team_air=float(team_slice["air_yards"].sum()) if "air_yards" in team_slice.columns else 0.0; team_carries=float(team_slice["carries"].sum()) if "carries" in team_slice.columns else 0.0
        snap_ctx=snap_bank.get((norm(player),str(team or "")),{}); qb_rush=pbp_player_rush.get((norm(player),str(team or "")),{}); rz_ctx=pbp_red_zone_bank.get((norm(player),_normalize_nfl_team(team)),{})
        target_share=round(100*targets/max(1.0,team_targets),2) if team_targets else ""
        air_yards_share=round(100*air/max(1.0,team_air),2) if team_air else ""
        carries=float(g["carries"].sum()); carries_share=round(100*carries/max(1.0,team_carries),2) if team_carries else ""
        # A transparent route proxy is retained separately. We do NOT label snap share
        # as actual route participation.
        snap=safe_float(snap_ctx.get("snap_share"))
        route_proxy=None
        if snap is not None and str(pos or "").upper() in {"WR","TE","RB"}:
            scale=0.97 if str(pos or "").upper()=="WR" else 0.84 if str(pos or "").upper()=="TE" else 0.63
            route_proxy=round(float(clamp(snap*scale,0,100)),2)
        row={
            "player":player,"team":str(team or ""),"position":str(pos or ""),
            "snap_share":snap_ctx.get("snap_share",""),"last3_snap_share":snap_ctx.get("last3_snap_share",""),
            "last5_snap_share":snap_ctx.get("last5_snap_share",""),"route_participation":"",
            "route_participation_proxy":route_proxy if route_proxy is not None else "",
            "target_share":target_share,"air_yards_share":air_yards_share,"carries_share":carries_share,
            "red_zone_touch_share":rz_ctx.get("red_zone_touch_share",""),"red_zone_carries":rz_ctx.get("red_zone_carries",""),"red_zone_targets":rz_ctx.get("red_zone_targets",""),"red_zone_pass_attempts":rz_ctx.get("red_zone_pass_attempts",""),"goal_line_touches":rz_ctx.get("goal_line_touches",""),"red_zone_data_quality":"CURRENT_PBP" if rz_ctx else "",
            "targets_pg":round(targets/gp,3),"receptions_pg":round(float(g["receptions"].sum())/gp,3),
            "pass_attempts_pg":round(float(g["attempts"].sum())/gp,3),"completions_pg":round(float(g["completions"].sum())/gp,3),
            "receiving_yards_pg":round(float(g["receiving_yards"].sum())/gp,3),"passing_yards_pg":round(float(g["passing_yards"].sum())/gp,3),
            "rush_attempts_pg":round(float(g["carries"].sum())/gp,3),"rushing_yards_pg":round(float(g["rushing_yards"].sum())/gp,3),
            "yards_per_carry":round(float(g["rushing_yards"].sum())/max(1.0,float(g["carries"].sum())),3),
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
            "updated_at":now_iso(),"source":f"nflverse_current_{season}",
        }
        row.update(qb_rush); player_rows.append(row)

    # Weekly stats are enough for pace. PBP enriches the same team rows with the
    # deeper modules instead of replacing them.
    team_context={}
    for team,g in logs.groupby("team",dropna=False):
        team=_normalize_nfl_team(team)
        if not team: continue
        weeks=max(1,g["week_num"].nunique()); pass_att=float(g["attempts"].sum()); rush_att=float(g["carries"].sum()); plays=pass_att+rush_att
        ctx={
            "current_plays_pg":round(plays/weeks,2),"current_pass_rate":round(100*pass_att/max(1.0,plays),2),
            "current_rush_rate":round(100*rush_att/max(1.0,plays),2),"current_games":int(weeks),
            "updated_at":now_iso(),"source":f"nflverse_player_weekly_{season}",
        }
        pctx=(pbp_team_context or {}).get(team,{}) if isinstance(pbp_team_context,dict) else {}
        if isinstance(pctx,dict) and pctx:
            ctx.update({k:v for k,v in pctx.items() if _usable_context_value(v)})
            ctx["source"]=f"nflverse_current_pbp_{season}"
            ctx["current_pbp_rows"]=int(len(pbp))
        if team in ol_continuity:
            ctx.update(ol_continuity[team])
        team_context[team]=ctx

    if player_rows: pd.DataFrame(player_rows).to_csv(CURRENT_USAGE_FILE,index=False)
    if team_context: save_json(CURRENT_TEAM_CONTEXT_FILE,team_context)
    request_log("AUTO_CURRENT_CONTEXT","SAVED",f"season={season} players={len(player_rows)} teams={len(team_context)} pbp={len(pbp)}")
    return {"status":"SAVED","season":season,"players":len(player_rows),"teams":len(team_context),"pbp_rows":int(len(pbp)),
            "deep_modules":bool(len(pbp)),"files":[CURRENT_USAGE_FILE.name,CURRENT_TEAM_CONTEXT_FILE.name]}

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


def run_game_day_refresh():
    """Explicit one-click preparation: baseline, current context, then live lines."""
    started=time.perf_counter()
    phase6=build_phase6_nfl_database(NFL_LAST_SEASON,force_refresh=False)
    current=build_current_season_data_files(NFL_CURRENT_SEASON,force_refresh=False)
    for cached_fn in [_current_season_context_bank,_online_passing_yards_context_bank,_online_receiving_yards_context_bank]:
        try:
            cached_fn.clear()
        except Exception:
            pass
    try:
        fetch_underdog_nfl_props.clear(); fetch_underdog_nfl_moneylines.clear(); safe_get_json.clear()
    except Exception:
        pass
    savant_refresh=[]
    for savant_season in [NFL_CURRENT_SEASON,NFL_LAST_SEASON]:
        try:
            savant_refresh.extend(refresh_nfl_savant_data(savant_season,SAVANT_DIR,force=False))
        except Exception as exc:
            savant_refresh.append({"board":"ALL","season":savant_season,"status":"LAST_GOOD_OR_MISSING","rows":0,"detail":str(exc)[:180]})
            request_log("NFL_SAVANT_REFRESH","ERROR",f"{savant_season}: {str(exc)[:180]}")
    cache_bust=int(time.time())
    props=fetch_underdog_nfl_props(cache_bust)
    moneylines=fetch_underdog_nfl_moneylines(cache_bust)
    if props:
        save_last_pulled_board(props,moneylines)
    readiness=projection_database_readiness()
    report={
        "ran_at":now_iso(),"seconds":round(time.perf_counter()-started,2),
        "phase6_status":phase6.get("status"),"current_status":current.get("status"),
        "live_rows":len(props),"moneyline_rows":len(moneylines),
        "database_readiness":readiness,"phase6":phase6,"current":current,
        "savant_readiness":savant_data_readiness(SAVANT_DIR,NFL_LAST_SEASON),
        "savant_refresh":savant_refresh,
    }
    save_json(LOCAL_DIR / "nfl_game_day_refresh.json",report)
    return report,props,moneylines

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

PRODUCTION_OVERRIDE_TARGETS = {
    "injury_overrides.csv": INJURY_OVERRIDE_CSV,
    "final_inactives.csv": FINAL_INACTIVES_CSV,
    "role_overrides.csv": ROLE_OVERRIDE_CSV,
    "defensive_injury_overrides.csv": DEF_INJURY_OVERRIDE_CSV,
}

def _route_production_override_uploads(uploaded_files):
    """Accept the v7.51 production-data ZIP anywhere in Admin and route exact files."""
    results=[]
    for item in uploaded_files or []:
        name=Path(getattr(item,"name","upload")).name
        raw=_raw_bytes(item)
        members=[]
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for member in z.infolist():
                        if member.is_dir(): continue
                        members.append((Path(member.filename).name,z.read(member)))
            except Exception as exc:
                results.append({"file":name,"status":"ERROR","detail":f"zip read failed: {str(exc)[:120]}"})
                continue
        else:
            members=[(name,raw)]
        for clean_name,data in members:
            target=PRODUCTION_OVERRIDE_TARGETS.get(clean_name)
            if target is None:
                continue
            try:
                pd.read_csv(io.BytesIO(data),nrows=5)
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
                results.append({"file":clean_name,"status":"SAVED","target":target.name,
                                "detail":"production override routed"})
            except Exception as exc:
                results.append({"file":clean_name,"status":"ERROR","target":target.name,
                                "detail":str(exc)[:120]})
    return results

def _contains_production_override_files(uploaded_files):
    wanted=set(PRODUCTION_OVERRIDE_TARGETS)
    for item in uploaded_files or []:
        name=Path(getattr(item,"name","upload")).name
        raw=_raw_bytes(item)
        if name in wanted: return True
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    names={Path(m.filename).name for m in z.infolist() if not m.is_dir()}
                if names & wanted: return True
            except Exception:
                pass
    return False

def _render_nfl_savant_admin():
    readiness=savant_data_readiness(SAVANT_DIR,NFL_LAST_SEASON)
    c1,c2,c3=st.columns(3)
    c1.metric("Status",readiness.get("status","MISSING"))
    c2.metric("Core boards",f"{readiness.get('board_count',0)}/{readiness.get('required_count',8)}")
    c3.metric("Rows",readiness.get("rows",0))
    st.caption("Core efficiency context: passing/receiving/rushing + NGS + pressure + penalties. Route-tree is tracked separately as optional because no dependable public season-board feed is required by the core projection engine.")
    _ngs_boot=globals().get("EMBEDDED_NGS_BOOTSTRAP",{}) or {}
    _ngs_state=str(_ngs_boot.get("status") or "UNKNOWN")
    _ngs_installed=", ".join(_ngs_boot.get("installed") or [])
    if _ngs_state in {"INSTALLED","ALREADY_COMPLETE"}:
        st.success("Verified 2025 NGS fallback is built into v7.53. No NGS upload is required." + (f" Installed: {_ngs_installed}." if _ngs_installed else ""))
    elif _ngs_state=="ERROR":
        st.error("Embedded NGS bootstrap error: "+str(_ngs_boot.get("error") or "unknown"))
    else:
        st.info("Verified 2025 NGS fallback is built into v7.53; manual uploads remain optional for overrides or replacement datasets.")
    uploads=st.file_uploader(
        "Optional Savant replacement CSV/ZIP or Production Override Pack",type=["zip","csv"],accept_multiple_files=True,
        key="nfl_savant_pack_upload",
    )
    if uploads and not _contains_production_override_files(uploads):
        _preflight=[]
        for _nm,_payload in _iter_payloads(uploads):
            if isinstance(_payload,Exception):
                _preflight.append({"file":_nm,"board":"ZIP_ERROR","season":"","rows":0,"valid":False,"detail":str(_payload)[:100]})
                continue
            try:
                _fr=read_savant_csv(_payload); _bd=detect_savant_board(_nm,_fr); _ss=_season_from_name(_nm,NFL_LAST_SEASON)
                _pp=_prepare_savant_frame(_fr,_bd,_ss) if _bd else _fr
                _ok,_why=_frame_is_valid(_pp,_bd) if _bd else (False,"unknown board")
                _preflight.append({"file":_nm,"board":_bd or "UNKNOWN","season":_ss,"rows":len(_pp),"columns":len(_pp.columns),"valid":bool(_ok),"detail":_why})
            except Exception as _exc:
                _preflight.append({"file":_nm,"board":"ERROR","season":"","rows":0,"columns":0,"valid":False,"detail":str(_exc)[:120]})
        if _preflight:
            st.caption("Upload preflight — verify these rows BEFORE importing:")
            st.dataframe(pd.DataFrame(_preflight),use_container_width=True,hide_index=True)
    if st.button("Import / Auto-Route Data Pack",use_container_width=True,key="import_nfl_savant_pack"):
        if not uploads:
            st.warning("Choose a ZIP or one or more CSV files first.")
        elif _contains_production_override_files(uploads):
            routed=_route_production_override_uploads(uploads)
            st.session_state["nfl_savant_import_results"]=routed
            clear_projection_result_cache()
            saved=sum(1 for r in routed if r.get("status")=="SAVED")
            if saved:
                st.success(f"Recognized the v7.51 Production Data Pack and routed {saved} override files to Projection Data. This ZIP is not a Savant board pack.")
            else:
                st.warning("Production override filenames were detected, but none were saved.")
        else:
            results=import_savant_payloads(uploads,SAVANT_DIR,NFL_LAST_SEASON)
            st.session_state["nfl_savant_import_results"]=results
            clear_projection_result_cache()
            valid=sum(1 for r in results if r.get("valid"))
            saved=sorted({str(r.get("detected_board")) for r in results if r.get("valid") and r.get("saved_path")})
            if saved:
                st.success(f"Processed {len(results)} Savant files · {valid} valid · SAVED: "+", ".join(saved)+".")
            else:
                st.error(f"Processed {len(results)} files but saved 0 boards. Do not rebuild the feature store; inspect the import table below.")
    import_results=st.session_state.get("nfl_savant_import_results") or []
    if import_results:
        st.dataframe(pd.DataFrame(import_results),use_container_width=True,hide_index=True)
    b1,b2=st.columns(2)
    with b1:
        if st.button("Build / Refresh Feature Store",use_container_width=True,key="build_savant_store"):
            clear_savant_runtime_cache(); clear_projection_result_cache()
            status=build_savant_feature_store(SAVANT_DIR,NFL_LAST_SEASON)
            st.success(f"Feature store: {status.get('status')} · {status.get('player_rows',0)} players · {status.get('team_rows',0)} teams")
    with b2:
        if st.button("Pull Stable Savant APIs",use_container_width=True,key="pull_savant_apis"):
            with st.spinner("Refreshing stable NFL Savant JSON boards..."):
                results=refresh_nfl_savant_data(NFL_LAST_SEASON,SAVANT_DIR,force=True)
            st.session_state["nfl_savant_refresh_results"]=results
            clear_projection_result_cache()
    refresh_results=st.session_state.get("nfl_savant_refresh_results") or []
    if refresh_results:
        st.dataframe(pd.DataFrame(refresh_results),use_container_width=True,hide_index=True)
    backup=build_savant_backup_zip(SAVANT_DIR)
    st.download_button("Download Current Savant Pack",data=backup,file_name=f"nfl_savant_pack_{NFL_LAST_SEASON}.zip",mime="application/zip",use_container_width=True,key="download_savant_pack")
    if readiness.get("missing"):
        st.caption("Core fallback still needed for: "+", ".join(readiness.get("missing") or []))
    if readiness.get("optional_present") or readiness.get("optional_missing"):
        st.caption("Optional boards present: "+(", ".join(readiness.get("optional_present") or []) or "none")+" · optional unavailable/not loaded: "+(", ".join(readiness.get("optional_missing") or []) or "none"))
    override_status=[]
    for fn,target in PRODUCTION_OVERRIDE_TARGETS.items():
        p=Path(target)
        rows=0
        if p.exists():
            try: rows=len(pd.read_csv(p))
            except Exception: rows=0
        override_status.append({"override_file":fn,"installed":p.exists(),"rows":rows})
    st.markdown("**Production override pack status**")
    st.dataframe(pd.DataFrame(override_status),use_container_width=True,hide_index=True)
    st.caption("0 rows is valid for a blank template. Only verified real rows should be added; blank templates do not raise readiness by themselves.")

def _render_preseason_rotation_panel():
    st.caption("Coach-plan inputs own preseason workload. Saved shares are reconciled within each position room.")
    team=st.selectbox("Team",sorted(NFL_TEAM_ABBRS),key="preseason_rotation_team")
    player=st.text_input("Player",key="preseason_rotation_player",placeholder="Player name")
    position=st.selectbox("Position",["QB","RB","FB","WR","TE"],key="preseason_rotation_position")
    status=st.selectbox("Status",["ACTIVE","LIMITED_WORKLOAD","EXTENDED_WORK","RESTING"],key="preseason_rotation_status")
    c1,c2=st.columns(2)
    with c1:
        snap=st.number_input("Expected snap %",0.0,100.0,0.0,1.0,key="preseason_rotation_snap")
        drives=st.number_input("Expected QB drives",0.0,12.0,0.0,0.5,key="preseason_rotation_drives")
        routes=st.number_input("Expected routes",0.0,70.0,0.0,1.0,key="preseason_rotation_routes")
    with c2:
        attempts=st.number_input("Expected pass attempts",0.0,55.0,0.0,1.0,key="preseason_rotation_attempts")
        carries=st.number_input("Expected carries",0.0,40.0,0.0,1.0,key="preseason_rotation_carries")
        targets=st.number_input("Expected targets",0.0,25.0,0.0,0.5,key="preseason_rotation_targets")
    confidence=st.slider("Rotation confidence",0.0,1.0,0.80,0.05,key="preseason_rotation_confidence")
    note=st.text_area("Coach / rotation note",key="preseason_rotation_note")
    if st.button("Save Preseason Rotation",use_container_width=True,key="save_preseason_rotation_v732"):
        if not player.strip():
            st.warning("Add a player name first.")
        else:
            data=load_preseason_rotations(); players=data.setdefault("players",{})
            payload={"team":team,"player":player.strip(),"position":position,"status":status,"confidence":confidence,"note":note,"updated_at":now_iso()}
            if status=="RESTING": payload["preseason_snap_share"]=0.0
            elif snap>0: payload["preseason_snap_share"]=round(snap/100.0,4)
            if drives>0: payload["preseason_expected_drives"]=drives
            if attempts>0: payload["preseason_expected_pass_attempts"]=attempts
            if routes>0: payload["preseason_expected_routes"]=routes
            if carries>0: payload["preseason_expected_carries"]=carries
            if targets>0: payload["preseason_expected_targets"]=targets
            players[f"{team}|{norm(player)}"]=payload
            data["updated_at"]=now_iso(); save_json(PRESEASON_ROTATION_FILE,data); clear_projection_result_cache()
            st.success(f"Saved rotation for {player.strip()}.")
    saved=load_preseason_rotations().get("players",{})
    if saved and st.checkbox("Show saved rotations",False,key="show_preseason_rotations_v732"):
        st.dataframe(pd.DataFrame(list(saved.values())),use_container_width=True,hide_index=True)



def regular_season_readiness_panel():
    """Visible regular-season data health panel; never mixes preseason grades."""
    db=projection_database_readiness()
    current_players=load_current_usage_bank()
    current_teams=load_current_team_context()
    depth=load_depth_chart_bank()
    injuries=load_injury_bank()
    finals=load_final_inactives_context()
    weather=load_weather_context()
    try:
        selected=int(NFL_CURRENT_SEASON)
        savant=_savant_banks(SAVANT_DIR,selected)
        savant_players=len(savant.get("players",{})); savant_teams=len(savant.get("teams",{}))
        if not savant_players and not savant_teams:
            selected=int(NFL_LAST_SEASON); savant=_savant_banks(SAVANT_DIR,selected)
            savant_players=len(savant.get("players",{})); savant_teams=len(savant.get("teams",{}))
    except Exception:
        selected=int(NFL_LAST_SEASON); savant_players=0; savant_teams=0

    deep_fields={
        "Possessions":["drives_pg","points_per_drive","yards_per_drive","plays_per_drive","third_down_conversion_rate","red_zone_trips_pg"],
        "Trenches":["pressure_allowed_rate","sack_given_pressure_rate","def_pressure_rate","def_sack_given_pressure_rate","ol_returning_starters_pct"],
        "Hidden yards":["return_yards_pg","punt_return_yards_pg","net_punt_yards","fg_over_expected_per_attempt","penalty_yards_pg"],
        "Game state":["neutral_epa_per_play","neutral_success_rate","neutral_pass_rate","def_neutral_epa_allowed"],
    }
    deep_scores={}
    if current_teams:
        for label,fields in deep_fields.items():
            total=max(1,len(current_teams)*len(fields))
            have=sum(1 for ctx in current_teams.values() if isinstance(ctx,dict) for f in fields if _usable_context_value(ctx.get(f)))
            deep_scores[label]=100*have/total
    else:
        deep_scores={k:0 for k in deep_fields}
    route_have=0
    if current_players:
        route_have=sum(1 for r in current_players.values() if isinstance(r,dict) and (
            _usable_context_value(r.get("route_participation")) or _usable_context_value(r.get("route_participation_proxy"))
        ))
    skill_score=min(100,100*route_have/max(1,len(current_players))) if current_players else 0

    checks=[
        ("Historical core",100 if db.get("ready") else 55),
        (("Current player usage" if any((safe_float(r.get("current_games"),0) or 0)>=1 for r in current_players.values() if isinstance(r,dict)) else "Current player usage (pre-Week-1 expected 0)"),
         (min(100,100*sum(1 for r in current_players.values() if isinstance(r,dict) and (safe_float(r.get("current_games"),0) or 0)>=1 and (safe_float(r.get("source_season"),NFL_CURRENT_SEASON) or NFL_CURRENT_SEASON)==NFL_CURRENT_SEASON)/350) if current_players else 0)),
        ("Current team context",min(100,100*len(current_teams)/32) if current_teams else 0),
        ("Possession / drive data",deep_scores.get("Possessions",0)),
        ("OL/DL + pressure data",deep_scores.get("Trenches",0)),
        ("Hidden yards / special teams",deep_scores.get("Hidden yards",0)),
        ("Neutral game-state data",deep_scores.get("Game state",0)),
        ("Skill-role / route coverage",skill_score),
        ("Depth charts",min(100,100*len(depth)/500) if depth else 0),
        ("Injuries",min(100,100*len(injuries)/120) if injuries else 0),
        ("Final inactives",100 if isinstance(finals,dict) and (finals.get("teams") or finals.get("confirmed_matchups")) else 35),
        (f"NGS / Savant ({selected})" + (" PRIOR" if int(selected)!=int(NFL_CURRENT_SEASON) else " CURRENT"),min(100,55+min(45,savant_teams*1.4)) if savant_players or savant_teams else 0),
        ("Weather",100 if weather else 55),
    ]
    # Core/deep modules matter more than optional live hooks in the global score.
    score=int(round(np.mean([v for _,v in checks]))) if checks else 0
    deep_floor=min(deep_scores.values()) if deep_scores else 0
    if deep_floor<35: score=min(score,79)
    return {"score":score,"checks":checks,"deep_scores":deep_scores,"label":"STRONG" if score>=82 else "USABLE" if score>=70 else "BUILDING"}

st.markdown(f"""
<div class='hero-panel'>
  <div class='big-title'>NFL Prop Engine</div>
  <div class='sub-title'>Team-logo IQ cards · opportunity-first props · deep possession Moneyline · first downs · trenches · turnovers/fumbles · penalties · special teams · stadium/noise/weather · CLV · save/grade</div>
  <span class='badge'>{APP_VERSION}</span><span class='badge good-badge'>MLB framework converted to NFL structure</span>
</div>
""", unsafe_allow_html=True)

season_options=["PRESEASON","REGULAR"]
if st.session_state.get("nfl_season_mode") not in season_options:
    st.session_state["nfl_season_mode"]="PRESEASON" if datetime.now().month in {7,8} else "REGULAR"
active_season_mode=st.radio(
    "Season Mode",season_options,horizontal=True,key="nfl_season_mode",
    format_func=lambda value:"Preseason" if value=="PRESEASON" else "Regular Season",
)
if active_season_mode=="PRESEASON":
    st.info("Preseason mode is rotation-driven. Savant and 2025 history are efficiency priors only; unknown workload is PASS-gated.")
else:
    st.caption("Regular-season mode uses the full current NFL workload engine with regular-season-only learning.")
    reg_health=regular_season_readiness_panel()
    st.markdown(f"**Regular Season Data Readiness: {reg_health['score']}/100 · {reg_health['label']}**")
    with st.expander("Regular-season readiness breakdown",expanded=False):
        st.dataframe(pd.DataFrame([{"Layer":name,"Readiness":round(score,1)} for name,score in reg_health["checks"]]),use_container_width=True,hide_index=True)
        st.caption("This is a data-health audit only. Preseason results and calibration are excluded from regular-season learning.")

with st.sidebar:
    st.header("NFL Controls")
    source_mode="Live Underdog only"
    st.success("LIVE UNDERDOG ONLY")
    download_package=Path(__file__).resolve().with_name("NFL_PROP_ENGINE_V740_FULL_FOOTBALL.zip")
    if download_package.exists():
        st.download_button(
            "Download Live-Ready ZIP",
            data=download_package.read_bytes(),
            file_name=download_package.name,
            mime="application/zip",
            use_container_width=True,
            key="download_live_ready_package",
        )

    if "board_pull_id" not in st.session_state:
        st.session_state["board_pull_id"] = 0
    if "nfl_live_rows" not in st.session_state:
        _cached=load_last_pulled_board()
        st.session_state["nfl_live_rows"]=_cached.get("rows",[]) or []
    if "nfl_moneyline_rows" not in st.session_state:
        _cached_ml=load_last_pulled_moneylines()
        st.session_state["nfl_moneyline_rows"]=_cached_ml.get("rows",[]) or []
    if "nfl_pull_status" not in st.session_state:
        st.session_state["nfl_pull_status"]="READY"

    st.subheader("Game Day Readiness")
    sidebar_readiness=projection_database_readiness()
    st.metric("Projection database",sidebar_readiness.get("status","BLOCKED"))
    if not sidebar_readiness.get("ready") and active_season_mode=="PRESEASON":
        st.warning("Preseason estimates can run with partial historical data, but workload and reliability gates still control official plays.")
    elif not sidebar_readiness.get("ready"):
        st.error("Regular-season official plays are blocked until historical and current-roster coverage is complete.")
    elif sidebar_readiness.get("warnings"):
        st.warning("Projections are enabled. Injury coverage is partial, so player status and final inactives remain individual safety checks.")
    with st.expander("Readiness details",expanded=False):
        readiness_rows=[]
        for readiness_key,minimum in sidebar_readiness.get("minimums",{}).items():
            count=sidebar_readiness.get("counts",{}).get(readiness_key,0)
            readiness_rows.append({
                "dataset":readiness_key.replace("_"," ").title(),
                "loaded":count,"required":minimum,"blocking":"YES","ready":count>=minimum,
            })
        for readiness_key,minimum in sidebar_readiness.get("advisory_minimums",{}).items():
            count=sidebar_readiness.get("counts",{}).get(readiness_key,0)
            readiness_rows.append({
                "dataset":readiness_key.replace("_"," ").title(),
                "loaded":count,"required":minimum,"blocking":"NO","ready":count>=minimum,
            })
        if readiness_rows:
            st.dataframe(pd.DataFrame(readiness_rows),use_container_width=True,hide_index=True)
    prepare_clicked=st.button("Prepare Game Day Data + Lines",use_container_width=True,type="primary",key="prepare_game_day")
    if prepare_clicked:
        with st.spinner("Building verified data, refreshing current context, and pulling live lines..."):
            game_day_report,game_day_props,game_day_moneylines=run_game_day_refresh()
        st.session_state["nfl_game_day_report"]=game_day_report
        if game_day_props:
            st.session_state["nfl_live_rows"]=game_day_props
            st.session_state["nfl_moneyline_rows"]=game_day_moneylines
            st.session_state["nfl_pull_status"]=f"GAME DAY · {len(game_day_props)} rows"
        clear_projection_result_cache()
        st.rerun()
    last_game_day=st.session_state.get("nfl_game_day_report") or load_json(LOCAL_DIR / "nfl_game_day_refresh.json",{})
    if last_game_day:
        last_ready=(last_game_day.get("database_readiness") or {}).get("status","BLOCKED")
        st.caption(f"Last preparation: {last_game_day.get('ran_at','')} · {last_ready} · {last_game_day.get('live_rows',0)} live rows")

    st.subheader("Live Lines")
    st.caption("One Streamlit rerun is normal when you pull. The last good board stays visible if the network request fails.")
    pull_clicked=st.button("🔄 Pull Fresh Underdog Lines", use_container_width=True, type="primary")
    if pull_clicked:
        st.session_state["board_pull_id"] = int(st.session_state.get("board_pull_id", 0)) + 1
        _pid=st.session_state["board_pull_id"]
        try:
            fetch_underdog_nfl_props.clear(); fetch_underdog_nfl_moneylines.clear(); safe_get_json.clear()
        except Exception:
            pass
        with st.spinner("Pulling fresh Underdog NFL lines…"):
            _pulled=fetch_underdog_nfl_props(_pid)
            _pulled_ml=fetch_underdog_nfl_moneylines(_pid)
        if _pulled:
            st.session_state["nfl_live_rows"]=_pulled
            st.session_state["nfl_moneyline_rows"]=_pulled_ml
            save_last_pulled_board(_pulled,_pulled_ml)
            clear_projection_result_cache()
            st.session_state["nfl_pull_status"]=f"SUCCESS · {len(_pulled)} rows"
            st.success(f"Fresh pull loaded: {len(_pulled)} valid NFL lines. Main-line selection will run before projection.")
        else:
            # Do not blank the page when a fresh request fails. Keep the last successful
            # board visible and tell the user the live request returned no valid rows.
            st.session_state["nfl_pull_status"]="NO VALID LIVE ROWS · previous board preserved"
            st.warning("Fresh request returned no valid NFL rows. The last successful board was preserved instead of clearing the page.")
    last_board_meta=load_last_pulled_board()
    st.caption(f"Last successful pull: {last_board_meta.get('pulled_at') or 'None'} · {last_board_meta.get('row_count',0)} rows")
    st.caption(f"Status: {st.session_state.get('nfl_pull_status','READY')}")

    with st.expander("Manual Board Import / Cache", expanded=False):
        st.caption("Fallback only. Live pull is always attempted from the button above; there is no separate 'use saved board' switch anymore.")
        manual_upload = st.file_uploader("Upload CSV/TXT board", type=["csv", "txt"], key="manual_board_upload")
        manual_text = st.text_area("Paste Underdog board text", height=130, placeholder="Pass Yards\nJ. Goff\n271.5\nDET vs NO", key="manual_board_text")
        if st.button("📥 Load Manual Board", use_container_width=True, key="load_manual_board_btn"):
            manual_rows = _filter_live_board_to_phase6_model(parse_manual_underdog_board(manual_text, manual_upload))
            if manual_rows:
                save_last_pulled_board(manual_rows, [])
                st.session_state["nfl_live_rows"]=manual_rows
                st.session_state["nfl_moneyline_rows"]=[]
                clear_projection_result_cache()
                st.success(f"Loaded {len(manual_rows)} valid manual rows.")
            else:
                st.warning("No valid manual NFL rows found.")
        if st.button("🧹 Clear Saved Board", use_container_width=True, key="clear_saved_board_cache_btn"):
            save_json(BOARD_CACHE_FILE,{"pulled_at":None,"source":"CLEARED","row_count":0,"rows":[]})
            save_json(MONEYLINE_CACHE_FILE,{"pulled_at":None,"source":"CLEARED","row_count":0,"rows":[]})
            st.session_state["nfl_live_rows"]=[]; st.session_state["nfl_moneyline_rows"]=[]
            clear_projection_result_cache()
            st.success("Saved board cleared.")

    # Keep every supported market active; render only the chosen section.
    prop_filter=list(ACTIVE_NFL_MARKET_ORDER)
    setting_defaults={
        "xgb_assist_enabled":False,"xgb_min_rows":50,"xgb_blend_weight":0.22,
        "advanced_sim_assist_enabled":True,"smart_calibration_enabled":True,
        "team_volume_reconciliation_enabled":True,"bayes_min_games":5,
        "ensemble_ml_assist_enabled":False,"ensemble_min_rows":75,
        "ensemble_blend_weight":0.16,"primary_lines_only":True,
        "minimum_data_score":0,"show_all_cards":False,"nfl_table_page_size":50,
    }
    for setting_key,setting_value in setting_defaults.items():
        st.session_state.setdefault(setting_key,setting_value)
    with st.expander("Advanced Model Settings", expanded=False):
        st.caption("Changes take effect together when you apply them.")
        with st.form("advanced_model_settings_form_v75"):
            draft_xgb=st.toggle("XGBoost Assist after grading",value=bool(st.session_state["xgb_assist_enabled"]),key="draft_xgb_v75")
            draft_xgb_min=st.slider("XGB min graded rows",25,250,int(st.session_state["xgb_min_rows"]),5,key="draft_xgb_min_v75")
            draft_xgb_blend=st.slider("XGB max blend",0.05,0.40,float(st.session_state["xgb_blend_weight"]),0.01,key="draft_xgb_blend_v75")
            draft_advanced=st.toggle("Bayesian / Markov / Poisson Assist",value=bool(st.session_state["advanced_sim_assist_enabled"]),key="draft_advanced_v75")
            draft_smart=st.toggle("Smart role calibration",value=bool(st.session_state["smart_calibration_enabled"]),key="draft_smart_v75")
            draft_reconcile=st.toggle("Team-volume reconciliation",value=bool(st.session_state["team_volume_reconciliation_enabled"]),key="draft_reconcile_v75")
            draft_bayes_min=st.slider("Bayesian min player games",3,12,int(st.session_state["bayes_min_games"]),1,key="draft_bayes_min_v75")
            draft_ensemble=st.toggle("Random Forest / Tree Ensemble Assist",value=bool(st.session_state["ensemble_ml_assist_enabled"]),key="draft_ensemble_v75")
            draft_ensemble_min=st.slider("Ensemble min graded rows",50,400,int(st.session_state["ensemble_min_rows"]),5,key="draft_ensemble_min_v75")
            draft_ensemble_blend=st.slider("Ensemble max blend",0.04,0.30,float(st.session_state["ensemble_blend_weight"]),0.01,key="draft_ensemble_blend_v75")
            draft_primary=st.checkbox("Use one primary line per player/prop",value=bool(st.session_state["primary_lines_only"]),key="draft_primary_v75")
            draft_min_score=st.slider("Minimum Data Score",0,99,int(st.session_state["minimum_data_score"]),key="draft_min_score_v75")
            draft_show_all=st.checkbox("Larger detailed-card pages",value=bool(st.session_state["show_all_cards"]),key="draft_show_all_v75")
            table_options=[25,50,100]
            current_table_size=int(st.session_state["nfl_table_page_size"])
            draft_table_size=st.selectbox("Table rows per page",table_options,index=table_options.index(current_table_size if current_table_size in table_options else 50),key="draft_table_size_v75")
            apply_settings=st.form_submit_button("Apply Settings",use_container_width=True,type="primary")
            if apply_settings:
                st.session_state.update({
                    "xgb_assist_enabled":draft_xgb,"xgb_min_rows":draft_xgb_min,"xgb_blend_weight":draft_xgb_blend,
                    "advanced_sim_assist_enabled":draft_advanced,"smart_calibration_enabled":draft_smart,
                    "team_volume_reconciliation_enabled":draft_reconcile,"bayes_min_games":draft_bayes_min,
                    "ensemble_ml_assist_enabled":draft_ensemble,"ensemble_min_rows":draft_ensemble_min,
                    "ensemble_blend_weight":draft_ensemble_blend,"primary_lines_only":draft_primary,
                    "minimum_data_score":draft_min_score,"show_all_cards":draft_show_all,"nfl_table_page_size":draft_table_size,
                })
                clear_projection_result_cache()
                st.success("Model settings applied.")
    primary_lines_only=bool(st.session_state["primary_lines_only"])
    min_score=int(st.session_state["minimum_data_score"])
    st.session_state["nfl_card_page_size"]=24 if st.session_state["show_all_cards"] else 12
    st.divider()
    st.caption("API keys can be added in Streamlit secrets or Railway variables later.")
    show_feed_debug=st.checkbox("Show Underdog feed debug", False)
    if active_season_mode=="PRESEASON":
        with st.expander("Preseason Rotations",expanded=True):
            _render_preseason_rotation_panel()
    with st.expander("NFL Savant Data Pack",expanded=False):
        _render_nfl_savant_admin()
    with st.expander("Projection Data", expanded=False):
        _render_projection_data_admin()
    with st.expander("Admin: Phase 6 Database", expanded=False):
        _render_phase6_admin()
    st.code("STORAGE_DIR=nfl_engine", language="bash")

PRIMARY_PROP_SECTIONS = {
    "Pass Yards": ["Passing Yards"],
    "Pass TDs": ["Passing TDs"],
    "Receiving Yards": ["Receiving Yards"],
    "Rushing Yards": ["Rushing Yards"],
    "Rush + Rec TDs": ["Anytime TD"],
    "FG Made": ["Field Goals Made"],
    "Receptions": ["Receptions"],
    "Rush Attempts": ["Rush Attempts"],
    "Pass Attempts": ["Pass Attempts"],
    "Completions": ["Completions"],
}
TOOL_SECTIONS=["Best Edges","Player Cards","Alt-Line Ladder","Closing Review","Exposure","Correlation Builder","Save + Grade","Learning Dashboard","Money Line","Backtest"]
NO_CURRENT_BOARD_SECTIONS={"Learning Dashboard","Money Line","Backtest"}
page_options=list(PRIMARY_PROP_SECTIONS.keys())+TOOL_SECTIONS
active_page=st.selectbox("NFL Prop Section",page_options,index=0,key="nfl_main_page")
st.caption("The selected market is projected on demand. Full-board tools build the complete slate only when opened.")

pull_id=int(st.session_state.get("board_pull_id",0))
live=[]
moneylines=[]
live=list(st.session_state.get("nfl_live_rows",[]) or [])
moneylines=list(st.session_state.get("nfl_moneyline_rows",[]) or [])
if not live:
    cached_board=load_last_pulled_board(); cached_money=load_last_pulled_moneylines()
    live=cached_board.get("rows",[]) or []
    moneylines=cached_money.get("rows",[]) or []
    if live:
        st.session_state["nfl_live_rows"]=live
        st.session_state["nfl_moneyline_rows"]=moneylines
        request_log("UNDERDOG_BOARD_CACHE","RESTORED_LAST_SUCCESS",f"rows={len(live)} pulled_at={cached_board.get('pulled_at')}")
raw_all=list(live)
phase_counts={
    "PRESEASON":sum(1 for row in raw_all if row_matches_season_mode(row,"PRESEASON")),
    "REGULAR":sum(1 for row in raw_all if row_matches_season_mode(row,"REGULAR")),
    "UNKNOWN":sum(1 for row in raw_all if not row_matches_season_mode(row,"PRESEASON") and not row_matches_season_mode(row,"REGULAR")),
}
raw_all=[row for row in raw_all if row_matches_season_mode(row,active_season_mode)]
st.caption(f"Underdog phase split: {phase_counts['PRESEASON']} preseason · {phase_counts['REGULAR']} regular · {phase_counts['UNKNOWN']} unknown. Only {active_season_mode.lower()} rows are active.")
if live and not raw_all:
    st.warning(f"No {active_season_mode.lower()} rows were detected. Rows from the other season mode were not mixed into this board.")
selected_raw = _select_primary_market_lines(raw_all) if primary_lines_only else list(raw_all)
selected_raw = apply_market_integrity_guards(selected_raw)
if active_season_mode=="PRESEASON":
    selected_raw=[r for r in selected_raw if (_canon_prop_label(r.get("prop")) or r.get("prop")) in PRESEASON_SUPPORTED_MARKETS]
if active_page in PRIMARY_PROP_SECTIONS:
    requested_props=set(PRIMARY_PROP_SECTIONS[active_page])
    raw=[r for r in selected_raw if (_canon_prop_label(r.get("prop")) or r.get("prop")) in requested_props]
elif active_page in NO_CURRENT_BOARD_SECTIONS:
    raw=[]
else:
    raw=selected_raw
if active_season_mode=="PRESEASON" and raw:
    raw=apply_preseason_team_rotation_context(raw)
board_readiness=projection_database_readiness()
blocked_live_rows=[]
if not board_readiness.get("ready") and active_season_mode=="REGULAR":
    blocked_live_rows=list(raw)
    raw=[]
    missing_text=" · ".join(board_readiness.get("missing",[])[:5])
    st.error(f"PROJECTIONS BLOCKED: game-day data is incomplete. {missing_text}")
    if blocked_live_rows:
        st.caption(f"{len(blocked_live_rows)} live lines were received but are not being projected from fallback baselines.")
elif not board_readiness.get("ready") and active_season_mode=="PRESEASON":
    st.warning("Historical coverage is partial. Rotation-first preseason estimates remain visible, but weak rows are PASS-gated.")
_market_cache_scope=",".join(sorted(str(x) for x in (prop_filter or [])))
projection_cache_key = f"{active_season_mode}|{_market_cache_scope}|" + _board_projection_cache_key(raw, primary_lines_only)
projection_cache=st.session_state.setdefault("nfl_projection_cache",{})
cache_entry=projection_cache.get(projection_cache_key)
cache_hit=isinstance(cache_entry,dict) and isinstance(cache_entry.get("rows"),list)
projection_errors=list(cache_entry.get("errors",[])) if cache_hit else []
if cache_hit:
    projected_base = cache_entry.get("rows", [])
    st.session_state["nfl_projection_cache_seconds"]=cache_entry.get("seconds",0)
else:
    projected_base=[]
    started=time.perf_counter()
    total=max(1, len(raw))
    # Adaptive Monte Carlo keeps large full-market boards responsive on Railway/mobile.
    # Smaller boards retain more samples; large boards use enough samples for stable
    # probabilities without forcing millions of unnecessary draws on every refresh.
    sim_count = 5000 if len(raw) > 250 else 7000 if len(raw) > 100 else 10000
    progress=st.progress(0, text=f"Building NFL projections: 0/{len(raw)} · {sim_count:,} sims each") if raw else None
    for idx, _r in enumerate(raw, start=1):
        _canon = _canon_prop_label(_r.get("prop")) or _r.get("prop")
        if _canon in ACTIVE_NFL_MARKETS and _canon in prop_filter:
            _rr=dict(_r); _rr["prop"]=_canon
            try:
                projected_base.append(project_row_preseason(_rr, sims=sim_count) if active_season_mode=="PRESEASON" else project_row(_rr, sims=sim_count))
            except Exception as exc:
                projection_errors.append({"player":_rr.get("player"), "prop":_rr.get("prop"), "line":_rr.get("line"), "error":str(exc)[:240]})
        if idx == total or idx % max(1, total//25) == 0:
            progress.progress(min(1.0, idx/total), text=f"Building NFL projections: {idx}/{len(raw)} · {sim_count:,} sims each")
    if bool(st.session_state.get("team_volume_reconciliation_enabled", True)):
        projected_base=reconcile_preseason_team_volume(projected_base) if active_season_mode=="PRESEASON" else reconcile_team_projection_volume(projected_base)
    flush_tracking_state()
    if progress is not None:
        progress.empty()
    projection_seconds=round(time.perf_counter()-started,2)
    projection_cache[projection_cache_key]={"rows":projected_base,"errors":projection_errors,"seconds":projection_seconds}
    while len(projection_cache)>16:
        projection_cache.pop(next(iter(projection_cache)))
    st.session_state["nfl_projection_cache"]=projection_cache
    st.session_state["nfl_projection_cache_seconds"]=projection_seconds
for _p in projected_base:
    _x=_p.get("xgb_assist") or {}
    _p["xgb_status"] = _x.get("status", "OFF")
projected=[p for p in projected_base if p.get("data_score",0)>=min_score]
if projection_errors:
    st.error(f"{len(projection_errors)} rows failed safely instead of freezing the app.")
    with st.expander("Projection errors", expanded=False):
        st.dataframe(pd.DataFrame(projection_errors), use_container_width=True, hide_index=True)

# Final board integrity audit: no unknown prop can reach the UI.
invalid_board_rows=[p for p in projected if p.get("prop") not in ACTIVE_NFL_MARKETS or not _valid_market_line(p.get("prop"),p.get("line"),season_mode_for_row(p))]
if invalid_board_rows:
    st.error(f"Blocked {len(invalid_board_rows)} invalid market rows before display.")
    projected=[p for p in projected if p not in invalid_board_rows]

df=pd.DataFrame(projected)
real_count=len(projected)
best_edges=[p for p in projected if p.get("action_tier")=="BET"]
mode_before=rows_for_season_mode(load_json(PICK_LOG,[]),active_season_mode)
mode_after=rows_for_season_mode(load_json(AFTER_LOG,[]),active_season_mode)
mode_results=rows_for_season_mode(load_json(RESULT_LOG,[]),active_season_mode)

st.markdown("<div class='kpi-strip'>"+
    f"<div class='kpi-box'><div class='kpi-label'>Player Cards</div><div class='kpi-value'>{len(projected)}</div><div class='kpi-sub'>shown on board</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Live Lines</div><div class='kpi-value'>{real_count}</div><div class='kpi-sub'>{'Underdog detected' if real_count else 'live feed has no validated rows'}</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Best Edges</div><div class='kpi-value'>{len(best_edges)}</div><div class='kpi-sub'>prob/edge filtered</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Before Saves</div><div class='kpi-value'>{len(mode_before)}</div><div class='kpi-sub'>{active_season_mode.lower()} snapshots</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>After Saves</div><div class='kpi-value'>{len(mode_after)}</div><div class='kpi-sub'>{active_season_mode.lower()} closing</div></div>"+
    f"<div class='kpi-box'><div class='kpi-label'>Graded</div><div class='kpi-value'>{len(mode_results)}</div><div class='kpi-sub'>{active_season_mode.lower()} learning</div></div>"+
    "</div>", unsafe_allow_html=True)

side_audit=side_distribution_audit(projected,active_season_mode)
if not side_audit.empty:
    warnings=[text for text in side_audit.get("warning",pd.Series(dtype=str)).astype(str).tolist() if text]
    if warnings:
        st.warning("Side Distribution Audit: "+" · ".join(warnings[:3]))
    with st.expander("Side Distribution Audit",expanded=False):
        st.dataframe(side_audit,use_container_width=True,hide_index=True)
        st.caption("Diagnostic only. The app never shifts projections to force equal OVER and UNDER counts.")

if live:
    cached_meta = load_last_pulled_board()
    build_note = "cached projections reused" if cache_hit else f"built in {st.session_state.get('nfl_projection_cache_seconds','—')}s"
    st.success(f"🟢 Underdog NFL feed: {len(live)} valid rows · {len(projected_base)} projected in this market view · {build_note}. Last board pull: {cached_meta.get('pulled_at') or 'current refresh'}.")
else:
    st.warning("No live Underdog NFL rows were detected. Click 🔄 Pull Fresh Underdog Lines in the sidebar when props are posted.")

if 'show_feed_debug' in globals() and show_feed_debug:
    req_log=load_json(REQUEST_LOG,[])
    st.caption("Latest Underdog/API request log")
    st.dataframe(pd.DataFrame(req_log[-25:]), use_container_width=True, hide_index=True)

if not board_readiness.get("ready") and active_season_mode=="REGULAR":
    st.warning("No fallback projections are allowed. Use Sidebar → Prepare Game Day Data + Lines; the board unlocks only after every required coverage gate passes.")

if active_page in PRIMARY_PROP_SECTIONS:
    wanted=set(PRIMARY_PROP_SECTIONS[active_page])
    rows=[p for p in projected if p.get("prop") in wanted]
    st.markdown(f"<div class='section-title-pro'>{active_page}</div>", unsafe_allow_html=True)
    view_mode=st.radio("View",["Compact","Full Table","Detailed Cards"],horizontal=True,index=0,key=f"view_{re.sub(r'[^a-z0-9]+','_',active_page.lower())}")
    if view_mode=="Compact":
        _render_compact_prop_board(rows,active_page)
    elif view_mode=="Full Table":
        _render_prop_table(rows,active_page)
    else:
        _render_player_cards(rows,header=None)

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

    c1,c2,c3=st.columns(3)
    with c1:
        before_scope=st.selectbox("Before save scope", ["ALL", "OFFICIAL_ONLY"], index=0, help="ALL saves the full visible live board. OFFICIAL_ONLY saves only bettable rows.")
        if st.button("💾 Save OFFICIAL BEFORE — Full Board", use_container_width=True):
            n, slate_id = save_snapshot(PICK_LOG, projected, "BEFORE", scope=before_scope, source_note="full_board_before")
            st.success(f"Saved {n} BEFORE rows · Slate ID: {slate_id}")
    with c2:
        after_scope=st.selectbox("After save scope", ["ALL", "OFFICIAL_ONLY"], index=0, help="Use this before grading if you want a closing snapshot of the same board.")
        if st.button("📌 Save AFTER / Closing — Full Board", use_container_width=True):
            n, slate_id = save_snapshot(AFTER_LOG, projected, "AFTER", scope=after_scope, source_note="full_board_after")
            st.success(f"Saved {n} AFTER rows · Slate ID: {slate_id}")
    with c3:
        st.metric("Current Board Rows", len(projected))
        st.metric("Bettable Rows", sum(1 for p in projected if p.get("bettable")))
        st.metric("Live Rows", len(projected))

    st.divider()
    st.subheader("Clear Board Logs")
    st.caption("Use this when you want a clean board log. This does NOT delete the Phase 6 historical database.")
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
    before_groups=[group for group in _snapshot_groups(PICK_LOG,"BEFORE") if any(graded_row_season_mode(row)==active_season_mode for row in (group.get("rows") or []))]
    if not before_groups:
        st.info("No BEFORE slates saved yet. Save the full board first, then come back here to grade it.")
    else:
        choice=st.selectbox("Choose saved BEFORE slate", before_groups, format_func=lambda x: x["label"])
        saved_rows=[row for row in choice["rows"] if graded_row_season_mode(row)==active_season_mode]
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
            outcome=graded[0].get("grade_outcome","NO ACTION") if graded else "NO ACTION"
            scale=graded[0].get("new_learning_scale") if graded else None
            st.success(f"Graded. Result: {outcome} · New learning scale: {scale}")

elif active_page == 'Learning Dashboard':
    st.markdown("<div class='section-title-pro'>Learning Dashboard + Calibration</div>", unsafe_allow_html=True)
    results=rows_for_season_mode(load_json(RESULT_LOG,[]),active_season_mode); learn=load_json(LEARN_FILE,{})
    st.caption(f"Showing {active_season_mode.lower()} grades only. Preseason and regular-season calibration never share samples.")
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
    st.markdown("<div class='section-title-pro'>Moneyline Game Cards · Full Football Possession Model</div>", unsafe_allow_html=True)
    active_moneylines=[row for row in moneylines if row_matches_season_mode(row,active_season_mode)]
    active_moneyline_props=[row for row in live if row_matches_season_mode(row,active_season_mode)]
    moneyline_cards=build_moneyline_game_cards(active_moneylines,active_moneyline_props,sims=18000)
    exact_price_games=sum(1 for card in moneyline_cards if card.get("price_status")=="LIVE MARKET")
    ready_games=sum(1 for card in moneyline_cards if not card.get("blocked"))
    official_ml_games=sum(1 for card in moneyline_cards if card.get("official_moneyline_ready"))
    m1,m2,m3,m4=st.columns(4)
    m1.metric("Slate Games",len(moneyline_cards))
    m2.metric("Model Ready",ready_games)
    m3.metric("Official Ready",official_ml_games)
    m4.metric("Live ML Prices",exact_price_games)
    if active_season_mode=="REGULAR" and moneyline_cards:
        reg_ready=[(card.get("regular_season_readiness") or {}).get("score") for card in moneyline_cards if not card.get("blocked")]
        reg_ready=[safe_float(x) for x in reg_ready if safe_float(x) is not None]
        if reg_ready:
            st.caption(f"Regular-season readiness: slate average {np.mean(reg_ready):.0f}/100 · official requires readiness 72+ plus non-LOW reliability. Preseason calibration remains isolated.")
    if exact_price_games:
        st.success(f"Exact live moneyline prices attached to {exact_price_games} game cards.")
    elif moneyline_cards:
        st.info("MODEL ONLY: the live Underdog NFL feed has matchups but no team moneyline prices. Model odds are labeled; no sportsbook price is fabricated.")
    _render_moneyline_cards(moneyline_cards)
    if active_season_mode=="REGULAR" and moneyline_cards:
        st.subheader("Regular-Season Moneyline Save + Grade")
        c1,c2=st.columns(2)
        if c1.button("Save REGULAR Moneyline Board",use_container_width=True,key="save_regular_ml_board_v746"):
            n,sid=save_moneyline_snapshot(moneyline_cards,"BEFORE")
            st.success(f"Saved {n} regular-season Moneyline games · {sid}")
        ml_results_file=c2.file_uploader("Grade Moneyline CSV (away, home, away_score, home_score)",type=["csv"],key="ml_grade_csv_v746")
        if ml_results_file is not None and st.button("Grade Saved REGULAR Moneylines",use_container_width=True,key="grade_regular_ml_v746"):
            graded=grade_moneyline_results_csv(ml_results_file)
            st.success(f"Graded {len(graded)} regular-season Moneyline games.") if graded else st.warning("No saved Moneyline games matched that results file.")
        ml_cal=moneyline_calibration_summary()
        if ml_cal:
            q1,q2,q3,q4,q5=st.columns(5)
            q1.metric("ML Games",ml_cal.get("games",0)); q2.metric("Win Rate",f"{100*ml_cal.get('win_rate',0):.1f}%")
            q3.metric("Brier",f"{ml_cal.get('brier',0):.3f}"); q4.metric("Log Loss",f"{ml_cal.get('log_loss',0):.3f}"); q5.metric("Margin MAE",f"{ml_cal.get('margin_mae',0):.1f}")
    if active_moneylines:
        with st.expander("Exact moneyline feed rows",expanded=False):
            st.dataframe(pd.DataFrame(active_moneylines),use_container_width=True,hide_index=True)

elif active_page == 'Backtest':
    st.markdown("<div class='section-title-pro'>Backtest + Edge Buckets</div>", unsafe_allow_html=True)
    _render_backtest_dashboard(active_season_mode)


# MANUAL FULL LIVE AUDIT DOWNLOAD — OFF BY DEFAULT / DIAGNOSTIC ONLY
# =============================================================================
def _v749_audit_safe(v, depth=0):
    if depth > 10: return '<max-depth>'
    if v is None or isinstance(v, (str, int, bool)): return v
    if isinstance(v, float): return v if math.isfinite(v) else None
    if isinstance(v, Path): return str(v)
    if isinstance(v, (datetime, pd.Timestamp)):
        try: return v.isoformat()
        except Exception: return str(v)
    if isinstance(v, dict): return {str(k): _v749_audit_safe(x, depth+1) for k,x in v.items()}
    if isinstance(v, (list, tuple, set)): return [_v749_audit_safe(x, depth+1) for x in v]
    if isinstance(v, pd.DataFrame): return [_v749_audit_safe(x, depth+1) for x in v.to_dict('records')]
    if isinstance(v, pd.Series): return _v749_audit_safe(v.to_dict(), depth+1)
    try:
        if pd.isna(v): return None
    except Exception: pass
    try:
        if isinstance(v, np.integer): return int(v)
        if isinstance(v, np.floating):
            x=float(v); return x if math.isfinite(x) else None
    except Exception: pass
    return str(v)


def _v749_call_loader(name):
    fn = globals().get(name)
    if not callable(fn): return None, 'loader unavailable'
    try: return _v749_audit_safe(fn()), None
    except Exception as e: return None, str(e)[:500]


def _v749_build_audit_zip():
    active_rows = list(globals().get('selected_raw') or [])
    live_rows = list(globals().get('live') or [])
    season_mode = str(globals().get('active_season_mode') or 'REGULAR')
    merged, merge_errors = [], []
    merge_fn = globals().get('merge_nfl_context')
    canon_fn = globals().get('_canon_prop_label')
    for row in active_rows:
        rr = dict(row)
        if callable(canon_fn): rr['prop'] = canon_fn(rr.get('prop')) or rr.get('prop')
        try:
            merged.append(_v749_audit_safe(merge_fn(rr) if callable(merge_fn) else rr))
        except Exception as e:
            merge_errors.append({'player':rr.get('player'),'team':rr.get('team'),'opponent':rr.get('opponent'),'prop':rr.get('prop'),'line':rr.get('line'),'error':str(e)[:500]})

    loader_names = [
        'load_usage_bank','load_current_usage_bank','load_depth_chart_bank','load_role_override_bank','load_market_context_bank',
        'load_travel_context_bank','load_matchup_context_bank','load_qb_context_bank',
        'load_defensive_injury_context','load_final_inactives_context','load_splits_context_bank',
        'load_personnel_context_bank','load_current_team_context','load_weather_context','load_team_context','load_injury_bank'
    ]
    banks, loader_errors = {}, {}
    for name in loader_names:
        data, err = _v749_call_loader(name)
        banks[name] = data
        if err: loader_errors[name] = err

    readiness = {}
    for key, fn_name in [('projection_database','projection_database_readiness'),('regular_season','regular_season_readiness_panel')]:
        fn=globals().get(fn_name)
        if callable(fn):
            try: readiness[key]=_v749_audit_safe(fn())
            except Exception as e: readiness[key]={'error':str(e)[:500]}

    meta = {
        'generated_at': globals().get('now_iso', lambda: datetime.now().isoformat())(),
        'diagnostic_only': True, 'season_mode': season_mode,
        'app_version': globals().get('APP_VERSION'), 'model_version': globals().get('MODEL_VERSION'),
        'current_season': globals().get('NFL_CURRENT_SEASON'), 'prior_season': globals().get('NFL_LAST_SEASON'),
        'live_rows': len(live_rows), 'active_rows': len(active_rows), 'merged_rows': len(merged),
        'merge_errors': len(merge_errors), 'active_markets': sorted(list(globals().get('ACTIVE_NFL_MARKETS') or [])),
    }
    settings = {
        'PROP_CONFIG': _v749_audit_safe(globals().get('PROP_CONFIG',{})),
        'ROLE_SAFETY_MINIMUMS': _v749_audit_safe(globals().get('ROLE_SAFETY_MINIMUMS',{})),
        'session_settings': {k:_v749_audit_safe(st.session_state.get(k)) for k in [
            'primary_lines_only','team_volume_reconciliation_enabled','advanced_sim_assist_enabled','smart_calibration_enabled'
        ]}
    }

    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        def put(name,obj): z.writestr(name,json.dumps(_v749_audit_safe(obj),indent=2,sort_keys=True))
        put('00_META.json',meta); put('01_LIVE_UNDERDOG_ROWS.json',live_rows); put('02_ACTIVE_ROWS.json',active_rows)
        put('03_MERGED_MODEL_INPUTS.json',merged); put('04_LOADED_DATA_BANKS.json',banks); put('05_LOADER_ERRORS.json',loader_errors)
        put('06_MERGE_ERRORS.json',merge_errors); put('07_READINESS.json',readiness); put('08_MODEL_SETTINGS.json',settings)
        _pcache=st.session_state.get('nfl_projection_cache',{})
        put('09_PROJECTION_CACHE.json',_pcache); put('10_MONEYLINE_ROWS.json',globals().get('moneylines',[]))
        _all_proj=[]; _all_err=[]; _seen=set()
        for _ck,_ce in (_pcache.items() if isinstance(_pcache,dict) else []):
            if not isinstance(_ce,dict): continue
            for _pr in _ce.get('rows',[]) or []:
                _id=(str(_pr.get('player')),str(_pr.get('prop')),str(_pr.get('line')),str(_pr.get('team')),str(_pr.get('opponent')))
                if _id not in _seen:
                    _seen.add(_id); _all_proj.append(_pr)
            _all_err.extend(_ce.get('errors',[]) or [])
        _market_counts={}
        for _pr in _all_proj:
            _mk=str(_pr.get('prop') or 'UNKNOWN'); _market_counts[_mk]=_market_counts.get(_mk,0)+1
        put('09B_ALL_CACHED_PROJECTIONS.json',_all_proj)
        put('09C_PROJECTION_ERRORS_ALL.json',_all_err)
        put('09D_PROJECTION_MARKET_COUNTS.json',_market_counts)
        # Actual loaded Savant/NGS pack used by the app, current + prior season.
        savant_dir=globals().get('SAVANT_DIR'); pack_fn=globals().get('load_savant_pack'); manifest_fn=globals().get('load_savant_manifest')
        if callable(manifest_fn) and savant_dir is not None:
            try: put('11_SAVANT_MANIFEST.json',manifest_fn(savant_dir))
            except Exception as e: z.writestr('11_SAVANT_MANIFEST_ERROR.txt',str(e))
        if callable(pack_fn) and savant_dir is not None:
            for label,season in [('current',globals().get('NFL_CURRENT_SEASON')),('prior',globals().get('NFL_LAST_SEASON'))]:
                try:
                    pack=pack_fn(savant_dir,season=season) or {}
                    pmeta={}
                    for board,frame in pack.items():
                        if isinstance(frame,pd.DataFrame):
                            safe_name=re.sub(r'[^a-z0-9_-]+','_',str(board).lower())
                            z.writestr(f'savant_loaded/{label}_{safe_name}.csv',frame.to_csv(index=False))
                            pmeta[str(board)]={'rows':len(frame),'columns':list(frame.columns)}
                    put(f'savant_loaded/{label}_pack_meta.json',pmeta)
                except Exception as e: z.writestr(f'savant_loaded/{label}_ERROR.txt',str(e))
    return buf.getvalue(), meta


with st.sidebar.expander('🧪 FULL LIVE AUDIT DOWNLOAD', expanded=False):
    st.caption('OFF by default. Diagnostic export only — it does not change projection formulas or run automatically.')
    _v749_audit_on = st.toggle('Enable audit download', value=False, key='v749_manual_audit_toggle')
    if _v749_audit_on:
        st.caption('One ZIP: live lines + merged model inputs + current/prior usage + team/opponent/depth/injury/weather + Savant/NGS + readiness/config/cache.')
        if st.button('BUILD AUDIT ZIP', use_container_width=True, key='v749_build_audit_zip'):
            with st.spinner('Building one audit ZIP from the data loaded by the app…'):
                try:
                    _blob,_meta=_v749_build_audit_zip()
                    st.session_state['v749_audit_blob']=_blob
                    st.session_state['v749_audit_meta']=_meta
                    st.success(f"Audit ready · {_meta.get('active_rows',0)} active rows · {_meta.get('merged_rows',0)} merged")
                except Exception as e:
                    st.error(f'Audit failed safely: {str(e)[:600]}')
        if st.session_state.get('v749_audit_blob'):
            _stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
            st.download_button('⬇️ DOWNLOAD AUDIT ZIP',data=st.session_state['v749_audit_blob'],file_name=f'nfl_v753_full_live_audit_{_stamp}.zip',mime='application/zip',use_container_width=True,key='v749_download_audit_zip')
