"""Harvest TACC-related NSF awards from the NSF Award Search API into data/normalized/nsf.json."""

import collections
import html
import re

import common

SOURCE = "nsf"
API_BASE = "https://api.nsf.gov/services/v1/awards.json"
SINGLE_AWARD_URL = "https://api.nsf.gov/services/v1/awards/{award_number}.json"
AWARD_PAGE_URL = "https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_number}"
KEYWORDS = ["tacc.utexas.edu", '"Texas Advanced Computing Center"', "TACC"]
# The API documents 25 as the page size; larger values work today but are not promised.
RESULTS_PER_PAGE = 25
PRINT_FIELDS = ",".join([
    "id", "title", "agency", "awardeeName", "awardeeCity", "awardeeStateCode", "awardeeCountryCode",
    "startDate", "expDate", "estimatedTotalAmt", "fundsObligatedAmt", "piFirstName", "piMiddeInitial",
    "piLastName", "piEmail", "coPDPI", "pdPIName", "piId", "abstractText", "fundProgramName",
    "primaryProgram", "transType",
])
FLAGSHIP_AWARDS = {
    "0622780": "Ranger", "1134872": "Stampede", "1540931": "Stampede2", "2320757": "Stampede3",
    "1818253": "Frontera", "1854828": "Frontera", "2323116": "LCCF", "1341711": "Wrangler",
    "1419152": "Chameleon", "1520817": "DesignSafe", "1445604": "Jetstream", "2005506": "Jetstream2",
    "1053575": "XSEDE", "1548562": "XSEDE2",
}
TACC_EMAIL_DOMAIN = "tacc.utexas.edu"
UT_AUSTIN_INSTITUTION_ID = f"ror:{common.UT_AUSTIN_ROR_ID}"
TACC_MENTION_PATTERN = re.compile(r"\bTACC\b|Texas Advanced Computing Center", re.IGNORECASE)
PARTNER_WORD_PATTERN = re.compile(
    r"partner|sub-?award|sub-?contract|sub-?recipient|collaborat|in conjunction with|jointly|team",
    re.IGNORECASE)
DESCRIPTION_LENGTH = 400
FORMER_CO_PI_SUFFIX_PATTERN = re.compile(r"\s*\(Former\)\s*$", re.IGNORECASE)


def fetch_keyword_awards(keyword):
    awards = []
    offset = 0  # the API's offset is 0-based despite docs examples starting at 1
    while True:
        url = common.build_url(API_BASE, {"keyword": keyword, "rpp": RESULTS_PER_PAGE, "offset": offset,
                                          "printFields": PRINT_FIELDS})
        response = common.fetch_json(url, source=SOURCE)["response"]
        page_awards = response.get("award", [])
        awards.extend(page_awards)
        total_count = response.get("metadata", {}).get("totalCount", 0)
        offset += RESULTS_PER_PAGE
        if not page_awards or offset >= total_count:
            break
    print(f"[{SOURCE}] keyword {keyword!r}: {len(awards)} awards (API totalCount {total_count})")
    return awards


def fetch_single_award(award_number):
    url = common.build_url(SINGLE_AWARD_URL.format(award_number=award_number), {"printFields": PRINT_FIELDS})
    awards = common.fetch_json(url, source=SOURCE)["response"].get("award", [])
    return awards[0] if awards else None


def iso_date(nsf_date):
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", nsf_date or "")
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else None


def clean_abstract(text):
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", without_tags).strip()


def split_name_and_email(listed_person):
    """coPDPI entries look like 'First M Last email@host'; the email may be absent."""
    tokens = listed_person.split()
    if tokens and "@" in tokens[-1]:
        return " ".join(tokens[:-1]), tokens[-1].lower()
    return " ".join(tokens), None


def strip_former_marker(name):
    """NSF appends '(Former)' to co-PIs who left the award; it is not part of the person's name."""
    return FORMER_CO_PI_SUFFIX_PATTERN.sub("", name)


def first_last_key(name):
    tokens = common.normalize_name(name).split()
    if not tokens:
        return None
    return (tokens[0], tokens[-1])


def is_tacc_email(email):
    return bool(email) and email.split("@")[-1] == TACC_EMAIL_DOMAIN


