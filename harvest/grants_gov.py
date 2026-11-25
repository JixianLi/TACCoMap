"""Harvest open/forecasted Grants.gov funding opportunities relevant to TACC-type work.

Run: python3 harvest/grants_gov.py
"""

import datetime
import html
import json
import re
import time
import urllib.request

import common

SOURCE = "grants_gov"
SEARCH_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
OPEN_STATUSES = "forecasted|posted"
HISTORICAL_STATUSES = ("closed", "archived")
PAGE_SIZE = 100
DESCRIPTION_MAX_CHARACTERS = 4000

KEYWORDS = [
    "cyberinfrastructure",
    "high performance computing",
    "advanced computing",
    "supercomputing",
    "research computing",
    "scientific computing",
    "computational science",
    "data infrastructure",
    "science gateways",
    "research infrastructure",
    "AI infrastructure",
]
NSF_CISE_OAC_ALN = "47.070"



def keyword_query(keyword):
    # Grants.gov ORs unquoted words ("high performance computing" unquoted gives ~25x more hits).
    return f'"{keyword}"' if " " in keyword else keyword


def search_all(search_body):
    hits = []
    start_record = 0
    while True:
        body = dict(search_body, oppStatuses=search_body.get("oppStatuses", OPEN_STATUSES),
                    rows=PAGE_SIZE, startRecordNum=start_record)
        data = common.fetch_json(SEARCH_URL, source=SOURCE, method="POST", body=body)["data"]
        page_hits = data.get("oppHits") or []
        hits.extend(page_hits)
        start_record += len(page_hits)
        if not page_hits or start_record >= data.get("hitCount", 0):
            return hits


def hit_count(search_body):
    body = dict(search_body, rows=1)
    return common.fetch_json(SEARCH_URL, source=SOURCE, method="POST", body=body)["data"].get("hitCount", 0)


def fetch_detail(opportunity_id):
    # Bypasses common.fetch_json on purpose: it always writes the raw cache, and detail payloads carry
    # agency contact emails, which must never be stored.
    request = urllib.request.Request(
        DETAIL_URL, data=json.dumps({"opportunityId": int(opportunity_id)}).encode(), method="POST",
        headers={"User-Agent": common.USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
        time.sleep(0.2)
    except Exception as error:
        print(f"[{SOURCE}] detail fetch failed for {opportunity_id}: {error}")
        return {}
    data = payload.get("data") or {}
    return data.get("synopsis") or data.get("forecast") or {}


def us_date_to_iso(text):
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def html_to_plain_text(raw_html):
    if not raw_html:
        return None
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    text = common.scrub_contact_details(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > DESCRIPTION_MAX_CHARACTERS:
        text = text[:DESCRIPTION_MAX_CHARACTERS].rsplit(" ", 1)[0] + " …"
    return text or None


def dollars_or_none(text):
    try:
        value = float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_collaboration(hit, matched_keywords, detail, retrieved_at):
    opportunity_id = hit["id"]
    url = f"https://www.grants.gov/search-results-detail/{opportunity_id}"
    start_date = us_date_to_iso(hit.get("openDate"))
    return {
        "id": f"opportunity:grantsgov:{opportunity_id}",
        "kind": "funding_opportunity",
        "relationship_types": ["funding_opportunity"],
        "title": common.scrub_contact_details(html.unescape(hit.get("title") or "")) or None,
        "year": int(start_date[:4]) if start_date else None,
        "start_date": start_date,
        "end_date": us_date_to_iso(hit.get("closeDate")),
        "amount_usd": None,
        "funder": hit.get("agency") or hit.get("agencyCode"),
        "venue": None,
        "url": url,
        "description": html_to_plain_text(detail.get("synopsisDesc") or detail.get("forecastDesc")),
        "identifiers": {
            "opportunity_number": hit.get("number"),
            "aln": hit.get("cfdaList") or [],
        },
        "participants": [],
        "participants_truncated": False,
        "sources": [common.source_reference(SOURCE, opportunity_id, url, retrieved_at)],
        "status": hit.get("oppStatus"),
        "document_type": hit.get("docType"),
        "agency_code": hit.get("agencyCode"),
        "matched_keywords": sorted(matched_keywords),
        "estimated_total_funding_usd": dollars_or_none(detail.get("estimatedFunding")),
        "award_ceiling_usd": dollars_or_none(detail.get("awardCeiling")),
        "award_floor_usd": dollars_or_none(detail.get("awardFloor")),
    }


def report_historical_counts():
    print(f"[{SOURCE}] closed/archived hit counts (not harvested; per query, overlapping):")
    queries = [("keyword", keyword_query(keyword)) for keyword in KEYWORDS] + [("cfda", NSF_CISE_OAC_ALN)]
    for status in HISTORICAL_STATUSES:
        total = 0
        for field, value in queries:
            count = hit_count({field: value, "oppStatuses": status})
            total += count
            print(f"  {status:9s} {field}={value}: {count}")
        print(f"  {status:9s} sum over queries (with duplicates): {total}")


def main():
    retrieved_at = common.utc_now_iso()
    hits_by_id = {}
    matched_keywords_by_id = {}

    queries = [(keyword, {"keyword": keyword_query(keyword)}) for keyword in KEYWORDS]
    queries.append((f"ALN {NSF_CISE_OAC_ALN}", {"cfda": NSF_CISE_OAC_ALN}))
    for label, search_body in queries:
        hits = search_all(search_body)
        print(f"[{SOURCE}] {label}: {len(hits)} open/forecasted")
        for hit in hits:
            hits_by_id.setdefault(hit["id"], hit)
            matched_keywords_by_id.setdefault(hit["id"], set()).add(label)

    collaborations = []
    for opportunity_id, hit in hits_by_id.items():
        detail = fetch_detail(opportunity_id)
        collaborations.append(build_collaboration(hit, matched_keywords_by_id[opportunity_id], detail, retrieved_at))

    report_historical_counts()

    common.write_normalized(
        SOURCE,
        query_description=(
            "Grants.gov search2, statuses forecasted|posted, keywords (phrases quoted): "
            + ", ".join(KEYWORDS) + f"; plus ALN {NSF_CISE_OAC_ALN}. Union by opportunity id; "
            "details from fetchOpportunity."
        ),
        collaborations=collaborations,
        people=[],
        institutions=[],
    )


if __name__ == "__main__":
    main()
