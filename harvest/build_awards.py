"""Institution at the time of award for NSF/NIH PIs and co-PIs, and the award relationship types that follow.

SCHEMA.md "Institution at the time": an award participant's institution must be supported by evidence dated to the
award's active period (start year .. end/expiration year), with one rule for lead PIs and co-PIs. Inputs are
data/evidence/affiliation_evidence.json (OpenAlex affiliation years and ORCID employment per NSF/NIH person id),
USAspending subawards under the same prime award, and the same person's other awards in an overlapping period.
"""

import collections
import datetime
import json

import build_institutions
import common

AFFILIATION_EVIDENCE_PATH = common.PROJECT_ROOT / "data" / "evidence" / "affiliation_evidence.json"
USABLE_MATCH_CONFIDENCES = {"high", "medium"}
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
EVIDENCE_SOURCES = ("orcid_employment", "openalex_author_affiliation_years")
# Another award of the same person counts for a missing co-PI when the active periods overlap within this many years.
CROSS_AWARD_YEAR_TOLERANCE = 1
MIN_TACC_SHARE_OF_CONTAINER_YEARS = 0.5
TACC_INSTITUTION_ID = f"ror:{common.TACC_ROR_ID}"
UT_AUSTIN_INSTITUTION_ID = f"ror:{common.UT_AUSTIN_ROR_ID}"
AWARD_ID_PREFIXES = ("award:nsf:", "award:nih:")
AWARD_RELATIONSHIP_TYPES = {"tacc_lead_award", "tacc_partner_award", "tacc_mention_award"}


def load_affiliation_evidence(path=AFFILIATION_EVIDENCE_PATH):
    """person id -> evidence entry (all confidences; callers filter with is_usable_match)."""
    if not path.exists():
        print(f"warning: no affiliation evidence at {path}; award co-PIs stay unknown")
        return {}
    return {entry["person_id"]: entry for entry in json.loads(path.read_text())["people"]}


def without_rejected_orcid_evidence(evidence_by_person, rejected_evidence_links):
    """Evidence for award assignment: an ORCID the plausibility check rejected for this person (build_people
    rejected_evidence_links) contributes no ORCID employment."""
    filtered = {}
    for person_id, entry in evidence_by_person.items():
        orcid = (entry.get("orcid") or "").strip().rsplit("/", 1)[-1].upper()
        if orcid and (person_id, orcid) in rejected_evidence_links:
            entry = dict(entry, affiliations=[affiliation for affiliation in entry["affiliations"]
                                              if affiliation["source"] != "orcid_employment"])
        filtered[person_id] = entry
    return filtered


def is_usable_match(entry):
    return bool(entry) and entry.get("match_confidence") in USABLE_MATCH_CONFIDENCES


def weaker_confidence(first, second):
    return first if CONFIDENCE_RANK[first] <= CONFIDENCE_RANK[second] else second


class InstitutionHierarchy:
    """ROR parent chains (cached ROR records), so TACC counts as within UT Austin and the more specific of two
    nested affiliations can be kept."""

    def __init__(self, ror_organizations):
        self.ror_organizations = ror_organizations
        self.ancestor_ids_by_id = {}
        self.fetch_failures = []

    def ancestors(self, institution_id):
        if institution_id in self.ancestor_ids_by_id:
            return self.ancestor_ids_by_id[institution_id]
        self.ancestor_ids_by_id[institution_id] = set()
        if not institution_id or not institution_id.startswith("ror:"):
            return set()
        ror = institution_id.removeprefix("ror:")
        organization = self.ror_organizations.get(ror)
        if organization is None:
            try:
                organization = build_institutions.fetch_ror_organization(ror)
            except Exception as error:
                self.fetch_failures.append((institution_id, repr(error)))
                return set()
            self.ror_organizations[ror] = organization
        ancestor_ids = set()
        parent_rors = build_institutions.ror_parent_ids(organization)
        # Several ROR parents mark a consortium or joint facility (NSF CI Compass lists Utah, Notre Dame, USC, ...),
        # not a unit of each: treating it as inside all of them let it swallow a co-PI's own university affiliation.
        if len(parent_rors) != 1:
            self.ancestor_ids_by_id[institution_id] = ancestor_ids
            return ancestor_ids
        for parent_ror in parent_rors:
            parent_id = f"ror:{parent_ror}"
            ancestor_ids.add(parent_id)
            ancestor_ids |= self.ancestors(parent_id)
        ancestor_ids.discard(institution_id)
        self.ancestor_ids_by_id[institution_id] = ancestor_ids
        return ancestor_ids

    def is_same_or_within(self, institution_id, container_id):
        return institution_id == container_id or container_id in self.ancestors(institution_id)

    def most_specific(self, institution_ids):
        """Drop ids that are ancestors of another id in the set ({UT Austin, TACC} -> {TACC})."""
        unique_ids = set(institution_ids)
        return {institution_id for institution_id in unique_ids
                if not any(institution_id in self.ancestors(other_id) for other_id in unique_ids if other_id != institution_id)}


