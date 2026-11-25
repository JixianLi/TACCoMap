"""Registered names of every ORCID in the dataset (and, for TACC staff / award / allocation people, public works and
affiliations) -> data/evidence/orcid_names.json.

build_people.py keeps an ORCID on a record only if the record's listed name fits the ORCID's registered name, and
for TACC staff / PI ORCIDs only if the ORCID's public record corroborates at least one record (a listed DOI or a
matching affiliation). OpenAlex sometimes attaches a wrong ORCID to an author cluster; this is the check.

Fetched:
  personal-details for every ORCID on a person record, a participation, or an affiliation evidence link;
  activities (works + affiliations) for the subset that is TACC staff or a PI: a participant at TACC on a
  staff_coauthor record, a pi/co_pi on an award, an allocation PI, or an ORCID the affiliation evidence links to an
  NSF/NIH person;
  works for the other ORCIDs with a participation that needs hard evidence (it fails the name check, fits only
  through the nickname table or a given-name-only registration, or the ORCID only appears under initials on large
  collaboration papers) and could be rescued by a works overlap (build_people.works_rescue_precondition: the family
  name fits, the record has a DOI and a complete author list with no other author of that family name).

Only the public endpoints /v3.0/<orcid>/personal-details, /v3.0/<orcid>/activities and /v3.0/<orcid>/works are used;
none returns email addresses (those live under /person and /email, which are never requested). Raw responses are
cached in data/raw/orcid_names/.

Output: {orcid: {"given_names", "family_name", "other_names": [...], "retrieved_at"[, "activities": {
"public_work_count", "dataset_work_dois": [DOIs also present in this dataset], "affiliations": [{"section",
"organization", "ror", "city", "country", "start_year", "end_year"}]}][, "works": {"public_work_count",
"dataset_work_dois"}]} or null when nothing is public}.

Run: python3 harvest/orcid_names.py   (~10 requests/s, ~40 min for ~22k ORCIDs the first time; cached afterwards,
and an interrupted run resumes from the cache)
"""

import collections
import concurrent.futures
import json
import re
import sys
import urllib.error

import build_people
import common

ORCID_API = "https://pub.orcid.org/v3.0"
SOURCE = "orcid_names"
REQUEST_PAUSE_SECONDS = 0.1
PARALLEL_REQUESTS = 6
STAFF_OR_PI_ROLES = {"pi", "co_pi"}
# Some people type an email address, a social handle or a phone number into "other names"; never store those.
CONTACT_DETAIL_PATTERN = re.compile(r"@|\d[\d\s().+-]{6,}")
AFFILIATION_SECTIONS = ["employments", "educations", "qualifications", "invited-positions", "memberships", "services",
                        "distinctions"]


def load_inputs():
    people, collaborations = {}, {}
    for path in sorted(common.NORMALIZED_DIRECTORY.glob("*.json")):
        document = json.loads(path.read_text())
        for person in document.get("people", []):
            people.setdefault(person["id"], person)
        for collaboration in document.get("collaborations", []):
            if collaboration["id"] in collaborations:
                collaborations[collaboration["id"]]["participants"] = (
                    collaborations[collaboration["id"]].get("participants") or []) + (collaboration.get("participants") or [])
            else:
                collaborations[collaboration["id"]] = dict(collaboration)
    evidence_path = common.PROJECT_ROOT / "data" / "evidence" / "affiliation_evidence.json"
    evidence_by_person = ({entry["person_id"]: entry for entry in json.loads(evidence_path.read_text())["people"]}
                          if evidence_path.exists() else {})
    return people, collaborations, evidence_by_person


def is_tacc_participation(participant):
    return participant.get("institution_id") in build_people.TACC_INSTITUTION_IDS or bool(
        build_people.affiliation_evidence.TACC_NAME_PATTERN.search(participant.get("institution_name_as_listed") or ""))


def select_orcids(records_by_orcid):
    """(every ORCID, subset also needing activities)."""
    activities_check = set()
    for orcid, records in records_by_orcid.items():
        for record in records:
            if record["kind"] == "evidence":
                activities_check.add(orcid)
                continue
            if record["kind"] != "participant":
                continue
            collaboration, participant = record["collaboration"], record["participant"]
            relationship_types = set(collaboration.get("relationship_types") or [])
            if (participant.get("role") in STAFF_OR_PI_ROLES or collaboration.get("kind") == "allocation"
                    or ("staff_coauthor" in relationship_types and is_tacc_participation(participant))):
                activities_check.add(orcid)
    return set(records_by_orcid), activities_check


def needs_works(entry, records):
    """Whether an ORCID without fetched activities has a participation needing hard evidence that the works overlap
    could rescue."""
    if not entry or "activities" in entry or not build_people.registered_name_candidates(entry):
        return False
    large_collaboration_only = build_people.initials_only_large_collaboration(records)
    return any((large_collaboration_only or build_people.name_check_failure(record["name"], entry))
               and build_people.works_rescue_precondition(record, entry) for record in records if record["name"])


def fetch_orcid(orcid, section, pause_seconds=REQUEST_PAUSE_SECONDS):
    try:
        return common.fetch_json(f"{ORCID_API}/{orcid}/{section}", source=SOURCE, pause_seconds=pause_seconds)
    except urllib.error.HTTPError as error:
        if error.code in (404, 409, 410):
            return None
        raise