def people_on_award(award):
    """Return [(name, email, role)] for the lead PI and co-PIs. Emails are used transiently only."""
    lead_name = " ".join(part for part in [award.get("piFirstName"), award.get("piMiddeInitial"),
                                           award.get("piLastName")] if part)
    lead_name = lead_name or award.get("pdPIName") or ""
    listed = [(lead_name, (award.get("piEmail") or "").lower() or None, "pi")]
    for listed_person in award.get("coPDPI") or []:
        name, email = split_name_and_email(listed_person)
        if name:
            listed.append((name, email, "co_pi"))
    return listed


def find_conflicting_pi_ids(awards):
    """piIds listed with more than one lead-PI name.

    When an award's PI is replaced, NSF shows the new PI's name but keeps the original piId
    (e.g. Stampede 1134872 lists the current PI with the original PI's piId), so such a piId does not
    identify one person and must not be used as a person key.
    """
    names_by_pi_id = collections.defaultdict(set)
    for award in awards:
        key = first_last_key(people_on_award(award)[0][0])
        if key and award.get("piId"):
            names_by_pi_id[award["piId"]].add(key)
    return {pi_id for pi_id, names in names_by_pi_id.items() if len(names) > 1}


def build_pi_id_by_name(awards, conflicting_pi_ids):
    """Map first/last name keys to NSF piIds so a person listed only as co-PI elsewhere reuses that id."""
    ids_by_name = collections.defaultdict(set)
    for award in awards:
        key = first_last_key(people_on_award(award)[0][0])
        if key and award.get("piId") and award["piId"] not in conflicting_pi_ids:
            ids_by_name[key].add(award["piId"])
    return {key: next(iter(ids)) for key, ids in ids_by_name.items() if len(ids) == 1}


def abstract_names_tacc_as_partner(abstract):
    for sentence in re.split(r"(?<=[.;])\s+", abstract):
        if TACC_MENTION_PATTERN.search(sentence) and PARTNER_WORD_PATTERN.search(sentence):
            return True
    return False


def classify_provisional_relationship(abstract, awardee_is_ut_austin):
    """Provisional type from source facts only; build.py upgrades to lead/partner from dated person evidence."""
    if not awardee_is_ut_austin and abstract_names_tacc_as_partner(abstract):
        return ["tacc_partner_award"]
    return ["tacc_mention_award"]


def build_records(award, pi_id_by_name, conflicting_pi_ids, retrieved_at):
    award_number = award["id"]
    abstract = clean_abstract(award.get("abstractText"))
    awardee_name = award.get("awardeeName") or None
    awardee_is_ut_austin = common.normalize_name(awardee_name) == "university of texas at austin"
    awardee_institution_id = UT_AUSTIN_INSTITUTION_ID if awardee_is_ut_austin else None
    api_url = SINGLE_AWARD_URL.format(award_number=award_number)
    source = common.source_reference(SOURCE, award_number, api_url, retrieved_at)

    awardee_participant = common.participant(name_as_listed=awardee_name or "(awardee not listed)",
                                             role="awardee_org", institution_id=awardee_institution_id,
                                             institution_name_as_listed=awardee_name)
    awardee_participant["institution_evidence"] = (
        {"method": "source_listed", "detail": "NSF awardeeName", "confidence": "high"} if awardee_name else None)
    participants = [awardee_participant]
    people = []
    for name_as_listed, email, role in people_on_award(award):
        name = strip_former_marker(name_as_listed)
        if not name:
            continue
        name_key = first_last_key(name)
        if role == "pi":
            nsf_pi_id = award.get("piId") if award.get("piId") not in conflicting_pi_ids else None
        else:
            nsf_pi_id = pi_id_by_name.get(name_key)
        person_id = f"nsf:{nsf_pi_id}" if nsf_pi_id else f"name:{common.normalize_name(name)}|nsf"
        if role == "pi":
            person_participant = common.participant(
                name_as_listed=name_as_listed, role=role, person_id=person_id,
                institution_id=awardee_institution_id, institution_name_as_listed=awardee_name)
            person_participant["institution_evidence"] = (
                {"method": "nsf_awardee_rule", "detail": f"NSF lead PI must be at the awardee ({awardee_name})",
                 "confidence": "high"} if awardee_name else None)
        else:
            # NSF does not say where co-PIs are; build.py fills this from dated affiliation evidence.
            person_participant = common.participant(name_as_listed=name_as_listed, role=role, person_id=person_id)
            person_participant["institution_evidence"] = None
        # Weak, undated hint only: NSF shows the person's CURRENT email, not the one at award time.
        person_participant["current_email_domain_is_tacc"] = is_tacc_email(email)
        participants.append(person_participant)
        people.append({"id": person_id, "name": name, "orcid": None, "openalex_author_id": None,
                       "other_ids": {"nsf_pi_id": nsf_pi_id} if nsf_pi_id else {}, "sources": [source]})

    amount = award.get("estimatedTotalAmt") or award.get("fundsObligatedAmt")
    start_date = iso_date(award.get("startDate"))
    collaboration = {
        "id": f"award:nsf:{award_number}",
        "kind": "award",
        "relationship_types": classify_provisional_relationship(abstract, awardee_is_ut_austin),
        "title": common.scrub_contact_details(html.unescape(award.get("title") or "")),
        "year": int(start_date[:4]) if start_date else None,
        "start_date": start_date,
        "end_date": iso_date(award.get("expDate")),
        "amount_usd": float(amount) if amount else None,
        "funder": "NSF",
        "venue": award.get("fundProgramName") or None,
        "url": AWARD_PAGE_URL.format(award_number=award_number),
        "description": common.scrub_contact_details(abstract)[:DESCRIPTION_LENGTH] or None,
        "identifiers": {"award_number": award_number, "nsf_transaction_type": award.get("transType")},
        "participants": participants,
        "participants_truncated": False,
        "sources": [source],
    }

    institutions = []
    if awardee_name:
        institutions.append({
            "id": awardee_institution_id or f"name:{common.normalize_name(awardee_name)}",
            "name": awardee_name,
            "ror": common.UT_AUSTIN_ROR_ID if awardee_is_ut_austin else None,
            "openalex_id": None,
            "country_code": award.get("awardeeCountryCode") or None,
            "city": (award.get("awardeeCity") or "").title() or None,
            "latitude": None, "longitude": None, "source_types": [], "parent_ror": None,
            "sources": [source],
        })
    return collaboration, people, institutions