def resolve_evidence_institution_names(evidence_by_person, institutions, ror_organizations, use_ror_api):
    """ORCID employments carry free-text organizations (name: ids); match them like participant affiliations.

    Rewrites the evidence entries in place; returns the number of distinct names resolved.
    """
    occurrences = collections.Counter(
        affiliation["institution_name"] for entry in evidence_by_person.values() if is_usable_match(entry)
        for affiliation in entry["affiliations"]
        if affiliation["institution_id"].startswith("name:") and affiliation.get("institution_name"))
    if not occurrences:
        return 0
    resolved, _, _ = build_institutions.resolve_names(
        occurrences, build_institutions.load_aliases(),
        build_institutions.build_dataset_name_index(institutions, ror_organizations), ror_organizations, use_ror_api)
    for entry in evidence_by_person.values():
        for affiliation in entry["affiliations"]:
            match = resolved.get(affiliation.get("institution_name")) if affiliation["institution_id"].startswith("name:") else None
            if match:
                affiliation["institution_id"] = match[0]
    return len(resolved)


def award_active_period(collaboration):
    """(start year, end year) from the award's start and end/expiration dates; end missing -> start year only."""
    start_year = int(collaboration["start_date"][:4]) if collaboration.get("start_date") else collaboration.get("year")
    if start_year is None:
        return None
    end_year = int(collaboration["end_date"][:4]) if collaboration.get("end_date") else start_year
    return start_year, max(start_year, end_year)


def describe_period(period):
    return f"award active period {period[0]}-{period[1]}" if period[0] != period[1] else f"year {period[0]}"


def orcid_span(affiliation):
    """(first year, last year) of an ORCID employment; an open end means current, an undated entry is unusable."""
    start_year, end_year = affiliation.get("start_year"), affiliation.get("end_year")
    if start_year is None and end_year is None:
        return None
    if start_year is None:
        return end_year, end_year
    return start_year, end_year if end_year is not None else datetime.date.today().year


def openalex_years_in_period(affiliation, period):
    return [year for year in affiliation.get("years_with_works") or [] if period[0] <= year <= period[1]]


def dated_candidates(entry, period):
    """institution id -> the person's affiliations overlapping the award's active period: ORCID employment spans and
    OpenAlex affiliation years together, so evidence covering only part of a long award cannot hide the rest."""
    candidates = collections.defaultdict(list)
    for affiliation in entry["affiliations"]:
        if affiliation["source"] == "orcid_employment":
            span = orcid_span(affiliation)
            if span and span[0] <= period[1] and span[1] >= period[0]:
                candidates[affiliation["institution_id"]].append(affiliation)
        elif affiliation["source"] == "openalex_author_affiliation_years" and openalex_years_in_period(affiliation, period):
            candidates[affiliation["institution_id"]].append(affiliation)
    return candidates


def evidence_window(entry, period):
    """The award's active period, or the year before it starts (the proposal year) when the person has no dated
    evidence inside the period at all (e.g. TACC evidence through 2024, award starts 2025, no later works yet)."""
    if dated_candidates(entry, period):
        return period
    return period[0] - 1, period[0] - 1


def window_note(window, period):
    if window == period:
        return ""
    return f" (no dated evidence inside {describe_period(period)}; used the year before it starts)"


