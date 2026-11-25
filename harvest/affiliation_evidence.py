"""Dated affiliation evidence for NSF/NIH award PIs and co-PIs -> data/evidence/affiliation_evidence.json.

For each person who is pi/co_pi in data/normalized/nsf.json or nih.json:
  1. search OpenAlex authors by name and score candidates by name compatibility, institution overlap with
     the person's awards (awardee, TACC, UT Austin; dated to the award period), co-authorship with other
     participants of the same awards, and works_count; ambiguous people are left unmatched, and so are matches
     with no affiliation during any award period at the awardee, a subawardee, or in the awardee's country;
  2. turn the matched author's affiliations[] (institution + years with works) into dated spans;
  3. if an ORCID is known, add employment spans from the ORCID public API.

build.py uses these spans as dated evidence for "institution at the time" of each award.

Run: python3 harvest/affiliation_evidence.py   (needs OPENALEX_API_KEY set in the environment)
"""

import collections
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import common

OPENALEX_BASE = "https://api.openalex.org"
ORCID_EMPLOYMENTS_URL = "https://pub.orcid.org/v3.0/{orcid}/employments"
OPENALEX_CACHE_DIRECTORY = common.RAW_DIRECTORY / "affiliation_evidence_openalex"
ORCID_CACHE_DIRECTORY = common.RAW_DIRECTORY / "affiliation_evidence_orcid"
OUTPUT_PATH = common.PROJECT_ROOT / "data" / "evidence" / "affiliation_evidence.json"
INPUT_SOURCES = ["nsf", "nih"]
PERSON_ROLES = {"pi", "co_pi"}
CANDIDATES_PER_SEARCH = 25
# Stop issuing new OpenAlex requests below this remaining daily budget, so other harvesters still have room.
OPENALEX_BUDGET_RESERVE_USD = 0.25
MAX_ATTEMPTS = 6
AWARD_YEAR_TOLERANCE = 1
# OpenAlex affiliation years are years with at least one work; people do not publish every year.
MAX_YEAR_GAP_WITHIN_SPAN = 2
TACC_INSTITUTION_ID = f"ror:{common.TACC_ROR_ID}"
UT_AUSTIN_INSTITUTION_ID = f"ror:{common.UT_AUSTIN_ROR_ID}"
TACC_NAME_PATTERN = re.compile(r"texas advanced computing center|\bTACC\b", re.IGNORECASE)
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "former"}
# Extra words that make a different institution when added to a name ("Indiana University" vs
# "Indiana State University"), so subset matching must not ignore them.
DISTINGUISHING_INSTITUTION_WORDS = {"state", "tech", "technology", "polytechnic", "medical", "health", "community",
                                    "southern", "northern", "eastern", "western", "central", "christian", "baptist"}
# TACC was founded in 2001; earlier OpenAlex years "at TACC" come from misattributed works.
TACC_FOUNDED_YEAR = 2001
INSTITUTION_STOPWORDS = {"the", "of", "at", "and", "in", "for", "inc", "main", "campus"}
NICKNAME_GROUPS = [
    {"daniel", "dan", "danny"}, {"david", "dave"}, {"william", "will", "bill", "billy", "liam"},
    {"robert", "rob", "bob", "bobby", "robbie"}, {"james", "jim", "jimmy", "jamie"},
    {"michael", "mike", "mick"}, {"thomas", "tom", "tommy"}, {"christopher", "chris"},
    {"steven", "stephen", "steve"}, {"joseph", "joe", "joey"}, {"edward", "ed", "eddie", "ted"},
    {"kenneth", "ken", "kenny"}, {"katherine", "catherine", "kathryn", "kate", "katie", "kathy", "cathy"},
    {"jennifer", "jen", "jenny"}, {"matthew", "matt"}, {"nicholas", "nick", "nicolas"},
    {"anthony", "tony"}, {"andrew", "andy", "drew"}, {"gregory", "greg"}, {"jeffrey", "jeff", "geoffrey", "geoff"},
    {"richard", "rick", "rich", "dick"}, {"samuel", "sam"}, {"benjamin", "ben"},
    {"alexander", "alex", "alexandra", "alexandre"}, {"patricia", "pat", "patty", "trish"}, {"patrick", "pat"},
    {"elizabeth", "liz", "beth", "betsy"}, {"susan", "sue", "suzanne"}, {"ronald", "ron"}, {"donald", "don"},
    {"timothy", "tim"}, {"jonathan", "jon", "john", "johnny", "jack"}, {"lawrence", "larry", "laurence"},
    {"charles", "charlie", "chuck"}, {"philip", "phillip", "phil"}, {"douglas", "doug"},
    {"frederick", "fred"}, {"raymond", "ray"}, {"walter", "walt"}, {"eugene", "gene"},
    {"harold", "hal"}, {"albert", "al"}, {"alan", "allan", "allen", "al"}, {"margaret", "maggie", "peggy"},
    {"deborah", "debra", "deb", "debbie"}, {"rebecca", "becky"}, {"victoria", "vicki", "vicky"},
    {"nancy", "nan"}, {"barbara", "barb"}, {"carlos", "charles"}, {"jerome", "jerry"}, {"gerald", "jerry"},
    {"joshua", "josh"}, {"zachary", "zach"}, {"vincent", "vince"}, {"leonard", "len", "leo"},
]
# Above this many name-compatible authors a name is "common": equally supported candidates are then
# treated as different people (ambiguous) instead of split profiles of one person.
MAX_CANDIDATES_FOR_SPLIT_PROFILES = 5
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


