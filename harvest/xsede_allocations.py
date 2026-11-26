"""XSEDE allocations on TACC-operated resources, July 2011 - August 2022, from the program's public archive.

Source: Towns, John; Hart, David (2023): XSEDE: Allocations Awards and Usage for the NSF Cyberfrastructure Portfolio,
2004-2022. University of Illinois at Urbana-Champaign. https://doi.org/10.13012/B2IDB-3731847_V1 (CC0). The Illinois
Data Bank refuses scripted downloads: download the dataset's files in a browser into data/raw/xsede_allocations/
unchanged. Both CSVs start 2011-07-01 (XSEDE's first day) despite the title.

One record per project (Grant_Number), like an award: the award file has one row per allocation period, and only the
periods that used a TACC resource count, for the dates and for where the project is placed. The award file has no
resource column; resources come from the usage file, joined by Grant_Number and overlapping dates. A project number
continued under ACCESS has the same id as its ACCESS record (allocation:<number>), and build.py merges the two.
"""

import collections
import csv
import datetime
import hashlib
import re

import common

SOURCE = "xsede_allocations"
DATASET_DOI = "10.13012/B2IDB-3731847_V1"
DATASET_URL = f"https://doi.org/{DATASET_DOI}"
RAW_DIRECTORY = common.RAW_DIRECTORY / SOURCE
AWARD_FILE = "XSEDE allocation award history.csv"
USAGE_FILE = "XSEDE allocation usage history.csv"
# SHA-1 values published in the dataset's README (v3.0): the files must be the released ones, byte for byte.
EXPECTED_SHA1 = {
    AWARD_FILE: "e6baa159ef17420cf42d33be8d4e159587b81710",
    USAGE_FILE: "e34029a8c76588a719bcb8e6557d5a707e781a60",
}
# Resource names are unique internal designations such as "stampede2.tacc.xsede" or "ranger.tacc.teragrid";
# "jetstream.tacc.xsede" is the IU/TACC Jetstream the ACCESS harvester also keeps, Jetstream2 is Indiana's.
TACC_RESOURCE_PATTERN = re.compile(r"\.tacc\.")
DESCRIPTION_LENGTH = 400


def verify_checksums():
    for file_name, expected in EXPECTED_SHA1.items():
        path = RAW_DIRECTORY / file_name
        if not path.exists():
            raise SystemExit(f"missing {path}: download the files of {DATASET_URL} into {RAW_DIRECTORY}")
        actual = hashlib.sha1(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"{path}: SHA-1 {actual} does not match the published {expected}")


