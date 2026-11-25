"""Harvest NIH RePORTER awards that mention TACC -> data/normalized/nih.json.

Run: python3 harvest/nih.py
"""

import collections
import re

import common

SOURCE = "nih"
SEARCH_URL = "https://api.reporter.nih.gov/v2/projects/search"
PAGE_SIZE = 500
# Like NSF: RePORTER abstracts are investigators' text whose reuse terms are unconfirmed, so only a short excerpt.
DESCRIPTION_LENGTH = 400
UT_AUSTIN_ORG_NAME = "UNIVERSITY OF TEXAS AT AUSTIN"
TACC_FULL_NAME_PATTERN = re.compile(r"texas\s+advanced\s+computing\s+center", re.IGNORECASE)
TACC_ACRONYM_PATTERN = re.compile(r"\bTACC\b")
COMPUTING_CONTEXT_PATTERN = re.compile(
    r"supercomput|high[- ]performance|\bHPC\b|computing|cluster|Stampede|Frontera|Lonestar|"
    r"Ranger|Maverick|Corral|Jetstream|Vista|cyberinfrastructure", re.IGNORECASE)
COMPUTING_CONTEXT_WINDOW = 200
# Phrases where TACC itself (not just its machines) does the work. Deliberately narrow:
# "we will use TACC resources" stays a mention.
TACC_PERFORMING_PATTERN = re.compile(
    r"(TACC|Texas Advanced Computing Center)(['’]s)?\s+(will|staff|team|personnel|researchers)\b"
    r"|\b(at|within)\s+(the\s+)?(TACC|Texas Advanced Computing Center),?\s+we\b"
    r"|\b(led|performed|conducted)\s+by\s+(the\s+)?(TACC|Texas Advanced Computing Center)",
    re.IGNORECASE)

SEARCHES = [
    {
        "label": "full_name",
        "criteria": {"advanced_text_search": {
            "operator": "and", "search_field": "projecttitle,terms,abstracttext",
            "search_text": "\"Texas Advanced Computing Center\""}},
    },
    {
        "label": "acronym_ut_austin",
        "criteria": {
            "advanced_text_search": {
                "operator": "and", "search_field": "projecttitle,terms,abstracttext", "search_text": "TACC"},
            "org_names": [UT_AUSTIN_ORG_NAME],
        },
    },
]

QUERY_DESCRIPTION = (
    "NIH RePORTER v2 projects/search: (1) advanced_text_search \"Texas Advanced Computing Center\" "
    "in title/terms/abstract; (2) text TACC restricted to org UNIVERSITY OF TEXAS AT AUSTIN, kept only "
    "if the text names the Texas Advanced Computing Center or has TACC within "
    f"{COMPUTING_CONTEXT_WINDOW} chars of a computing word. Project-years grouped by core_project_num.")


def fetch_all_project_years(criteria):
    project_years = []
    offset = 0
    while True:
        body = {"criteria": criteria, "offset": offset, "limit": PAGE_SIZE,
                "sort_field": "appl_id", "sort_order": "asc"}
        payload = common.fetch_json(SEARCH_URL, source=SOURCE, method="POST", body=body)
        page = payload.get("results") or []
        project_years.extend(page)
        offset += len(page)
        if not page or offset >= payload["meta"]["total"]:
            return project_years


def searchable_text(project_year):
    return " ".join(project_year.get(field) or "" for field in ("project_title", "abstract_text", "terms"))


def refers_to_computing_center(project_year):
    text = searchable_text(project_year)
    if TACC_FULL_NAME_PATTERN.search(text):
        return True
    for match in TACC_ACRONYM_PATTERN.finditer(text):
        window = text[max(0, match.start() - COMPUTING_CONTEXT_WINDOW):match.end() + COMPUTING_CONTEXT_WINDOW]
        if COMPUTING_CONTEXT_PATTERN.search(window):
            return True
    return False


def iso_date(value):
    return value[:10] if value else None


def organization_institution(organization):
    name = organization.get("org_name")
    if not name:
        return None, None
    if name == UT_AUSTIN_ORG_NAME:
        return f"ror:{common.UT_AUSTIN_ROR_ID}", name
    return f"name:{common.normalize_name(name)}", name