class OpenAlexBudgetExhausted(Exception):
    pass


def redact(url):
    return re.sub(r"api_key=[^&]+", "api_key=REDACTED", url)


def cache_path_for(directory, url):
    return directory / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.json"


class OpenAlexClient:
    """GET with a cache keyed on the URL WITHOUT the api key, and a daily-budget guard from response headers."""

    def __init__(self):
        self.api_key = os.environ.get("OPENALEX_API_KEY")
        self.remaining_usd = None
        self.num_requests = 0

    def get(self, path, params):
        public_url = common.build_url(f"{OPENALEX_BASE}/{path}", params)
        cache_path = cache_path_for(OPENALEX_CACHE_DIRECTORY, public_url)
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        if self.remaining_usd is not None and self.remaining_usd < OPENALEX_BUDGET_RESERVE_USD:
            raise OpenAlexBudgetExhausted(f"remaining ${self.remaining_usd:.4f} below reserve")
        request_url = common.build_url(f"{OPENALEX_BASE}/{path}", {**params, "api_key": self.api_key}
                                       if self.api_key else params)
        payload = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                request = urllib.request.Request(request_url, headers={"User-Agent": common.USER_AGENT,
                                                                       "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    remaining = response.headers.get("X-RateLimit-Remaining-USD")
                    if remaining is not None:
                        self.remaining_usd = float(remaining)
                break
            except urllib.error.HTTPError as error:
                # 429 is usually the per-second limit, not the daily budget; only the header tells them apart.
                if error.code == 429 and self.remaining_usd is not None and \
                        self.remaining_usd < OPENALEX_BUDGET_RESERVE_USD:
                    raise OpenAlexBudgetExhausted("HTTP 429 with daily budget below reserve") from None
                if error.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if error.code == 429:
                    body = redact(error.read().decode("utf-8", "ignore"))[:200]
                    raise OpenAlexBudgetExhausted(f"HTTP 429 after {MAX_ATTEMPTS} attempts: {body}") from None
                raise RuntimeError(f"OpenAlex HTTP {error.code} for {redact(request_url)}") from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAlex network error {error!r} for {redact(public_url)}") from None
        self.num_requests += 1
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload))
        time.sleep(0.12)
        return payload


def fetch_orcid_employments(orcid):
    url = ORCID_EMPLOYMENTS_URL.format(orcid=orcid)
    cache_path = cache_path_for(ORCID_CACHE_DIRECTORY, url)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    request = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(f"[affiliation_evidence] ORCID HTTP {error.code} for {orcid}")
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload))
    time.sleep(0.1)
    return payload


# ---------- names ----------

def name_tokens(name):
    return [token for token in common.normalize_name(name).split() if token not in NAME_SUFFIXES]


