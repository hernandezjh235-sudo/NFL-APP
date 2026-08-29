
# -----------------------------------------------------------------------------
# MANUAL FULL LIVE AUDIT EXPORT — diagnostic only, OFF by default
# -----------------------------------------------------------------------------
def _audit_safe_value(value, depth=0):
    if depth > 12:
        return '<max-depth>'
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): _audit_safe_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_audit_safe_value(v, depth + 1) for v in value]
    if isinstance(value, pd.DataFrame):
        return [_audit_safe_value(r, depth + 1) for r in value.to_dict('records')]
    if isinstance(value, pd.Series):
        return _audit_safe_value(value.to_dict(), depth + 1)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            x = float(value)
            return x if math.isfinite(x) else None
    except Exception:
        pass
    return str(value)


def _audit_loader_snapshot():
    loaders = {
        'historical_usage_bank': load_usage_bank,
        'current_usage_bank': load_current_usage_bank,
        'depth_chart_bank': load_depth_chart_bank,
        'market_context_bank': load_market_context_bank,
        'travel_context_bank': load_travel_context_bank,
        'matchup_context_bank': load_matchup_context_bank,
        'qb_context_bank': load_qb_context_bank,
        'defensive_injury_context': load_defensive_injury_context,
        'final_inactives_context': load_final_inactives_context,
        'splits_context_bank': load_splits_context_bank,
        'personnel_context_bank': load_personnel_context_bank,
        'current_team_context': load_current_team_context,
        'weather_context': load_weather_context,
        'historical_team_context': load_team_context,
        'injury_bank': load_injury_bank,
    }
    data, errors = {}, {}
    for name, loader in loaders.items():
        try:
            data[name] = _audit_safe_value(loader())
        except Exception as exc:
            errors[name] = str(exc)[:500]
    return data, errors