def orcid_covers_period(candidates, period):
    covered_years = set()
    for affiliations in candidates.values():
        for affiliation in affiliations:
            span = orcid_span(affiliation) if affiliation["source"] == "orcid_employment" else None
            if span:
                covered_years |= set(range(max(span[0], period[0]), min(span[1], period[1]) + 1))
    current_year = datetime.date.today().year
    return covered_years >= set(range(period[0], min(period[1], current_year) + 1))


def is_specific_tacc_evidence(candidates, period, hierarchy):
    """True if the person's TACC evidence in the period is more than a stray TACC-tagged paper.

    OpenAlex tags TACC staff papers with both TACC and UT Austin, but also tags some UT Austin faculty who co-author
    with TACC staff with TACC (e.g. TACC in 1 of 6 UT Austin years). ORCID employment at TACC always counts;
    OpenAlex TACC years count when they are at least MIN_TACC_SHARE_OF_CONTAINER_YEARS of the years with works at
    an institution containing TACC (UT Austin).
    """
    tacc_affiliations = [affiliation for candidate_id, affiliations in candidates.items()
                         if hierarchy.is_same_or_within(candidate_id, TACC_INSTITUTION_ID) for affiliation in affiliations]
    if any(affiliation["source"] == "orcid_employment" for affiliation in tacc_affiliations):
        return True
    tacc_years = {year for affiliation in tacc_affiliations for year in openalex_years_in_period(affiliation, period)}
    container_years = {year for candidate_id, affiliations in candidates.items()
                       if candidate_id != TACC_INSTITUTION_ID and hierarchy.is_same_or_within(TACC_INSTITUTION_ID, candidate_id)
                       for affiliation in affiliations if affiliation["source"] == "openalex_author_affiliation_years"
                       for year in openalex_years_in_period(affiliation, period)}
    return bool(tacc_years) and len(tacc_years) >= MIN_TACC_SHARE_OF_CONTAINER_YEARS * len(container_years)


def co_pi_candidates(entry, period, hierarchy):
    """Dated candidates for a co-PI: ORCID alone when its employment spans cover the whole active period (self-reported,
    and OpenAlex adds co-author affiliation noise), else ORCID and OpenAlex together. Weak TACC evidence counts as UT
    Austin. period is the evidence window (evidence_window)."""
    candidates = dated_candidates(entry, period)
    if orcid_covers_period(candidates, period):
        candidates = {institution_id: orcid_affiliations for institution_id, affiliations in candidates.items()
                      if (orcid_affiliations := [affiliation for affiliation in affiliations
                                                 if affiliation["source"] == "orcid_employment"])}
    if is_specific_tacc_evidence(candidates, period, hierarchy):
        return candidates
    demoted = collections.defaultdict(list)
    for institution_id, affiliations in candidates.items():
        is_tacc = hierarchy.is_same_or_within(institution_id, TACC_INSTITUTION_ID)
        demoted[UT_AUSTIN_INSTITUTION_ID if is_tacc else institution_id].extend(affiliations)
    return demoted


def describe_support(institution_id, supporting_affiliations, period):
    name = next((affiliation["institution_name"] for affiliation in supporting_affiliations
                 if affiliation["institution_id"] == institution_id and affiliation.get("institution_name")),
                supporting_affiliations[0].get("institution_name") or institution_id)
    parts = []
    # Clipped to the period: employment years outside the award would publish a career history (and an open end,
    # the current employer) that the placement does not rest on.
    spans = sorted({(max(span[0], period[0]), min(span[1], period[1])) for affiliation in supporting_affiliations
                    if affiliation["source"] == "orcid_employment" and (span := orcid_span(affiliation))})
    if spans:
        parts.append(f"ORCID employment at {name} ({', '.join(f'{first}-{last}' for first, last in spans)})")
    years = sorted({year for affiliation in supporting_affiliations
                    if affiliation["source"] == "openalex_author_affiliation_years"
                    for year in openalex_years_in_period(affiliation, period)})
    if years:
        parts.append(f"OpenAlex works affiliated with {name} in {', '.join(map(str, years))}")
    return f"{' and '.join(parts)} within {describe_period(period)}"