def search_query_for(name):
    """'Jane Q Doe' -> 'Jane Doe': middle initials make OpenAlex search stricter, not better."""
    cleaned = re.sub(r"\(.*?\)", " ", name)
    words = [word for word in cleaned.replace(",", " ").split()
             if len(word.strip(".")) > 1 and common.normalize_name(word) not in NAME_SUFFIXES]
    return " ".join(words)


def first_names_compatible(first_a, first_b):
    """2 = same name or nickname, 1 = initial only, 0 = incompatible."""
    if first_a == first_b:
        return 2
    if len(first_a) == 1 or len(first_b) == 1:
        return 1 if first_a[0] == first_b[0] else 0
    if any(first_a in group and first_b in group for group in NICKNAME_GROUPS):
        return 2
    return 0


def name_compatibility(person_name, candidate_names):
    person = name_tokens(person_name)
    if len(person) < 2:
        return 0
    best = 0
    for candidate_name in candidate_names:
        candidate = name_tokens(candidate_name)
        if len(candidate) < 2:
            continue
        same_surname = person[-1] == candidate[-1] or (
            person[-1] in candidate[1:] and candidate[-1] in person[1:])
        if not same_surname:
            continue
        best = max(best, first_names_compatible(person[0], candidate[0]))
    return best


# ---------- institutions ----------

def institution_name_tokens(name):
    return {token for token in common.normalize_name(name).split() if token not in INSTITUTION_STOPWORDS}


def institution_names_match(name_a, name_b):
    tokens_a, tokens_b = institution_name_tokens(name_a), institution_name_tokens(name_b)
    if not tokens_a or not tokens_b:
        return False
    smaller, larger = sorted([tokens_a, tokens_b], key=len)
    return smaller == larger or (len(smaller) >= 2 and smaller <= larger
                                 and not (larger - smaller) & DISTINGUISHING_INSTITUTION_WORDS)


def openalex_institution_id(institution):
    ror_url = institution.get("ror") or ""
    if ror_url:
        return "ror:" + ror_url.rstrip("/").rsplit("/", 1)[-1]
    return "openalex:" + (institution.get("id") or "").rsplit("/", 1)[-1]


def plausible_institution_matches(affiliation_institution_id, affiliation_name, plausible):
    if plausible["institution_id"] and plausible["institution_id"] == affiliation_institution_id:
        return True
    return bool(plausible["name"]) and institution_names_match(affiliation_name, plausible["name"])


# ---------- inputs ----------

def year_of(date_text):
    return int(date_text[:4]) if date_text else None


def load_subawardees_by_prime():
    """prime award collaboration id -> [{institution_id, name}] of USAspending subawardees."""
    normalized = json.loads((common.NORMALIZED_DIRECTORY / "usaspending.json").read_text())
    subawardees = collections.defaultdict(list)
    for collaboration in normalized["collaborations"]:
        prime_id = (collaboration.get("identifiers") or {}).get("prime_award_collaboration_id")
        for participant in collaboration["participants"]:
            if prime_id and participant["role"] == "subawardee_org":
                subawardees[prime_id].append({"institution_id": participant.get("institution_id"),
                                              "name": participant.get("institution_name_as_listed")})
    return subawardees


