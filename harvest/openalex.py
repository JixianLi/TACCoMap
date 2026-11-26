"""Harvest TACC-related papers from OpenAlex into data/normalized/openalex.json.

Sets:
  staff         works with a TACC-affiliated author (institution id OR raw affiliation string)
  acknowledgment works whose title/abstract/full text mentions "Texas Advanced Computing Center"

Optional: set OPENALEX_API_KEY to use a personal key (without it, requests share a small
free daily budget per IP address, and only page-number paging up to 10,000 results works).
The key is never printed: error messages carry the URL with api_key redacted.
"""

import collections
import os
import re

import common

SOURCE = "openalex"
API_BASE = "https://api.openalex.org"
TACC_PHRASE = "Texas Advanced Computing Center"
TACC_AFFILIATION_PATTERN = re.compile(r"texas advanced computing cent|\bTACC\b", re.IGNORECASE)
WORK_FIELDS = ("id,doi,title,publication_year,publication_date,primary_location,"
               "authorships,is_authors_truncated,type")
# OpenAlex work types recorded as datasets (e.g. Zenodo, Figshare and HuBMAP deposits), software included, as DataCite
# software is; every other type (data-paper and software-paper too) is a paper.
DATASET_WORK_TYPES = {"dataset", "software"}
INSTITUTION_FIELDS = ("id,ror,display_name,country_code,geo,type,lineage,associated_institutions,"
                      "homepage_url")
INSTITUTION_BATCH_SIZE = 50
DANGLING_CONTACT_LABEL_PATTERN = re.compile(
    r"(?:\be-?mails?(?: address(?:es)?)?\s*[:.]?|\belectronic address\s*:|\b(?:tel(?:ephone)?|phone|fax|mobile|orcid)\.?\s*:"
    r"|\bcorrespd\.)\s*(?=$|[;,)|\]#])",
    re.IGNORECASE)
PER_PAGE = 200
PAGE_PAGING_LIMIT = 10_000

TACC_ROR_URL = f"https://ror.org/{common.TACC_ROR_ID}"
TACC_OPENALEX_URL = f"https://openalex.org/{common.TACC_OPENALEX_ID}"
TEXAS_ASSOCIATION_OF_COMMUNITY_COLLEGES_ROR_URL = "https://ror.org/02smbds60"
NCSA_BULGARIA_ROR_URL = "https://ror.org/03g9ch715"
NCSA_ILLINOIS_ROR_URL = "https://ror.org/03r10zj06"
NCSA_ILLINOIS_OPENALEX_URL = "https://openalex.org/I4210135837"
BULGARIA_PATTERN = re.compile(r"bulgari|sofia", re.IGNORECASE)
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
# Letters that Unicode NFKD does not decompose to ASCII; apostrophes and LaTeX braces are removed
# so "D'Hondt"/"D’Hondt" and "B{ö}ser"/"Böser" compare equal.
NAME_TRANSLITERATION = str.maketrans({"ı": "i", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "ß": "ss",
                                      "œ": "oe", "Œ": "Oe", "æ": "ae", "Æ": "Ae", "ə": "a", "Ə": "A",
                                      "đ": "d", "Đ": "D", "ð": "d", "þ": "th", "'": "", "’": "", "‘": "",
                                      "{": "", "}": "", "\\": ""})
# Below this length, one-edit and prefix matches pair unrelated names (Chen/Cheng, Clay/Clayton).
FUZZY_TOKEN_MIN_LENGTH = 5

STAFF_FILTERS = [
    f"institutions.id:{common.TACC_OPENALEX_ID}",
    f'raw_affiliation_strings.search:"{TACC_PHRASE}"',
]
ACKNOWLEDGMENT_FILTER = f'fulltext.search:"{TACC_PHRASE}"'


def api_key():
    return os.environ.get("OPENALEX_API_KEY") or None


def with_api_key(params):
    return {**params, "api_key": api_key()} if api_key() else params


