# TACCoMap

A dataset and browser of TACC (Texas Advanced Computing Center) collaborations, 2001–present: papers, awards,
subawards, datasets, allocations and funding opportunities, with the people and institutions involved. A 3D globe
view is planned on top of the same data.

```
harvest/<source>.py  ->  data/normalized/<source>.json  ->  harvest/build.py  ->  web/data/*.json  ->  web/ (static browser)
```

The data contract is `SCHEMA.md`; project rules are in `CLAUDE.md`. The pipeline and browser are Python 3 standard
library and plain browser JavaScript, with no dependencies to install; only the map preview in `map/` needs Node. The project Python is pinned with uv
(`.python-version`, `pyproject.toml`): run `uv sync` once, then use `uv run python …` as below.

## Running

```sh
# 1. Harvest (each writes data/normalized/<source>.json; raw API responses are cached in data/raw/<source>/)
uv run python harvest/openalex.py          # optional: export OPENALEX_API_KEY=... (see gaps below)
uv run python harvest/nsf.py
uv run python harvest/usaspending.py       # needs nsf.json first
uv run python harvest/nih.py
uv run python harvest/datacite.py
uv run python harvest/access_allocations.py
uv run python harvest/grants_gov.py
uv run python harvest/affiliation_evidence.py  # needs nsf.json/nih.json/usaspending.json and OPENALEX_API_KEY; dated PI/co-PI affiliations
uv run python harvest/orcid_names.py           # after affiliation_evidence.py; ~22.5k ORCIDs (~40 min first time, cached, resumable);
                                         # rerun before build.py whenever build_people.py's hard-evidence rules change (cached: seconds)

# 2. Build: merge sources, match free-text institutions to ROR, classify institutions, write web/data/
uv run python harvest/build.py                       # first run makes ~3k ROR API calls (~45 min); cached afterwards
uv run python harvest/build.py --skip-ror-matching   # fast, offline-ish build (not for a published site)
# maintainer-only outputs (merge log, full person records) go to data/review/ (gitignored, never published);
# --review-directory DIR writes them elsewhere

# 3. Serve the browser
uv run python -m http.server -d web 8000             # open http://localhost:8000

# 4. Map preview (WebUVisBox scenario in map/; needs Node 18+). build.py writes map/public/data/map_institutions.json
git submodule update --init                          # once: map/vendor/webuvisbox
cd map && npm install && npm run dev                 # open the URL Vite prints; keep step 3 running for "open in browser" links

# 5. Publish the map (only the map; the record browser is not published) to GitHub Pages
sh scripts/publish_map.sh                            # build into publish/ and run the guard (--map-site)
sh scripts/publish_map.sh --push                     # same, then force-push publish/ as the gh-pages branch
```

Re-running a harvester is cheap because responses are cached; delete `data/raw/<source>/` to force a refresh.
`data/raw/` is gitignored and **may contain email addresses** from the APIs (OpenAlex raw affiliation strings, the
NSF `piEmail` field, ORCID, ACCESS and DataCite responses) — never publish it. Normalized and web files are email-free.

Before committing or publishing, run the publish guard (details in `SCHEMA.md`, "Publishing guard"):

```sh
sh scripts/install_hooks.sh                          # once: pre-commit hook running the guard on staged files
uv run python harvest/check_publishable.py                 # tracked + untracked-not-ignored files, plus web/
uv run python harvest/check_publishable.py --site DIR      # a staged GitHub Pages folder (allowlist + content checks)
```

Institutions are handled in `harvest/build_institutions.py` (called from `build.py`):

