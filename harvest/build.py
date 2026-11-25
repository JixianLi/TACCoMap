"""Merge data/normalized/*.json into the browser's web/data/*.json files. Contract: SCHEMA.md."""

import argparse
import collections
import json
import pathlib
import re
import sys

import build_awards
import build_institutions
import build_people
import common

KIND_TO_OUTPUT_FILE = {
    "paper": "papers.json",
    "award": "awards.json",
    "subaward": "awards.json",
    "dataset": "datasets.json",
    "allocation": "allocations.json",
    "funding_opportunity": "funding_opportunities.json",
}
RELATIONSHIP_TYPES = [
    "staff_coauthor", "resource_acknowledgment", "tacc_lead_award", "tacc_partner_award",
    "tacc_mention_award", "subaward", "tacc_hosted_data", "tacc_resource_allocation", "funding_opportunity",
]
# TACC was formally formed in 2001; earlier records belong to its predecessor supercomputing center.
FIRST_COLLABORATION_DATE = "2001-01-01"
# Allowlist, so a QA field added to person records later stays out of the published site until someone adds it here.
# Each is read by web/app.js or web/data.js: id (routes, links), name and display_name (shown, searched),
# orcid and openalex_author_id (links, column, search), other_ids (identifier rows, search), merged_ids and
# name_variants (person drawer, search), sources (facet, column, provenance table), collaboration_ids (timeline),
# collaboration_count (column, sort).
PUBLISHED_PERSON_FIELDS = (
    "id", "name", "display_name", "orcid", "openalex_author_id", "other_ids", "merged_ids", "name_variants", "sources",
    "collaboration_ids", "collaboration_count",
)
# Kept in data/normalized only: NSF's current email domain is an undated employer hint, not evidence for the award.
UNPUBLISHED_PARTICIPANT_FIELDS = ("current_email_domain_is_tacc",)
def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_directory", nargs="?", default=str(common.NORMALIZED_DIRECTORY))
    parser.add_argument("output_directory", nargs="?", default=str(common.PROJECT_ROOT / "web" / "data"))
    parser.add_argument("--review-directory", default=str(common.PROJECT_ROOT / "data" / "review"),
                        help="maintainer-only outputs (merge log, full people records); never published")
    parser.add_argument("--overrides", default=str(build_institutions.OVERRIDES_PATH))
    parser.add_argument("--skip-ror-matching", action="store_true",
                        help="do not call the ROR affiliation API (manual aliases and dataset-name matches still apply)")
    return parser.parse_args()


def load_normalized_files(input_directory):
    documents = []
    for path in sorted(pathlib.Path(input_directory).glob("*.json")):
        documents.append(json.loads(path.read_text()))
        print(f"loaded {path.name}")
    return documents


def first_non_null(existing, incoming):
    return existing if existing is not None else incoming


def merge_sources(existing_sources, incoming_sources):
    seen = {(source["source"], source["source_record_id"]) for source in existing_sources}
    merged = list(existing_sources)
    for source in incoming_sources:
        key = (source["source"], source["source_record_id"])
        if key not in seen:
            seen.add(key)
            merged.append(source)
    return merged


def union_list(existing, incoming):
    return list(dict.fromkeys((existing or []) + (incoming or [])))


def merge_record_fields(existing, incoming):
    """Scalars: first non-null wins. Lists/dicts: union. sources: deduped by (source, record id)."""
    for key, value in incoming.items():
        if key == "sources":
            existing["sources"] = merge_sources(existing.get("sources") or [], value or [])
        elif key == "participants":
            existing["participants"] = (existing.get("participants") or []) + (value or [])
        elif key == "participants_truncated":
            existing[key] = bool(existing.get(key)) or bool(value)
        elif isinstance(value, list):
            existing[key] = union_list(existing.get(key), value)
        elif isinstance(value, dict):
            existing[key] = {**value, **(existing.get(key) or {})}
        else:
            existing[key] = first_non_null(existing.get(key), value)
    return existing