def _build_manual_live_audit_zip():
    active_rows = list(selected_raw or [])
    merged_rows, merge_errors = [], []
    for row in active_rows:
        rr = dict(row)
        rr['prop'] = _canon_prop_label(rr.get('prop')) or rr.get('prop')
        try:
            merged_rows.append(_audit_safe_value(merge_nfl_context(rr)))
        except Exception as exc:
            merge_errors.append({
                'player': rr.get('player'), 'team': rr.get('team'),
                'opponent': rr.get('opponent'), 'prop': rr.get('prop'),
                'line': rr.get('line'), 'error': str(exc)[:500],
            })

    loaded_banks, loader_errors = _audit_loader_snapshot()
    readiness = {
        'projection_database': _audit_safe_value(projection_database_readiness()),
        'regular_season': _audit_safe_value(regular_season_readiness_panel()) if active_season_mode == 'REGULAR' else None,
    }
    settings = {
        'season_mode': active_season_mode,
        'app_version': APP_VERSION,
        'model_version': MODEL_VERSION,
        'nfl_current_season': NFL_CURRENT_SEASON,
        'nfl_last_season': NFL_LAST_SEASON,
        'active_markets': sorted(ACTIVE_NFL_MARKETS),
        'prop_config': _audit_safe_value(PROP_CONFIG),
        'role_safety_minimums': _audit_safe_value(ROLE_SAFETY_MINIMUMS),
        'primary_lines_only': bool(st.session_state.get('primary_lines_only', True)),
        'team_volume_reconciliation_enabled': bool(st.session_state.get('team_volume_reconciliation_enabled', True)),
        'advanced_sim_assist_enabled': bool(st.session_state.get('advanced_sim_assist_enabled', True)),
        'smart_calibration_enabled': bool(st.session_state.get('smart_calibration_enabled', True)),
    }
    meta = {
        'generated_at': now_iso(),
        'diagnostic_only': True,
        'active_season_mode': active_season_mode,
        'live_rows_total': len(live or []),
        'active_primary_rows': len(active_rows),
        'merged_rows': len(merged_rows),
        'merge_errors': len(merge_errors),
    }

    audit_buf = io.BytesIO()
    with zipfile.ZipFile(audit_buf, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr('00_META.json', json.dumps(_audit_safe_value(meta), indent=2, sort_keys=True))
        zf.writestr('01_LIVE_UNDERDOG_ROWS.json', json.dumps(_audit_safe_value(live), indent=2, sort_keys=True))
        zf.writestr('02_ACTIVE_PRIMARY_ROWS.json', json.dumps(_audit_safe_value(active_rows), indent=2, sort_keys=True))
        zf.writestr('03_MERGED_MODEL_INPUTS.json', json.dumps(_audit_safe_value(merged_rows), indent=2, sort_keys=True))
        zf.writestr('04_LOADED_DATA_BANKS.json', json.dumps(_audit_safe_value(loaded_banks), indent=2, sort_keys=True))
        zf.writestr('05_LOADER_ERRORS.json', json.dumps(_audit_safe_value(loader_errors), indent=2, sort_keys=True))
        zf.writestr('06_MERGE_ERRORS.json', json.dumps(_audit_safe_value(merge_errors), indent=2, sort_keys=True))
        zf.writestr('07_READINESS.json', json.dumps(_audit_safe_value(readiness), indent=2, sort_keys=True))
        zf.writestr('08_MODEL_SETTINGS_AND_MARKET_CONFIG.json', json.dumps(_audit_safe_value(settings), indent=2, sort_keys=True))
        zf.writestr('09_PROJECTION_CACHE.json', json.dumps(_audit_safe_value(st.session_state.get('nfl_projection_cache', {})), indent=2, sort_keys=True))
        zf.writestr('10_MONEYLINE_ROWS.json', json.dumps(_audit_safe_value(moneylines), indent=2, sort_keys=True))
        try:
            zf.writestr('11_SAVANT_MANIFEST.json', json.dumps(_audit_safe_value(load_savant_manifest(SAVANT_DIR)), indent=2, sort_keys=True))
        except Exception as exc:
            zf.writestr('11_SAVANT_MANIFEST_ERROR.txt', str(exc))

        for season_label, season_value in (('current', NFL_CURRENT_SEASON), ('prior', NFL_LAST_SEASON)):
            try:
                pack = load_savant_pack(SAVANT_DIR, season=season_value)
                savant_meta = {}
                for board_name, frame in (pack or {}).items():
                    if isinstance(frame, pd.DataFrame):
                        safe_name = re.sub(r'[^a-z0-9_\-]+', '_', str(board_name).lower())
                        zf.writestr(f'savant_loaded/{season_label}_{safe_name}.csv', frame.to_csv(index=False))
                        savant_meta[board_name] = {'rows': len(frame), 'columns': list(frame.columns)}
                zf.writestr(f'savant_loaded/{season_label}_pack_meta.json', json.dumps(_audit_safe_value(savant_meta), indent=2, sort_keys=True))
            except Exception as exc:
                zf.writestr(f'savant_loaded/{season_label}_ERROR.txt', str(exc))

    return audit_buf.getvalue(), meta


with st.sidebar.expander('🧪 Full Live Audit Download', expanded=False):
    st.caption('OFF by default. It only reads data already loaded by the app; it does not change projections.')
    _audit_enabled = st.toggle('Enable audit export', value=False, key='enable_full_live_audit_export_v749')
    if _audit_enabled:
        st.caption('Includes live lines, merged model inputs, current/prior usage, team/opponent context, depth/injuries, weather, Savant/NGS boards, readiness, model config and projection cache.')
        if st.button('BUILD AUDIT ZIP', use_container_width=True, key='build_full_live_audit_v749'):
            with st.spinner('Reading loaded NFL data and building the audit file…'):
                try:
                    _audit_blob, _audit_meta = _build_manual_live_audit_zip()
                    st.session_state['full_live_audit_blob_v749'] = _audit_blob
                    st.session_state['full_live_audit_meta_v749'] = _audit_meta
                    st.success(f"Audit ready · {_audit_meta.get('active_primary_rows', 0)} active rows · {_audit_meta.get('merged_rows', 0)} merged inputs")
                except Exception as exc:
                    st.error(f"Audit failed safely: {str(exc)[:600]}")
        if st.session_state.get('full_live_audit_blob_v749'):
            _audit_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            st.download_button(
                '⬇️ DOWNLOAD AUDIT ZIP',
                data=st.session_state['full_live_audit_blob_v749'],
                file_name=f"nfl_v749_full_live_audit_{active_season_mode.lower()}_{_audit_stamp}.zip",
                mime='application/zip',
                use_container_width=True,
                key='download_full_live_audit_v749',
            )