- **Name resolution.** Free-text affiliations (and `name:` ids in `additional_institution_ids`) are matched, in order,
  against `data/manual/institution_aliases.json` (hand-verified variant → ROR id, including campus-vs-system and
  wrong-organization corrections such as "UC Berkeley" → UC Berkeley, not the UC System), then exact names/aliases of
  ROR institutions already in the dataset, then the ROR affiliation API. A trailing `(domain.edu)` hint is stripped
  first. ROR's `chosen` match is accepted only if a majority of the meaningful words of one of the organization's ROR
  names occur in the raw string (accent-folded, generic words like "University"/"Institute" ignored, and at least one
  supporting word that is not merely descriptive such as "engineering"/"council"/"solutions"), or an
  acronym/space-free name matches. A one-word raw string matches only the ROR display name or an acronym. A match is
  rejected when the raw string fully names a more specific organization: a ROR child of the chosen one, or another
  dataset organization covering more of the string (the chosen organization's own city/state/country words do not
  count). Rejected matches are written, with a `reason`, to a local diagnostics file outside the repo
  (`REJECTED_ROR_MATCHES_PATH` in `build_institutions.py`).
  Strings naming TACC resolve to TACC whenever they would otherwise be UT Austin or unresolved. Strings that name no
  organization (placeholders, a bare place or address, collaboration names, the record title, citation fragments)
  become `missing`. A string that is a personal address (a private residence or GPS coordinates, or a street, PO box,
  flat number, full postcode or "unaffiliated/independent/retired" with no organization named) becomes `missing`
  with `institution_name_as_listed` = `[address removed]` (`not_an_institution:personal_address`). After ROR
  matching, common street-level patterns (house number and street, PO box, suite) are removed from as-listed
  affiliations; city and postcode stay. Institutional street addresses in other formats can remain; home
  addresses that the automatic test misses are added to a hand-checked list (stored as hashes). Participants carry `institution_match_method` (`manual_alias`, `dataset_name_exact`,
  `ror_affiliation_api`, `tacc_name`, `not_an_institution:<kind>`, `bulk_affiliation_guard`); see `SCHEMA.md`.
- **Bulk-affiliation guard.** On papers with ≥50 participants, an institution held by more than half of them is
  removed from all of them (OpenAlex smears IceCube member institutions such as Southern University over whole author
  lists); on truncated author lists (`participants_truncated`, OpenAlex keeps the first 100) the threshold is more
  than one third (strict, exact fractions). Removed primaries become `missing`. build.py prints the counts per
  threshold and the top institutions and writes the affected papers to a local diagnostics file outside the repo
  (`BULK_AFFILIATION_PAPERS_PATH` in `build_institutions.py`)
  (`collaboration_id`, `title`, `participant_count`, `participants_truncated`, `min_share`, `removed_institutions[]`
  with `participant_count`, `primary_count` and `share_band`). The browser still shows (muted, "missing")
  and searches the removed raw string, so searching "Southern University" finds guarded IceCube authors.
- **Enrichment.** Every `ror:` institution is filled from its ROR record (cached in `data/raw/ror/`), including
  `website` (ROR `website` link, else OpenAlex `homepage_url`). Only ROR/OpenAlex organization types stay in
  `source_types`; source-specific labels (NIH "SCHOOLS OF MEDICINE" etc.) go to `source_categories`.