def merge_by_id(documents, section):
    merged = {}
    for document in documents:
        for record in document.get(section, []):
            if record["id"] in merged:
                merge_record_fields(merged[record["id"]], record)
            else:
                merged[record["id"]] = json.loads(json.dumps(record))
    return merged


def collaboration_date(collaboration):
    """Award/allocation start, publication date or subaward action date (start_date), else the year; None if undated."""
    if collaboration.get("start_date"):
        return collaboration["start_date"]
    return f"{collaboration['year']:04d}-01-01" if collaboration.get("year") else None


def drop_collaborations_before_tacc(collaborations, people):
    """Drop collaborations dated before FIRST_COLLABORATION_DATE, subawards of dropped primes, and people referenced
    only by dropped records. Runs before institution and person resolution so dropped records are never evidence."""
    dropped_ids = {collaboration_id for collaboration_id, collaboration in collaborations.items()
                   if collaboration_date(collaboration) and collaboration_date(collaboration) < FIRST_COLLABORATION_DATE}
    dropped_ids |= {collaboration_id for collaboration_id, collaboration in collaborations.items()
                    if (collaboration.get("identifiers") or {}).get("prime_award_collaboration_id") in dropped_ids}
    undated_counts = collections.Counter(collaboration.get("kind") for collaboration in collaborations.values()
                                         if not collaboration_date(collaboration))
    dropped_counts = collections.Counter(collaborations[collaboration_id].get("kind") for collaboration_id in dropped_ids)
    dropped_person_ids = {participant["person_id"] for collaboration_id in dropped_ids
                          for participant in collaborations[collaboration_id].get("participants") or []
                          if participant.get("person_id")}
    for collaboration_id in dropped_ids:
        collaborations.pop(collaboration_id)
    kept_person_ids = {participant["person_id"] for collaboration in collaborations.values()
                       for participant in collaboration.get("participants") or [] if participant.get("person_id")}
    orphaned_person_ids = (dropped_person_ids - kept_person_ids) & set(people)
    for person_id in orphaned_person_ids:
        people.pop(person_id)
    print(f"\ndropped {len(dropped_ids)} collaborations dated before {FIRST_COLLABORATION_DATE}: {dict(dropped_counts)}; "
          f"{len(orphaned_person_ids)} people referenced only by them")
    print(f"  kept {sum(undated_counts.values())} undated collaborations: {dict(undated_counts)}")


def rewrite_openalex_institutions_to_ror(institutions):
    """Re-key openalex:I… records that carry a ROR id; merge into any existing ror: record."""
    institution_id_rewrite = {}
    for institution_id in list(institutions):
        institution = institutions[institution_id]
        ror = build_institutions.bare_ror_id(institution.get("ror"))
        if not institution_id.startswith("openalex:") or not ror:
            continue
        ror_institution_id = f"ror:{ror}"
        institutions.pop(institution_id)
        institution["id"] = ror_institution_id
        institution["ror"] = ror
        institution["openalex_id"] = institution.get("openalex_id") or institution_id.removeprefix("openalex:")
        if ror_institution_id in institutions:
            merge_record_fields(institutions[ror_institution_id], institution)
        else:
            institutions[ror_institution_id] = institution
        institution_id_rewrite[institution_id] = ror_institution_id
    return institution_id_rewrite


def rewrite_participant_institution_ids(collaborations, institution_id_rewrite):
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            institution_id = participant.get("institution_id")
            participant["institution_id"] = institution_id_rewrite.get(institution_id, institution_id)
            participant["additional_institution_ids"] = list(dict.fromkeys(
                institution_id_rewrite.get(value, value) for value in participant.get("additional_institution_ids") or []))


def rewrite_participant_person_ids(collaborations, person_id_rewrite):
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            participant["person_id"] = person_id_rewrite.get(participant["person_id"], participant["person_id"])


def participant_key(participant):
    if participant.get("person_id"):
        return ("person", participant["person_id"], participant["role"])
    return ("name", common.normalize_name(participant.get("name_as_listed")), participant["role"])