def merge_by_id(records_by_id, new_records):
    """Keep the first record per id but accumulate every award it appears on in its sources."""
    for record in new_records:
        existing = records_by_id.setdefault(record["id"], record)
        if existing is not record:
            existing["sources"].extend(record["sources"])


def main():
    retrieved_at = common.utc_now_iso()
    awards_by_number = {}
    for keyword in KEYWORDS:
        for award in fetch_keyword_awards(keyword):
            awards_by_number.setdefault(award["id"], award)
    print(f"[{SOURCE}] keyword union: {len(awards_by_number)} unique awards")

    missing_flagships = [number for number in FLAGSHIP_AWARDS if number not in awards_by_number]
    for award_number in missing_flagships:
        award = fetch_single_award(award_number)
        if award:
            awards_by_number[award_number] = award
        print(f"[{SOURCE}] flagship {award_number} ({FLAGSHIP_AWARDS[award_number]}) missing from keyword "
              f"union -> fetched by id: {'ok' if award else 'NOT FOUND'}")

    awards = list(awards_by_number.values())
    conflicting_pi_ids = find_conflicting_pi_ids(awards)
    pi_id_by_name = build_pi_id_by_name(awards, conflicting_pi_ids)
    print(f"[{SOURCE}] {len(conflicting_pi_ids)} piIds listed with several PI names; not used as person ids")

    collaborations, people_by_id, institutions_by_id = [], {}, {}
    for award in sorted(awards, key=lambda record: record["id"]):
        collaboration, award_people, award_institutions = build_records(award, pi_id_by_name,
                                                                        conflicting_pi_ids, retrieved_at)
        collaborations.append(collaboration)
        merge_by_id(people_by_id, award_people)
        merge_by_id(institutions_by_id, award_institutions)

    relationship_counts = collections.Counter(kind for record in collaborations
                                              for kind in record["relationship_types"])
    print(f"[{SOURCE}] provisional relationship types: {dict(relationship_counts)}")
    common.write_normalized(
        SOURCE,
        query_description=(f"NSF Award Search API keyword union {KEYWORDS}; plus flagship awards fetched by id "
                           f"if absent: {missing_flagships}. relationship_types are provisional "
                           f"(tacc_partner_award / tacc_mention_award from the abstract only); build.py "
                           f"assigns lead/partner from dated person evidence."),
        collaborations=collaborations, people=list(people_by_id.values()),
        institutions=list(institutions_by_id.values()))


if __name__ == "__main__":
    main()