- **Classification** (`institution_type` ∈ `government | commercial | nonprofit | educational | research | healthcare |
  archive | other`, with `institution_type_reason`) follows the order in `SCHEMA.md`: manual override → website
  domain (incl. `.go.xx`, `.int`, `.govt.nz`, `.gv.at`, `.mil.xx` as government; a .com domain loses to a
  non-company ROR/OpenAlex type) → University/College in the name (street/town words like "College Station" ignored)
  → any parent organization educational (so TACC, SDSC, NCSA and university health systems are `educational`) →
  ROR/OpenAlex type → name heuristics (name-only institutions) → `other`. Parents outside the dataset are fetched
  from ROR to classify them. `data/institution_overrides.json` (`{"<institution id>": {"institution_type": …,
  "note": …}}`) holds hand-checked corrections; each `note` states the public fact relied on (ownership or governance per the
  organization's website, ROR record or a public registry; some cite a URL): the round-3 review of every .com
  institution and every parent-educational / multi-parent case added 46; the round-4 teaching-hospital review added
  40 (86 total). Hospitals that are only teaching affiliates of a university (state, regional, public-service,
  independent nonprofit or company owners, e.g. UPMC, IU Health, Rigshospitalet, VUMC) are `healthcare`;
  university-owned ones (Michigan Medicine, NYU Langone, Keck/USC) stay `educational`; consortium-run federal
  centers with `.edu` sites (NCAR, NOIRLab, SLAC) stay `educational`.
- Institutions that no collaboration references are dropped from `web/data/institutions.json`.

Awards and people are handled in `harvest/build_awards.py` and `harvest/build_people.py` (also called from `build.py`):

- **Date cut.** Only collaborations dated 2001 or later are built (TACC was formally formed in 2001); earlier
  supercomputing-center records, subawards of dropped primes and people referenced only by them are dropped before
  anything else runs. Undated records are kept. build.py prints the counts.
- **Institution at the time of award.** Uses `data/evidence/affiliation_evidence.json` (dated OpenAlex affiliation
  years and ORCID employment for NSF/NIH PIs and co-PIs; only `high`/`medium` person matches) and USAspending
  subawards under the same prime. One rule for lead PIs and co-PIs: evidence must overlap the award's active period
  (start year .. end/expiration year), so co-PIs added after the start are placed the same way; with no evidence in
  the period at all, the year before the start (proposal year) is used. A lead PI keeps the awardee
  (`nsf_awardee_rule`), refined to TACC when evidence in the period places them at TACC (one stray TACC-tagged paper
  counts as UT Austin). A co-PI gets the single candidate institution; with several, the unique one that is (part of)
  the awardee or a subawardee wins only if the person's evidence covers it in at least as many years as every other
  candidate; otherwise the co-PI stays `missing` ("unknown"). A co-PI with no own evidence can take the same
  person's institution from another award in an overlapping period (`cross_award_dated`). Nested affiliations
  collapse to the more specific one (UT Austin + TACC → TACC); a consortium with several ROR parents is never a unit
  of them. ORCID plausibility (below) runs first, so an evidence ORCID rejected for that person never places a co-PI.
  The current email domain is never used. Such a placement is inferred, not listed by the funder: it leaves
  `institution_name_as_listed` as the source gave it (null for NSF co-PIs), ORCID employment years in its evidence
  `detail` are clipped to the award period, and the browser marks it "inferred". Then `relationship_types` for NSF/NIH awards is recomputed:
  `tacc_lead_award` if the lead PI was at TACC, `tacc_partner_award` if a co-PI was or the abstract names TACC as a
  partner, else `tacc_mention_award`. Details in `SCHEMA.md`.
- **Person resolution.** Precision over recall, and since round 5 an ORCID is kept, and a nickname-only merge made,
  only on hard evidence: the ORCID's works list the record's DOI, the source itself asserts the ORCID (DataCite
  creators/contributors), an ORCID affiliation matches the record's institution in that year (or, for award PIs/co-PIs,
  the awardee or a subawardee during the award period), or the ORCID's registered/other names contain the listed
  variant. Otherwise the ORCID is stripped or the records stay separate, listed in the merge log for maintainer review.
  First, every ORCID is checked against
  `data/evidence/orcid_names.json`: an ORCID whose registered name does not fit the listed name
  (`orcid_name_mismatch`), whose given-name initials all conflict with the registered ones although the family name
  fits (`orcid_initial_conflict`, "J. Doe" vs "Mary Doe"), or (staff/PI ORCIDs) whose public works/affiliations
  corroborate none of its records, is stripped (participations move to the OpenAlex cluster or a name-only id;
  people carry `orcid_rejected`). Names that fit only through the nickname table, single-given-name registrations
  and ORCIDs seen only under initials on large-collaboration papers (≥50 authors or truncated) also need hard
  evidence. A record needing evidence is kept on works overlap (complete author list, no other author of that
  surname), a DataCite assertion, a matching ORCID affiliation, an experience match (nickname/given-name-only names
  only: same place within 2 years as another record of that ORCID), the same listed variant kept elsewhere, or a
  `confirm_orcid` entry; kept records are listed in the person's `orcid_check_overrides`. Cyrillic and Greek names
  are transliterated (Cyrillic "е" compared as both "ye" and "e"). After merging, a group loses its ORCID when a
  member was rejected for it or a merged full name conflicts with it without hard evidence. Then records merge on hard keys: ORCID, OpenAlex author id, NSF piId, NIH profile id, plus the
  ORCID/OpenAlex ids the evidence file links to NSF/NIH people (refused when middle initials differ; rejected
  evidence ORCIDs excluded). Two different ORCIDs never merge. Name-based merging then repeats until stable: groups
  merge when every pair of names is compatible (nickname table, initials, same surname) and both list the same place
  (same institution or ROR ancestor/descendant) in an overlapping window, and no competing ORCID namesake or
  conflicting full name makes it ambiguous. A merge that works only through a different-letter nickname pair ("Bill" / "William"; same-letter pairs like "Steve" / "Stephen" count like initials)
  also needs secondary evidence: a second shared place, the target ORCID's registered or other names fitting the
  source name, or works overlap; otherwise it is refused (`nickname_only_without_secondary_evidence`). NSF name-only records merge only on an exactly equal name with dated
  evidence at the awardee/subawardee in the award period. `data/manual/person_aliases.json`
  (`{"merge": [{"person_ids": [id, …], "reason": …}], "split": [{"id": …, "reason": …}], "strip_orcid": [{"person_id" | "openalex_id": …,
  "orcid": …, "reason": …}], "confirm_orcid": [{"orcid": …, "person_ids"?: […], "name_variants"?: […], "reason": …,
  "confirmed_by": "maintainer"}], "display_name": [{"person_id": …, "display_name": …, "reason": …}]}`) forces
  merges, blocks an id from automatic merging, removes a wrong ORCID (and drops a matching evidence link entirely),
  confirms an ORCID the automatic checks would strip, or sets the name shown for a person ("R. Quinn Sample");
  maintainer corrections go here, with a pointer to public evidence in `reason` (a DOI, an ORCID works or
  employment entry, a DataCite or OpenAlex record), never personal background. Each folded-in id is logged with its
  reason and evidence in `data/review/person_merge_log.json` (gitignored, never published), which also lists refused
  merges and stripped ORCIDs for maintainer review. People carry `merged_ids`, `name_variants`,
  `collaboration_count` and `display_name` (manual entry, else `name`). The published `web/data/people.json` keeps
  only direct collaborators and only the fields the browser reads; the full records, including the ORCID QA fields, go to
  `data/review/people_full.json`.

