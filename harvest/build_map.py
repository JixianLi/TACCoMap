"""The map preview's data files (map/public/data/). Contract: SCHEMA.md, "Map aggregate" and "Map records".

map_institutions.json, loaded with the page: each institution carries counts of its collaborations grouped by
(year, kind, relationship types), so the browser can re-count under any filter without double counting a record
that has several relationship types.
map_records.json, loaded on the first selection: one row per collaboration with its title, link and the number of
people from each institution on it. No names: people are only counted.
"""

import collections

import common

MAP_KINDS = ("paper", "award", "subaward", "dataset", "allocation")
# Order fixes each type's bit in the counts' relationship mask: bit i set <=> MAP_RELATIONSHIP_TYPES[i] applies.
MAP_RELATIONSHIP_TYPES = (
    "staff_coauthor", "resource_acknowledgment", "tacc_lead_award", "tacc_partner_award", "tacc_mention_award",
    "subaward", "tacc_hosted_data", "tacc_resource_allocation",
)
# TACC and its parent university are drawn as one hub glyph, not as collaborator circles.
HUB_INSTITUTION_IDS = ("ror:00xg4bh43", "ror:00hj54h04")
COORDINATE_DECIMALS = 4


def collaboration_year(collaboration):
    if collaboration.get("start_date"):
        return int(collaboration["start_date"][:4])
    return collaboration.get("year")


def relationship_mask(relationship_types):
    return sum(1 << index for index, relationship_type in enumerate(MAP_RELATIONSHIP_TYPES)
               if relationship_type in relationship_types)


def count_rows(counter):
    """[[year or null, kind index, relationship mask, count], ...] sorted for stable output."""
    return [[year, kind_index, mask, count]
            for (year, kind_index, mask), count in sorted(counter.items(), key=lambda item: (item[0][0] or 0, item[0][1:]))]


def rounded(value):
    return None if value is None else round(value, COORDINATE_DECIMALS)


def people_per_institution(collaboration):
    """[[institution id, number of people], ...], most people first. A person listed under several institutions
    counts for each; a participant without a person id is told apart by the name as listed."""
    people_by_institution = collections.defaultdict(set)
    for participant in collaboration.get("participants") or []:
        person_key = participant.get("person_id") or ("name", participant.get("name_as_listed"))
        institution_ids = ([participant["institution_id"]] if participant.get("institution_id") else []) \
            + list(participant.get("additional_institution_ids") or [])
        for institution_id in institution_ids:
            people_by_institution[institution_id].add(person_key)
    return sorted(([institution_id, len(people)] for institution_id, people in people_by_institution.items()),
                  key=lambda pair: (-pair[1], pair[0]))


def record_url(collaboration):
    doi = (collaboration.get("identifiers") or {}).get("doi")
    return collaboration.get("url") or (f"https://doi.org/{doi}" if doi else None)


def build_map_records(collaborations, institutions, clean_text):
    """clean_text: the same rounding of coordinates in titles that the published record files get."""
    records = []
    for collaboration in collaborations.values():
        kind = collaboration.get("kind")
        if kind not in MAP_KINDS:
            continue
        institution_people = [pair for pair in people_per_institution(collaboration) if pair[0] in institutions]
        # A record whose institutions are known only from the record itself (no participant carries them) still
        # lists them, with no people counted.
        listed = {pair[0] for pair in institution_people}
        institution_people += [[institution_id, 0] for institution_id in collaboration.get("institution_ids") or []
                               if institution_id in institutions and institution_id not in listed]
        if not institution_people:
            continue
        records.append([
            collaboration["id"], MAP_KINDS.index(kind), collaboration_year(collaboration),
            relationship_mask(collaboration.get("relationship_types") or []),
            clean_text(collaboration.get("title")) if collaboration.get("title") else None,
            record_url(collaboration), institution_people, bool(collaboration.get("participants_truncated")),
        ])
    return {
        "generated_at": common.utc_now_iso(),
        "kinds": list(MAP_KINDS),
        "relationship_types": list(MAP_RELATIONSHIP_TYPES),
        "fields": ["id", "kind_index", "year", "relationship_mask", "title", "url", "institution_people",
                   "participants_truncated"],
        "records": records,
    }


def build_map_aggregate(collaborations, institutions, direct_relationship_types):
    counts_by_institution = collections.defaultdict(collections.Counter)
    collaboration_counts = collections.Counter()
    for collaboration in collaborations.values():
        kind = collaboration.get("kind")
        if kind not in MAP_KINDS:
            continue
        key = (collaboration_year(collaboration), MAP_KINDS.index(kind),
               relationship_mask(collaboration.get("relationship_types") or []))
        collaboration_counts[key] += 1
        for institution_id in collaboration.get("institution_ids") or []:
            counts_by_institution[institution_id][key] += 1
    map_institutions = [{
        "id": institution_id,
        "name": institution.get("name"),
        "city": institution.get("city"),
        "country_code": institution.get("country_code"),
        "institution_type": institution.get("institution_type"),
        "latitude": rounded(institution.get("latitude")),
        "longitude": rounded(institution.get("longitude")),
        "counts": count_rows(counts_by_institution[institution_id]),
    } for institution_id, institution in institutions.items() if institution_id in counts_by_institution]
    return {
        "generated_at": common.utc_now_iso(),
        "kinds": list(MAP_KINDS),
        "relationship_types": list(MAP_RELATIONSHIP_TYPES),
        "direct_relationship_types": [relationship_type for relationship_type in MAP_RELATIONSHIP_TYPES
                                      if relationship_type in direct_relationship_types],
        "hub_institution_ids": list(HUB_INSTITUTION_IDS),
        "collaboration_counts": count_rows(collaboration_counts),
        "institutions": map_institutions,
    }
