# NFL Prop Engine — Railway / Streamlit Ready

This is the NFL version of the prop-engine structure. It keeps the clean player-card UI, projections, pure upside, alt ladders, correlation builder, save-before/save-after, final grading, and learning logs.

## Files
- `app.py` — Streamlit app
- `requirements.txt` — Python dependencies
- `Procfile` — Railway start command
- `.streamlit/config.toml` — Streamlit server config

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Railway
1. Upload/push all files to GitHub.
2. Create Railway project from GitHub repo.
3. Railway will use the Procfile.
4. Add optional env var: `STORAGE_DIR=nfl_engine`.

## Important
When no live NFL prop feed is detected, the app shows clearly labeled DEMO cards so the UI can be tested before the season. Do not treat DEMO rows as real picks.