def support_method(supporting_affiliations):
    sources = {affiliation["source"] for affiliation in supporting_affiliations}
    return next(source for source in EVIDENCE_SOURCES if source in sources)


def support_confidence(supporting_affiliations, period, entry):
    """ORCID employment or OpenAlex works in at least two years of the period -> high; one OpenAlex year -> medium."""
    years = {year for affiliation in supporting_affiliations
             if affiliation["source"] == "openalex_author_affiliation_years"
             for year in openalex_years_in_period(affiliation, period)}
    has_orcid = any(affiliation["source"] == "orcid_employment" for affiliation in supporting_affiliations)
    confidence = "high" if has_orcid or len(years) >= 2 else "medium"
    return weaker_confidence(confidence, entry["match_confidence"])


def tacc_refinement(entry, period, institution_id, hierarchy):
    """Evidence placing the person at TACC during the active period when they are placed at an institution that
    contains TACC (UT Austin), or None: nested affiliations collapse to the most specific."""
    if institution_id == TACC_INSTITUTION_ID or not hierarchy.is_same_or_within(TACC_INSTITUTION_ID, institution_id):
        return None
    window = evidence_window(entry, period)
    candidates = dated_candidates(entry, window)
    supporting_affiliations = [affiliation for candidate_id, affiliations in candidates.items()
                               if hierarchy.is_same_or_within(candidate_id, TACC_INSTITUTION_ID)
                               for affiliation in affiliations]
    if not supporting_affiliations or not is_specific_tacc_evidence(candidates, window, hierarchy):
        return None
    return {"method": support_method(supporting_affiliations),
            "detail": f"{describe_support(TACC_INSTITUTION_ID, supporting_affiliations, window)}"
                      f"{window_note(window, period)}; TACC is a unit of {institution_id}",
            "confidence": support_confidence(supporting_affiliations, window, entry)}


def subawardee_ids_by_prime(collaborations):
    subawardee_ids = collections.defaultdict(set)
    for collaboration in collaborations.values():
        prime_id = (collaboration.get("identifiers") or {}).get("prime_award_collaboration_id")
        if collaboration.get("kind") != "subaward" or not prime_id:
            continue
        for participant in collaboration.get("participants") or []:
            if participant["role"] == "subawardee_org" and participant.get("institution_id"):
                subawardee_ids[prime_id].add(participant["institution_id"])
    return subawardee_ids


def supported_years(affiliations, window):
    """Years of the window covered by ORCID employment spans or OpenAlex works in these affiliations."""
    years = set()
    for affiliation in affiliations:
        if affiliation["source"] == "orcid_employment":
            span = orcid_span(affiliation)
            if span:
                years |= set(range(max(span[0], window[0]), min(span[1], window[1]) + 1))
        else:
            years |= set(openalex_years_in_period(affiliation, window))
    return years


def nested_support_years(institution_id, candidates, window, hierarchy):
    """Supported years of a candidate together with the candidates nested with it (TACC with UT Austin)."""
    return supported_years([affiliation for candidate_id, affiliations in candidates.items()
                            if hierarchy.is_same_or_within(institution_id, candidate_id)
                            or hierarchy.is_same_or_within(candidate_id, institution_id)
                            for affiliation in affiliations], window)