def load_award_people():
    """person_id -> {name, awards:[{collaboration_id, role, start_year, end_year, awardee_name,
    awardee_institution_id, awardee_country_code, subawardees, peer_person_ids}]}

    Awards starting before TACC_FOUNDED_YEAR are skipped: build.py drops them, so they must not support a match."""
    people = {}
    subawardees_by_prime = load_subawardees_by_prime()
    for source in INPUT_SOURCES:
        normalized = json.loads((common.NORMALIZED_DIRECTORY / f"{source}.json").read_text())
        names_by_id = {person["id"]: person["name"] for person in normalized["people"]}
        country_by_institution_id = {institution["id"]: institution.get("country_code")
                                     for institution in normalized["institutions"]}
        for collaboration in normalized["collaborations"]:
            start_year = year_of(collaboration.get("start_date")) or collaboration.get("year")
            end_year = year_of(collaboration.get("end_date")) or start_year
            if start_year is not None and start_year < TACC_FOUNDED_YEAR:
                continue
            awardees = [participant for participant in collaboration["participants"]
                        if participant["role"] == "awardee_org"]
            person_participants = [participant for participant in collaboration["participants"]
                                   if participant["role"] in PERSON_ROLES and participant["person_id"]]
            for participant in person_participants:
                person = people.setdefault(participant["person_id"], {
                    "name": names_by_id.get(participant["person_id"], participant["name_as_listed"]),
                    "awards": []})
                for awardee in awardees or [None]:
                    awardee_id = awardee["institution_id"] if awardee else None
                    person["awards"].append({
                        "collaboration_id": collaboration["id"],
                        "role": participant["role"],
                        "start_year": start_year,
                        "end_year": end_year,
                        "awardee_name": awardee["institution_name_as_listed"] if awardee else None,
                        "awardee_institution_id": awardee_id,
                        # NSF only funds US organizations; unresolved NSF awardees have no institution record.
                        "awardee_country_code": country_by_institution_id.get(awardee_id)
                                                or ("US" if source == "nsf" else None),
                        "subawardees": subawardees_by_prime.get(collaboration["id"], []),
                        "peer_person_ids": [other["person_id"] for other in person_participants
                                            if other["person_id"] != participant["person_id"]],
                    })
    return people


def plausible_institutions(person):
    """Awardees of the person's awards plus TACC and UT Austin, each with the award window."""
    plausible = []
    for award in person["awards"]:
        if award["start_year"] is None:
            continue
        window = (award["start_year"] - AWARD_YEAR_TOLERANCE, award["end_year"] + AWARD_YEAR_TOLERANCE)
        if award["awardee_name"]:
            plausible.append({"institution_id": award["awardee_institution_id"], "name": award["awardee_name"],
                              "window": window, "is_lead_pi_awardee": award["role"] == "pi"})
        plausible.append({"institution_id": TACC_INSTITUTION_ID, "name": "Texas Advanced Computing Center",
                          "window": window, "is_lead_pi_awardee": False})
        plausible.append({"institution_id": UT_AUSTIN_INSTITUTION_ID, "name": "University of Texas at Austin",
                          "window": window, "is_lead_pi_awardee": False})
    return plausible


# ---------- candidate scoring ----------

def candidate_signals(person, candidate):
    candidate_names = [candidate.get("display_name") or ""] + (candidate.get("display_name_alternatives") or [])
    signals = {"name_score": name_compatibility(person["name"], candidate_names),
               "dated_lead_awardee_overlap": False, "dated_overlap": False, "undated_overlap": False,
               "matched_institutions": set()}
    plausible = plausible_institutions(person)
    for affiliation in candidate.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        affiliation_id = openalex_institution_id(institution)
        affiliation_name = institution.get("display_name") or ""
        years = affiliation.get("years") or []
        for plausible_institution in plausible:
            if not plausible_institution_matches(affiliation_id, affiliation_name, plausible_institution):
                continue
            signals["undated_overlap"] = True
            signals["matched_institutions"].add(affiliation_name)
            window_start, window_end = plausible_institution["window"]
            if any(window_start <= year <= window_end for year in years):
                signals["dated_overlap"] = True
                if plausible_institution["is_lead_pi_awardee"]:
                    signals["dated_lead_awardee_overlap"] = True
    return signals


def confidence_from_signals(signals, num_coauthored_works, is_only_full_name_candidate):
    name_score = signals["name_score"]
    if name_score == 0:
        return "none"
    if signals["dated_lead_awardee_overlap"] or (name_score == 2 and signals["dated_overlap"]):
        return "high"
    if num_coauthored_works and signals["dated_overlap"]:
        return "high"
    if name_score == 2 and (signals["undated_overlap"] or num_coauthored_works):
        return "medium"
    if name_score == 1 and signals["dated_overlap"]:
        return "medium"
    if name_score == 2 and is_only_full_name_candidate:
        return "low"
    return "none"