def build_institution_record(project_year, institution_id, retrieved_at):
    organization = project_year.get("organization") or {}
    location = project_year.get("geo_lat_lon") or {}
    organization_type = (project_year.get("organization_type") or {}).get("name")
    return {
        "id": institution_id,
        "name": organization.get("org_name"),
        "ror": institution_id.removeprefix("ror:") if institution_id.startswith("ror:") else None,
        "openalex_id": None,
        "country_code": organization.get("org_fips"),
        "city": (organization.get("org_city") or "").title() or None,
        "latitude": location.get("lat"),
        "longitude": location.get("lon"),
        "source_types": [organization_type] if organization_type and organization_type != "Unavailable" else [],
        "parent_ror": None,
        "sources": [project_year_source(project_year, retrieved_at)],
    }


def project_year_source(project_year, retrieved_at):
    return common.source_reference(SOURCE, project_year["appl_id"], project_year.get("project_detail_url"), retrieved_at)


def award_amount(project_years):
    """Sum per fiscal year; a parent (non-subproject) record already covers its subprojects,
    so subproject amounts are only used for years where the parent record was not matched."""
    by_fiscal_year = collections.defaultdict(list)
    for project_year in project_years:
        by_fiscal_year[project_year.get("fiscal_year")].append(project_year)
    total = 0
    any_amount = False
    for records in by_fiscal_year.values():
        parents = [record for record in records if not record.get("subproject_id")]
        for record in parents or records:
            if record.get("award_amount") is not None:
                total += record["award_amount"]
                any_amount = True
    return total if any_amount else None


def representative_record(project_years):
    """Latest parent record if any (subproject titles are often just 'Project-002')."""
    parents = [record for record in project_years if not record.get("subproject_id")]
    candidates = parents or project_years
    return max(candidates, key=lambda record: (record.get("fiscal_year") or 0, record["appl_id"]))


def classify_relationship(project_years):
    for project_year in project_years:
        organization_name = (project_year.get("organization") or {}).get("org_name")
        if organization_name == UT_AUSTIN_ORG_NAME and TACC_PERFORMING_PATTERN.search(searchable_text(project_year)):
            return "tacc_partner_award"
    return "tacc_mention_award"