def redacted(text):
    return text.replace(api_key(), "REDACTED") if api_key() else text


def fetch_openalex(url):
    """common.fetch_json, but any exception message is re-raised with the API key redacted."""
    try:
        return common.fetch_json(url, source=SOURCE)
    except Exception as error:
        raise RuntimeError(redacted(f"OpenAlex request failed ({error!r}) for {url}")) from None


def short_id(openalex_url):
    return openalex_url.rsplit("/", 1)[-1] if openalex_url else None


def without_contact_details(text):
    """Raw affiliation strings often embed emails and phone/fax numbers; the contract forbids storing them."""
    if not text:
        return text
    # Dropped rather than marked: these strings become names and name: ids.
    cleaned = common.scrub_contact_details(text).replace(common.CONTACT_REMOVED, "")
    cleaned = DANGLING_CONTACT_LABEL_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"[(\[<][\s,;:.]*[)\]>]", "", cleaned)
    return re.sub(r"\s*[,;]\s*(?=[,;]|$)|\s{2,}", " ", cleaned).strip(" ,;.|") or None


def strip_prefix(value, prefix):
    return value[len(prefix):] if value and value.startswith(prefix) else value


def fetch_works(filter_expression):
    return fetch_works_by_cursor(filter_expression) if api_key() else fetch_works_by_page(filter_expression)


def fetch_works_by_cursor(filter_expression):
    """Cursor paging has no result cap but OpenAlex only allows it with an API key."""
    works = []
    cursor = "*"
    num_pages = 0
    while cursor:
        params = with_api_key({"filter": filter_expression, "per-page": PER_PAGE,
                               "cursor": cursor, "select": WORK_FIELDS})
        payload = fetch_openalex(common.build_url(f"{API_BASE}/works", params))
        if num_pages == 0:
            print(f"[{SOURCE}] {filter_expression}: {payload['meta']['count']} works reported")
        num_pages += 1
        works.extend(payload["results"])
        cursor = payload["meta"].get("next_cursor") if payload["results"] else None
    print(f"[{SOURCE}] {filter_expression}: fetched {len(works)} works in {num_pages} cursor pages")
    return works


def fetch_works_by_page(filter_expression):
    """Keyless fallback: page=N works without a key but only up to 10,000 results."""
    works = []
    index_page = 1
    while True:
        params = with_api_key({"filter": filter_expression, "per-page": PER_PAGE,
                               "page": index_page, "select": WORK_FIELDS})
        payload = fetch_openalex(common.build_url(f"{API_BASE}/works", params))
        total_count = payload["meta"]["count"]
        if index_page == 1:
            print(f"[{SOURCE}] {filter_expression}: {total_count} works reported")
            if total_count > PAGE_PAGING_LIMIT:
                print(f"[{SOURCE}] WARNING: only the first {PAGE_PAGING_LIMIT} works are reachable")
        works.extend(payload["results"])
        if not payload["results"] or index_page * PER_PAGE >= min(total_count, PAGE_PAGING_LIMIT):
            break
        index_page += 1
    print(f"[{SOURCE}] {filter_expression}: fetched {len(works)} works in {index_page} pages")
    return works


def fetch_institutions(openalex_institution_ids):
    sorted_ids = sorted(openalex_institution_ids)
    records = []
    for index_batch in range(0, len(sorted_ids), INSTITUTION_BATCH_SIZE):
        batch = sorted_ids[index_batch:index_batch + INSTITUTION_BATCH_SIZE]
        params = with_api_key({"filter": "openalex_id:" + "|".join(batch),
                               "per-page": INSTITUTION_BATCH_SIZE, "select": INSTITUTION_FIELDS})
        payload = fetch_openalex(common.build_url(f"{API_BASE}/institutions", params))
        records.extend(payload["results"])
    return records


def institution_id_for(ror_url, openalex_url):
    ror = strip_prefix(ror_url, "https://ror.org/")
    return f"ror:{ror}" if ror else f"openalex:{short_id(openalex_url)}"