def deduplicate_participants(participants):
    """Same person+role listed by two sources -> one participant; fill gaps from the later copy."""
    merged = {}
    for participant in participants:
        key = participant_key(participant)
        if key not in merged:
            merged[key] = dict(participant)
            continue
        existing = merged[key]
        if existing.get("institution_status") != "resolved" and participant.get("institution_status") == "resolved" \
                or existing.get("institution_status") == "missing" and participant.get("institution_status") == "unresolved":
            for field in ("institution_id", "institution_name_as_listed", "institution_status",
                          "institution_evidence", "institution_match_method"):
                existing[field] = participant.get(field)
        existing["additional_institution_ids"] = union_list(
            existing.get("additional_institution_ids"), participant.get("additional_institution_ids"))
        for field, value in participant.items():
            existing[field] = first_non_null(existing.get(field), value)
    return list(merged.values())


def participant_institution_ids(participant):
    primary = [participant["institution_id"]] if participant.get("institution_id") else []
    return primary + list(participant.get("additional_institution_ids") or [])


def derive_collaboration_fields(collaboration, institutions):
    participants = collaboration.get("participants") or []
    institution_ids = list(dict.fromkeys(
        institution_id for participant in participants for institution_id in participant_institution_ids(participant)))
    linked_institutions = [institutions[institution_id] for institution_id in institution_ids if institution_id in institutions]
    collaboration["has_missing_institution"] = any(participant["institution_status"] == "missing" for participant in participants)
    collaboration["has_unresolved_institution"] = any(participant["institution_status"] == "unresolved" for participant in participants)
    collaboration["institution_ids"] = institution_ids
    collaboration["country_codes"] = sorted({institution["country_code"] for institution in linked_institutions if institution.get("country_code")})
    collaboration["institution_types"] = sorted({institution["institution_type"] for institution in linked_institutions})


def attach_collaboration_links(collaborations, people, institutions):
    collaboration_ids_by_person = collections.defaultdict(list)
    collaboration_count_by_institution = collections.Counter()
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            person = people.get(participant["person_id"]) if participant["person_id"] else None
            participant["person_display_name"] = person.get("display_name") if person else None
        for person_id in dict.fromkeys(participant["person_id"] for participant in collaboration.get("participants") or []):
            if person_id:
                collaboration_ids_by_person[person_id].append(collaboration["id"])
        for institution_id in collaboration["institution_ids"]:
            collaboration_count_by_institution[institution_id] += 1
    for person_id, person in people.items():
        person["collaboration_ids"] = collaboration_ids_by_person.get(person_id, [])
        person["collaboration_count"] = len(person["collaboration_ids"])
    for institution_id, institution in institutions.items():
        institution["collaboration_count"] = collaboration_count_by_institution.get(institution_id, 0)
    orphan_person_ids = set(collaboration_ids_by_person) - set(people)
    return orphan_person_ids


def write_compact_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return path.stat().st_size


def build_meta(documents, output_counts):
    return {
        "generated_at": common.utc_now_iso(),
        "sources": [{"source": document.get("source"), "retrieved_at": document.get("retrieved_at"),
                     "query_description": document.get("query_description")} for document in documents],
        "counts": output_counts,
        "vocabulary": {"relationship_types": RELATIONSHIP_TYPES, "institution_types": build_institutions.INSTITUTION_TYPES},
    }


def write_person_merge_log(review_directory, merge_log, refused_merges, stripped_orcids):
    path = pathlib.Path(review_directory) / "person_merge_log.json"
    path.write_text(json.dumps({"generated_at": common.utc_now_iso(), "merges": merge_log,
                                "refused_merges": refused_merges, "stripped_orcids": stripped_orcids},
                               ensure_ascii=False, indent=1))
    return path


# Person pages are published only for direct collaborators (maintainer decision, 2026-11-25): authors and co-authors on records
# with a TACC-affiliated author (staff_coauthor), PIs/co-PIs of TACC-led or TACC-partner awards, and allocation PIs.
# Everyone else (acknowledgment-only authors, hosted-dataset creators, PIs of awards that only mention TACC) stays a
# name on each record; their person_id is withheld so the published files cannot regroup their records.
DIRECT_RELATIONSHIP_TYPES = {"staff_coauthor", "tacc_lead_award", "tacc_partner_award", "tacc_resource_allocation"}