def co_pi_institution_at_time(entry, period, subawardee_ids, awardee_id, hierarchy):
    """Returns (institution_id, evidence, outcome); institution_id None when unknown.

    Candidates are the person's own dated affiliations overlapping the award's active period (co_pi_candidates),
    collapsed to the most specific of nested affiliations. One candidate -> assigned. Several ->
    the unique one that is (part of) the awardee or of a USAspending subawardee under this prime, provided the person's
    evidence covers it in at least as many years of the window as every other candidate (1 year at a subawardee
    vs 6 at another candidate is a conflict, not the subawardee), else unknown.
    A subawardee is never assigned without the person's own dated evidence for it. The person's current email domain
    is deliberately not used: it would put people who later moved to TACC at TACC on their earlier awards.
    outcome is a short label for statistics: the method used, 'awardee_tiebreak', 'conflict' or 'no_dated_evidence'.
    """
    window = evidence_window(entry, period)
    candidates = co_pi_candidates(entry, window, hierarchy)
    if not candidates:
        return None, None, "no_dated_evidence"
    candidate_ids = hierarchy.most_specific(candidates)
    subawarded_ids = {institution_id for institution_id in candidate_ids
                      if any(hierarchy.is_same_or_within(institution_id, subawardee_id) for subawardee_id in subawardee_ids)}
    awardee_part_ids = {institution_id for institution_id in candidate_ids
                        if awardee_id and hierarchy.is_same_or_within(institution_id, awardee_id)}
    if len(candidate_ids) == 1:
        chosen_id = next(iter(candidate_ids))
        outcome = support_method(candidates[chosen_id])
    elif len(subawarded_ids | awardee_part_ids) == 1 and all(
            len(nested_support_years(next(iter(subawarded_ids | awardee_part_ids)), candidates, window, hierarchy))
            >= len(nested_support_years(other_id, candidates, window, hierarchy)) for other_id in candidate_ids):
        chosen_id, outcome = next(iter(subawarded_ids | awardee_part_ids)), "awardee_tiebreak"
    else:
        return None, {"conflicting_candidates": sorted(candidate_ids), "window": list(window)}, "conflict"
    support = describe_support(chosen_id, candidates[chosen_id], window) + window_note(window, period)
    if chosen_id in subawarded_ids and chosen_id not in awardee_part_ids:
        evidence = {"method": "usaspending_subaward",
                    "detail": f"{support}; that institution received a subaward under this prime award",
                    "confidence": weaker_confidence("high", entry["match_confidence"])}
        outcome = "usaspending_subaward"
    else:
        evidence = {"method": support_method(candidates[chosen_id]), "detail": support,
                    "confidence": support_confidence(candidates[chosen_id], window, entry)}
    if len(candidate_ids) > 1:
        others = sorted(candidates[other_id][0].get("institution_name") or other_id
                        for other_id in candidate_ids if other_id != chosen_id)
        evidence["detail"] += f"; tied with {'; '.join(others)}, chosen because it is (part of) the " \
                              f"{'awardee' if chosen_id in awardee_part_ids else 'subawardee'}"
        evidence["confidence"] = weaker_confidence("medium", evidence["confidence"])
    return chosen_id, evidence, outcome


def set_participant_institution(participant, institution_id, evidence):
    """An inferred placement: institution_name_as_listed stays what the source listed (None for NSF co-PIs)."""
    participant["institution_id"] = institution_id
    participant["institution_status"] = "unresolved" if institution_id.startswith("name:") else "resolved"
    participant["institution_evidence"] = evidence


def refine_participant_to_tacc(participant, entry, period, hierarchy):
    """UT Austin -> TACC when the person's dated evidence in the active period says TACC. Returns True if refined."""
    refinement = tacc_refinement(entry, period, participant["institution_id"], hierarchy) if is_usable_match(entry) else None
    if refinement is None:
        return False
    previous = participant.get("institution_evidence") or {}
    if previous.get("method") in ("nsf_awardee_rule", "source_listed"):
        refinement["detail"] = f"lead PI at awardee ({previous.get('detail')}); {refinement['detail']}"
    participant["institution_id"] = TACC_INSTITUTION_ID
    participant["institution_status"] = "resolved"
    participant["institution_evidence"] = refinement
    return True


def person_name_key(participant):
    """Normalized full name, only when it carries a middle name/initial (first, middle, last), else None."""
    tokens = common.normalize_name(participant.get("name_as_listed")).split()
    return " ".join(tokens) if len(tokens) >= 3 else None


def resolved_award_roles(collaborations):
    """Award PIs/co-PIs whose institution came from the awardee rule or dated evidence (not from cross-award reuse)."""
    resolved = []
    for collaboration in collaborations.values():
        period = award_active_period(collaboration)
        if not collaboration["id"].startswith(AWARD_ID_PREFIXES) or period is None:
            continue
        for participant in collaboration.get("participants") or []:
            if participant["role"] in ("pi", "co_pi") and participant.get("institution_id") \
                    and (participant.get("institution_evidence") or {}).get("method") != "cross_award_dated":
                resolved.append((collaboration["id"], period, participant))
    return resolved