def raw_strings_for_institution(authorship, openalex_institution_url):
    linked = [affiliation.get("raw_affiliation_string") or ""
              for affiliation in authorship.get("affiliations") or []
              if openalex_institution_url in (affiliation.get("institution_ids") or [])]
    return linked or list(authorship.get("raw_affiliation_strings") or [])


def replace_authorship_institution(authorship, institution, replacement_openalex_url, replacement_ror_url):
    old_openalex_url = institution.get("id")
    institution["id"] = replacement_openalex_url
    institution["ror"] = replacement_ror_url
    for affiliation in authorship.get("affiliations") or []:
        affiliation["institution_ids"] = [
            replacement_openalex_url if institution_url == old_openalex_url else institution_url
            for institution_url in affiliation.get("institution_ids") or []]


def correct_known_mistags(works):
    """Fix OpenAlex institution matches known to be wrong, in place, before anything reads them.

    OpenAlex maps "TACC" to the Texas Association of Community Colleges (whose homepage is
    tacc.org) and maps "NCSA" to the Bulgarian NCSA even for Urbana-Champaign authors.
    """
    correction_counts = collections.Counter()
    for work in works:
        for authorship in work.get("authorships") or []:
            institutions = authorship.get("institutions") or []
            for institution in institutions:
                raw_text = " ".join(raw_strings_for_institution(authorship, institution.get("id")))
                if (institution.get("ror") == TEXAS_ASSOCIATION_OF_COMMUNITY_COLLEGES_ROR_URL
                        and TACC_AFFILIATION_PATTERN.search(raw_text)):
                    replace_authorship_institution(authorship, institution, TACC_OPENALEX_URL, TACC_ROR_URL)
                    correction_counts["community_colleges_to_tacc"] += 1
                elif institution.get("ror") == NCSA_BULGARIA_ROR_URL and not BULGARIA_PATTERN.search(raw_text):
                    replace_authorship_institution(authorship, institution, NCSA_ILLINOIS_OPENALEX_URL,
                                                   NCSA_ILLINOIS_ROR_URL)
                    correction_counts["ncsa_bulgaria_to_illinois"] += 1
            unique_institutions = list({institution.get("id"): institution for institution in institutions}.values())
            if len(unique_institutions) != len(institutions):
                authorship["institutions"] = unique_institutions
    return correction_counts


def name_tokens(name):
    """Comparable name tokens: transliterated and accent-folded, hyphenated parts and adjacent
    token pairs also joined ("Di Marco" ~ "DiMarco"), single-letter initials and generational
    suffixes dropped, German ae/oe/ue spellings folded to match umlauts."""
    words = [word for word in common.normalize_name((name or "").translate(NAME_TRANSLITERATION).replace("-", " ")).split()
             if len(word) >= 2 and word not in NAME_SUFFIXES]
    tokens = set(words) | {first + second for first, second in zip(words, words[1:])}
    return {token.replace("ae", "a").replace("oe", "o").replace("ue", "u") for token in tokens}


def is_at_most_one_edit_apart(first, second):
    if abs(len(first) - len(second)) > 1:
        return False
    shorter, longer = sorted((first, second), key=len)
    for idx, (shorter_letter, longer_letter) in enumerate(zip(shorter, longer)):
        if shorter_letter != longer_letter:
            remainder = idx + 1 if len(shorter) == len(longer) else idx
            return shorter[remainder:] == longer[idx + 1:]
    return True


def are_tokens_compatible(first, second):
    """Equal, or (both long enough) a truncation or a one-letter spelling/transliteration
    difference such as Kovačev/Kovachev, Smithh/Smith, Yilma/Yılmaz."""
    if first == second:
        return True
    if min(len(first), len(second)) < FUZZY_TOKEN_MIN_LENGTH:
        return False
    return first.startswith(second) or second.startswith(first) or is_at_most_one_edit_apart(first, second)


