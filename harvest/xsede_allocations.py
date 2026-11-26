"""TeraGrid and XSEDE allocations on TACC-operated resources, 2005 - August 2022, from the program's public archive.

Sources (both CC0, Illinois Data Bank; it refuses scripted downloads, so download each dataset's files in a browser,
unchanged, into its directory under data/raw/):
- Towns, John; Hart, David (2023): XSEDE: Allocations Awards and Usage for the NSF Cyberfrastructure Portfolio,
  2004-2022. https://doi.org/10.13012/B2IDB-3731847_V1 -> data/raw/xsede_allocations/. Both CSVs start 2011-07-01
  (XSEDE's first day) despite the title; it supplies every period from then on.
- XSEDE: Allocations Awards for the NSF Cyberinfrastructure Portfolio, 2004-2019.
  https://doi.org/10.13012/B2IDB-0757799_V1 -> data/raw/xsede04-19/. It supplies only the periods that start before
  2011-07-01 (TeraGrid: Ranger, Lonestar, Spur, Longhorn, ...); later ones come from the newer edition.

One record per project (Grant_Number), like an award: the award file has one row per allocation period, and only the
periods that used a TACC resource count, for the dates and for where the project is placed. The award file has no
resource column; resources come from the same edition's usage file, joined by Grant_Number and overlapping dates. A
project's periods from both editions make one record. A project number continued under ACCESS has the same id as its
ACCESS record (allocation:<number>), and build.py merges the two.
"""

import collections
import csv
import datetime
import hashlib
import re

import common

SOURCE = "xsede_allocations"
XSEDE_START = "2011-07-01"
EDITIONS = (
    {
        "doi": "10.13012/B2IDB-0757799_V1",
        "directory": common.RAW_DIRECTORY / "xsede04-19",
        "award_file": "XSEDE_allocation_award_history_2004-2019.csv",
        "usage_file": "XSEDE_allocation_usage_history_2004-2019.csv",
        # The README (v2.0) publishes a4784d1560c2ce1f6278739dae65cc9f8583a9cc for the award file, but the file the
        # DOI serves (checked inside the Data Bank's own zip, 2026-11-26) is this one; the usage file matches the README.
        "sha1": {"XSEDE_allocation_award_history_2004-2019.csv": "9cdcd59821098a923d668a42804e130e32cb54e0",
                 "XSEDE_allocation_usage_history_2004-2019.csv": "bf95742cc71e54b5fdaab912fc54c53697c89a82"},
        "date_formats": ("%m/%d/%y", "%m/%d/%Y"),
        "keeps_period": lambda start_date: start_date < XSEDE_START,
    },
    {
        "doi": "10.13012/B2IDB-3731847_V1",
        "directory": common.RAW_DIRECTORY / "xsede_allocations",
        "award_file": "XSEDE allocation award history.csv",
        "usage_file": "XSEDE allocation usage history.csv",
        # Published in the dataset's README (v3.0).
        "sha1": {"XSEDE allocation award history.csv": "e6baa159ef17420cf42d33be8d4e159587b81710",
                 "XSEDE allocation usage history.csv": "e34029a8c76588a719bcb8e6557d5a707e781a60"},
        "date_formats": ("%Y-%m-%d",),
        "keeps_period": lambda start_date: start_date >= XSEDE_START,
    },
)
# Resource names are unique internal designations such as "stampede2.tacc.xsede" or "ranger.tacc.teragrid";
# "jetstream.tacc.xsede" is the IU/TACC Jetstream the ACCESS harvester also keeps, Jetstream2 is Indiana's.
TACC_RESOURCE_PATTERN = re.compile(r"\.tacc\.")
DESCRIPTION_LENGTH = 400


def verify_checksums(edition):
    for file_name, expected in edition["sha1"].items():
        path = edition["directory"] / file_name
        if not path.exists():
            raise SystemExit(f"missing {path}: download the files of https://doi.org/{edition['doi']} into {edition['directory']}")
        actual = hashlib.sha1(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"{path}: SHA-1 {actual} does not match the expected {expected}")


