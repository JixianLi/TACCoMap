# TACCoMap

Map of TACC (Texas Advanced Computing Center) collaborations 2001–present: harvest → browse → globe.

- Data contract: `SCHEMA.md`. Harvesters in `harvest/`, one file per source, stdlib-only Python (no pip deps).
- Naming: `snake_case` for Python and for all JSON keys; `camelCase` for JavaScript identifiers.
- Web UI: static files in `web/`, no build step. Run with the project Python (uv, `.python-version`): `uv run python harvest/build.py && uv run python -m http.server -d web 8000`.
- Map preview: `map/`, a WebUVisBox scenario (Vite + React + TypeScript; WebUVisBox is the git submodule `map/vendor/webuvisbox`, read-only here). Reads only `map/public/data/map_institutions.json` from `build.py`. Run: `cd map && npm install && npm run dev`. TypeScript identifiers are `camelCase`; the scenario config's `global_data` keys are `snake_case`. 2D Mercator for now; a 3D globe replaces it later.
- Every record carries `sources[]` provenance. Never store email addresses.
- Do not crawl tacc.utexas.edu, texascale.org, or other TACC-owned sites: their robots.txt bans AI agents. Curate such data by hand into `data/manual/` instead.
- Person location = institution listed on that specific collaboration record, never a tracked personal location.