Every participant carries `institution_status` (`resolved` / `unresolved` / `missing`), and each collaboration has
`has_missing_institution` / `has_unresolved_institution` flags that the browser can filter on.

## Data sources

| source | provides | relationship types | known gaps |
|---|---|---|---|
| OpenAlex (`openalex.py`) | papers with TACC-affiliated authors, and papers whose text mentions TACC; authors with ORCID; institutions with ROR, geo and type | `staff_coauthor`, `resource_acknowledgment` | No API key → shared $0.10/day budget per IP and page paging capped at 10,000 results per query. Full-text search is not limited to acknowledgments, so some "acknowledgment" papers only mention TACC in passing. Datasets mislabelled as papers, arXiv/published duplicates not merged. Bare "TACC" regex for staff detection is unreviewed. ~19% of authorships have no affiliation. |
| NSF Award Search (`nsf.py`) | awards mentioning TACC; PIs/co-PIs; awardee orgs | `tacc_lead_award`, `tacc_partner_award`, `tacc_mention_award` | NSF gives no co-PI institution; build.py fills it only from dated evidence (see above), so ~1/3 of co-PIs stay unknown (by design when evidence is ambiguous). Lead/partner depends on OpenAlex/ORCID coverage: TACC staff without a matched profile or with no TACC-affiliated work in the award period are not recognized. Co-PI lists are the award's current list; every listed co-PI is placed by the award's active period. |
| USAspending (`usaspending.py`) | FFATA subaward reports under the NSF primes | `subaward` | One record per report, so amounts across reports double-count. Subawards under mention-only primes don't imply TACC money. Mojibake descriptions; foreign country codes null. |
| NIH RePORTER (`nih.py`) | NIH projects mentioning TACC | `tacc_lead_award`, `tacc_partner_award`, `tacc_mention_award` | Only 12 awards. Amounts cover matched years only. Co-PI institutions come only from dated evidence, so an award whose contact PI has no usable OpenAlex/ORCID match stays `tacc_mention_award` even if TACC leads it. Abstracts are cut to 400 characters, like NSF. |
| DataCite (`datacite.py`) | DesignSafe DOIs (client `tdl.tacc`) and DOIs with TACC-affiliated creators | `tacc_hosted_data`, `staff_coauthor` | DesignSafe creators almost never have affiliations (most `missing`). Affiliation query misses records that only say "TACC". Some untyped names (GitHub handles) became fake people. |
| ACCESS allocations (`access_allocations.py`) | current allocations on TACC resources (Stampede2/3, Ranch, Jetstream, …) with PI and PI institution | `tacc_resource_allocation` | Current allocations only — no history, and Frontera/Lonestar/Vista/Corral are not allocated through ACCESS. Name-based person ids. Placeholder resource amounts. |
| Grants.gov (`grants_gov.py`) | open/forecasted solicitations relevant to TACC-type work | `funding_opportunity` | Not collaborations; no people/institutions. "research infrastructure" keyword is noisy (mostly NIH center grants). |
| ROR (in `build_institutions.py`) | affiliation-string → ROR matching; names, geo, types, parents, websites for ROR ids | — | Only the API's `chosen` match is accepted, and only if it passes the word-overlap and more-specific-name checks. Research institutes without a ROR record (Oden, SCI) are aliased to the parent university. The word check is a heuristic: a raw string that merely shares a city or distinctive word with a wrong organization can still pass. ROR parent links include founder/membership links, so "any parent educational" needs manual overrides (see above). |
| ORCID (`affiliation_evidence.py`, `orcid_names.py`) | employment spans for award PIs/co-PIs; registered names of every ORCID and public activities/works for plausibility checks | — | All ~22.5k ORCIDs are name-checked; activities (works + affiliations) only for staff/PI ORCIDs, works only for ORCIDs needing hard evidence. The hard-evidence rule costs recall: ~670 ORCIDs seen only under initials on large collaborations (works overlap cannot be checked on truncated author lists), and correct ORCIDs of people listed under a nickname, middle name, changed name or romanized/English given name, are stripped unless evidence or `confirm_orcid` keeps them. The activities check runs before co-PI institutions exist. A full-name record merged in later does not restore an ORCID stripped earlier. |