def describe_signals(signals, num_coauthored_works):
    parts = [{2: "name full/nickname match", 1: "name initial match"}[signals["name_score"]]]
    if signals["dated_lead_awardee_overlap"]:
        parts.append("affiliated with lead-PI awardee during award period")
    elif signals["dated_overlap"]:
        parts.append("affiliated with awardee/TACC/UT Austin during award period")
    elif signals["undated_overlap"]:
        parts.append("affiliated with awardee/TACC/UT Austin (outside award period)")
    if num_coauthored_works:
        parts.append(f"{num_coauthored_works} works co-authored with other award participants")
    if signals["matched_institutions"]:
        parts.append("institutions: " + ", ".join(sorted(signals["matched_institutions"])[:4]))
    return "; ".join(parts)


def homonym_guard_failure(person, profiles):
    """None if, for at least one of the person's awards, a candidate profile has an affiliation during the award period
    (+-AWARD_YEAR_TOLERANCE) at the awardee, at a USAspending subawardee under that prime, or in the awardee's
    country; else the reason the match is dropped. Stops same-name researchers elsewhere from being taken for an award
    PI (e.g. a US award PI matched to a same-name author at a university abroad)."""
    for award in person["awards"]:
        if award["start_year"] is None:
            continue
        window_start, window_end = award["start_year"] - AWARD_YEAR_TOLERANCE, award["end_year"] + AWARD_YEAR_TOLERANCE
        accepted = [{"institution_id": award["awardee_institution_id"], "name": award["awardee_name"]}] + award["subawardees"]
        for profile in profiles:
            for affiliation in profile.get("affiliations") or []:
                if not any(window_start <= year <= window_end for year in affiliation.get("years") or []):
                    continue
                institution = affiliation.get("institution") or {}
                if award["awardee_country_code"] and institution.get("country_code") == award["awardee_country_code"]:
                    return None
                if any(plausible_institution_matches(openalex_institution_id(institution), institution.get("display_name") or "",
                                                     accepted_institution) for accepted_institution in accepted):
                    return None
    return ("homonym guard: no affiliation during any award period at the awardee, a subawardee, "
            "or in the awardee's country")


def count_coauthored_works(client, candidate_id, peer_author_ids):
    if not peer_author_ids:
        return 0
    peers = "|".join(sorted(peer_author_ids)[:50])
    payload = client.get("works", {"filter": f"authorships.author.id:{candidate_id},authorships.author.id:{peers}",
                                   "per-page": 1, "select": "id"})
    return payload.get("meta", {}).get("count", 0)


def short_author_id(author):
    return (author.get("id") or "").rsplit("/", 1)[-1]


def match_person(client, person, matched_author_by_person, use_coauthors):
    """Return (chosen candidate or None, confidence, reason, supporting split-profile candidates)."""
    query = search_query_for(person["name"])
    if len(query.split()) < 2:
        return None, "none", "name too short to search", []
    payload = client.get("authors", {"search": query, "per-page": CANDIDATES_PER_SEARCH})
    candidates = [candidate for candidate in payload.get("results", [])
                  if name_compatibility(person["name"], [candidate.get("display_name") or ""]
                                        + (candidate.get("display_name_alternatives") or [])) > 0]
    if not candidates:
        return None, "none", f"no name-compatible OpenAlex author for search {query!r}", []

    peer_author_ids = set()
    if use_coauthors:
        for award in person["awards"]:
            for peer_person_id in award["peer_person_ids"]:
                peer_match = matched_author_by_person.get(peer_person_id)
                if peer_match:
                    peer_author_ids.add(peer_match)

    num_full_name_candidates = sum(
        1 for candidate in candidates if candidate_signals(person, candidate)["name_score"] == 2)
    scored = []
    for candidate in candidates:
        signals = candidate_signals(person, candidate)
        num_coauthored = 0
        if peer_author_ids and signals["name_score"] > 0 and not signals["dated_overlap"]:
            num_coauthored = count_coauthored_works(client, short_author_id(candidate),
                                                    peer_author_ids - {short_author_id(candidate)})
        confidence = confidence_from_signals(signals, num_coauthored,
                                             is_only_full_name_candidate=num_full_name_candidates == 1)
        scored.append((CONFIDENCE_RANK[confidence], candidate.get("works_count") or 0, confidence, candidate,
                       describe_signals(signals, num_coauthored), signals))
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    best_rank, _works, best_confidence, best_candidate, best_reason, _signals = scored[0]
    if best_rank == 0:
        return None, "none", f"{len(candidates)} name-compatible candidates, none supported by institution evidence", []

    same_rank = [entry for entry in scored[1:] if entry[0] == best_rank]
    best_orcid = best_candidate.get("orcid")
    conflicting = [entry for entry in same_rank if entry[3].get("orcid") and best_orcid
                   and entry[3]["orcid"] != best_orcid]
    if conflicting:
        return None, "none", (f"ambiguous: {len(conflicting) + 1} {best_confidence}-confidence candidates "
                              f"with different ORCIDs"), []
    supported_ties = [entry for entry in same_rank if entry[0] >= CONFIDENCE_RANK["medium"]]
    if supported_ties and len(candidates) > MAX_CANDIDATES_FOR_SPLIT_PROFILES:
        return None, "none", (f"ambiguous: common name ({len(candidates)} name-compatible authors), "
                              f"{len(supported_ties) + 1} equally supported"), []
    # For uncommon names, other candidates with institution support are split OpenAlex profiles of the same
    # person (one staff member has 4). Name-only (low) ties are not, since nothing ties them together.
    split_profiles = [entry[3] for entry in scored[1:]
                      if entry[0] >= CONFIDENCE_RANK["medium"] and entry[5]["undated_overlap"]
                      and len(candidates) <= MAX_CANDIDATES_FOR_SPLIT_PROFILES
                      and not (entry[3].get("orcid") and best_orcid and entry[3]["orcid"] != best_orcid)]
    reason = f"search {query!r}: {best_reason}; works_count {best_candidate.get('works_count')}"
    if split_profiles:
        reason += f"; plus {len(split_profiles)} split profile(s) with the same support"
    return best_candidate, best_confidence, reason, split_profiles