def overlapping_award_institutions(collaboration_id, period, participant, resolved_roles, tolerance):
    """institution id -> [(other award id, period, role, how matched, other participant)] for the same person's other
    awards whose active period overlaps this one +-tolerance years."""
    name_key = person_name_key(participant)
    matches = collections.defaultdict(list)
    for other_collaboration_id, other_period, other in resolved_roles:
        if other_collaboration_id == collaboration_id:
            continue
        same_person_id = bool(participant.get("person_id")) and other.get("person_id") == participant["person_id"]
        same_full_name = name_key is not None and person_name_key(other) == name_key
        if not (same_person_id or same_full_name):
            continue
        if other_period[0] > period[1] + tolerance or other_period[1] < period[0] - tolerance:
            continue
        matches[other["institution_id"]].append((other_collaboration_id, other_period, other["role"],
                                                 "same person id" if same_person_id else "same full name with middle initial",
                                                 other))
    return matches


def cross_award_evidence(institution_id, matches, period, hierarchy, tolerance):
    supporting = sorted({support[:4] for candidate_id in matches if hierarchy.is_same_or_within(institution_id, candidate_id)
                         for support in matches[candidate_id]})
    described = "; ".join(f"{role} on {other_id} ({first}-{last}, {how})"
                          for other_id, (first, last), role, how in supporting)
    return {"method": "cross_award_dated",
            "detail": f"at {institution_id} as {described}, overlapping {describe_period(period)}"
                      + (f" +-{tolerance} year" if tolerance else ""),
            "confidence": "medium"}


def cross_award_institution(collaboration_id, period, participant, resolved_roles, hierarchy):
    """(institution_id, evidence) from the same person's other awards overlapping this one, or (None, reason) when
    there are none or they disagree."""
    matches = overlapping_award_institutions(collaboration_id, period, participant, resolved_roles,
                                             CROSS_AWARD_YEAR_TOLERANCE)
    if not matches:
        return None, "no_overlapping_award"
    institution_ids = hierarchy.most_specific(matches)
    if len(institution_ids) > 1:
        return None, "cross_award_conflict"
    institution_id = next(iter(institution_ids))
    return institution_id, cross_award_evidence(institution_id, matches, period, hierarchy, CROSS_AWARD_YEAR_TOLERANCE)


def make_tacc_consistent(collaborations, resolved_roles, hierarchy, stats):
    """A person placed at UT Austin on one award and at TACC on another whose active period overlaps it is at TACC on
    both: the UT Austin placement is the less specific of two nested affiliations, not different evidence. No year
    tolerance here: a TACC placement on a 2011-2017 award says nothing about 2018 (people leave)."""
    for collaboration in collaborations.values():
        period = award_active_period(collaboration)
        if not collaboration["id"].startswith(AWARD_ID_PREFIXES) or period is None:
            continue
        for participant in collaboration.get("participants") or []:
            institution_id = participant.get("institution_id")
            if participant["role"] not in ("pi", "co_pi") or not institution_id or institution_id == TACC_INSTITUTION_ID \
                    or not hierarchy.is_same_or_within(TACC_INSTITUTION_ID, institution_id):
                continue
            matches = overlapping_award_institutions(collaboration["id"], period, participant, resolved_roles, 0)
            tacc_matches = {candidate_id: supports for candidate_id, supports in matches.items()
                            if hierarchy.is_same_or_within(candidate_id, TACC_INSTITUTION_ID)}
            if not tacc_matches:
                continue
            previous = participant.get("institution_evidence") or {}
            evidence = cross_award_evidence(TACC_INSTITUTION_ID, tacc_matches, period, hierarchy, 0)
            evidence["detail"] = f"{previous.get('detail')}; refined to TACC: {evidence['detail']}"
            participant["institution_id"] = TACC_INSTITUTION_ID
            participant["institution_status"] = "resolved"
            participant["institution_evidence"] = evidence
            stats[f"{participant['role']}_refined_to_tacc_cross_award"] += 1