## Browser

`web/` is static: `index.html`, `style.css`, `data.js` (pure filtering/sorting/routing logic, testable with node) and
`app.js` (DOM). Tabs per record type, faceted filters (relationship type, institution type, country, source,
missing/unresolved institution flags), search, and detail panels for collaborations, people (timeline with the
institution listed on each record, or the inferred one marked "inferred" for award participants the funder does not
place) and institutions. State lives in the URL hash, so views can be bookmarked.
People are shown by `display_name` (falling back to `name`); person search covers id, display name, name, name
variants, ORCID, OpenAlex id and other ids. Participant rows in collaboration and institution views show the name as
listed on that record.

## Data provenance: public sources only

Every record in this dataset comes from a publicly accessible source: open web APIs that need no login, plus one
free API key (OpenAlex) that raises the request budget and lifts the 10,000-result paging cap; it unlocks no
non-public data. No TACC-internal,
UT Austin-internal or otherwise non-public data is used. No TACC-owned website (tacc.utexas.edu, texascale.org, …)
is crawled: their robots.txt bans automated agents.

| source | endpoint(s) called | access | collected | not collected | terms (as published by the provider) |
|---|---|---|---|---|---|
| OpenAlex | `https://api.openalex.org/works`, `/institutions` (`openalex.py`); `/authors`, `/works` (`affiliation_evidence.py`) | public; optional free API key in `OPENALEX_API_KEY` (budget and paging only; never stored in clear or logged; `affiliation_evidence.py` cache keys exclude it, `openalex.py` cache keys hash the URL including it, so a run with another key refetches) | work id, DOI, title, date, venue, authorships (names, ORCIDs, institutions, raw affiliation strings); institution names, ROR ids, geo, type; author affiliation years for award PIs/co-PIs | abstracts and full text; contact details in affiliation strings are scrubbed (`[contact removed]`) | CC0 |
| ROR | `https://api.ror.org/v2/organizations?affiliation=…`, `/v2/organizations/<id>` (`build_institutions.py`) | public, no auth | organization names, types, parents, websites, locations | — | CC0; location data from GeoNames, CC BY 4.0 |
| ORCID public API | `https://pub.orcid.org/v3.0/<orcid>/employments` (`affiliation_evidence.py`); `/personal-details`, `/activities`, `/works` (`orcid_names.py`) | public, no auth | registered and other names; employment spans; works DOIs and affiliations (for a subset) | `/person` and `/email` are never requested; emails, handles and phone numbers typed into other names are dropped | public data released by ORCID under CC0 |
| DataCite | `https://api.datacite.org/dois` (`datacite.py`) | public, no auth | DesignSafe (client `tdl.tacc`) DOIs and DOIs with TACC-affiliated creators: title, description, creators, contributors, affiliations, name identifiers | contact details in names and text are scrubbed | metadata CC0 |
| NSF Award Search | `https://api.nsf.gov/services/v1/awards.json`, `/awards/<number>.json` (`nsf.py`) | public, no auth | award title, dates, amounts, awardee, PI/co-PI names and NSF ids, program, abstract (first 400 characters) | PI email: read only to tell a TACC address apart, never stored in normalized or web data | US federal government public data |
| NIH RePORTER | `https://api.reporter.nih.gov/v2/projects/search` (`nih.py`) | public, no auth | project title, dates, amounts, organization, PIs and profile ids, abstract (first 400 characters) | contact details scrubbed | US federal government public data; abstract reuse terms not confirmed, hence the excerpt |
| USAspending | `https://api.usaspending.gov/api/v2/search/spending_by_award/`, `/spending_by_award_count/` (`usaspending.py`) | public, no auth | FFATA subaward reports under the NSF primes: subawardee, date, amount, description (400 characters), location | — | US federal government public data |
| Grants.gov | `https://api.grants.gov/v1/api/search2`, `/fetchOpportunity` (`grants_gov.py`) | public, no auth | open/forecasted opportunities: title, agency, dates, synopsis | agency contact emails: detail responses are not cached, and the kept text is scrubbed | US federal government public data; reuse terms not formally verified |
| ACCESS allocations | `https://allocations.access-ci.org/current-projects.json` (`access_allocations.py`) | public, no auth | current allocations on TACC resources: title, abstract (400 characters), PI name, PI institution, resources | contact details scrubbed | no reuse terms are stated for this endpoint |