def is_name_compatible(raw_author_name, display_name):
    """True unless both names have usable tokens and no pair of tokens is compatible.

    Order-free on purpose (Chinese family-name-first order), which makes it lenient: a shared
    given name also counts as compatible.
    """
    raw_tokens = name_tokens(raw_author_name)
    display_tokens = name_tokens(display_name)
    if not raw_tokens or not display_tokens:
        return True
    return any(are_tokens_compatible(raw_token, display_token)
               for raw_token in raw_tokens for display_token in display_tokens)


def is_tacc_authorship(authorship):
    if any(short_id(institution.get("id")) == common.TACC_OPENALEX_ID
           for institution in authorship.get("institutions") or []):
        return True
    return any(TACC_AFFILIATION_PATTERN.search(text or "")
               for text in authorship.get("raw_affiliation_strings") or [])


def raw_affiliation_for_institution(authorship, openalex_institution_url):
    for affiliation in authorship.get("affiliations") or []:
        if openalex_institution_url in (affiliation.get("institution_ids") or []):
            return without_contact_details(affiliation.get("raw_affiliation_string"))
    raw_strings = authorship.get("raw_affiliation_strings") or []
    return without_contact_details(raw_strings[0]) if raw_strings else None


def person_from_author(author, openalex_work_id, work_url, retrieved_at):
    openalex_author_id = short_id(author.get("id"))
    orcid = strip_prefix(author.get("orcid"), "https://orcid.org/")
    if orcid:
        person_id = f"orcid:{orcid}"
    elif openalex_author_id:
        person_id = f"openalex:{openalex_author_id}"
    else:
        return None
    return {
        "id": person_id,
        "name": author.get("display_name"),
        "orcid": orcid,
        "openalex_author_id": openalex_author_id,
        "other_ids": {},
        # An ORCID-only authorship has no OpenAlex author record, so it points to the work that lists it.
        "sources": [common.source_reference(SOURCE, openalex_author_id, author.get("id"), retrieved_at)
                    if openalex_author_id else common.source_reference(SOURCE, openalex_work_id, work_url, retrieved_at)],
    }


def person_from_raw_name(raw_author_name, openalex_work_id, work_url, retrieved_at):
    normalized = common.normalize_name(raw_author_name)
    if not normalized:
        return None
    return {
        "id": f"name:{normalized}|{SOURCE}",
        "name": raw_author_name,
        "orcid": None,
        "openalex_author_id": None,
        "other_ids": {},
        "sources": [common.source_reference(SOURCE, openalex_work_id, work_url, retrieved_at)],
    }


def participant_from_authorship(authorship, person):
    author = authorship.get("author") or {}
    name_as_listed = without_contact_details(authorship.get("raw_author_name") or author.get("display_name"))
    person_id = person["id"] if person else None
    institutions = authorship.get("institutions") or []
    raw_strings = [cleaned for cleaned in map(without_contact_details, authorship.get("raw_affiliation_strings") or [])
                   if cleaned]
    if institutions:
        institution_ids = [institution_id_for(institution.get("ror"), institution.get("id"))
                           for institution in institutions]
        participant_record = common.participant(
            name_as_listed=name_as_listed, role="author", person_id=person_id,
            institution_id=institution_ids[0],
            institution_name_as_listed=raw_affiliation_for_institution(authorship, institutions[0].get("id")),
            additional_institution_ids=institution_ids[1:])
    elif raw_strings:
        participant_record = common.participant(
            name_as_listed=name_as_listed, role="author", person_id=person_id,
            institution_name_as_listed=raw_strings[0],
            additional_institution_ids=[f"name:{common.normalize_name(text)}" for text in raw_strings[1:]])
    else:
        participant_record = common.participant(name_as_listed=name_as_listed, role="author", person_id=person_id)
    participant_record["institution_evidence"] = (
        None if participant_record["institution_status"] == "missing"
        else {"method": "source_listed", "detail": "openalex authorship", "confidence": "high"})
    return participant_record