def prefetch(orcids, section):
    """Fill the response cache with PARALLEL_REQUESTS workers; each pauses PARALLEL_REQUESTS * REQUEST_PAUSE_SECONDS
    after a request, so together they stay at about 10 requests/s. Failures are left for the sequential pass, which
    retries them and stops on a persistent error; an interrupted run resumes from the cache."""
    def fetch_quietly(orcid):
        try:
            fetch_orcid(orcid, section, pause_seconds=PARALLEL_REQUESTS * REQUEST_PAUSE_SECONDS)
            return True
        except Exception:
            return False
    ordered = sorted(orcids)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(PARALLEL_REQUESTS) as executor:
        for index_orcid, fetched in enumerate(executor.map(fetch_quietly, ordered), start=1):
            failures += not fetched
            if index_orcid % 1000 == 0:
                print(f"  prefetched {section}: {index_orcid}/{len(ordered)} ({failures} failed)", flush=True)


def text_value(field):
    return (field or {}).get("value") if isinstance(field, dict) else None


def registered_name(personal_details):
    name = (personal_details or {}).get("name") or {}
    other_names = [other.get("content") for other in ((personal_details or {}).get("other-names") or {}).get("other-name") or []
                   if other.get("content") and not CONTACT_DETAIL_PATTERN.search(other["content"])]
    credit_name = text_value(name.get("credit-name"))
    if credit_name:
        other_names.append(credit_name)
    return text_value(name.get("given-names")), text_value(name.get("family-name")), other_names


def date_year(date):
    return int(text_value((date or {}).get("year"))) if text_value((date or {}).get("year")) else None


def activities_summary(activities, dataset_dois):
    affiliations = []
    for section in AFFILIATION_SECTIONS:
        for group in ((activities or {}).get(section) or {}).get("affiliation-group") or []:
            for summary_wrapper in group.get("summaries") or []:
                summary = next(iter(summary_wrapper.values()), None) or {}
                organization = summary.get("organization") or {}
                disambiguated = organization.get("disambiguated-organization") or {}
                ror = None
                if disambiguated.get("disambiguation-source") == "ROR":
                    ror = (disambiguated.get("disambiguated-organization-identifier") or "").rstrip("/").rsplit("/", 1)[-1] or None
                affiliations.append({"section": section.rstrip("s"), "organization": organization.get("name"), "ror": ror,
                                     "city": (organization.get("address") or {}).get("city"),
                                     "country": (organization.get("address") or {}).get("country"),
                                     "start_year": date_year(summary.get("start-date")),
                                     "end_year": date_year(summary.get("end-date"))})
    return {**works_summary((activities or {}).get("works"), dataset_dois), "affiliations": affiliations}


def works_summary(works, dataset_dois):
    """Count and dataset DOIs of a works response (/works, or the works section of /activities)."""
    work_groups = (works or {}).get("group") or []
    work_dois = set()
    for group in work_groups:
        for external_id in ((group.get("external-ids") or {}).get("external-id") or []):
            if (external_id.get("external-id-type") or "").lower() == "doi" and external_id.get("external-id-value"):
                work_dois.add(external_id["external-id-value"].strip().lower().removeprefix("https://doi.org/"))
    return {"public_work_count": len(work_groups), "dataset_work_dois": sorted(work_dois & dataset_dois)}


def main():
    people, collaborations, evidence_by_person = load_inputs()
    records_by_orcid = build_people.collect_orcid_records(people, collaborations, evidence_by_person)
    name_check, activities_check = select_orcids(records_by_orcid)
    dataset_dois = {((collaboration.get("identifiers") or {}).get("doi") or "").lower()
                    for collaboration in collaborations.values()} - {""}
    print(f"[{SOURCE}] {len(name_check)} ORCIDs to check, {len(activities_check)} with works/affiliations")
    prefetch(name_check, "personal-details")
    prefetch(activities_check, "activities")
    output = {}
    counts = collections.Counter()
    for index_orcid, orcid in enumerate(sorted(name_check), start=1):
        retrieved_at = common.utc_now_iso()
        given_names, family_name, other_names = registered_name(fetch_orcid(orcid, "personal-details"))
        entry = {"given_names": given_names, "family_name": family_name, "other_names": other_names,
                 "retrieved_at": retrieved_at}
        if orcid in activities_check:
            entry["activities"] = activities_summary(fetch_orcid(orcid, "activities"), dataset_dois)
        has_public_content = given_names or family_name or other_names or entry.get("activities", {}).get("public_work_count") \
            or entry.get("activities", {}).get("affiliations")
        output[orcid] = entry if has_public_content else None
        counts["public" if given_names or family_name or other_names else "name_not_public"] += 1
        if index_orcid % 2500 == 0:
            print(f"  {index_orcid}/{len(name_check)} {dict(counts)}", flush=True)
    works_check = {orcid for orcid in name_check if needs_works(output[orcid], records_by_orcid[orcid])}
    print(f"[{SOURCE}] {len(works_check)} ORCIDs with a participation needing hard evidence: fetching works")
    prefetch(works_check, "works")
    for orcid in sorted(works_check):
        output[orcid]["works"] = works_summary(fetch_orcid(orcid, "works"), dataset_dois)
        counts["works_fetched"] += 1
    output_path = common.PROJECT_ROOT / "data" / "evidence" / "orcid_names.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=1, ensure_ascii=False, sort_keys=True))
    print(f"[{SOURCE}] wrote {output_path}: {dict(counts)}")


if __name__ == "__main__":
    sys.exit(main())