# ---------- affiliation spans ----------

def year_runs(years):
    runs = []
    for year in sorted(set(years)):
        if runs and year - runs[-1][-1] <= MAX_YEAR_GAP_WITHIN_SPAN:
            runs[-1].append(year)
        else:
            runs.append([year])
    return runs


def openalex_affiliation_spans(author):
    author_id = short_author_id(author)
    spans = []
    for affiliation in author.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        institution_id = openalex_institution_id(institution)
        years = affiliation.get("years") or []
        if institution_id == TACC_INSTITUTION_ID:
            years = [year for year in years if year >= TACC_FOUNDED_YEAR]
        for run in year_runs(years):
            spans.append({
                "institution_id": institution_id,
                "institution_name": institution.get("display_name"),
                "start_year": run[0],
                "end_year": run[-1],
                "years_with_works": run,
                "source": "openalex_author_affiliation_years",
                "source_url": f"{OPENALEX_BASE}/authors/{author_id}",
            })
    return spans


def orcid_date_year(date):
    year = ((date or {}).get("year") or {}).get("value")
    return int(year) if year else None


def orcid_employment_spans(orcid):
    payload = fetch_orcid_employments(orcid)
    if not payload:
        return []
    spans = []
    for group in payload.get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            employment = summary.get("employment-summary") or {}
            organization = employment.get("organization") or {}
            organization_name = organization.get("name")
            department = employment.get("department-name")
            disambiguated = organization.get("disambiguated-organization") or {}
            if TACC_NAME_PATTERN.search(f"{organization_name or ''} {department or ''}"):
                institution_id, institution_name = TACC_INSTITUTION_ID, "Texas Advanced Computing Center"
            elif disambiguated.get("disambiguation-source") == "ROR":
                identifier = disambiguated.get("disambiguated-organization-identifier") or ""
                institution_id = "ror:" + identifier.rstrip("/").rsplit("/", 1)[-1]
                institution_name = organization_name
            else:
                institution_id = f"name:{common.normalize_name(organization_name)}"
                institution_name = organization_name
            spans.append({
                "institution_id": institution_id,
                "institution_name": institution_name,
                "organization_name_as_listed": organization_name,
                "department_name": department,
                "start_year": orcid_date_year(employment.get("start-date")),
                "end_year": orcid_date_year(employment.get("end-date")),
                "source": "orcid_employment",
                "source_url": ORCID_EMPLOYMENTS_URL.format(orcid=orcid),
            })
    return spans


