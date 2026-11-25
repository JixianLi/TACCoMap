"""Harvest DataCite DOIs related to TACC into data/normalized/datacite.json.

Two queries against https://api.datacite.org/dois:
  (A) every DOI minted by the DesignSafe client (tdl.tacc) -> tacc_hosted_data
  (B) every DOI with a creator/contributor affiliated with TACC (by name or ROR) -> staff_coauthor
"""

import collections
import re

import common

SOURCE = "datacite"
API_BASE = "https://api.datacite.org/dois"
DESIGNSAFE_CLIENT_ID = "tdl.tacc"
TACC_NAME = "Texas Advanced Computing Center"
TACC_ROR_URL = f"https://ror.org/{common.TACC_ROR_ID}"
TACC_AFFILIATION_QUERY = " OR ".join([
    f'creators.affiliation.name:"{TACC_NAME}"',
    f'creators.affiliation.affiliationIdentifier:"{TACC_ROR_URL}"',
    f'contributors.affiliation.name:"{TACC_NAME}"',
    f'contributors.affiliation.affiliationIdentifier:"{TACC_ROR_URL}"',
])
DESCRIPTION_MAX_CHARACTERS = 2000
TACC_ACRONYM_PATTERN = re.compile(r"\bTACC\b")
# DesignSafe appends the email domain to institution names, e.g. "University of Washington (washington.edu)".
DOMAIN_SUFFIX_PATTERN = re.compile(r"\s*\([a-z0-9.-]+\.[a-z]{2,}\)\s*$", re.IGNORECASE)
ORGANIZATION_NAME_PATTERN = re.compile(
    r"\b(university|universidad|college|institute|center|centre|laboratory|consortium|foundation|"
    r"inc|llc|ltd|corporation|company|department|agency|school|association|society|council)\b",
    re.IGNORECASE)

statistics = collections.Counter()


def scrub_contact_details(text):
    scrubbed = common.scrub_contact_details(text)
    if scrubbed != text:
        statistics["strings_with_contact_details_scrubbed"] += 1
    return scrubbed


def scrub_affiliation_name(text):
    """Remove contact details; None if nothing institutional remains."""
    scrubbed = scrub_contact_details((text or "").strip())
    if not re.search(r"\w", scrubbed.replace(common.CONTACT_REMOVED, "")):
        if scrubbed:
            statistics["affiliations_only_contact_details_treated_as_missing"] += 1
        return None
    return scrubbed


def fetch_all_dois(extra_params):
    params = {"page[cursor]": 1, "page[size]": 1000, "affiliation": "true", "publisher": "true"}
    params.update(extra_params)
    url = common.build_url(API_BASE, params)
    records = []
    index_page = 0
    while url:
        payload = common.fetch_json(url, source=SOURCE)
        page_records = payload.get("data", [])
        records.extend(page_records)
        index_page += 1
        print(f"[{SOURCE}] page {index_page}: {len(page_records)} records "
              f"(total so far {len(records)} of {payload.get('meta', {}).get('total')})")
        if not page_records:
            break
        url = payload.get("links", {}).get("next")
    return records


def ror_id_from_affiliation(affiliation):
    identifier = affiliation.get("affiliationIdentifier") or ""
    scheme = (affiliation.get("affiliationIdentifierScheme") or "").upper()
    match = re.search(r"ror\.org/(0[a-z0-9]{8})", identifier)
    if match:
        return match.group(1)
    if scheme == "ROR" and re.fullmatch(r"0[a-z0-9]{8}", identifier):
        return identifier
    return None


def is_tacc_affiliation(affiliation):
    name = affiliation.get("name") or ""
    if "autism" in name.lower():
        return False
    return (ror_id_from_affiliation(affiliation) == common.TACC_ROR_ID
            or TACC_NAME.lower() in name.lower()
            or bool(TACC_ACRONYM_PATTERN.search(name)))


def institution_id_for_name(name):
    if not name or name.lower().startswith("http"):
        return None
    normalized = common.normalize_name(DOMAIN_SUFFIX_PATTERN.sub("", name))
    return f"name:{normalized}" if normalized else None


def orcid_from_name_identifiers(name_identifiers):
    for name_identifier in name_identifiers or []:
        if (name_identifier.get("nameIdentifierScheme") or "").upper() != "ORCID":
            continue
        match = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", name_identifier.get("nameIdentifier") or "")
        if match:
            return match.group(1)
    return None


def is_personal(party):
    name_type = party.get("nameType")
    if name_type == "Personal":
        return True
    if name_type == "Organizational":
        return False
    # Many DataCite records omit nameType; infer it. Untyped single-string names default to
    # person (they are mostly people or GitHub handles from Zenodo) unless they look like an org.
    statistics["parties_without_name_type"] += 1
    if party.get("contributorType") == "HostingInstitution":
        return False
    if (party.get("givenName") or party.get("familyName")
            or orcid_from_name_identifiers(party.get("nameIdentifiers"))):
        return True
    if ORGANIZATION_NAME_PATTERN.search(party.get("name") or ""):
        return False
    statistics["untyped_single_string_names_treated_as_person"] += 1
    return True


