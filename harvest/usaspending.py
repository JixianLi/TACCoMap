"""Harvest FFATA subawards under the NSF awards in data/normalized/nsf.json from USAspending.

Run nsf.py first. Writes data/normalized/usaspending.json.
"""

import collections
import datetime
import json

import common

SOURCE = "usaspending"
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
COUNT_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award_count/"
PRIME_AWARD_PAGE_URL = "https://www.usaspending.gov/award/{generated_id}"
NSF_AGENCY_FILTER = {"type": "awarding", "tier": "toptier", "name": "National Science Foundation"}
# Grants, cooperative agreements and other assistance; NSF primes in nsf.json are all assistance awards.
ASSISTANCE_AWARD_TYPE_CODES = ["02", "03", "04", "05"]
SUBAWARD_FIELDS = [
    "Sub-Award ID", "Sub-Awardee Name", "Sub-Award Date", "Sub-Award Amount", "Sub-Award Description",
    "Sub-Recipient UEI", "Sub-Recipient Location", "Prime Award ID", "Prime Recipient Name",
    "Prime Award Recipient UEI", "prime_award_generated_internal_id",
]
AWARD_IDS_PER_REQUEST = 50
RESULTS_PER_PAGE = 100
# The search endpoints reject earlier time_period dates; the total-count check catches anything older.
EARLIEST_SEARCH_DATE = datetime.date(2007, 10, 1)
DESCRIPTION_LENGTH = 400


def load_nsf_awards():
    nsf_path = common.NORMALIZED_DIRECTORY / "nsf.json"
    collaborations = json.loads(nsf_path.read_text())["collaborations"]
    return {record["identifiers"]["award_number"]: record for record in collaborations}


def count_subawards(filters):
    response = common.fetch_json(COUNT_URL, source=SOURCE, method="POST", body={"subawards": True, "filters": filters})
    return sum(response.get("results", {}).values())


def fetch_one_page(filters, page):
    body = {"subawards": True, "filters": filters, "fields": SUBAWARD_FIELDS, "limit": RESULTS_PER_PAGE,
            "page": page, "sort": "Sub-Award Date", "order": "asc"}
    return common.fetch_json(SEARCH_URL, source=SOURCE, method="POST", body=body)


def fetch_subawards_in_window(base_filters, start_date, end_date):
    """Fetch every row with an action date in [start_date, end_date] without relying on paging.

    No sortable subaward field is unique, so page boundaries over a sort with ties duplicate or
    skip rows. Instead the date window is halved until each window fits in a single page.
    """
    filters = {**base_filters, "time_period": [{"start_date": start_date.isoformat(),
                                                "end_date": end_date.isoformat()}]}
    expected_count = count_subawards(filters)
    if expected_count == 0:
        return []
    if expected_count <= RESULTS_PER_PAGE:
        rows = fetch_one_page(filters, 1).get("results", [])
        if len(rows) != expected_count:
            print(f"[{SOURCE}] WARNING window {start_date}..{end_date}: {len(rows)} rows, API count {expected_count}")
        return rows
    if start_date == end_date:
        # A single day with more rows than a page: paging is unavoidable here, so report it.
        rows, page = [], 1
        while True:
            response = fetch_one_page(filters, page)
            rows.extend(response.get("results", []))
            if not response.get("page_metadata", {}).get("hasNext"):
                break
            page += 1
        print(f"[{SOURCE}] WARNING {start_date}: {expected_count} rows on one day, paged ({len(rows)} rows fetched)")
        return rows
    midpoint = start_date + (end_date - start_date) // 2
    return (fetch_subawards_in_window(base_filters, start_date, midpoint)
            + fetch_subawards_in_window(base_filters, midpoint + datetime.timedelta(days=1), end_date))


def fetch_subawards(award_numbers):
    base_filters = {"award_type_codes": ASSISTANCE_AWARD_TYPE_CODES, "award_ids": award_numbers,
                    "agencies": [NSF_AGENCY_FILTER]}
    total_count = count_subawards(base_filters)
    rows = fetch_subawards_in_window(base_filters, EARLIEST_SEARCH_DATE, datetime.date.today())
    return rows, total_count


def country_code_from_location(location):
    """USAspending gives ISO-3166 alpha-3; the contract wants alpha-2. Only the US mapping is known here."""
    alpha3 = (location or {}).get("location_country_code")
    return "US" if alpha3 == "USA" else None


def institution_record(name, location, source):
    return {
        "id": f"name:{common.normalize_name(name)}",
        "name": name,
        "ror": None,
        "openalex_id": None,
        "country_code": country_code_from_location(location),
        "city": ((location or {}).get("city_name") or "").title() or None,
        "latitude": None, "longitude": None, "source_types": [], "parent_ror": None,
        "sources": [source],
    }


def assign_record_keys(rows):
    """One key per row (= one FFATA report).

    The same subaward number recurs once per reported action, and USAspending exposes no row id,
    so rows identical in prime/subaward number/date/amount get an ordinal suffix rather than
    silently collapsing into one record.
    """
    ordered_rows = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    occurrences = collections.Counter()
    keyed_rows = []
    for row in ordered_rows:
        base_key = (f"{row['Prime Award ID']}:{row.get('Sub-Award ID') or ''}:"
                    f"{row.get('Sub-Award Date')}:{row.get('Sub-Award Amount')}")
        occurrences[base_key] += 1
        record_key = base_key if occurrences[base_key] == 1 else f"{base_key}:{occurrences[base_key]}"
        keyed_rows.append((record_key, row))
    num_suffixed = sum(count - 1 for count in occurrences.values())
    return keyed_rows, num_suffixed