def collaboration_from_work(work, relationship_types, people_by_id, retrieved_at, misattribution_counts):
    openalex_work_id = short_id(work["id"])
    doi = strip_prefix(work.get("doi"), "https://doi.org/")
    doi = doi.lower() if doi else None
    primary_location = work.get("primary_location") or {}
    venue = (primary_location.get("source") or {}).get("display_name")
    participants = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        raw_author_name = without_contact_details(authorship.get("raw_author_name"))
        if raw_author_name and author.get("id") and not is_name_compatible(raw_author_name, author.get("display_name")):
            # OpenAlex attached this authorship to a different person; keep only the listed name.
            person = person_from_raw_name(raw_author_name, openalex_work_id, work["id"], retrieved_at)
            misattribution_counts["dropped_author_ids"] += 1
            misattribution_counts["dropped_orcids"] += bool(author.get("orcid"))
        else:
            person = person_from_author(author, openalex_work_id, work["id"], retrieved_at)
        if person:
            people_by_id.setdefault(person["id"], person)
        participants.append(participant_from_authorship(authorship, person))
    kind = "dataset" if work.get("type") in DATASET_WORK_TYPES else "paper"
    return {
        "id": f"{kind}:doi:{doi}" if doi else f"{kind}:openalex:{openalex_work_id}",
        "kind": kind,
        "openalex_type": work.get("type"),
        "relationship_types": relationship_types,
        "title": common.scrub_contact_details(work.get("title")),
        "year": work.get("publication_year"),
        "start_date": work.get("publication_date"),
        "end_date": None,
        "amount_usd": None,
        "funder": None,
        "venue": venue,
        "url": f"https://doi.org/{doi}" if doi else work["id"],
        "description": None,
        "identifiers": {"doi": doi, "openalex": openalex_work_id},
        "participants": participants,
        "participants_truncated": bool(work.get("is_authors_truncated")),
        "sources": [common.source_reference(SOURCE, openalex_work_id, work["id"], retrieved_at)],
    }


def parent_ror_for(record):
    for associated in record.get("associated_institutions") or []:
        if associated.get("relationship") == "parent" and associated.get("ror"):
            return strip_prefix(associated["ror"], "https://ror.org/")
    return None


def institution_from_record(record, retrieved_at):
    geo = record.get("geo") or {}
    ror = strip_prefix(record.get("ror"), "https://ror.org/")
    openalex_institution_id = short_id(record["id"])
    return {
        "id": institution_id_for(record.get("ror"), record["id"]),
        "name": record.get("display_name"),
        "ror": ror,
        "openalex_id": openalex_institution_id,
        "country_code": record.get("country_code") or geo.get("country_code"),
        "city": geo.get("city"),
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
        "source_types": [record["type"]] if record.get("type") else [],
        "parent_ror": parent_ror_for(record),
        "website": record.get("homepage_url"),
        "sources": [common.source_reference(SOURCE, openalex_institution_id, record["id"], retrieved_at)],
    }


def report(collaborations):
    year_counts = collections.Counter(collaboration["year"] for collaboration in collaborations)
    relationship_counts = collections.Counter(
        "+".join(sorted(collaboration["relationship_types"])) for collaboration in collaborations)
    status_counts = collections.Counter(
        participant["institution_status"]
        for collaboration in collaborations for participant in collaboration["participants"])
    truncated = sum(collaboration["participants_truncated"] for collaboration in collaborations)
    print(f"[{SOURCE}] relationship types: {dict(relationship_counts)}")
    print(f"[{SOURCE}] OpenAlex work types: {dict(collections.Counter(collaboration['openalex_type'] for collaboration in collaborations).most_common())}")
    print(f"[{SOURCE}] authorship institution_status: {dict(status_counts)}")
    print(f"[{SOURCE}] works with truncated author lists: {truncated}")
    print(f"[{SOURCE}] years: " + ", ".join(
        f"{year}:{count}" for year, count in sorted(year_counts.items(), key=lambda item: (item[0] is None, item[0] or 0))))