def iso_date(text, date_formats):
    for date_format in date_formats:
        try:
            return datetime.datetime.strptime(text.strip(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def read_rows(edition, file_name):
    """Rows with Start_Date and End_Date as ISO dates; rows whose dates do not parse are dropped."""
    with open(edition["directory"] / file_name, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    parsed = []
    for row in rows:
        start_date, end_date = iso_date(row["Start_Date"], edition["date_formats"]), iso_date(row["End_Date"], edition["date_formats"])
        if start_date and end_date:
            parsed.append({**row, "Start_Date": start_date, "End_Date": end_date})
    return parsed


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
    with TACC usage but no such award row (no PI or organization to place them). One edition's rows only."""
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
            matched[grant_number] = (periods, usage_rows)
        else:
            unattributed.append(grant_number)
    return matched, unattributed


def pi_name(award):
    first, last = display_case(award["PI_First_Name"].strip()), display_case(award["PI_Last_Name"].strip())
    return f"{first} {last}".strip() or None


def build_records(grant_number, periods, usage_rows, source_refs):
    periods = sorted(periods, key=lambda award: award["Start_Date"])
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
                       "sources": source_refs})
        if organization:
            institutions.append({"id": f"name:{common.normalize_name(organization)}", "name": organization,
                                 "ror": None, "openalex_id": None, "country_code": None, "city": None,
                                 "latitude": None, "longitude": None, "source_types": [], "parent_ror": None,
                                 "sources": source_refs})
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
        "sources": source_refs,
    }
    return collaboration, people, institutions


def main():
    periods_by_grant = collections.defaultdict(list)
    usage_by_grant = collections.defaultdict(list)
    source_refs_by_grant = collections.defaultdict(list)
    for edition in EDITIONS:
        verify_checksums(edition)
        awards = [row for row in read_rows(edition, edition["award_file"]) if edition["keeps_period"](row["Start_Date"])]
        usage = [row for row in read_rows(edition, edition["usage_file"]) if edition["keeps_period"](row["Start_Date"])]
        retrieved_at = datetime.datetime.fromtimestamp(
            (edition["directory"] / edition["award_file"]).stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
        matched, unattributed = tacc_periods_by_grant(awards, usage)
        for grant_number, (periods, usage_rows) in matched.items():
            periods_by_grant[grant_number].extend(periods)
            usage_by_grant[grant_number].extend(usage_rows)
            source_refs_by_grant[grant_number].append(
                common.source_reference(SOURCE, grant_number, f"https://doi.org/{edition['doi']}", retrieved_at))
        print(f"[{SOURCE}] {edition['doi']}: {len(awards)} award rows, {len(usage)} usage rows in its range; "
              f"{len(matched)} projects with TACC time, {len(unattributed)} more with TACC usage but no overlapping "
              f"award row (no PI or organization), left out")

    collaborations, people, institutions = [], [], []
    for grant_number in sorted(periods_by_grant):
        collaboration, project_people, project_institutions = build_records(
            grant_number, periods_by_grant[grant_number], usage_by_grant[grant_number], source_refs_by_grant[grant_number])
        collaborations.append(collaboration)
        people.extend(project_people)
        institutions.extend(project_institutions)

    both_editions = sum(len(source_refs) > 1 for source_refs in source_refs_by_grant.values())
    resource_counts = collections.Counter(resource["name"] for collaboration in collaborations
                                          for resource in collaboration["resources"])
    print(f"[{SOURCE}] {len(collaborations)} projects ({both_editions} with periods in both editions)")
    for name, count in resource_counts.most_common():
        print(f"[{SOURCE}] {count} projects on {name}")
    common.write_normalized(
        SOURCE,
        query_description=("TeraGrid/XSEDE allocation award and usage history, CC0: "
                           + "; ".join(f"https://doi.org/{edition['doi']}" for edition in EDITIONS)
                           + f" (the 2004-2019 edition for periods starting before {XSEDE_START}, the 2004-2022 "
                           "edition from then to 2022-08-31): projects with usage on a resource whose name contains "
                           "'.tacc.' (" + ", ".join(sorted(resource_counts)) + "); one record per project, from its "
                           "allocation periods that overlap TACC usage."),
        collaborations=collaborations, people=people, institutions=institutions)


if __name__ == "__main__":
    main()