# ---------- main ----------

def build_person_entry(person_id, person, candidate, confidence, reason, split_profiles):
    entry = {"person_id": person_id, "name": person["name"], "openalex_author_id": None,
             "additional_openalex_author_ids": [], "orcid": None, "match_confidence": confidence,
             "match_reason": reason, "affiliations": []}
    if candidate is None:
        return entry
    orcid_url = candidate.get("orcid") or next((profile.get("orcid") for profile in split_profiles
                                               if profile.get("orcid")), None)
    entry["openalex_author_id"] = f"openalex:{short_author_id(candidate)}"
    entry["additional_openalex_author_ids"] = [f"openalex:{short_author_id(profile)}" for profile in split_profiles]
    entry["orcid"] = orcid_url.rsplit("/", 1)[-1] if orcid_url else None
    for author in [candidate] + split_profiles:
        entry["affiliations"].extend(openalex_affiliation_spans(author))
    if entry["orcid"]:
        entry["affiliations"].extend(orcid_employment_spans(entry["orcid"]))
    return entry


def main():
    if not os.environ.get("OPENALEX_API_KEY"):
        print("[affiliation_evidence] OPENALEX_API_KEY is not set; set it in the environment first", file=sys.stderr)
        sys.exit(1)
    client = OpenAlexClient()
    people = load_award_people()
    print(f"[affiliation_evidence] {len(people)} award people (pi/co_pi) from {INPUT_SOURCES}")

    # Pass 1 matches without co-authorship; pass 2 retries the unmatched/weak ones using the authors found
    # for the other participants of their awards.
    results = {}
    matched_author_by_person = {}
    budget_exhausted = False
    for pass_number, use_coauthors in [(1, False), (2, True)]:
        for person_id, person in sorted(people.items()):
            if pass_number == 2 and results.get(person_id, (None, "none"))[1] in ("high", "medium"):
                continue
            try:
                candidate, confidence, reason, split_profiles = match_person(
                    client, person, matched_author_by_person, use_coauthors)
            except OpenAlexBudgetExhausted as error:
                print(f"[affiliation_evidence] OpenAlex budget guard: {error}; stopping new requests")
                budget_exhausted = True
                break
            results[person_id] = (candidate, confidence, reason, split_profiles)
            if candidate is not None and confidence in ("high", "medium"):
                matched_author_by_person[person_id] = short_author_id(candidate)
        print(f"[affiliation_evidence] pass {pass_number}: "
              f"{collections.Counter(result[1] for result in results.values())}; "
              f"{client.num_requests} OpenAlex requests so far, remaining budget ${client.remaining_usd}")
        if budget_exhausted:
            break

    entries = []
    for person_id, person in sorted(people.items()):
        if person_id in results:
            candidate, confidence, reason, split_profiles = results[person_id]
        else:
            candidate, confidence, reason, split_profiles = None, "none", "not searched (OpenAlex budget)", []
        guard_failure = homonym_guard_failure(person, [candidate] + split_profiles) if candidate else None
        if guard_failure:
            candidate, confidence, reason, split_profiles = None, "none", f"{reason}; dropped by {guard_failure}", []
        entries.append(build_person_entry(person_id, person, candidate, confidence, reason, split_profiles))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "generated_at": common.utc_now_iso(),
        "description": ("Dated affiliation spans for NSF/NIH award PIs/co-PIs. OpenAlex spans are runs of years in "
                        "which the author has works listing that institution (gaps <= "
                        f"{MAX_YEAR_GAP_WITHIN_SPAN} years merged); they can be noisy (misattributed works). "
                        f"TACC years before {TACC_FOUNDED_YEAR} are dropped. ORCID spans are self-reported employment; null years mean not stated. Use only "
                        "high/medium matches as evidence."),
        "people": entries,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=1, ensure_ascii=False))
    confidence_counts = collections.Counter(entry["match_confidence"] for entry in entries)
    print(f"[affiliation_evidence] wrote {OUTPUT_PATH}: {len(entries)} people; {dict(confidence_counts)}; "
          f"{sum(1 for entry in entries if entry['orcid'])} with ORCID")


if __name__ == "__main__":
    main()
