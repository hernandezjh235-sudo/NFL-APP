# -*- coding: utf-8 -*-
"""Diagnostic wrapper for NFL v7.49.

Runs app.py unchanged, then adds a one-shot full-live audit ZIP control.
No projection formulas, calibration, side selection, grading, or saved history are modified.
"""
from pathlib import Path
import io, json, math, zipfile
from datetime import datetime

# Execute the production app in this same namespace so the diagnostic section can
# call the exact production projection/context functions and inspect runtime files.
_app = Path(__file__).with_name("app.py")
exec(compile(_app.read_text(encoding="utf-8"), str(_app), "exec"), globals(), globals())


def _audit_safe(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            v = float(value)
            return v if math.isfinite(v) else None
    except Exception:
        pass
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): _audit_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_audit_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _flat(row):
    out = {}
    for k, v in (row or {}).items():
        if isinstance(v, (dict, list, tuple, set)):
            try:
                out[k] = json.dumps(_audit_safe(v), sort_keys=True, separators=(",", ":"))
            except Exception:
                out[k] = str(v)
        else:
            out[k] = _audit_safe(v)
    return out


def _runtime_files():
    roots = []
    for candidate in (LOCAL_DIR, PHASE6_DIR, SAVANT_DIR):
        try:
            p = Path(candidate)
            if p.exists() and p not in roots:
                roots.append(p)
        except Exception:
            pass
    files, seen = [], set()
    for root in roots:
        try:
            for fp in root.rglob("*"):
                if not fp.is_file() or fp.suffix.lower() not in {".csv", ".json"}:
                    continue
                low = fp.name.lower()
                if any(x in low for x in ("secret", "token", "credential", "password", "cookie", "session")):
                    continue
                try:
                    key = str(fp.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    files.append(fp)
                except Exception:
                    continue
        except Exception:
            continue
    return files


def _build_full_audit(live_rows, season_mode, sims):
    season_mode = str(season_mode or "REGULAR").upper()
    rows = []
    for r in list(live_rows or []):
        if not row_matches_season_mode(r, season_mode):
            continue
        prop = _canon_prop_label(r.get("prop")) or r.get("prop")
        if prop not in ACTIVE_NFL_MARKETS:
            continue
        rr = dict(r)
        rr["prop"] = prop
        if season_mode == "PRESEASON" and prop not in PRESEASON_SUPPORTED_MARKETS:
            continue
        rows.append(rr)
    rows = apply_market_integrity_guards(_select_primary_market_lines(rows))
    if season_mode == "PRESEASON" and rows:
        rows = apply_preseason_team_rotation_context(rows)

    projected_all, errors = [], []
    prog = st.progress(0, text=f"Full audit: 0/{len(rows)}") if rows else None
    total = max(1, len(rows))
    for i, rr in enumerate(rows, 1):
        try:
            p = project_row_preseason(rr, sims=sims) if season_mode == "PRESEASON" else project_row(rr, sims=sims)
            projected_all.append(p)
        except Exception as exc:
            errors.append({
                "player": rr.get("player"), "team": rr.get("team"), "opponent": rr.get("opponent"),
                "prop": rr.get("prop"), "line": rr.get("line"), "error": str(exc)[:500],
            })
        if prog is not None and (i == total or i % max(1, total // 40) == 0):
            prog.progress(min(1.0, i / total), text=f"Full audit: {i}/{len(rows)}")
    if bool(st.session_state.get("team_volume_reconciliation_enabled", True)):
        projected_all = reconcile_preseason_team_volume(projected_all) if season_mode == "PRESEASON" else reconcile_team_projection_volume(projected_all)
    if prog is not None:
        prog.empty()

    meta = {
        "generated_at": now_iso(), "app_version": APP_VERSION, "model_version": MODEL_VERSION,
        "season_mode": season_mode, "nfl_current_season": NFL_CURRENT_SEASON, "nfl_last_season": NFL_LAST_SEASON,
        "raw_live_rows_seen": len(live_rows or []), "validated_rows": len(rows),
        "projected_rows": len(projected_all), "projection_errors": len(errors), "sims_per_row": sims,
        "active_markets": sorted(ACTIVE_NFL_MARKETS),
        "database_readiness": projection_database_readiness(),
        "regular_season_readiness": regular_season_readiness_panel() if season_mode == "REGULAR" else None,
        "diagnostic_only": True,
        "note": "Built by audit_wrapper.py using unchanged production app.py projection functions.",
    }
    payload = {
        "meta": _audit_safe(meta), "raw_live_rows": _audit_safe(rows),
        "projected_rows": _audit_safe(projected_all), "projection_errors": _audit_safe(errors),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00_audit_meta.json", json.dumps(_audit_safe(meta), indent=2, sort_keys=True))
        zf.writestr("01_full_projection_audit.json", json.dumps(payload, indent=2, sort_keys=True))
        if projected_all:
            zf.writestr("02_projected_rows_flat.csv", pd.DataFrame([_flat(r) for r in projected_all]).to_csv(index=False))
        if rows:
            zf.writestr("03_raw_live_rows_flat.csv", pd.DataFrame([_flat(r) for r in rows]).to_csv(index=False))
        zf.writestr("04_projection_errors.json", json.dumps(_audit_safe(errors), indent=2))
        try:
            zf.writestr("05_request_log.json", json.dumps(_audit_safe(load_json(REQUEST_LOG, [])), indent=2))
        except Exception as exc:
            zf.writestr("05_request_log_ERROR.txt", str(exc))
        manifest = []
        used = set()
        for fp in _runtime_files():
            try:
                rel = f"runtime_inputs/{fp.name}"
                if rel in used:
                    rel = f"runtime_inputs/{fp.parent.name}__{fp.name}"
                used.add(rel)
                data = fp.read_bytes()
                zf.writestr(rel, data)
                manifest.append({
                    "zip_path": rel, "source_path": str(fp), "bytes": len(data),
                    "modified": datetime.fromtimestamp(fp.stat().st_mtime).isoformat(timespec="seconds"),
                })
            except Exception as exc:
                manifest.append({"source_path": str(fp), "error": str(exc)[:300]})
        zf.writestr("06_runtime_input_manifest.json", json.dumps(_audit_safe(manifest), indent=2, sort_keys=True))
    return buf.getvalue(), meta


with st.sidebar.expander("🧪 FULL LIVE AUDIT — ALL PROPS", expanded=True):
    st.caption("One click audits every active market and packages the live rows, merged projection inputs, nested model breakdowns, readiness, errors, request log, and all whitelisted runtime CSV/JSON files. Production app.py stays unchanged.")
    _audit_key = f"full_audit_{active_season_mode}"
    if st.button("BUILD EVERYTHING", use_container_width=True, key=f"build_{_audit_key}"):
        with st.spinner("Building every active prop into one audit ZIP…"):
            try:
                _sims = 5000 if active_season_mode == "REGULAR" else 4000
                _blob, _meta = _build_full_audit(selected_raw, active_season_mode, _sims)
                st.session_state[_audit_key] = _blob
                st.session_state[_audit_key + "_meta"] = _meta
                st.success(f"Ready: {_meta.get('projected_rows', 0)} projections · {_meta.get('projection_errors', 0)} errors")
            except Exception as exc:
                st.error(f"Audit failed safely: {str(exc)[:500]}")
    if st.session_state.get(_audit_key):
        _stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "⬇️ DOWNLOAD ONE ZIP",
            data=st.session_state[_audit_key],
            file_name=f"nfl_full_live_audit_{active_season_mode.lower()}_{_stamp}.zip",
            mime="application/zip", use_container_width=True, key=f"download_{_audit_key}",
        )