**Auditability.**
- Every API response is cached under `data/raw/<source>/`, keyed by a hash of the request (method, URL, body;
  the OpenAlex API key is never written to disk in clear), so each run is reproducible and every record can be traced to the exact
  response it came from. The one exception is the Grants.gov detail endpoint, which is fetched live and not cached
  because its responses carry agency contact emails, so those records can be re-checked only by re-fetching.
- Every collaboration and person carries `sources[]` with the source, the source's record id, a URL (where the
  source has one) and the retrieval time. Institutions from a source carry `sources[]` too; institutions created
  only from an affiliation string have an empty `sources[]` and are traceable through the collaborations that list them; each normalized file states its `query_description`.
- Hand curation (`data/manual/`, `data/institution_overrides.json`) records only decisions with a pointer to public
  evidence (a DOI, an ORCID record, a DataCite or OpenAlex record, Crossref metadata looked up by hand, an
  NSF/NIH/USAspending award record, a ROR id, an organization's public website).
- Raw caches (`data/raw/`), evidence files (`data/evidence/`) and review files (`data/review/`) are gitignored and
  never published.

## Privacy

**Purpose.** To show which organizations and people have worked with TACC, through public records of papers,
awards, datasets and allocations.

**What is shown.** For each collaboration, the participants as the source lists them. A person's location is
always the institution listed on that specific collaboration record (or, for award participants the funder does not
place, an institution inferred from dated public evidence in the award period and marked "inferred"); it is never
a tracked personal location. Person pages exist only for direct collaborators: authors and co-authors on records
with a TACC-affiliated author, PIs/co-PIs of TACC-led or TACC-partner awards, and allocation PIs. Everyone else
(authors who only acknowledged TACC resources, creators of datasets hosted at TACC, PIs of awards that only
mention TACC) appears only as a name on each record, with no person id in the published files, so their records
are not linked into a profile.

**Potential minors.** Anyone listed at a secondary school (high school, secondary or middle school, school
district, preparatory school, gymnasium, lycée, …) on any record is treated as a potential minor: on every record
they appear only as their listed name and the school (the school part of the affiliation, without town or
postcode). No person page, person id, role, evidence or other affiliation is published for them, and
`check_publishable.py` fails if a published record shows more.

**Coordinates in descriptions.** Coordinates in record titles and descriptions (for example a dataset's sensor
site) are rounded to two decimals (about 1 km) in the published files.

**What is not published.** Email addresses and phone numbers (never stored); personal addresses and GPS
coordinates listed as affiliations (`[address removed]`) and common street-level patterns in affiliations; the person merge
log and the ORCID QA fields (`data/review/`); NSF's current-email-domain flag; raw API caches and evidence files.

**Corrections and removal.** Open an issue on this repository's issue tracker. Identity corrections (merge,
split, remove a wrong ORCID, display name) are made in `data/manual/person_aliases.json` with a pointer to public
evidence, and the site is rebuilt. There is no dedicated suppression list yet: removing a person or record entirely
currently needs a change to the build, followed by a rebuild and republish. Copies of the data made before a
correction cannot be recalled.

## License

- **Code** (`harvest/`, `scripts/`, and the `.html`/`.js`/`.css` files in `web/`): MIT, see `LICENSE`. Copyright The
  University of Texas at Austin. `LICENSE` does not cover the JSON data files (`data/`, `web/data/`).
- **Data** (`web/data/`, `data/`): this project claims no copyright in the data and grants no license for it. The
  data comes from the sources listed above, and each source's own terms apply. When you reuse it, credit the sources:
  - OpenAlex (CC0), ROR (CC0), DataCite metadata (CC0), ORCID public data (CC0): attribution is not required but
    requested by the providers.
  - **Geographic coordinates: © GeoNames, CC BY 4.0, via ROR.** This attribution is required.
  - NSF Award Search, NIH RePORTER, USAspending.gov, Grants.gov: US federal government data.
  - ACCESS allocations (allocations.access-ci.org): no reuse terms stated.
- This is not an official product of TACC or The University of Texas at Austin, and no endorsement by them or by any
  data provider is implied.