def main():
    retrieved_at = common.utc_now_iso()
    works_by_id = {}
    staff_set_ids = set()
    for filter_expression in STAFF_FILTERS:
        for work in fetch_works(filter_expression):
            works_by_id.setdefault(work["id"], work)
            staff_set_ids.add(work["id"])
    acknowledgment_set_ids = set()
    for work in fetch_works(ACKNOWLEDGMENT_FILTER):
        works_by_id.setdefault(work["id"], work)
        acknowledgment_set_ids.add(work["id"])
    correction_counts = correct_known_mistags(works_by_id.values())
    print(f"[{SOURCE}] institution mis-tag corrections (authorship-level): {dict(correction_counts)}")

    people_by_id = {}
    misattribution_counts = collections.Counter()
    collaborations_by_id = {}
    shared_doi_merges = 0
    staff_query_without_tacc_author = 0
    for work_url, work in works_by_id.items():
        has_tacc_author = any(is_tacc_authorship(authorship) for authorship in work.get("authorships") or [])
        relationship_types = []
        if has_tacc_author:
            relationship_types.append("staff_coauthor")
        elif work_url in acknowledgment_set_ids:
            relationship_types.append("resource_acknowledgment")
        if has_tacc_author and work_url in acknowledgment_set_ids:
            relationship_types.append("resource_acknowledgment")
        if not relationship_types:
            staff_query_without_tacc_author += 1
            continue
        collaboration = collaboration_from_work(work, relationship_types, people_by_id, retrieved_at,
                                                misattribution_counts)
        existing = collaborations_by_id.get(collaboration["id"])
        if existing:
            # Several OpenAlex works can share one DOI (preprint/version duplicates); keep the first.
            shared_doi_merges += 1
            existing["relationship_types"] += [value for value in relationship_types
                                               if value not in existing["relationship_types"]]
            existing["sources"] += collaboration["sources"]
        else:
            collaborations_by_id[collaboration["id"]] = collaboration
    collaborations = list(collaborations_by_id.values())
    print(f"[{SOURCE}] misattributed authorships (raw name incompatible with OpenAlex author name): "
          f"{dict(misattribution_counts)}")
    print(f"[{SOURCE}] merged {shared_doi_merges} works that share a DOI with another work")
    if staff_query_without_tacc_author:
        print(f"[{SOURCE}] skipped {staff_query_without_tacc_author} staff-query works where no "
              f"authorship matched TACC locally")

    referenced_institution_ids = {common.TACC_OPENALEX_ID}
    for work in works_by_id.values():
        for authorship in work.get("authorships") or []:
            for institution in authorship.get("institutions") or []:
                if institution.get("id"):
                    referenced_institution_ids.add(short_id(institution["id"]))
    institution_records = fetch_institutions(referenced_institution_ids)
    institutions = [institution_from_record(record, retrieved_at) for record in institution_records]
    print(f"[{SOURCE}] institutions: {len(referenced_institution_ids)} referenced, "
          f"{len(institution_records)} fetched")

    report(collaborations)
    common.write_normalized(
        SOURCE,
        query_description=(
            f"OpenAlex works: staff = filter {STAFF_FILTERS[0]} UNION {STAFF_FILTERS[1]}; "
            f"acknowledgment = filter {ACKNOWLEDGMENT_FILTER} (title/abstract/full text). "
            "staff_coauthor if any authorship has TACC institution or a raw affiliation matching "
            "TACC; resource_acknowledgment if in the acknowledgment set. Institution mis-tags corrected: "
            "Texas Association of Community Colleges -> TACC when the raw affiliation names TACC; "
            "NCSA Bulgaria -> NCSA Illinois unless the raw affiliation names Bulgaria/Sofia."),
        collaborations=collaborations,
        people=list(people_by_id.values()),
        institutions=institutions,
    )


if __name__ == "__main__":
    main()