# Potential minors (maintainer decision, 2026-11-25): anyone listed at a secondary school on any record is published
# only as a name and that institution - no person page, person id, role, evidence or other affiliations - and the
# listed affiliation is cut to the school's own part (no street, town or postcode).
SECONDARY_SCHOOL_PATTERN = re.compile(
    r"\bhigh\s+school\b|\bsecondary\s+school\b|\bmiddle\s+school\b|\bjunior\s+high\b|\bindependent\s+school\s+district\b|"
    r"\bISD\b|\bprep(?:aratory)?\s+school\b|\bgymnasium\b|\blyc[ée]e\b|\bscience\s+academy\b|\bthacher\s+school\b",
    re.IGNORECASE)
POTENTIAL_MINOR_PARTICIPANT_FIELDS = ("name_as_listed", "institution_id", "institution_name_as_listed", "institution_status")
# DesignSafe and other dataset descriptions can give a private site (e.g. a house roof) to the arc-second; the
# published text keeps two decimals (about 1 km).
# Only numbers that are clearly coordinates are rounded (a degree sign, a lat/long label, or a pair right after a
# GPS/coordinates/location word), so measurement lists like "0.443, 0.512" stay intact.
DECIMAL_DEGREES_PATTERN = re.compile(r"-?\d{1,3}\.\d{3,}(?=\s*°)")
LABELLED_COORDINATE_PATTERN = re.compile(r"(\b(?:lat(?:itude)?|lon(?:g(?:itude)?)?|lng)\b\s*[:=]?\s*)(-?\d{1,3}\.\d{3,})",
                                         re.IGNORECASE)
CONTEXT_COORDINATE_PAIR_PATTERN = re.compile(
    r"(\b(?:gps|coordinates?|location|loc|position|lat(?:itude)?|long(?:itude)?)\b[^.\n]{0,40}?)(-?\d{1,3}\.\d{3,})(\s*,\s*)"
    r"(-?\d{1,3}\.\d{3,})",
    re.IGNORECASE)