def assign_award_institutions(collaborations, evidence_by_person, hierarchy):
    """Lead PIs: awardee, refined to TACC on dated evidence. Co-PIs: dated evidence, then the same person's other
    awards in an overlapping period. Returns stats."""
    subawardee_ids = subawardee_ids_by_prime(collaborations)
    stats = collections.Counter()
    conflicts = []
    # The person's own dated evidence names several institutions: another award must not pick one of them.
    conflicted_participant_ids = set()
    for collaboration in collaborations.values():
        if not collaboration["id"].startswith(AWARD_ID_PREFIXES):
            continue
        period = award_active_period(collaboration)
        awardee_id = next((participant.get("institution_id") for participant in collaboration.get("participants") or []
                           if participant["role"] == "awardee_org"), None)
        if period is None:
            stats["awards_without_year"] += 1
            continue
        for participant in collaboration.get("participants") or []:
            entry = evidence_by_person.get(participant.get("person_id"))
            if participant["role"] == "pi":
                stats["lead_pi"] += 1
                if participant.get("institution_id") == UT_AUSTIN_INSTITUTION_ID:
                    stats["lead_pi_at_ut_austin"] += 1
                if participant.get("institution_id") and refine_participant_to_tacc(participant, entry, period, hierarchy):
                    stats["lead_pi_moved_to_tacc"] += 1
            elif participant["role"] == "co_pi" and participant["institution_status"] == "missing":
                stats["co_pi_missing_before"] += 1
                if not is_usable_match(entry):
                    stats["co_pi_no_usable_person_match"] += 1
                    continue
                institution_id, evidence, outcome = co_pi_institution_at_time(
                    entry, period, subawardee_ids.get(collaboration["id"], set()), awardee_id, hierarchy)
                stats[f"co_pi_{outcome}"] += 1
                if outcome == "conflict":
                    conflicts.append({"award": collaboration["id"], "person_id": participant["person_id"], **evidence})
                    conflicted_participant_ids.add(id(participant))
                    continue
                if institution_id is None:
                    continue
                set_participant_institution(participant, institution_id, evidence)
                if refine_participant_to_tacc(participant, entry, period, hierarchy):
                    stats["co_pi_refined_to_tacc"] += 1

    resolved_roles = resolved_award_roles(collaborations)
    for collaboration in collaborations.values():
        period = award_active_period(collaboration)
        if not collaboration["id"].startswith(AWARD_ID_PREFIXES) or period is None:
            continue
        for participant in collaboration.get("participants") or []:
            if participant["role"] != "co_pi" or participant["institution_status"] != "missing" \
                    or id(participant) in conflicted_participant_ids:
                continue
            institution_id, evidence = cross_award_institution(
                collaboration["id"], period, participant, resolved_roles, hierarchy)
            if institution_id is None:
                stats[f"co_pi_still_missing_{evidence}"] += 1
                continue
            stats["co_pi_cross_award_dated"] += 1
            set_participant_institution(participant, institution_id, evidence)
    make_tacc_consistent(collaborations, resolved_award_roles(collaborations), hierarchy, stats)
    return {"counts": stats, "conflicts": conflicts}


def is_at_tacc(participant, hierarchy):
    return bool(participant.get("institution_id")) and hierarchy.is_same_or_within(participant["institution_id"], TACC_INSTITUTION_ID)


def recompute_award_relationship_types(collaborations, hierarchy):
    """tacc_lead_award iff the lead PI was at TACC at the time; tacc_partner_award iff a co-PI was, or the harvester
    found TACC named as a partner in the abstract; else tacc_mention_award. Returns {award id: (before, after)}."""
    changes = {}
    for collaboration in collaborations.values():
        if not collaboration["id"].startswith(AWARD_ID_PREFIXES):
            continue
        before = list(collaboration.get("relationship_types") or [])
        participants = collaboration.get("participants") or []
        if any(participant["role"] == "pi" and is_at_tacc(participant, hierarchy) for participant in participants):
            award_type = "tacc_lead_award"
        elif "tacc_partner_award" in before or any(
                participant["role"] == "co_pi" and is_at_tacc(participant, hierarchy) for participant in participants):
            award_type = "tacc_partner_award"
        else:
            award_type = "tacc_mention_award"
        after = [value for value in before if value not in AWARD_RELATIONSHIP_TYPES] + [award_type]
        collaboration["relationship_types"] = after
        if before != after:
            changes[collaboration["id"]] = (before, after)
    return changes