def build_records(record_key, row, prime_award, retrieved_at):
    prime_award_number = row["Prime Award ID"]
    subaward_number = row.get("Sub-Award ID") or ""
    action_date = row.get("Sub-Award Date")
    amount = row.get("Sub-Award Amount")
    generated_id = row.get("prime_award_generated_internal_id")
    prime_url = PRIME_AWARD_PAGE_URL.format(generated_id=generated_id) if generated_id else None
    source = common.source_reference(SOURCE, record_key, prime_url, retrieved_at)

    prime_name = row.get("Prime Recipient Name") or None
    subawardee_name = row.get("Sub-Awardee Name") or None
    participants = [
        common.participant(name_as_listed=prime_name or "(prime recipient not listed)",
                           role="prime_awardee_org", institution_name_as_listed=prime_name),
        common.participant(name_as_listed=subawardee_name or "(subawardee not listed)",
                           role="subawardee_org", institution_name_as_listed=subawardee_name),
    ]
    description = common.scrub_contact_details((row.get("Sub-Award Description") or "").strip())
    collaboration = {
        "id": f"subaward:usaspending:{record_key}",
        "kind": "subaward",
        "relationship_types": ["subaward"],
        "title": common.scrub_contact_details(
            f"Subaward to {subawardee_name} under NSF {prime_award_number}: {prime_award['title']}"),
        "year": int(action_date[:4]) if action_date else None,
        "start_date": action_date,
        "end_date": None,
        "amount_usd": amount,
        "funder": "NSF",
        "venue": None,
        "url": prime_url,
        "description": description[:DESCRIPTION_LENGTH] or None,
        "identifiers": {
            "prime_award_number": prime_award_number,
            "prime_award_collaboration_id": prime_award["id"],
            "prime_award_generated_internal_id": generated_id,
            "subaward_number": subaward_number or None,
            "subawardee_uei": row.get("Sub-Recipient UEI"),
            "prime_awardee_uei": row.get("Prime Award Recipient UEI"),
        },
        "participants": participants,
        "participants_truncated": False,
        "sources": [source],
    }
    institutions = []
    if prime_name:
        institutions.append(institution_record(prime_name, None, source))
    if subawardee_name:
        institutions.append(institution_record(subawardee_name, row.get("Sub-Recipient Location"), source))
    return collaboration, institutions


def main():
    retrieved_at = common.utc_now_iso()
    nsf_awards = load_nsf_awards()
    award_numbers = sorted(nsf_awards)

    rows = []
    total_api_count = 0
    for index_batch in range(0, len(award_numbers), AWARD_IDS_PER_REQUEST):
        batch_rows, batch_count = fetch_subawards(award_numbers[index_batch:index_batch + AWARD_IDS_PER_REQUEST])
        rows.extend(batch_rows)
        total_api_count += batch_count
        if len(batch_rows) != batch_count:
            print(f"[{SOURCE}] WARNING batch {index_batch // AWARD_IDS_PER_REQUEST}: fetched {len(batch_rows)} rows, "
                  f"API count {batch_count}")
    print(f"[{SOURCE}] fetched {len(rows)} rows; API count {total_api_count}")
    unrelated_rows = [row for row in rows if row.get("Prime Award ID") not in nsf_awards]
    rows = [row for row in rows if row.get("Prime Award ID") in nsf_awards]
    print(f"[{SOURCE}] {len(rows)} subaward rows for {len(award_numbers)} NSF primes "
          f"({len(unrelated_rows)} rows with unexpected prime ids dropped)")

    keyed_rows, num_suffixed = assign_record_keys(rows)
    print(f"[{SOURCE}] {num_suffixed} rows share prime/subaward number/date/amount with another row; "
          f"kept as separate records with an ordinal suffix")
    collaborations, institutions_by_id = [], {}
    for record_key, row in keyed_rows:
        collaboration, institutions = build_records(record_key, row, nsf_awards[row["Prime Award ID"]], retrieved_at)
        collaborations.append(collaboration)
        for institution in institutions:
            existing = institutions_by_id.setdefault(institution["id"], institution)
            if existing is not institution and institution["sources"][0] not in existing["sources"]:
                existing["sources"].append(institution["sources"][0])
                existing["city"] = existing["city"] or institution["city"]
                existing["country_code"] = existing["country_code"] or institution["country_code"]

    primes_with_subawards = collections.Counter(row["Prime Award ID"] for row in rows)
    print(f"[{SOURCE}] {len(primes_with_subawards)} primes have subawards; top: "
          f"{primes_with_subawards.most_common(5)}")
    common.write_normalized(
        SOURCE,
        query_description=("USAspending /search/spending_by_award/ subawards=true, NSF awarding agency, "
                           "award_ids = every NSF award number in data/normalized/nsf.json; fetched by "
                           "action-date windows small enough for one page each, checked against "
                           "/search/spending_by_award_count/"),
        collaborations=collaborations, people=[], institutions=list(institutions_by_id.values()))


if __name__ == "__main__":
    main()