def display_name(party):
    given = (party.get("givenName") or "").strip()
    family = (party.get("familyName") or "").strip()
    if given and family:
        return f"{given} {family}"
    return (party.get("name") or "").strip()


def build_participant(party, role, source_ref, people, institutions):
    name_as_listed = scrub_contact_details((party.get("name") or display_name(party)).strip())
    person_id = None
    personal = is_personal(party)
    if personal:
        orcid = orcid_from_name_identifiers(party.get("nameIdentifiers"))
        person_name = scrub_contact_details(display_name(party))
        if orcid:
            person_id = f"orcid:{orcid}"
        elif common.normalize_name(person_name):
            person_id = f"name:{common.normalize_name(person_name)}|{SOURCE}"
        if person_id:
            people.append({"id": person_id, "name": person_name, "orcid": orcid,
                           "openalex_author_id": None, "other_ids": {}, "sources": [source_ref]})

    institution_ids = []
    first_affiliation_name = None
    for affiliation in party.get("affiliation") or []:
        if isinstance(affiliation, str):
            affiliation = {"name": affiliation}
        affiliation_name = scrub_affiliation_name(affiliation.get("name"))
        ror_id = ror_id_from_affiliation(affiliation)
        if ror_id:
            institution_id = f"ror:{ror_id}"
            institution_name = None if (affiliation_name or "").lower().startswith("http") else affiliation_name
            institutions.append({"id": institution_id, "name": institution_name, "ror": ror_id,
                                 "openalex_id": None, "country_code": None, "city": None,
                                 "latitude": None, "longitude": None, "source_types": [],
                                 "parent_ror": None, "sources": [source_ref]})
        else:
            institution_id = institution_id_for_name(affiliation_name)
            if not institution_id:
                continue
        if institution_id not in institution_ids:
            institution_ids.append(institution_id)
        if first_affiliation_name is None:
            first_affiliation_name = affiliation_name

    evidence_detail = "datacite affiliation"
    # An organizational party (e.g. DesignSafe HostingInstitution) is its own institution.
    if (not personal and not institution_ids and scrub_affiliation_name(name_as_listed)
            and institution_id_for_name(name_as_listed)):
        institution_ids.append(institution_id_for_name(name_as_listed))
        first_affiliation_name = name_as_listed
        statistics["organizational_parties_used_as_own_institution"] += 1
        if party.get("contributorType") == "HostingInstitution":
            evidence_detail = "datacite hosting institution"
        else:
            evidence_detail = "datacite organizational party name"

    built = common.participant(
        name_as_listed=name_as_listed,
        role=role,
        person_id=person_id,
        institution_id=institution_ids[0] if institution_ids else None,
        institution_name_as_listed=first_affiliation_name,
        additional_institution_ids=institution_ids[1:],
    )
    built["roles"] = [role]
    built["institution_evidence"] = source_listed_evidence(evidence_detail) if built["institution_id"] else None
    return built


def source_listed_evidence(detail):
    return {"method": "source_listed", "detail": detail, "confidence": "high"}


def merge_cross_role_duplicates(participants):
    """Fold a person listed under several roles on one record (typically creator + contributor) into one participant.

    The first listing keeps its position; role becomes 'creator' if any listing is a creator, and every
    role is kept in 'roles'. Same-role repeats (e.g. creator listed twice) are left untouched on purpose:
    with name-only ids they may be two different people who share a name.
    """
    listings_by_person = collections.defaultdict(list)
    for listing in participants:
        if listing["person_id"]:
            listings_by_person[listing["person_id"]].append(listing)
    merged_participants = []
    absorbed_listing_ids = set()
    for listing in participants:
        if id(listing) in absorbed_listing_ids:
            continue
        listings = listings_by_person.get(listing["person_id"], [listing]) if listing["person_id"] else [listing]
        if len({other["role"] for other in listings}) < 2:
            merged_participants.append(listing)
            continue
        roles = []
        for other in listings:
            for role in other["roles"]:
                if role not in roles:
                    roles.append(role)
        roles.sort(key=lambda role: role != "creator")
        merged = dict(listing)
        merged["role"] = roles[0]
        merged["roles"] = roles
        institution_ids = []
        for other in listings:
            for institution_id in [other["institution_id"], *other["additional_institution_ids"]]:
                if institution_id and institution_id not in institution_ids:
                    institution_ids.append(institution_id)
        if not merged["institution_id"] and institution_ids:
            first_with_institution = next(other for other in listings if other["institution_id"])
            for key in ("institution_id", "institution_name_as_listed", "institution_status", "institution_evidence"):
                merged[key] = first_with_institution[key]
            statistics["cross_role_merges_that_filled_missing_institution"] += 1
        merged["additional_institution_ids"] = [institution_id for institution_id in institution_ids
                                                if institution_id != merged["institution_id"]]
        merged_participants.append(merged)
        absorbed_listing_ids.update(id(other) for other in listings)
        statistics["cross_role_duplicate_listings_removed"] += len(listings) - 1
    return merged_participants


