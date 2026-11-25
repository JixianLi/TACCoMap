"""Harvest current ACCESS allocations on TACC-operated resources.

Source: https://allocations.access-ci.org/current-projects.json (public, paginated, 20 per page).
Only CURRENTLY ACTIVE allocations are exposed; historical XSEDE/ACCESS allocations are not.
"""

import json
import re

import common

SOURCE = "access_allocations"
BASE_URL = "https://allocations.access-ci.org/current-projects.json"
DESCRIPTION_LENGTH = 400

# Selection is by resource NAME over an unfiltered scan, not by the endpoint's resources=<id> filter:
# the filter list (current-projects.json?filters=1) only offers 4 TACC ids, but projects still carry
# legacy XSEDE-era TACC resources (Stampede, Maverick, Wrangler, Jetstream Storage) that have no filter id.
# "IU/TACC" Jetstream (original) was jointly operated by Indiana University and TACC, so it is kept;
# "Indiana Jetstream2" is Indiana-operated and does not match.
# Frontera, Lonestar, Vista, Corral and Chameleon are not allocated through ACCESS.
TACC_RESOURCE_PATTERN = re.compile(r"\bTACC\b")

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>,;]+", re.IGNORECASE)


def fetch_all_projects():
    """Yield (project, page_url) for every page of current projects (all resources)."""
    index_page = 1
    while True:
        page_url = common.build_url(BASE_URL, {"page": index_page})
        payload = common.fetch_json(page_url, source=SOURCE)
        for project in payload.get("projects") or []:
            yield project, page_url
        total_pages = payload.get("pages") or 0
        if index_page >= total_pages:
            break
        index_page += 1


def reorder_name(name_as_listed):
    """ACCESS lists PIs as 'Last, First'; return 'First Last'."""
    if not name_as_listed:
        return None
    if "," in name_as_listed:
        last, first = name_as_listed.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name_as_listed.strip()


def truncate_description(abstract):
    if not abstract:
        return None
    collapsed = re.sub(r"\s+", " ", common.scrub_contact_details(abstract)).strip()
    if len(collapsed) <= DESCRIPTION_LENGTH:
        return collapsed
    return collapsed[:DESCRIPTION_LENGTH].rsplit(" ", 1)[0] + "…"


def extract_publication_dois(publications):
    """Publications arrive in an undocumented shape (strings or dicts); scan everything for DOIs."""
    dois = []
    for publication in publications or []:
        text = publication if isinstance(publication, str) else json.dumps(publication)
        for match in DOI_PATTERN.findall(text):
            doi = match.rstrip(".)'").lower()
            if doi not in dois:
                dois.append(doi)
    return dois


def is_tacc_resource(resource):
    return bool(TACC_RESOURCE_PATTERN.search(resource.get("resourceName") or ""))


def compact_resources(resources):
    return [
        {
            "name": resource.get("resourceName"),
            "amount": resource.get("allocation"),
            "units": resource.get("units"),
            "tacc_operated": is_tacc_resource(resource),
        }
        for resource in resources or []
    ]


def year_of(date_text):
    return int(date_text[:4]) if date_text and date_text[:4].isdigit() else None


def build_records(project, page_url, retrieved_at):
    request_number = project["requestNumber"]
    source_ref = common.source_reference(SOURCE, request_number, page_url, retrieved_at)

    pi_as_listed = (project.get("pi") or "").strip() or None
    pi_name = reorder_name(pi_as_listed)
    institution_as_listed = (project.get("piInstitution") or "").strip() or None

    person = None
    participants = []
    if pi_as_listed:
        person_id = f"name:{common.normalize_name(pi_name)}|access"
        person = {"id": person_id, "name": pi_name, "orcid": None, "openalex_author_id": None,
                  "other_ids": {}, "sources": [source_ref]}
        participants.append(common.participant(
            name_as_listed=pi_as_listed, role="pi", person_id=person_id,
            institution_name_as_listed=institution_as_listed))

    institution = None
    if institution_as_listed:
        institution = {"id": f"name:{common.normalize_name(institution_as_listed)}",
                       "name": institution_as_listed, "ror": None, "openalex_id": None,
                       "country_code": None, "city": None, "latitude": None, "longitude": None,
                       "source_types": [], "parent_ror": None, "sources": [source_ref]}

    identifiers = {"request_number": request_number, "project_id": project.get("projectId")}
    publication_dois = extract_publication_dois(project.get("publications"))
    if publication_dois:
        identifiers["publication_dois"] = publication_dois

    collaboration = {
        "id": f"allocation:access:{request_number}",
        "kind": "allocation",
        "relationship_types": ["tacc_resource_allocation"],
        "title": common.scrub_contact_details(project.get("requestTitle")),
        "year": year_of(project.get("beginDate")),
        "start_date": project.get("beginDate"),
        "end_date": project.get("endDate"),
        "amount_usd": None,
        "funder": "NSF ACCESS",
        "venue": None,
        "url": None,
        "description": truncate_description(project.get("abstract")),
        "field_of_science": project.get("fos"),
        "allocation_type": project.get("allocationType"),
        "resources": compact_resources(project.get("resources")),
        "identifiers": identifiers,
        "participants": participants,
        "participants_truncated": False,
        "sources": [source_ref],
    }
    return collaboration, person, institution


def main():
    retrieved_at = common.utc_now_iso()
    collaborations, people, institutions = [], [], []
    seen_request_numbers = set()
    projects_per_tacc_resource = {}
    num_projects_scanned = 0

    for project, page_url in fetch_all_projects():
        num_projects_scanned += 1
        tacc_resources = [resource for resource in project.get("resources") or [] if is_tacc_resource(resource)]
        if not tacc_resources or project["requestNumber"] in seen_request_numbers:
            continue
        seen_request_numbers.add(project["requestNumber"])
        for resource in tacc_resources:
            name = resource.get("resourceName")
            projects_per_tacc_resource[name] = projects_per_tacc_resource.get(name, 0) + 1
        collaboration, person, institution = build_records(project, page_url, retrieved_at)
        collaborations.append(collaboration)
        if person:
            people.append(person)
        if institution:
            institutions.append(institution)

    print(f"[{SOURCE}] scanned {num_projects_scanned} current projects")
    for name, count in sorted(projects_per_tacc_resource.items(), key=lambda item: -item[1]):
        print(f"[{SOURCE}] {count} projects on {name}")

    common.write_normalized(
        SOURCE,
        query_description=("Current ACCESS allocations (allocations.access-ci.org/current-projects.json, all pages) "
                           "having at least one resource whose name contains 'TACC': "
                           + "; ".join(sorted(projects_per_tacc_resource))
                           + ". Current allocations only; no historical XSEDE/ACCESS data."),
        collaborations=collaborations, people=people, institutions=institutions)


if __name__ == "__main__":
    main()
