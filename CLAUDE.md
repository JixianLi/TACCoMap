# TACCoMap

Map of TACC (Texas Advanced Computing Center) collaborations 2001–present: harvest → browse → globe.

- Data contract: `SCHEMA.md`. Harvesters in `harvest/`, one file per source, stdlib-only Python (no pip deps).
- Naming: `snake_case` for Python and for all JSON keys; `camelCase` for JavaScript identifiers.
- Web UI: static files in `web/`, no build step. Run: `python3 harvest/build.py && python3 -m http.server -d web 8000`.
- Every record carries `sources[]` provenance. Never store email addresses.
- Do not crawl tacc.utexas.edu, texascale.org, or other TACC-owned sites: their robots.txt bans AI agents. Curate such data by hand into `data/manual/` instead.
- Person location = institution listed on that specific collaboration record, never a tracked personal location.