def compact_funding_references(funding_references):
    compact = []
    for reference in funding_references or []:
        entry = {key: reference.get(source_key) for key, source_key in [
            ("funder_name", "funderName"), ("funder_identifier", "funderIdentifier"),
            ("award_number", "awardNumber"), ("award_title", "awardTitle")]}
        entry = {key: value for key, value in entry.items() if value}
        if entry:
            compact.append(entry)
    return compact


def first_title(attributes):
    for title in attributes.get("titles") or []:
        if title.get("title") and not title.get("titleType"):
            return title["title"]
    titles = attributes.get("titles") or []
    return titles[0].get("title") if titles else None


def first_description(attributes):
    for description in attributes.get("descriptions") or []:
        text = description.get("description")
        if isinstance(text, list):
            text = " ".join(str(part) for part in text)
        if text and description.get("descriptionType") in (None, "Abstract", "Other"):
            return scrub_contact_details(text.strip())[:DESCRIPTION_MAX_CHARACTERS]
    return None


def publisher_name(attributes):
    publisher = attributes.get("publisher")
    if isinstance(publisher, dict):
        return publisher.get("name")
    return publisher


def normalize_record(record, retrieved_at, people, institutions):
    attributes = record["attributes"]
    doi = (attributes.get("doi") or record["id"]).lower()
    api_url = f"{API_BASE}/{doi}"
    source_ref = common.source_reference(SOURCE, doi, api_url, retrieved_at)

    participants = []
    has_tacc_party = False
    for role, parties in (("creator", attributes.get("creators")), ("contributor", attributes.get("contributors"))):
        for party in parties or []:
            participants.append(build_participant(party, role, source_ref, people, institutions))
            if any(is_tacc_affiliation(affiliation if isinstance(affiliation, dict) else {"name": affiliation})
                   for affiliation in party.get("affiliation") or []):
                has_tacc_party = True

    participants = merge_cross_role_duplicates(participants)

    funding_references = compact_funding_references(attributes.get("fundingReferences"))
    identifiers = {"doi": doi}
    award_numbers = sorted({reference["award_number"] for reference in funding_references
                            if reference.get("award_number")})
    if award_numbers:
        identifiers["award_numbers"] = award_numbers

    relationship_types = []
    if record.get("relationships", {}).get("client", {}).get("data", {}).get("id") == DESIGNSAFE_CLIENT_ID:
        relationship_types.append("tacc_hosted_data")
    if has_tacc_party:
        relationship_types.append("staff_coauthor")

    year = attributes.get("publicationYear")
    return {
        "id": f"dataset:doi:{doi}",
        "kind": "dataset",
        "resource_type": (attributes.get("types") or {}).get("resourceTypeGeneral"),
        "relationship_types": relationship_types,
        "title": scrub_contact_details(first_title(attributes)),
        "year": int(year) if year else None,
        "start_date": None,
        "end_date": None,
        "amount_usd": None,
        "funder": None,
        "venue": publisher_name(attributes),
        "url": attributes.get("url") or f"https://doi.org/{doi}",
        "description": first_description(attributes),
        "identifiers": identifiers,
        "funding_references": funding_references,
        "participants": participants,
        "participants_truncated": False,
        "sources": [source_ref],
    }


def main():
    retrieved_at = common.utc_now_iso()
    designsafe_records = fetch_all_dois({"client-id": DESIGNSAFE_CLIENT_ID})
    tacc_affiliated_records = fetch_all_dois({"query": TACC_AFFILIATION_QUERY})
    statistics["designsafe_records_fetched"] = len(designsafe_records)
    statistics["tacc_affiliation_query_records_fetched"] = len(tacc_affiliated_records)

    people = []
    institutions = []
    collaborations_by_id = {}
    query_hit_without_verified_tacc_party = 0
    for record in designsafe_records + tacc_affiliated_records:
        collaboration = normalize_record(record, retrieved_at, people, institutions)
        if not collaboration["relationship_types"]:
            # Matched the affiliation query but no TACC affiliation survives our stricter check.
            query_hit_without_verified_tacc_party += 1
            collaboration["relationship_types"] = ["staff_coauthor"]
        collaborations_by_id.setdefault(collaboration["id"], collaboration)
    statistics["affiliation_query_hits_without_verified_tacc_party"] = query_hit_without_verified_tacc_party

    common.write_normalized(
        SOURCE,
        query_description=(
            f"DataCite /dois (affiliation=true, publisher=true): (A) client-id={DESIGNSAFE_CLIENT_ID} "
            f"[DesignSafe, tacc_hosted_data]; (B) query={TACC_AFFILIATION_QUERY} [staff_coauthor]"
        ),
        collaborations=list(collaborations_by_id.values()),
        people=people,
        institutions=institutions,
    )
    for key, value in sorted(statistics.items()):
        print(f"[{SOURCE}] {key}: {value}")


if __name__ == "__main__":
    main()