DMS_COORDINATE_PATTERN = re.compile(
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')?\s*([NSEW])")
COORDINATE_TEXT_FIELDS = ("title", "description")


def round_coordinates(text):
    text = DMS_COORDINATE_PATTERN.sub(
        lambda match: f"{int(match[1]) + int(match[2]) / 60 + float(match[3]) / 3600:.2f}°{match[4]}", text)
    text = DECIMAL_DEGREES_PATTERN.sub(lambda match: f"{float(match[0]):.2f}", text)
    text = CONTEXT_COORDINATE_PAIR_PATTERN.sub(
        lambda match: f"{match[1]}{float(match[2]):.2f}{match[3]}{float(match[4]):.2f}", text)
    return LABELLED_COORDINATE_PATTERN.sub(lambda match: f"{match[1]}{float(match[2]):.2f}", text)


def participant_institution_names(participant, institutions):
    institution_ids = [participant.get("institution_id")] + list(participant.get("additional_institution_ids") or [])
    return [participant.get("institution_name_as_listed") or ""] + [
        (institutions.get(institution_id) or {}).get("name") or "" for institution_id in institution_ids if institution_id]


def is_potential_minor_participant(participant, institutions):
    return any(SECONDARY_SCHOOL_PATTERN.search(name) for name in participant_institution_names(participant, institutions))


def potential_minor_ids(collaborations, institutions):
    return {participant["person_id"] for collaboration in collaborations.values()
            for participant in collaboration.get("participants") or []
            if participant.get("person_id") and is_potential_minor_participant(participant, institutions)}


def school_part(listed_affiliation):
    parts = [part.strip() for part in re.split(r"[,;]", listed_affiliation or "") if part.strip()]
    return next((part for part in parts if SECONDARY_SCHOOL_PATTERN.search(part)), None)


def potential_minor_participant(participant):
    published = {field: participant.get(field) for field in POTENTIAL_MINOR_PARTICIPANT_FIELDS}
    published["person_id"] = None
    published["institution_name_as_listed"] = school_part(participant.get("institution_name_as_listed"))
    return published


def direct_collaborator_ids(collaborations):
    return {participant["person_id"] for collaboration in collaborations.values()
            if DIRECT_RELATIONSHIP_TYPES & set(collaboration.get("relationship_types") or [])
            for participant in collaboration.get("participants") or [] if participant.get("person_id")}


def published_person(person):
    return {field: person[field] for field in PUBLISHED_PERSON_FIELDS if field in person}


def is_published_participant_field(participant, field, value, direct_ids):
    if field in ("person_id", "person_display_name") and participant.get("person_id") not in direct_ids:
        return field == "person_id"
    # A display name equal to the listed name (or null) is left out: the browser falls back to name_as_listed, and
    # the repeats alone kept papers.json above the publish guard's size limit.
    if field == "person_display_name":
        return value is not None and value != participant.get("name_as_listed")
    return field not in UNPUBLISHED_PARTICIPANT_FIELDS


def published_participant(participant, direct_ids, minor_ids, institutions):
    if participant.get("person_id") in minor_ids or is_potential_minor_participant(participant, institutions):
        return potential_minor_participant(participant)
    published = {field: value for field, value in participant.items()
                 if is_published_participant_field(participant, field, value, direct_ids)}
    if participant.get("person_id") not in direct_ids:
        published["person_id"] = None
    return published


def published_collaboration(collaboration, direct_ids, minor_ids, institutions):
    published = {**collaboration, "participants": [published_participant(participant, direct_ids, minor_ids, institutions)
                                                   for participant in collaboration.get("participants") or []]}
    for field in COORDINATE_TEXT_FIELDS:
        if published.get(field):
            published[field] = round_coordinates(published[field])
    return published


def write_review_outputs(review_directory, output_directory, people, merge_log, refused_merges, stripped_orcids):
    """Maintainer-only files go to the gitignored review directory; a merge log left in the site folder by an older
    build is removed so it cannot be published."""
    review_directory = pathlib.Path(review_directory)
    review_directory.mkdir(parents=True, exist_ok=True)
    (pathlib.Path(output_directory) / "person_merge_log.json").unlink(missing_ok=True)
    print(f"  wrote {write_person_merge_log(review_directory, merge_log, refused_merges, stripped_orcids)}")
    people_full_path = review_directory / "people_full.json"
    write_compact_json(people_full_path, list(people.values()))
    print(f"  wrote {people_full_path}")


def print_award_summary(award_stats, relationship_changes, collaborations):
    print("\naward institution at the time:", dict(award_stats["counts"]))
    print(f"  co-PIs left unknown because dated evidence conflicts: {len(award_stats['conflicts'])}")
    award_types = collections.Counter(
        relationship_type for collaboration in collaborations.values()
        if collaboration["id"].startswith(build_awards.AWARD_ID_PREFIXES)
        for relationship_type in collaboration["relationship_types"])
    print(f"award relationship types after recompute: {dict(award_types)}; {len(relationship_changes)} awards changed")


def print_person_summary(person_stats):
    print(f"\npeople: {person_stats['people_before']} records -> {person_stats['people_after']} after resolution")
    print(f"  merges by reason: {person_stats['merges_by_reason']}")
    print(f"  name-based merges skipped: {person_stats['name_based_skipped']}; refused merges: {person_stats['refused_merges']}")


def write_outputs(output_directory, documents, collaborations, people, institutions):
    output_directory = pathlib.Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    records_by_file = {file_name: [] for file_name in dict.fromkeys(KIND_TO_OUTPUT_FILE.values())}
    minor_ids = potential_minor_ids(collaborations, institutions)
    direct_ids = direct_collaborator_ids(collaborations) - minor_ids
    print(f"\npotential minors (secondary-school affiliation) published as name + institution only: {len(minor_ids)} people")
    unknown_kinds = collections.Counter()
    for collaboration in collaborations.values():
        file_name = KIND_TO_OUTPUT_FILE.get(collaboration.get("kind"))
        if file_name:
            records_by_file[file_name].append(published_collaboration(collaboration, direct_ids, minor_ids, institutions))
        else:
            unknown_kinds[collaboration.get("kind")] += 1
    if unknown_kinds:
        print(f"warning: skipped collaborations with unknown kind: {dict(unknown_kinds)}")
    records_by_file["people.json"] = [published_person(person) for person_id, person in people.items()
                                      if person_id in direct_ids]
    print(f"\npublished person pages for direct collaborators: {len(records_by_file['people.json'])} of {len(people)}")
    records_by_file["institutions.json"] = list(institutions.values())

    output_counts = {file_name: len(records) for file_name, records in records_by_file.items()}
    print("\noutput files:")
    for file_name, records in records_by_file.items():
        size = write_compact_json(output_directory / file_name, records)
        print(f"  {file_name:28} {len(records):>8} records {size / 1024:>10.1f} KiB")
    size = write_compact_json(output_directory / "meta.json", build_meta(documents, output_counts))
    print(f"  {'meta.json':28} {'':>8}         {size / 1024:>10.1f} KiB")


def print_institution_type_table(institutions):
    print("\ninstitution_type x institution_type_reason:")
    reason_counts_by_type = collections.defaultdict(collections.Counter)
    for institution in institutions.values():
        reason_counts_by_type[institution["institution_type"]][institution["institution_type_reason"]] += 1
    for institution_type in build_institutions.INSTITUTION_TYPES:
        reason_counts = reason_counts_by_type.get(institution_type, collections.Counter())
        print(f"  {institution_type:12} {sum(reason_counts.values()):>6}")
        for reason, count in reason_counts.most_common():
            print(f"      {reason:50} {count:>6}")
    unexpected_types = set(reason_counts_by_type) - set(build_institutions.INSTITUTION_TYPES)
    if unexpected_types:
        print(f"  warning: types outside the vocabulary: {sorted(unexpected_types)}")


def print_failures(label, failures, limit=20):
    for name, error in failures[:limit]:
        print(f"  {label} failed for {name!r}: {error}")
    if len(failures) > limit:
        print(f"  ... {len(failures) - limit} more {label} failures")


def print_summary(collaborations, institutions, status_before_matching, resolution_stats, fetch_failures,
                  dropped_institution_count):
    status_after = collections.Counter(
        participant["institution_status"] for collaboration in collaborations.values()
        for participant in collaboration.get("participants") or [])
    print("\nparticipant institution_status before name resolution:", dict(status_before_matching))
    print("participant institution_status after name resolution: ", dict(status_after))
    print("collaborations with missing institution:   ",
          sum(collaboration["has_missing_institution"] for collaboration in collaborations.values()))
    print("collaborations with unresolved institution:",
          sum(collaboration["has_unresolved_institution"] for collaboration in collaborations.values()))
    print(f"institutions written: {len(institutions)} ({dropped_institution_count} dropped with zero collaborations)")

    print_institution_type_table(institutions)

    print(f"\ninstitution name resolution: {resolution_stats['distinct_names_attempted']} distinct names, "
          f"{resolution_stats['occurrences_attempted']} occurrences (primary unresolved + name: additional ids)")
    if not resolution_stats["ror_api_used"]:
        print("  ROR affiliation API skipped (--skip-ror-matching)")
    print(f"  distinct names resolved by method: {resolution_stats['names_by_method']}")
    print(f"  occurrences resolved by method:    {resolution_stats['participants_by_method']}")
    print(f"  ROR chosen matches rejected by token check: {resolution_stats['rejected_ror_matches']} names, "
          f"{resolution_stats['rejected_ror_match_occurrences']} occurrences "
          f"(logged to {build_institutions.REJECTED_ROR_MATCHES_PATH})")
    print_failures("ROR affiliation request", resolution_stats["failed_requests"])
    print_failures("ROR record fetch", fetch_failures)


def main():
    arguments = parse_arguments()
    documents = load_normalized_files(arguments.input_directory)
    if not documents:
        sys.exit(f"no normalized files in {arguments.input_directory}")
    overrides = build_institutions.load_overrides(arguments.overrides)

    collaborations = merge_by_id(documents, "collaborations")
    people = merge_by_id(documents, "people")
    drop_collaborations_before_tacc(collaborations, people)
    institutions = merge_by_id(documents, "institutions")
    institution_id_rewrite = rewrite_openalex_institutions_to_ror(institutions)
    rewrite_participant_institution_ids(collaborations, institution_id_rewrite)
    for collaboration in collaborations.values():
        collaboration["participants"] = deduplicate_participants(collaboration.get("participants") or [])

    status_before_matching = collections.Counter(
        participant["institution_status"] for collaboration in collaborations.values()
        for participant in collaboration["participants"])
    build_institutions.separate_source_categories(institutions)
    ror_organizations = {}
    fetch_failures = build_institutions.fill_ror_institutions(institutions, ror_organizations)
    resolution_stats = build_institutions.resolve_unresolved_institutions(
        collaborations, institutions, ror_organizations, use_ror_api=not arguments.skip_ror_matching)
    build_institutions.remove_bulk_affiliations(collaborations, institutions)

    evidence_by_person = build_awards.load_affiliation_evidence()
    build_awards.resolve_evidence_institution_names(
        evidence_by_person, institutions, ror_organizations, use_ror_api=not arguments.skip_ror_matching)
    institution_hierarchy = build_awards.InstitutionHierarchy(ror_organizations)
    person_aliases = build_people.load_person_aliases()
    # Before award assignment, so an evidence ORCID rejected as another person's never places a co-PI.
    orcid_stripper = build_people.check_orcid_plausibility(
        people, collaborations, evidence_by_person, build_people.load_orcid_names(), institution_hierarchy,
        person_aliases["strip_orcid"])
    evidence_by_person = {person_id: entry for person_id, entry in evidence_by_person.items()
                          if person_id not in orcid_stripper.manually_rejected_evidence_person_ids}
    award_stats = build_awards.assign_award_institutions(
        collaborations, build_awards.without_rejected_orcid_evidence(evidence_by_person, orcid_stripper.rejected_evidence_links),
        institution_hierarchy)
    relationship_changes = build_awards.recompute_award_relationship_types(collaborations, institution_hierarchy)
    fetch_failures += institution_hierarchy.fetch_failures

    people, person_id_rewrite, merge_log, refused_merges, person_stats = build_people.resolve_people(
        people, collaborations, evidence_by_person, person_aliases,
        institution_hierarchy=institution_hierarchy, orcid_stripper=orcid_stripper)
    rewrite_participant_person_ids(collaborations, person_id_rewrite)
    for collaboration in collaborations.values():
        collaboration["participants"] = deduplicate_participants(collaboration["participants"])

    build_institutions.add_missing_institution_stubs(institutions, collaborations)
    fetch_failures += build_institutions.fill_ror_institutions(institutions, ror_organizations)
    build_institutions.ensure_institution_fields(institutions)
    fetch_failures += build_institutions.classify_institutions(institutions, overrides, ror_organizations)
    for collaboration in collaborations.values():
        derive_collaboration_fields(collaboration, institutions)
    orphan_person_ids = attach_collaboration_links(collaborations, people, institutions)
    if orphan_person_ids:
        print(f"warning: {len(orphan_person_ids)} participant person_ids have no person record")
    dropped_institution_count = build_institutions.drop_institutions_without_collaborations(institutions)

    write_outputs(arguments.output_directory, documents, collaborations, people, institutions)
    write_review_outputs(arguments.review_directory, arguments.output_directory, people, merge_log, refused_merges,
                         person_stats["stripped_orcids"])
    print_award_summary(award_stats, relationship_changes, collaborations)
    print_person_summary(person_stats)
    print_summary(collaborations, institutions, status_before_matching, resolution_stats, fetch_failures,
                  dropped_institution_count)


if __name__ == "__main__":
    main()