def read_csv(file_name):
    with open(RAW_DIRECTORY / file_name, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def periods_overlap(first, second):
    return first["Start_Date"] <= second["End_Date"] and first["End_Date"] >= second["Start_Date"]


def display_case(name):
    """Some names are stored all lowercase ('alex sun'); capitalize only those, leaving 'McDonald' alone."""
    return name.title() if name and name == name.lower() else name


def truncate_description(abstract):
    if not abstract:
        return None
    collapsed = re.sub(r"\s+", " ", common.scrub_contact_details(abstract)).strip()
    if len(collapsed) <= DESCRIPTION_LENGTH:
        return collapsed
    return collapsed[:DESCRIPTION_LENGTH].rsplit(" ", 1)[0] + "…"


def tacc_periods_by_grant(awards, usage):
    """{grant number: (award rows overlapping a TACC usage period, that grant's TACC usage rows)}; plus the grants
    with TACC usage but no such award row (no PI or organization to place them)."""
    tacc_usage_by_grant = collections.defaultdict(list)
    for row in usage:
        if TACC_RESOURCE_PATTERN.search(row["Resource_Name"]):
            tacc_usage_by_grant[row["Grant_Number"]].append(row)
    awards_by_grant = collections.defaultdict(list)
    for row in awards:
        awards_by_grant[row["Grant_Number"]].append(row)
    matched, unattributed = {}, []
    for grant_number, usage_rows in tacc_usage_by_grant.items():
        periods = [award for award in awards_by_grant.get(grant_number, [])
                   if any(periods_overlap(award, usage_row) for usage_row in usage_rows)]
        if periods:
            matched[grant_number] = (sorted(periods, key=lambda award: award["Start_Date"]), usage_rows)
        else:
            unattributed.append(grant_number)
    return matched, unattributed


def pi_name(award):
    first, last = display_case(award["PI_First_Name"].strip()), display_case(award["PI_Last_Name"].strip())
    return f"{first} {last}".strip() or None


def build_records(grant_number, periods, usage_rows, source_ref):
    latest = periods[-1]
    participants, people, institutions = [], [], []
    for award in periods:
        name = pi_name(award)
        organization = award["Organization"].strip() or None
        if not name:
            continue
        person_id = f"name:{common.normalize_name(name)}|allocation"
        participants.append(common.participant(
            name_as_listed=f"{award['PI_Last_Name'].strip()}, {award['PI_First_Name'].strip()}", role="pi",
            person_id=person_id, institution_name_as_listed=organization))
        people.append({"id": person_id, "name": name, "orcid": None, "openalex_author_id": None,
                       "other_ids": {"xsede_pi_id": award["PI_id"]} if award["PI_id"] else {},
                       "sources": [source_ref]})
        if organization:
            institutions.append({"id": f"name:{common.normalize_name(organization)}", "name": organization,
                                 "ror": None, "openalex_id": None, "country_code": None, "city": None,
                                 "latitude": None, "longitude": None, "source_types": [], "parent_ror": None,
                                 "sources": [source_ref]})
    participants = list({(item["person_id"], item["institution_id"]): item for item in participants}.values())
    resources = [{"name": name, "amount": None, "units": units, "tacc_operated": True}
                 for name, units in sorted({(row["Resource_Name"], row["Billable_Unit"]) for row in usage_rows})]
    start_date = periods[0]["Start_Date"]
    collaboration = {
        "id": f"allocation:{grant_number}",
        "kind": "allocation",
        "relationship_types": ["tacc_resource_allocation"],
        "title": common.scrub_contact_details(latest["Project_Title"]) or None,
        "year": int(start_date[:4]),
        "start_date": start_date,
        "end_date": max(award["End_Date"] for award in periods),
        "amount_usd": None,
        "funder": "NSF XSEDE",
        "venue": None,
        "url": None,
        "description": truncate_description(latest["Abstract"]),
        "field_of_science": latest["Field_of_Science"] or None,
        "allocation_type": latest["Project_Type"] or None,
        "resources": resources,
        "identifiers": {"grant_number": grant_number},
        "participants": participants,
        "participants_truncated": False,
        "sources": [source_ref],
    }
    return collaboration, people, institutions


def main():
    verify_checksums()
    awards, usage = read_csv(AWARD_FILE), read_csv(USAGE_FILE)
    retrieved_at = datetime.datetime.fromtimestamp(
        (RAW_DIRECTORY / AWARD_FILE).stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    matched, unattributed = tacc_periods_by_grant(awards, usage)
    collaborations, people, institutions = [], [], []
    for grant_number, (periods, usage_rows) in sorted(matched.items()):
        source_ref = common.source_reference(SOURCE, grant_number, DATASET_URL, retrieved_at)
        collaboration, project_people, project_institutions = build_records(grant_number, periods, usage_rows, source_ref)
        collaborations.append(collaboration)
        people.extend(project_people)
        institutions.extend(project_institutions)

    resource_counts = collections.Counter(resource["name"] for collaboration in collaborations
                                          for resource in collaboration["resources"])
    print(f"[{SOURCE}] {len(awards)} award rows, {len(usage)} usage rows")
    print(f"[{SOURCE}] {len(collaborations)} projects with TACC time; "
          f"{len(unattributed)} more with TACC usage but no overlapping award row (no PI or organization), left out")
    for name, count in resource_counts.most_common():
        print(f"[{SOURCE}] {count} projects on {name}")
    common.write_normalized(
        SOURCE,
        query_description=(f"XSEDE allocation award and usage history ({DATASET_URL}, CC0), 2011-07-01 to 2022-08-31: "
                           "projects with usage on a resource whose name contains '.tacc.' ("
                           + ", ".join(sorted(resource_counts)) + "); one record per project, from its "
                           "allocation periods that overlap TACC usage."),
        collaborations=collaborations, people=people, institutions=institutions)


if __name__ == "__main__":
    main()
