"""Shared helpers for TACCoMap harvesters. Contract documented in SCHEMA.md."""

import datetime
import hashlib
import json
import pathlib
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW_DIRECTORY = PROJECT_ROOT / "data" / "raw"
NORMALIZED_DIRECTORY = PROJECT_ROOT / "data" / "normalized"

# Generic client name on purpose: never put personal contact details in requests.
USER_AGENT = "TACCoMap-harvester/0.1"

CONTACT_REMOVED = "[contact removed]"
# Order matters: a labelled value goes with its label ("Electronic address: <Twitter profile URL>"), and URLs and
# emails go before phone numbers so digits inside them are not read as phones.
CONTACT_DETAIL_PATTERNS = [
    re.compile(r"\b(?:electronic\s+address|e-?mail(?:\s+address(?:es)?)?)\s*:\s*(?:\[[^\]]*\]|[^\s,;)\]]+)",
               re.IGNORECASE),
    re.compile(r"\bmailto[:][^\s,;)\]]+", re.IGNORECASE),
    # Cloudflare's email-protection placeholder, as scraped by OpenAlex.
    re.compile(r"\[email\s+protected\]", re.IGNORECASE),
    # The top-level domain must be letters, so chemistry like "Fe3O4@SiO2.NH2" is not an email.
    re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"),
    re.compile(r"[\w.+-]+\s*[\[({<]\s*at\s*[\])}>]\s*[\w-]+(?:\s*(?:[\[({<]\s*dot\s*[\])}>]|\.)\s*[\w-]+)+",
               re.IGNORECASE),
    # Spelled-out "<name> at <host> dot edu" only with a literal "dot": plain " at " is ordinary English.
    re.compile(r"\b[\w.+-]+\s+at\s+[\w-]+(?:\s+dot\s+[\w-]+)+\b", re.IGNORECASE),
    re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.|mobile\.|m\.)?(?:twitter|x|facebook|fb|instagram)\.com/[\w.@%-]+/?",
               re.IGNORECASE),
    re.compile(r"(?<![\w.-])(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w%-]+/?", re.IGNORECASE),
    # A GitHub profile (github.com/<user>) but not a repository (github.com/<user>/<repo>).
    re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+(?:/(?![\w.-]))?(?![\w./-])",
               re.IGNORECASE),
    # Personal pages, including the "https ://<user>.github" form OpenAlex produces from a broken line.
    re.compile(r"https?\s*:\s*//[\w-]+\.github(?:\.io)?\b[^\s,;)\]]*|\b[\w-]+\.github\.io\b[^\s,;)\]]*",
               re.IGNORECASE),
    # "Ph" and bare "Tel" are not labels: they collide with "Ph.D." and "Tel Aviv" / "In-Q-Tel".
    re.compile(r"\b(?:tel(?:ephone)?\.?\s*:|telephone|phone|fax|mobile)\.?\s*[:.]?\s*[+(]?\d[\d\s().\-/]{5,}\d",
               re.IGNORECASE),
    re.compile(r"\+\s?\d[\d\s().\-/]{6,}\d"),
    # North-American format only (optional leading 1, 3-3-4 digits with a dash before the last group),
    # so ZIP+4 codes (5-4) and street numbers are left alone.
    re.compile(r"(?:\b1[\s-]+)?(?:\(\d{3}\)|\b\d{3})[\s.-]?\d{3}-\d{4}\b"),
]
REPEATED_CONTACT_REMOVED_PATTERN = re.compile(rf"{re.escape(CONTACT_REMOVED)}(?:[\s,;]*{re.escape(CONTACT_REMOVED)})+")

TACC_ROR_ID = "00xg4bh43"
TACC_OPENALEX_ID = "I4388891828"
UT_AUSTIN_ROR_ID = "00hj54h04"


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def fetch_json(url, *, source, method="GET", body=None, headers=None, use_cache=True,
               max_retries=4, pause_seconds=0.2):
    """GET/POST a JSON endpoint, caching the raw response under data/raw/<source>/.

    The cache key covers method, URL and body, so re-running a harvester is free and
    reproducible. Delete data/raw/<source>/ to force a refresh.
    """
    cache_key = hashlib.sha256(f"{method} {url} {json.dumps(body, sort_keys=True)}".encode()).hexdigest()[:32]
    cache_path = RAW_DIRECTORY / source / f"{cache_key}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"

    for attempt in range(max_retries):
        try:
            request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload))
    time.sleep(pause_seconds)
    return payload


def build_url(base, params):
    return f"{base}?{urllib.parse.urlencode(params, doseq=True)}"


def normalize_name(text):
    """Lowercase ASCII-folded name with collapsed whitespace; used for name-based ids."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def scrub_contact_details(text):
    """Replace emails (also obfuscated), phone/fax numbers and personal social-media/GitHub URLs with
    CONTACT_REMOVED. The data contract forbids storing personal contact details; use on every free-text field."""
    if not text:
        return text
    scrubbed = text
    for pattern in CONTACT_DETAIL_PATTERNS:
        scrubbed = pattern.sub(CONTACT_REMOVED, scrubbed)
    return REPEATED_CONTACT_REMOVED_PATTERN.sub(CONTACT_REMOVED, scrubbed)


def source_reference(source, source_record_id, url, retrieved_at):
    return {"source": source, "source_record_id": str(source_record_id), "url": url, "retrieved_at": retrieved_at}


def participant(*, name_as_listed, role, person_id=None, institution_id=None,
                institution_name_as_listed=None, additional_institution_ids=None):
    """One party's involvement in one collaboration, with the institution AS OF that record.

    institution_status:
      resolved   - institution_id is a ror:/openalex: id
      unresolved - only a free-text name (institution_id is name:<normalized>, build step tries ROR matching)
      missing    - the source gives no institution at all
    """
    if institution_id and not institution_id.startswith("name:"):
        institution_status = "resolved"
    elif institution_name_as_listed:
        institution_status = "unresolved"
        institution_id = institution_id or f"name:{normalize_name(institution_name_as_listed)}"
    else:
        institution_status = "missing"
        institution_id = None
    return {
        "person_id": person_id,
        "name_as_listed": name_as_listed,
        "role": role,
        "institution_id": institution_id,
        "institution_name_as_listed": institution_name_as_listed,
        "institution_status": institution_status,
        "additional_institution_ids": additional_institution_ids or [],
    }


def write_normalized(source, *, query_description, collaborations, people, institutions):
    """Write data/normalized/<source>.json. Lists are de-duplicated by id (first wins)."""
    def deduplicate(records):
        seen = {}
        for record in records:
            seen.setdefault(record["id"], record)
        return list(seen.values())

    NORMALIZED_DIRECTORY.mkdir(parents=True, exist_ok=True)
    output = {
        "source": source,
        "retrieved_at": utc_now_iso(),
        "query_description": query_description,
        "collaborations": deduplicate(collaborations),
        "people": deduplicate(people),
        "institutions": deduplicate(institutions),
    }
    output_path = NORMALIZED_DIRECTORY / f"{source}.json"
    output_path.write_text(json.dumps(output, indent=1, ensure_ascii=False))
    print(f"[{source}] wrote {output_path}: {len(output['collaborations'])} collaborations, "
          f"{len(output['people'])} people, {len(output['institutions'])} institutions")
    return output_path