def build_award(core_project_num, project_years, retrieved_at, people_by_id, institutions_by_id):
    project_years = sorted(project_years, key=lambda record: (record.get("fiscal_year") or 0, record["appl_id"]))
    representative = representative_record(project_years)
    start_dates = [iso_date(record.get("project_start_date")) for record in project_years]
    end_dates = [iso_date(record.get("project_end_date")) for record in project_years]
    start_date = min((date for date in start_dates if date), default=None)
    end_date = max((date for date in end_dates if date), default=None)
    agency = (representative.get("agency_ic_admin") or {}).get("abbreviation")

    participants = []
    organization_institution_ids = []
    for project_year in reversed(project_years):
        institution_id, institution_name = organization_institution(project_year.get("organization") or {})
        if institution_id is None or institution_id in organization_institution_ids:
            continue
        organization_institution_ids.append(institution_id)
        institutions_by_id.setdefault(institution_id, build_institution_record(project_year, institution_id, retrieved_at))
        awardee_participant = common.participant(
            name_as_listed=institution_name, role="awardee_org",
            institution_id=institution_id, institution_name_as_listed=institution_name)
        awardee_participant["institution_evidence"] = {
            "method": "source_listed", "detail": "NIH RePORTER organization", "confidence": "high"}
        participants.append(awardee_participant)

    # Per person, newest project-year first: the name as listed and the awardee orgs of the years
    # in which they were contact PI. RePORTER only locates the contact PI (at that year's awardee
    # org); other PIs may be elsewhere, so their institution stays missing.
    investigators_by_person_id = {}
    for project_year in reversed(project_years):
        organization_id, _name = organization_institution(project_year.get("organization") or {})
        for investigator in project_year.get("principal_investigators") or []:
            profile_id = investigator.get("profile_id")
            full_name = " ".join((investigator.get("full_name") or "").split())
            person_id = f"nih:{profile_id}" if profile_id else f"name:{common.normalize_name(full_name)}|{SOURCE}"
            people_by_id.setdefault(person_id, {
                "id": person_id, "name": full_name, "orcid": None, "openalex_author_id": None,
                "other_ids": {"nih_profile_id": profile_id} if profile_id else {},
                "sources": [project_year_source(project_year, retrieved_at)],
            })
            investigator_summary = investigators_by_person_id.setdefault(
                person_id, {"name": full_name, "contact_institution_ids": [], "contact_fiscal_years": []})
            if investigator.get("is_contact_pi") and organization_id:
                if organization_id not in investigator_summary["contact_institution_ids"]:
                    investigator_summary["contact_institution_ids"].append(organization_id)
                investigator_summary["contact_fiscal_years"].append(project_year.get("fiscal_year"))

    for person_id, investigator_summary in investigators_by_person_id.items():
        contact_institution_ids = investigator_summary["contact_institution_ids"]
        if contact_institution_ids:
            institution_id = contact_institution_ids[0]
            person_participant = common.participant(
                name_as_listed=investigator_summary["name"], role="pi", person_id=person_id,
                institution_id=institution_id, institution_name_as_listed=institutions_by_id[institution_id]["name"],
                additional_institution_ids=contact_institution_ids[1:])
            fiscal_years = sorted({year for year in investigator_summary["contact_fiscal_years"] if year})
            person_participant["institution_evidence"] = {
                "method": "source_listed",
                "detail": f"NIH RePORTER contact PI at the awardee organization, fiscal years {fiscal_years}",
                "confidence": "high"}
        else:
            person_participant = common.participant(
                name_as_listed=investigator_summary["name"], role="co_pi", person_id=person_id)
            person_participant["institution_evidence"] = None
        participants.append(person_participant)

    return {
        "id": f"award:nih:{core_project_num}",
        "kind": "award",
        "relationship_types": [classify_relationship(project_years)],
        "title": common.scrub_contact_details(representative.get("project_title")),
        "year": int(start_date[:4]) if start_date else representative.get("fiscal_year"),
        "start_date": start_date,
        "end_date": end_date,
        "amount_usd": award_amount(project_years),
        "funder": f"NIH/{agency}" if agency else "NIH",
        "venue": None,
        "url": representative.get("project_detail_url"),
        "description": common.scrub_contact_details(
            representative.get("abstract_text") or "")[:DESCRIPTION_LENGTH] or None,
        "identifiers": {
            "award_number": core_project_num,
            "core_project_num": core_project_num,
            "project_nums": sorted({record["project_num"] for record in project_years if record.get("project_num")}),
            "appl_ids": [record["appl_id"] for record in project_years],
            "fiscal_years": sorted({record["fiscal_year"] for record in project_years if record.get("fiscal_year")}),
        },
        "participants": participants,
        "participants_truncated": False,
        "sources": [project_year_source(record, retrieved_at) for record in project_years],
    }


def main():
    retrieved_at = common.utc_now_iso()
    project_years_by_appl_id = {}
    for search in SEARCHES:
        results = fetch_all_project_years(search["criteria"])
        kept = [record for record in results if search["label"] == "full_name" or refers_to_computing_center(record)]
        new = [record for record in kept if record["appl_id"] not in project_years_by_appl_id]
        print(f"[{SOURCE}] search {search['label']}: {len(results)} project-years, kept {len(kept)}, new {len(new)}")
        for record in results:
            if record not in kept:
                print(f"[{SOURCE}]   dropped {record.get('project_num')}: {record.get('project_title')}")
        for record in kept:
            project_years_by_appl_id.setdefault(record["appl_id"], record)

    project_years_by_core = collections.defaultdict(list)
    for record in project_years_by_appl_id.values():
        project_years_by_core[record.get("core_project_num") or record["project_num"]].append(record)

    people_by_id = {}
    institutions_by_id = {}
    collaborations = [
        build_award(core_project_num, project_years, retrieved_at, people_by_id, institutions_by_id)
        for core_project_num, project_years in sorted(project_years_by_core.items())
    ]
    common.write_normalized(
        SOURCE, query_description=QUERY_DESCRIPTION, collaborations=collaborations,
        people=list(people_by_id.values()), institutions=list(institutions_by_id.values()))


if __name__ == "__main__":
    main()
