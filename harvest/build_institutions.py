"""Institution steps of build.py: resolve free-text affiliations to ROR, enrich from ROR, classify. Contract: SCHEMA.md."""

import collections
import difflib
import fractions
import hashlib
import html
import json
import pathlib
import re
import unicodedata
import urllib.parse

import common

OVERRIDES_PATH = common.PROJECT_ROOT / "data" / "institution_overrides.json"
ALIASES_PATH = common.PROJECT_ROOT / "data" / "manual" / "institution_aliases.json"
REJECTED_ROR_MATCHES_PATH = common.PROJECT_ROOT / "data" / "review" / "rejected_ror_matches.json"
BULK_AFFILIATION_PAPERS_PATH = common.PROJECT_ROOT / "data" / "review" / "bulk_affiliation_papers.json"
BULK_AFFILIATION_MIN_PARTICIPANTS = 50
# OpenAlex keeps only the first 100 authors of a truncated list, so a smeared institution is diluted by authors
# it never reached (ICEHAP Chiba at 40-48% of Pierre Auger lists); a lower share already marks a smear there.
BULK_AFFILIATION_MIN_SHARE = fractions.Fraction(1, 2)
BULK_AFFILIATION_MIN_SHARE_TRUNCATED = fractions.Fraction(1, 3)
ROR_API = "https://api.ror.org/v2/organizations"

INSTITUTION_TYPES = ["government", "commercial", "nonprofit", "educational", "research", "healthcare", "archive", "other"]

# ROR/OpenAlex organization types. Anything else a source puts in source_types (e.g. NIH "SCHOOLS OF MEDICINE")
# is a source-specific category and is moved to source_categories.
# Order matters when a ROR record has several types (e.g. ["education", "funder"]): first hit wins.
SOURCE_TYPE_PRIORITY = ["education", "facility", "company", "government", "nonprofit", "archive",
                        "healthcare", "funder", "other"]
SECTOR_NEUTRAL_SOURCE_TYPE_REASONS = {"source_type:funder", "source_type:other"}
SOURCE_TYPE_TO_INSTITUTION_TYPE = {
    "education": "educational", "facility": "research", "company": "commercial", "government": "government",
    "nonprofit": "nonprofit", "healthcare": "healthcare", "archive": "archive", "funder": "other", "other": "other",
}

# "univers" also catches Universität / Université / Universidad / Universiteit / Universidade.
UNIVERSITY_PATTERN = re.compile(r"\buniversi\w*|\bcollege\b", re.IGNORECASE)
# Place and street names that contain University/College but say nothing about the organization
# ("Practical Physics Solutions, LLC, College Station"); removed before the name rules run.
ADDRESS_NAME_PATTERN = re.compile(
    r"\bcollege\s+(station|park|lane|road|street|avenue|ave|drive)\b"
    r"|\buniversity\s+(drive|dr|avenue|ave|blvd|boulevard|road|rd|street|st|place|parkway|pkwy|way|park|heights|city|circle)\b",
    re.IGNORECASE)
NAME_HEURISTICS = [
    ("educational", "university_college_polytechnic_school",
     re.compile(r"\buniversi\w*|\bcollege\b|\binstitute of technology\b|\bpolytechnic\b|\bschool of\b", re.IGNORECASE)),
    ("archive", "museum_library_archive", re.compile(r"\b(museums?|librar(y|ies)|archives?)\b", re.IGNORECASE)),
    ("healthcare", "hospital_medical_center_clinic",
     re.compile(r"\b(hospitals?|medical cent(er|re)s?|clinics?)\b", re.IGNORECASE)),
    ("nonprofit", "foundation_association_society",
     re.compile(r"\b(foundations?|associations?|society|societies)\b", re.IGNORECASE)),
    ("research", "national_laboratory", re.compile(r"\bnational laborator(y|ies)\b", re.IGNORECASE)),
    ("commercial", "company_suffix", re.compile(r"\b(inc|llc|corporation|ltd|gmbh)\b", re.IGNORECASE)),
    ("government", "ministry_agency", re.compile(r"\b(ministry|agency)\b", re.IGNORECASE)),
]

GOVERNMENT_TOP_LEVEL_DOMAINS = {"gov", "mil", "int"}
# Under a country-code TLD: go.jp / go.kr / go.id / go.th, mil.br / mil.co, ...
GOVERNMENT_SECOND_LEVEL_LABELS = {"gov", "gob", "gouv", "go", "mil"}
GOVERNMENT_DOMAIN_SUFFIXES = (".gc.ca", ".govt.nz", ".gv.at")
EDUCATIONAL_SECOND_LEVEL_LABELS = {"edu", "ac"}
COMMERCIAL_SECOND_LEVEL_LABELS = {"com", "co"}

# Trailing "(ufl.edu)" style hints, as DesignSafe/DataCite appends them to affiliation names.
TRAILING_DOMAIN_PATTERN = re.compile(r"\s*\((?:[a-z0-9-]+\.)+[a-z]{2,}\)\s*$", re.IGNORECASE)
TRAILING_DOMAIN_HINT_PATTERN = re.compile(r"\(((?:[a-z0-9-]+\.)+[a-z]{2,})\)\s*$", re.IGNORECASE)
TRAILING_PARENTHETICAL_PATTERN = re.compile(r"\s*\([^)]*\)\s*$")

TACC_ROR_INSTITUTION_ID = f"ror:{common.TACC_ROR_ID}"
UT_AUSTIN_ROR_INSTITUTION_ID = f"ror:{common.UT_AUSTIN_ROR_ID}"
TACC_NAME_PATTERN = re.compile(r"texas\s+adv(?:anced|\.)?\s+comput(?:ing|er|\.)?\s+cent(?:er|re)", re.IGNORECASE)
TACC_ACRONYM_PATTERN = re.compile(r"(?<![A-Za-z])TACC(?![A-Za-z])")
TACC_ACRONYM_CONTEXT_PATTERN = re.compile(r"austin|texas|\bUT\b", re.IGNORECASE)

# Affiliation strings that name no organization. Compared as matching_key(part) of each comma/semicolon part.
PLACEHOLDER_AFFILIATION_KEYS = {
    "n a", "na", "none", "null", "nil", "unknown", "unknown institution", "not applicable", "not available", "tbd",
    "other", "unaffiliated", "independent", "independent researcher", "independent scholar", "independent consultant",
    "private address", "worldwide", "co pi", "pi", "dept", "department", "dept of", "school of", "institut",
    "university", "equal", "tab", "performing organization",
}
# Hand-checked strings from one IceCube art/outreach proceeding (doi 10.22323/1.395.1381 and 1.395.1146):
# neutrino-telescope sites and a conference venue, listed where affiliations belong.
OBSERVED_NON_INSTITUTION_KEYS = {
    "antares neutrino telescope porquerolles island france", "lake baikal russia", "marae new zealand",
    "online berlin germany",
    # A DataCite contributor naming the platform a dataset's posts came from, not an affiliation.
    "twitter",
}
# Hand-checked affiliations that locate one person's home but pass the automatic address test (a home-based
# "network" with street and full postcode, a bare village with postcode). Stored as sha256 of matching_key(raw string)
# so the published code does not repeat the address.
HAND_CHECKED_PERSONAL_ADDRESS_KEY_HASHES = {
    "90d6789c667053cc0fd7de785727782f82719426f058ef485020a10bd1486253",
    "ecafa331d80274dcdd1bdf1866e45c90cd951fac2bd222de65a9746611ffc0a6",
    "063479eb23874f6c5742e68ec37f0109e54a5c7406ba4b54225f41870997f2ba",
}
COLLABORATION_SUFFIX_PATTERN = re.compile(r"\bcollaborations?\W*$", re.IGNORECASE)
ORGANIZATION_WORD_PATTERN = re.compile(
    r"universi|institut|laborator|\bcent(er|re)\b|college|school|department|observatory|academy", re.IGNORECASE)
CITATION_FRAGMENT_PATTERN = re.compile(r"^\W*pos\s*\(", re.IGNORECASE)
AFFILIATION_PART_SEPARATOR_PATTERN = re.compile(r"[,;]")
STREET_SUFFIX_TOKENS = {"street", "st", "road", "rd", "avenue", "ave", "drive", "dr", "boulevard", "blvd", "lane", "ln",
                        "way", "weg", "str", "strasse", "allee", "platz", "marg", "court", "ct", "cir", "circle",
                        "trail", "turnpike", "parkway", "pkwy", "highway", "hwy", "box", "bag", "postal"}
STREET_COMPOUND_SUFFIXES = ("strasse", "weg", "allee", "platz")
# Street-level address pieces. Removed from every affiliation string: a person's location may only be the
# institution on the record (CLAUDE.md), and some authors list a home address, which cannot be told apart from an
# organization's street address by text alone.
ENGLISH_STREET_WORDS = (r"street|road|rd|avenue|ave|drive|boulevard|blvd|lane|ln|court|ct|way|circle|cir|terrace|crescent|"
                        r"cres|cr|trail|parkway|pkwy|highway|hwy|square|sq|plaza|pike|close|mall")
STREET_ADDRESS_PATTERNS = [
    # First, so "1050 Walnut St. Suite 300," ends at the separator the "St" rule below needs.
    re.compile(r"\b(?:suite|ste|apt|apartment|flat)\b\.?\s*#?\s*\d+\w*|#\s*\d+\w*", re.IGNORECASE),
    # "10 Rue Vauquelin", "12B avenue des Exemples", "5 place Jules Jansen": number first, street to the separator.
    re.compile(r"\b\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?(?:\s+|\s*,\s*)(?:rue|avenue|av\.|avenida|calle|rua|via|viale|piazza|corso|"
               r"boulevard|bd|chemin|quai|all[ée]e|place)\b[^,;()]*", re.IGNORECASE),
    # "Via della Vasca Navale 84", "ul. Orla 171", "os. Przykładowe 12/34", "Avenida Complutense 40".
    re.compile(r"\b(?:rue|avenida|avda\.?|av\.|calle|carrer|rua|via|viale|piazza|piazzale|p\.le|corso|largo|vicolo|paseo|"
               r"rambla|camino|cam[ií]|chemin|quai|ul\.|ulica|os\.)\s+[^,;()\d]{2,40}?\s*,?\s*\d+[a-z]?(?:\s*/\s*\d+)?\b",
               re.IGNORECASE),
    # "Platanenallee 6", "Notkestr. 85", "Eksempelvegen 3E", "Rijvisschestraat 120".
    re.compile(r"\b[^\W\d][\w'’-]*(?:strasse|straße|str\.|str|weg|vegen|veien|gata|gatan|gade|vej|gasse|allee|platz|laan|"
               r"straat|steeg|plein)\s+\d+[a-z]?(?:\s*[/-]\s*\d+[a-z]?)?\b", re.IGNORECASE),
    # "123 Example Court", "1200 New York Avenue NW", "4567 Sample Cr."; "St"/"Dr" only at the end of a part,
    # so "63110 St. Louis" keeps its city.
    re.compile(rf"\b\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?\s+(?:[^\W\d][\w'’.-]*\s+){{0,3}}"
               rf"(?:(?:{ENGLISH_STREET_WORDS})\b\.?|(?:st|dr)\b\.?(?=\s*(?:[,;]|$)))(?:\s+(?:[NS][EW]?|[EW])\b\.?)?",
               re.IGNORECASE),
    re.compile(r"\b(?:(?:g\.?\s*)?p\.?\s*o\.?\s*box|post\s+office\s+box|box|caixa\s+postal|private\s+bag|post\s+bag|postfach|"
               r"apartado(?:\s+postal)?)\s*\.?\s*\d+", re.IGNORECASE),
]
# "Sarsoni 90", "Nezavisimosti 114g": a street and house number with no street-type word. Text alone cannot tell
# it from "Dublin 4" or "Building 15", so it is removed only as a whole middle part (the first part names the
# organization, the last the country or a footnote number) whose word is not a known place.
UNMARKED_STREET_PART_PATTERN = re.compile(r"^\s*([^\W\d_][^\W\d_'’-]{3,})\s+\d{1,3}[a-z]?(?:\s*/\s*\d+)?\s*$",
                                          re.IGNORECASE)
FLAT_NUMBER_PATTERN = re.compile(r"\b\d+\s*/\s*\d+\b")
FULL_POSTCODE_PATTERN = re.compile(
    r"\b[A-Z]{2}\s*,?\s*\d{5}(?:-\d{4})?\b|\b\d{5}(?:-\d{3,4})?\b|\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b"
    r"|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b|\b\d{2}-\d{3}\b|\b\d{3}-\d{4}\b|\b\d{2}\.\d{3}-\d{3}\b|\b[A-Z]{1,2}-\d{4,5}\b|\b\d{6}\b")
# A private home or coordinates: the whole string is a personal location unless it names an organization
# (DesignSafe sensor sites "deployed at a residence (GPS LOC: 12°34'56.7"N ...)").
PRIVATE_SITE_PATTERN = re.compile(
    r"\bresiden(?:ce|tial)\b|\bprivate\s+(?:address|residence|home)\b|\bhome\s+address\b"
    r"|\bgps\b|\d\s*°\s*\d|\d\s*°\s*[NSEW]\b", re.IGNORECASE)
UNAFFILIATED_PATTERN = re.compile(r"\bunaffiliated\b|\bindependent\b|\bretired\b", re.IGNORECASE)
PERSONAL_ADDRESS_SIGNAL_PATTERNS = STREET_ADDRESS_PATTERNS + [FLAT_NUMBER_PATTERN, FULL_POSTCODE_PATTERN,
                                                              UNAFFILIATED_PATTERN]
PERSONAL_ADDRESS_PART_MAX_TOKENS = 4
ADDRESS_REMOVED = "[address removed]"
COUNTRY_SHORT_FORMS = {"usa", "us", "uk", "prc", "eu", "unitedstatesofamerica", "peoplesrepublicofchina",
                       "republicofkorea", "southkorea", "england", "scotland", "wales"}
PLACE_NAME_MAX_TOKENS = 5

# Words that appear in so many organization names that sharing them says nothing about identity
# (audit: "Purdue University" was matched to "Hue University of Education" by ROR).
GENERIC_NAME_TOKENS = set("""
the of and at in for on to an de del della degli di da do dos das des du la le les el los las et und fur der die van von voor
college colleges institute institutes institut instituto istituto institution school schools department departments dept
faculty faculties center centre centro centers laboratory laboratories laboratoire lab labs national research science
sciences scientific technology technologies technological education educational state international inc ltd llc corp
corporation company co gmbh division group program unit office trustees
""".split())
# Descriptive words that count as tokens but never identify an organization on their own
# (audit: "Ferguson Structural Engineering Laboratory" -> "Engineering Laboratory", "American Wood Council" ->
# "Council of Southeast Pennsylvania", "Nueva Granada Military University" -> "Military University").
WEAK_NAME_TOKENS = set("""
engineering engineers engineer ingenieria ingegneria ingenierie engenharia structural structures mechanics materials
physics chemistry biology medical medicine health clinical applied computing computer computational systems system
services service solutions consulting consultants associates management development resources energy environmental
environment information data analytics software design water ocean marine earth space military meteorology
meteorological council society foundation association agency authority administration bureau observatory museum
library network consortium alliance partners global american united states federal public private general central
northern southern eastern western north south east west new royal advanced high innovation studies
communications communication coordinating coordination meteorologie meteorologia cancer heart eye brain spine
orthopaedic orthopedic pediatric children george
""".split())
# Letters that NFKD does not decompose into an ASCII base letter.
UNDECOMPOSABLE_LETTERS = str.maketrans({"ı": "i", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss", "æ": "ae",
                                         "Æ": "AE", "đ": "d", "Đ": "D", "œ": "oe", "Œ": "OE"})
FUZZY_TOKEN_MIN_LENGTH = 5
FUZZY_TOKEN_MIN_RATIO = 0.85
ACRONYM_MIN_LENGTH = 3


def bare_ror_id(value):
    if not value:
        return None
    return value.strip().rstrip("/").rsplit("/", 1)[-1].removeprefix("ror:") or None


def separate_source_categories(institutions):
    """Keep only ROR/OpenAlex organization types in source_types; other source labels go to source_categories."""
    for institution in institutions.values():
        source_types, source_categories = [], list(institution.get("source_categories") or [])
        for value in institution.get("source_types") or []:
            if value.lower() in SOURCE_TYPE_TO_INSTITUTION_TYPE:
                source_types.append(value.lower())
            else:
                source_categories.append(value)
        institution["source_types"] = list(dict.fromkeys(source_types))
        institution["source_categories"] = list(dict.fromkeys(source_categories))


def load_overrides(path=OVERRIDES_PATH):
    path = pathlib.Path(path)
    if not path.exists():
        print(f"warning: no overrides file at {path}")
        return {}
    return json.loads(path.read_text())


def matching_key(name):
    """normalize_name without a leading "the", so "The University of X" == "UNIVERSITY OF X"."""
    return common.normalize_name(name).removeprefix("the ")


def load_aliases(path=ALIASES_PATH):
    """data/manual/institution_aliases.json: list of {institution_id, ror_name, note, variants: [...]}.

    Returns matching_key(variant) -> institution_id.
    """
    path = pathlib.Path(path)
    if not path.exists():
        print(f"warning: no institution alias file at {path}")
        return {}
    institution_id_by_key = {}
    for entry in json.loads(path.read_text())["aliases"]:
        for variant in entry["variants"]:
            key = matching_key(variant)
            if key in institution_id_by_key and institution_id_by_key[key] != entry["institution_id"]:
                raise ValueError(f"alias {variant!r} maps to two institutions")
            institution_id_by_key[key] = entry["institution_id"]
    return institution_id_by_key


def fetch_ror_organization(ror):
    return common.fetch_json(f"{ROR_API}/{ror}", source="ror")


def ror_display_name(organization):
    for name in organization.get("names", []):
        if "ror_display" in name.get("types", []):
            return name["value"]
    return None


def ror_website(organization):
    return next((link["value"] for link in organization.get("links") or [] if link.get("type") == "website"), None)


def ror_parent_ids(organization):
    return [bare_ror_id(relationship["id"]) for relationship in organization.get("relationships") or []
            if relationship.get("type") == "parent"]


def fill_institution_from_ror(institution, organization):
    geonames = (organization.get("locations") or [{}])[0].get("geonames_details") or {}
    parent_ids = ror_parent_ids(organization)
    institution["name"] = institution.get("name") or ror_display_name(organization)
    institution["country_code"] = institution.get("country_code") or geonames.get("country_code")
    institution["city"] = institution.get("city") or geonames.get("name")
    institution["latitude"] = first_non_null(institution.get("latitude"), geonames.get("lat"))
    institution["longitude"] = first_non_null(institution.get("longitude"), geonames.get("lng"))
    institution["source_types"] = institution.get("source_types") or organization.get("types") or []
    institution["parent_ror"] = institution.get("parent_ror") or (parent_ids[0] if parent_ids else None)
    institution["website"] = ror_website(organization) or institution.get("website")


def first_non_null(existing, incoming):
    return existing if existing is not None else incoming


def fill_ror_institutions(institutions, ror_organizations):
    """Fill every ror: institution from its ROR record (fetched once, cached on disk); returns fetch failures."""
    failures = []
    for institution_id, institution in institutions.items():
        if not institution_id.startswith("ror:"):
            continue
        ror = institution_id.removeprefix("ror:")
        organization = ror_organizations.get(ror)
        if organization is None:
            try:
                organization = fetch_ror_organization(ror)
            except Exception as error:
                failures.append((institution_id, repr(error)))
                continue
            ror_organizations[ror] = organization
        fill_institution_from_ror(institution, organization)
    return failures


def organization_names(organization):
    """(value, is_acronym) for every ROR name, with ROR's "(Country)" disambiguation suffix removed."""
    for name in organization.get("names") or []:
        yield TRAILING_PARENTHETICAL_PATTERN.sub("", name["value"]), "acronym" in name.get("types", [])


def build_dataset_name_index(institutions, ror_organizations):
    """matching_key(name) -> ror institution id, from ror: institutions already in the dataset.

    Uses each record's own name plus ROR labels and aliases (not acronyms: "ASU" etc. are ambiguous).
    Keys claimed by more than one institution are dropped.
    """
    institution_ids_by_key = collections.defaultdict(set)
    for institution_id, institution in institutions.items():
        if not institution_id.startswith("ror:"):
            continue
        names = [institution.get("name")]
        organization = ror_organizations.get(institution_id.removeprefix("ror:"))
        if organization:
            names += [value for value, is_acronym in organization_names(organization) if not is_acronym]
        for name in names:
            key = matching_key(name)
            if key:
                institution_ids_by_key[key].add(institution_id)
    return {key: next(iter(ids)) for key, ids in institution_ids_by_key.items() if len(ids) == 1}


def strip_trailing_domains(name):
    previous = None
    while previous != name:
        previous, name = name, TRAILING_DOMAIN_PATTERN.sub("", name)
    return name.strip()


def folded_text(text):
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(UNDECOMPOSABLE_LETTERS))
    return "".join(character for character in decomposed if not unicodedata.combining(character)).lower()


def meaningful_tokens(text):
    # Splitting on any non-ASCII-alphanumeric keeps "Wisconsin–Platteville" (en dash) as two tokens.
    return {token for token in re.findall(r"[a-z0-9]+", folded_text(text))
            if len(token) > 1 and token not in GENERIC_NAME_TOKENS and not token.startswith("univers")}


def compact_text(text):
    return re.sub(r"[^a-z0-9]", "", folded_text(text))


def tokens_match(token, other_token):
    """Exact, or near-identical long tokens (mojibake like "Concepcin" for "Concepción", typos like "Eistner")."""
    if token == other_token:
        return True
    return (len(token) >= FUZZY_TOKEN_MIN_LENGTH and len(other_token) >= FUZZY_TOKEN_MIN_LENGTH
            and difflib.SequenceMatcher(None, token, other_token).ratio() >= FUZZY_TOKEN_MIN_RATIO)


def ror_match_support(raw_name, organization):
    """Why a ROR-chosen organization plausibly is raw_name, or None.

    Accept when, for some ROR name of the organization, a majority of its meaningful tokens occur in the
    raw string and at least one of them is not in WEAK_NAME_TOKENS; or an acronym (3+ characters) is a raw token;
    or the names are equal ignoring spaces/punctuation ("M PC CORPORATION" == "MPC Corporation") and that name is
    not a single weak word. A one-word raw string only matches the display name or an acronym.
    """
    raw_tokens = meaningful_tokens(raw_name)
    raw_compact = compact_text(raw_name)
    is_single_word_raw = len(raw_name.split()) == 1
    for value, is_acronym, is_display_name in organization_names_with_kind(organization):
        if is_acronym:
            acronym = compact_text(value)
            if len(acronym) >= ACRONYM_MIN_LENGTH and acronym in raw_tokens:
                return f"acronym {value!r}"
            continue
        # A one-word raw string ("Jacobs", "Forell") matched to an alias/label is as ambiguous as an acronym.
        if is_single_word_raw and not is_display_name:
            continue
        name_tokens = meaningful_tokens(value)
        if compact_text(value) == raw_compact and (
                len(name_tokens) >= 2 or any(token not in WEAK_NAME_TOKENS for token in name_tokens)):
            return f"same name {value!r}"
        if not name_tokens:
            continue
        covered = [token for token in name_tokens if any(tokens_match(token, raw) for raw in raw_tokens)]
        if len(covered) * 2 > len(name_tokens) and any(token not in WEAK_NAME_TOKENS for token in covered):
            return f"{len(covered)}/{len(name_tokens)} tokens of {value!r}"
    return None


def organization_names_with_kind(organization):
    """(value, is_acronym, is_display_name) for every ROR name, "(Country)" suffix removed."""
    for name in organization.get("names") or []:
        types = name.get("types", [])
        yield TRAILING_PARENTHETICAL_PATTERN.sub("", name["value"]), "acronym" in types, "ror_display" in types


class CompetingNameIndex:
    """Token index over the non-acronym names of the dataset's ROR organizations (snapshot at construction).

    Used to reject a ROR choice when the raw string fully contains a *more specific* name of another
    organization: "University of Alabama, Birmingham" -> ROR chose University of Alabama (Tuscaloosa), but
    "University of Alabama at Birmingham" is entirely in the raw string. Child organizations named in the chosen
    record's ROR relationships count too (University of Hawaii System -> University of Hawaiʻi at Mānoa).
    """

    def __init__(self, ror_organizations):
        self.entries_by_token = collections.defaultdict(list)
        for ror, organization in ror_organizations.items():
            for value, is_acronym in organization_names(organization):
                if is_acronym:
                    continue
                name_tokens = frozenset(meaningful_tokens(value))
                for token in name_tokens:
                    self.entries_by_token[token].append((name_tokens, ror, value))

    def more_specific_name(self, raw_name, organization):
        """Name of another organization whose tokens all occur in raw_name and strictly extend what the chosen
        organization's names cover (its acronyms when no name token occurs: "USNO, Washington, DC"), or None.

        For non-child candidates, extra tokens that only repeat the chosen organization's own city/region/country
        do not count: "University of Chicago, Chicago, Illinois" does not name University of Illinois Chicago.
        """
        raw_tokens = meaningful_tokens(raw_name)
        chosen_ror = bare_ror_id(organization["id"])
        chosen_name_tokens, chosen_acronyms = set(), set()
        for value, is_acronym in organization_names(organization):
            if is_acronym:
                chosen_acronyms.add(compact_text(value))
            else:
                chosen_name_tokens |= meaningful_tokens(value)
        chosen_covered = ({raw_token for raw_token in raw_tokens
                           if any(tokens_match(raw_token, name_token) for name_token in chosen_name_tokens)}
                          or raw_tokens & chosen_acronyms)
        chosen_location_tokens = set()
        for location in organization.get("locations") or []:
            geonames = location.get("geonames_details") or {}
            for field in ("name", "country_subdivision_name", "country_name"):
                chosen_location_tokens |= meaningful_tokens(geonames.get(field))
        child_candidates = [(frozenset(meaningful_tokens(relationship["label"])), bare_ror_id(relationship["id"]),
                             relationship["label"])
                            for relationship in organization.get("relationships") or []
                            if relationship.get("type") == "child" and relationship.get("label")]
        dataset_candidates = [entry for token in raw_tokens for entry in self.entries_by_token.get(token, [])]
        for name_tokens, ror, value in child_candidates + dataset_candidates:
            if ror == chosen_ror or not name_tokens or not name_tokens <= raw_tokens or not name_tokens > chosen_covered:
                continue
            is_child = any(ror == child_ror for _, child_ror, _ in child_candidates)
            if is_child or not (name_tokens - chosen_covered) <= chosen_location_tokens:
                return f"{value!r} (ror:{ror})"
        return None


def query_ror_affiliation(name):
    url = common.build_url(ROR_API, {"affiliation": name})
    payload = common.fetch_json(url, source="ror")
    for item in payload.get("items", []):
        if item.get("chosen"):
            return item["organization"]
    return None


def participant_name_occurrences(collaborations):
    """Counter of names to resolve: unresolved primary affiliations (raw string) and name: additional ids.

    Additional affiliations carry no raw string, so their normalized id text is the name we match on.
    """
    occurrences = collections.Counter()
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            if participant["institution_status"] == "unresolved":
                occurrences[participant["institution_name_as_listed"]] += 1
            for institution_id in participant.get("additional_institution_ids") or []:
                if institution_id.startswith("name:"):
                    occurrences[institution_id.removeprefix("name:")] += 1
    return occurrences


def resolve_names(occurrences, aliases, dataset_name_index, ror_organizations, use_ror_api):
    """name -> (institution_id, match_method). Order: manual alias, dataset name, ROR affiliation API + token check."""
    resolved = {}
    rejected_matches = []
    failed_requests = []
    competing_name_index = CompetingNameIndex(ror_organizations) if use_ror_api else None
    for index_name, name in enumerate(occurrences, start=1):
        cleaned_name = strip_trailing_domains(html.unescape(name))
        key = matching_key(cleaned_name)
        if key in aliases:
            resolved[name] = (aliases[key], "manual_alias")
            continue
        if key in dataset_name_index:
            resolved[name] = (dataset_name_index[key], "dataset_name_exact")
            continue
        if not use_ror_api or not key:
            continue
        try:
            organization = query_ror_affiliation(cleaned_name)
        except Exception as error:
            failed_requests.append((name, repr(error)))
            continue
        if index_name % 500 == 0:
            print(f"  {index_name}/{len(occurrences)} names checked, {len(resolved)} resolved")
        if not organization:
            continue
        ror = bare_ror_id(organization["id"])
        support = ror_match_support(cleaned_name, organization)
        more_specific_name = competing_name_index.more_specific_name(cleaned_name, organization) if support else None
        if support is None or more_specific_name:
            rejected_matches.append({"name": name, "query": cleaned_name, "participant_count": occurrences[name],
                                     "ror_id": f"ror:{ror}", "ror_name": ror_display_name(organization),
                                     "reason": f"raw string names a more specific organization: {more_specific_name}"
                                     if more_specific_name else "no distinctive token support"})
            continue
        ror_organizations.setdefault(ror, organization)
        resolved[name] = (f"ror:{ror}", "ror_affiliation_api")
    for name in occurrences:
        # TACC is a UT Austin unit: a string naming TACC means TACC, whether UT Austin matched or nothing did.
        if names_tacc(html.unescape(name)) and \
                resolved.get(name, (UT_AUSTIN_ROR_INSTITUTION_ID,))[0] == UT_AUSTIN_ROR_INSTITUTION_ID:
            resolved[name] = (TACC_ROR_INSTITUTION_ID, "tacc_name")
    return resolved, rejected_matches, failed_requests


def names_tacc(name):
    return bool(TACC_NAME_PATTERN.search(name or "")
                or TACC_ACRONYM_PATTERN.search(name or "") and TACC_ACRONYM_CONTEXT_PATTERN.search(name or ""))


def apply_resolved_names(collaborations, resolved):
    """Rewrite participants in place; returns participants resolved per match method."""
    participants_by_method = collections.Counter()
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            if participant["institution_status"] == "unresolved" and participant["institution_name_as_listed"] in resolved:
                institution_id, method = resolved[participant["institution_name_as_listed"]]
                participant["institution_id"] = institution_id
                participant["institution_status"] = "resolved"
                participant["institution_match_method"] = method
                participants_by_method[method] += 1
            additional_ids = []
            for institution_id in participant.get("additional_institution_ids") or []:
                match = resolved.get(institution_id.removeprefix("name:")) if institution_id.startswith("name:") else None
                if match:
                    participants_by_method[f"{match[1]} (additional id)"] += 1
                additional_ids.append(match[0] if match else institution_id)
            participant["additional_institution_ids"] = [
                institution_id for institution_id in dict.fromkeys(additional_ids)
                if institution_id != participant.get("institution_id")]
    return participants_by_method


def place_names_from_ror(ror_organizations):
    """compact_text of every city, region and country named in the dataset's ROR records (the place gazetteer).

    Names made only of generic words are skipped: "University" (Mississippi) and "College" (Alaska) are towns.
    """
    place_names = set()
    for organization in ror_organizations.values():
        for location in organization.get("locations") or []:
            geonames = location.get("geonames_details") or {}
            for field in ("name", "country_subdivision_name", "country_name"):
                compact = compact_text(geonames.get(field))
                if len(compact) >= 3 and meaningful_tokens(geonames.get(field)):
                    place_names.add(compact)
    return place_names


def is_address_piece(tokens):
    """Optional house number(s), then 1-3 words ending in a street suffix: "257 Fuller Road", "P O Box", "Staudinger Weg"."""
    words = list(tokens)
    while words and any(character.isdigit() for character in words[0]):
        words.pop(0)
    return (len(tokens) >= 2 and 1 <= len(words) <= 3 and words[-1] in STREET_SUFFIX_TOKENS
            and not any(character.isdigit() for word in words for character in word))


def is_place_or_address(text, place_names):
    """True if text's tokens split entirely into places, country short forms, postcodes/numbers, upper-case
    2-letter state/country codes and street addresses, with at least one place or address ("Nagoya , Japan",
    "Lemont, IL 60439, USA 13", "Staudinger Weg 7")."""
    original_tokens = re.findall(r"[A-Za-z0-9]+", "".join(
        character for character in unicodedata.normalize("NFKD", (text or "").translate(UNDECOMPOSABLE_LETTERS))
        if not unicodedata.combining(character)))
    tokens = [token.lower() for token in original_tokens]
    # reachable[position] = whether a place/address piece was used to get there
    reachable = {0: False}
    for start in range(len(tokens)):
        if start not in reachable:
            continue
        found = reachable[start]
        token = tokens[start]
        steps = []
        # A state/country code only after a place and only upper case ("Chicago, IL"; not "UW, Madison", "of").
        is_code = len(token) == 2 and found and original_tokens[start].isupper()
        if any(character.isdigit() for character in token) or is_code:
            steps.append((start + 1, found))
        if any(token.endswith(suffix) and token != suffix for suffix in STREET_COMPOUND_SUFFIXES):
            steps.append((start + 1, True))
        for end in range(start + 1, min(len(tokens), start + PLACE_NAME_MAX_TOKENS) + 1):
            compact = "".join(tokens[start:end])
            if compact in place_names or compact in COUNTRY_SHORT_FORMS or is_address_piece(tokens[start:end]):
                steps.append((end, True))
        for end, piece_found in steps:
            reachable[end] = reachable.get(end, False) or piece_found
    return bool(tokens) and reachable.get(len(tokens), False)


def is_personal_address(text, place_names):
    """True if text is a private site, or an address/postcode/"unaffiliated" string in which no part names an
    organization: each comma/semicolon part, once its address pieces are removed, is empty, a placeholder, a place,
    or a few words ("Springfield", "Oakdale") that are not the leading part, where affiliations put the organization."""
    if ORGANIZATION_WORD_PATTERN.search(text):
        return False
    if PRIVATE_SITE_PATTERN.search(text):
        return True
    if not any(pattern.search(text) for pattern in PERSONAL_ADDRESS_SIGNAL_PATTERNS):
        return False
    parts = [part for part in AFFILIATION_PART_SEPARATOR_PATTERN.split(text) if part.strip()]
    for index_part, part in enumerate(parts):
        remainder = part
        for pattern in PERSONAL_ADDRESS_SIGNAL_PATTERNS:
            remainder = pattern.sub(" ", remainder)
        remainder_key = matching_key(remainder)
        is_short_remainder = len(remainder_key.split()) <= PERSONAL_ADDRESS_PART_MAX_TOKENS and (
            remainder != part or index_part > 0)
        if remainder_key and remainder_key not in PLACEHOLDER_AFFILIATION_KEYS and not is_short_remainder \
                and not is_place_or_address(remainder, place_names):
            return False
    return True


def without_unmarked_street_parts(text, place_names):
    pieces = re.split(r"([,;])", text)
    kept_pieces = pieces[:1]
    for index_piece in range(2, len(pieces), 2):
        match = UNMARKED_STREET_PART_PATTERN.match(pieces[index_piece])
        is_middle_part = index_piece < len(pieces) - 1
        if match and is_middle_part and compact_text(match[1]) not in place_names | COUNTRY_SHORT_FORMS:
            continue
        kept_pieces += pieces[index_piece - 1:index_piece + 1]
    return "".join(kept_pieces)


def without_street_address(text, place_names):
    """text with street-level pieces (house number and street, PO box, suite) removed; city and postcode stay."""
    if not text:
        return text
    stripped = without_unmarked_street_parts(text, place_names)
    for pattern in STREET_ADDRESS_PATTERNS:
        stripped = pattern.sub("", stripped)
    if stripped == text:
        return text
    stripped = re.sub(r"\(\s*[,;.-]?\s*\)", "", stripped)
    stripped = re.sub(r"\s*([,;])(?:\s*[,;.])+", r"\1", stripped)
    stripped = re.sub(r"\s+([,;)])", r"\1", stripped)
    return re.sub(r"\s{2,}", " ", stripped).strip(" ,;.-") or None


def non_institution_category(raw_name, place_names, title_key=None):
    """Why raw_name names no organization (placeholder, place/address, collaboration, ...), or None."""
    if not raw_name:
        return None
    text = html.unescape(raw_name)
    key = matching_key(text)
    if len(compact_text(text)) < 2 or key in PLACEHOLDER_AFFILIATION_KEYS:
        return "placeholder"
    if key in OBSERVED_NON_INSTITUTION_KEYS:
        return "observed_non_institution"
    if hashlib.sha256(key.encode()).hexdigest() in HAND_CHECKED_PERSONAL_ADDRESS_KEY_HASHES or is_personal_address(
            text, place_names):
        return "personal_address"
    if title_key and key == title_key:
        return "record_title"
    if CITATION_FRAGMENT_PATTERN.search(text):
        return "citation_fragment"
    parts = [part for part in AFFILIATION_PART_SEPARATOR_PATTERN.split(text) if part.strip()]
    if COLLABORATION_SUFFIX_PATTERN.search(text) and not any(ORGANIZATION_WORD_PATTERN.search(part) for part in parts):
        return "collaboration_name"
    other_parts = [part for part in parts if matching_key(part) not in PLACEHOLDER_AFFILIATION_KEYS]
    # Checked as one text so a state code can follow its city across a comma ("Chicago, IL, USA").
    if other_parts and is_place_or_address(" ".join(other_parts), place_names):
        return "placeholder" if len(other_parts) < len(parts) else "place_or_address"
    return None


def mark_non_institution_affiliations(collaborations, place_names):
    """Unresolved affiliations that name no organization -> institution_status missing (raw string kept, except a
    personal address, which is replaced by ADDRESS_REMOVED); name: additional ids of the same kind are dropped.
    Source-resolved strings that are only collaboration names (OpenAlex: "for the Fermilab Lattice Collaboration"
    -> Fermilab) are unset too.

    Returns {category: Counter(raw string)} for the build log.
    """
    raw_names_by_category = collections.defaultdict(collections.Counter)
    for collaboration in collaborations.values():
        title_key = matching_key(collaboration.get("title") or "")
        for participant in collaboration.get("participants") or []:
            raw_name = participant.get("institution_name_as_listed")
            category = None
            if participant["institution_status"] == "unresolved":
                category = non_institution_category(raw_name, place_names, title_key)
            elif participant["institution_status"] == "resolved" and \
                    non_institution_category(raw_name, place_names) == "collaboration_name":
                category = "collaboration_name"
            if category:
                participant["institution_id"] = None
                participant["institution_status"] = "missing"
                participant["institution_evidence"] = None
                participant["institution_match_method"] = f"not_an_institution:{category}"
                raw_names_by_category[category][raw_name] += 1
            if category == "personal_address":
                participant["institution_name_as_listed"] = ADDRESS_REMOVED
                # An organizational party (DesignSafe sensor site) is named by the same string.
                if not participant.get("person_id") and participant.get("name_as_listed") == raw_name:
                    participant["name_as_listed"] = ADDRESS_REMOVED
                # Sources split one affiliation into several ids, so the others are pieces of the removed address
                # (a town and postcode, a state).
                participant["additional_institution_ids"] = []
            kept_ids = []
            for institution_id in participant.get("additional_institution_ids") or []:
                additional_category = non_institution_category(institution_id.removeprefix("name:"), place_names) \
                    if institution_id.startswith("name:") else None
                if additional_category:
                    raw_names_by_category[f"{additional_category} (additional id)"][institution_id] += 1
                else:
                    kept_ids.append(institution_id)
            participant["additional_institution_ids"] = kept_ids
    return raw_names_by_category


def name_id_without_street_address(institution_id, raw_name, place_names):
    """The name: id a harvester built from raw_name (plain, or after dropping a trailing "(domain)"), rebuilt from
    raw_name without its street address; other ids are returned unchanged."""
    for candidate in (raw_name, strip_trailing_domains(raw_name)):
        if institution_id == f"name:{common.normalize_name(candidate)}":
            return f"name:{common.normalize_name(without_street_address(candidate, place_names))}"
    return institution_id


def strip_street_addresses(collaborations, place_names):
    """Remove street-level pieces from as-listed affiliations and from the name: ids built from them. Runs after
    ROR matching so matching (and its cache) still sees the source's full string. Returns changes by status."""
    changed = collections.Counter()
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            raw_name = participant.get("institution_name_as_listed")
            stripped = without_street_address(raw_name, place_names)
            if stripped != raw_name:
                participant["institution_name_as_listed"] = stripped
                if participant["institution_status"] == "unresolved":
                    participant["institution_id"] = name_id_without_street_address(participant["institution_id"],
                                                                                   raw_name, place_names)
                changed[participant["institution_status"]] += 1
            additional_ids = []
            for institution_id in participant.get("additional_institution_ids") or []:
                if institution_id.startswith("name:"):
                    stripped_text = without_street_address(institution_id.removeprefix("name:"), place_names)
                    stripped_id = f"name:{common.normalize_name(stripped_text)}"
                    if stripped_id != institution_id:
                        changed["additional id"] += 1
                    institution_id = stripped_id
                additional_ids.append(institution_id)
            participant["additional_institution_ids"] = [
                institution_id for institution_id in dict.fromkeys(additional_ids)
                if institution_id != participant.get("institution_id")]
    return changed


def prefer_tacc_over_ut_austin(collaborations):
    """Source-resolved participants whose raw string names TACC but whose id is UT Austin -> TACC."""
    upgraded = collections.Counter()
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            if participant.get("institution_id") == UT_AUSTIN_ROR_INSTITUTION_ID and \
                    names_tacc(participant.get("institution_name_as_listed")):
                participant["institution_id"] = TACC_ROR_INSTITUTION_ID
                participant["institution_match_method"] = "tacc_name"
                participant["additional_institution_ids"] = [
                    institution_id for institution_id in participant.get("additional_institution_ids") or []
                    if institution_id != TACC_ROR_INSTITUTION_ID]
                upgraded[participant["institution_name_as_listed"]] += 1
    return upgraded


def print_counter_examples(label, counter, limit=8):
    print(f"  {label}: {len(counter)} distinct, {sum(counter.values())} occurrences; "
          + "; ".join(f"{name!r} x{count}" for name, count in counter.most_common(limit)))


def resolve_unresolved_institutions(collaborations, institutions, ror_organizations, use_ror_api):
    """Match free-text institutions (primary and additional) to ROR ids; logs rejected ROR matches."""
    place_names = place_names_from_ror(ror_organizations)
    raw_names_by_category = mark_non_institution_affiliations(collaborations, place_names)
    print("affiliation strings that name no organization (participant institution -> missing):")
    for category, raw_names in sorted(raw_names_by_category.items()):
        if category.startswith("personal_address"):
            print(f"  {category}: {len(raw_names)} distinct, {sum(raw_names.values())} occurrences (not printed)")
        else:
            print_counter_examples(category, raw_names)
    occurrences = participant_name_occurrences(collaborations)
    print(f"institution name resolution: {len(occurrences)} distinct names, {sum(occurrences.values())} occurrences")
    aliases = load_aliases()
    dataset_name_index = build_dataset_name_index(institutions, ror_organizations)
    resolved, rejected_matches, failed_requests = resolve_names(
        occurrences, aliases, dataset_name_index, ror_organizations, use_ror_api)
    participants_by_method = apply_resolved_names(collaborations, resolved)
    tacc_upgrades = prefer_tacc_over_ut_austin(collaborations)
    print_counter_examples("source-resolved UT Austin -> TACC (string names TACC)", tacc_upgrades)
    print(f"  street addresses removed from as-listed affiliations: {dict(strip_street_addresses(collaborations, place_names))}")
    rejected_matches.sort(key=lambda match: -match["participant_count"])
    REJECTED_ROR_MATCHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    REJECTED_ROR_MATCHES_PATH.write_text(json.dumps(rejected_matches, indent=1, ensure_ascii=False))
    names_by_method = collections.Counter(method for _, method in resolved.values())
    return {
        "distinct_names_attempted": len(occurrences),
        "occurrences_attempted": sum(occurrences.values()),
        "names_by_method": dict(names_by_method),
        "participants_by_method": dict(participants_by_method),
        "rejected_ror_matches": len(rejected_matches),
        "rejected_ror_match_occurrences": sum(match["participant_count"] for match in rejected_matches),
        "failed_requests": failed_requests,
        "ror_api_used": use_ror_api,
    }


def remove_bulk_affiliations(collaborations, institutions):
    """On papers with >= BULK_AFFILIATION_MIN_PARTICIPANTS participants, an institution held (primary or additional)
    by more than BULK_AFFILIATION_MIN_SHARE of them (BULK_AFFILIATION_MIN_SHARE_TRUNCATED when OpenAlex truncated the
    author list) is removed from all of them: OpenAlex smears one collaboration member institution over a whole
    author list (IceCube ICRC proceedings). A removed primary becomes missing; other additional ids are not promoted.
    Logs affected papers to BULK_AFFILIATION_PAPERS_PATH."""
    affected_papers = []
    participants_affected = 0
    papers_by_institution = collections.Counter()
    participants_by_institution = collections.Counter()
    papers_by_share_band = collections.Counter()
    links_by_share_band = collections.Counter()
    for collaboration in collaborations.values():
        participants = collaboration.get("participants") or []
        if collaboration.get("kind") != "paper" or len(participants) < BULK_AFFILIATION_MIN_PARTICIPANTS:
            continue
        holder_counts = collections.Counter(
            institution_id for participant in participants
            for institution_id in {participant.get("institution_id"), *(participant.get("additional_institution_ids") or [])}
            if institution_id)
        min_share = (BULK_AFFILIATION_MIN_SHARE_TRUNCATED if collaboration.get("participants_truncated")
                     else BULK_AFFILIATION_MIN_SHARE)
        bulk_institution_ids = {institution_id for institution_id, count in holder_counts.items()
                                if count > min_share * len(participants)}
        if not bulk_institution_ids:
            continue
        share_bands = {institution_id: f"> {BULK_AFFILIATION_MIN_SHARE}"
                       if holder_counts[institution_id] > BULK_AFFILIATION_MIN_SHARE * len(participants)
                       else f"> {BULK_AFFILIATION_MIN_SHARE_TRUNCATED} only (truncated author list)"
                       for institution_id in bulk_institution_ids}
        papers_by_share_band.update(set(share_bands.values()))
        links_by_share_band.update({band: sum(holder_counts[institution_id] for institution_id in share_bands
                                              if share_bands[institution_id] == band)
                                    for band in set(share_bands.values())})
        primary_counts = collections.Counter()
        for participant in participants:
            if participant.get("institution_id") in bulk_institution_ids:
                primary_counts[participant["institution_id"]] += 1
                participant["institution_id"] = None
                participant["institution_status"] = "missing"
                participant["institution_evidence"] = None
                participant["institution_match_method"] = "bulk_affiliation_guard"
            additional_ids = participant.get("additional_institution_ids") or []
            kept_ids = [institution_id for institution_id in additional_ids if institution_id not in bulk_institution_ids]
            participant["additional_institution_ids"] = kept_ids
            if participant.get("institution_match_method") == "bulk_affiliation_guard" or len(kept_ids) < len(additional_ids):
                participants_affected += 1
        papers_by_institution.update(bulk_institution_ids)
        participants_by_institution.update({institution_id: holder_counts[institution_id] for institution_id in bulk_institution_ids})
        affected_papers.append({
            "collaboration_id": collaboration["id"], "title": collaboration.get("title"),
            "participant_count": len(participants),
            "participants_truncated": bool(collaboration.get("participants_truncated")), "min_share": str(min_share),
            "removed_institutions": [
                {"institution_id": institution_id, "name": (institutions.get(institution_id) or {}).get("name"),
                 "participant_count": holder_counts[institution_id], "primary_count": primary_counts[institution_id],
                 "share_band": share_bands[institution_id]}
                for institution_id in sorted(bulk_institution_ids, key=lambda value: -holder_counts[value])],
        })
    BULK_AFFILIATION_PAPERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BULK_AFFILIATION_PAPERS_PATH.write_text(json.dumps(affected_papers, indent=1, ensure_ascii=False))
    print(f"\nbulk-affiliation guard: {len(affected_papers)} papers with >= {BULK_AFFILIATION_MIN_PARTICIPANTS} participants, "
          f"{participants_affected} participants lost an institution link "
          f"({sum(participants_by_institution.values())} links removed; logged to {BULK_AFFILIATION_PAPERS_PATH})")
    for institution_id, link_count in participants_by_institution.most_common(10):
        print(f"  {institution_id:16} {(institutions.get(institution_id) or {}).get('name')!s:60.60} "
              f"{papers_by_institution[institution_id]:>4} papers {link_count:>6} links")
    for band, paper_count in sorted(papers_by_share_band.items()):
        print(f"  institution share {band}: {paper_count} papers, {links_by_share_band[band]} links removed")
    return affected_papers


def referenced_institution_names(collaborations):
    """institution id -> first raw name seen, for building stub records."""
    names = {}
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            if participant.get("institution_id"):
                names.setdefault(participant["institution_id"], participant.get("institution_name_as_listed"))
            for institution_id in participant.get("additional_institution_ids") or []:
                # Additional affiliations carry no raw string; a name: id's normalized text is the best name we have.
                names.setdefault(institution_id, institution_id.removeprefix("name:") if institution_id.startswith("name:") else None)
    return names


def add_missing_institution_stubs(institutions, collaborations):
    for institution_id, name in referenced_institution_names(collaborations).items():
        if institution_id in institutions:
            continue
        # A ror: stub gets its name from ROR; the raw string may be a sub-unit ("Department of Physics, MIT").
        institutions[institution_id] = {
            "id": institution_id, "name": None if institution_id.startswith("ror:") else name,
            "ror": institution_id.removeprefix("ror:") if institution_id.startswith("ror:") else None,
            "openalex_id": institution_id.removeprefix("openalex:") if institution_id.startswith("openalex:") else None,
            "country_code": None, "city": None, "latitude": None, "longitude": None,
            "source_types": [], "source_categories": [], "parent_ror": None, "website": None, "sources": [],
        }


def website_host(website):
    if not website:
        return None
    if "//" not in website:
        website = f"//{website}"
    return (urllib.parse.urlsplit(website).hostname or "").lower().rstrip(".") or None


def classify_by_website_domain(website):
    """SCHEMA.md rule 2; returns (type, reason) or None. Only the last two labels matter: tacc.utexas.edu -> .edu."""
    host = website_host(website)
    if not host:
        return None
    labels = host.split(".")
    top_level = labels[-1]
    second_level = labels[-2] if len(labels) >= 2 else None
    is_country_code = len(top_level) == 2
    if top_level in GOVERNMENT_TOP_LEVEL_DOMAINS:
        return "government", f"website_domain:.{top_level}"
    for suffix in GOVERNMENT_DOMAIN_SUFFIXES:
        if host.endswith(suffix):
            return "government", f"website_domain:{suffix}"
    if is_country_code and second_level in GOVERNMENT_SECOND_LEVEL_LABELS:
        return "government", f"website_domain:.{second_level}.xx"
    if top_level == "edu":
        return "educational", "website_domain:.edu"
    if is_country_code and second_level in EDUCATIONAL_SECOND_LEVEL_LABELS:
        return "educational", f"website_domain:.{second_level}.xx"
    if top_level == "com":
        return "commercial", "website_domain:.com"
    if is_country_code and second_level in COMMERCIAL_SECOND_LEVEL_LABELS:
        return "commercial", f"website_domain:.{second_level}.xx"
    return None


def classify_by_name_domain_hint(name):
    """Rule 2 applied to a trailing "(umass.edu)" hint in a name-only institution's raw affiliation string."""
    match = TRAILING_DOMAIN_HINT_PATTERN.search(name or "")
    domain_result = classify_by_website_domain(match.group(1)) if match else None
    if domain_result is None:
        return None
    return domain_result[0], domain_result[1].replace("website_domain:", "name_domain_hint:")


def classify_by_source_types(source_types):
    lowered_types = {value.lower() for value in source_types}
    for source_type in SOURCE_TYPE_PRIORITY:
        if source_type in lowered_types:
            return SOURCE_TYPE_TO_INSTITUTION_TYPE[source_type], f"source_type:{source_type}"
    return None


def classify_by_name_heuristic(name):
    for institution_type, rule_name, pattern in NAME_HEURISTICS:
        if pattern.search(name or ""):
            return institution_type, f"name_heuristic:{rule_name}"
    return None


class InstitutionClassifier:
    """SCHEMA.md classification order. Rule 4 needs the parent's type, so classification recurses up the ROR
    parent chain (memoized); parents outside the dataset are fetched from ROR (cached) and classified the same way."""

    def __init__(self, institutions, overrides, ror_organizations):
        self.institutions = institutions
        self.overrides = overrides
        self.ror_organizations = ror_organizations
        self.result_by_id = {}
        self.in_progress = set()
        self.parent_fetch_failures = []

    def parent_ids(self, institution_id, institution):
        organization = self.ror_organizations.get(institution_id.removeprefix("ror:")) if institution_id.startswith("ror:") else None
        if organization is not None:
            return ror_parent_ids(organization)
        return [institution["parent_ror"]] if institution.get("parent_ror") else []

    def record_for(self, institution_id):
        """Dataset record, or a minimal record built from the ROR organization for parents outside the dataset."""
        if institution_id in self.institutions:
            return self.institutions[institution_id]
        ror = institution_id.removeprefix("ror:")
        organization = self.ror_organizations.get(ror)
        if organization is None:
            try:
                organization = fetch_ror_organization(ror)
            except Exception as error:
                self.parent_fetch_failures.append((institution_id, repr(error)))
                return None
            self.ror_organizations[ror] = organization
        parent_ids = ror_parent_ids(organization)
        return {"id": institution_id, "name": ror_display_name(organization), "website": ror_website(organization),
                "source_types": organization.get("types") or [], "parent_ror": parent_ids[0] if parent_ids else None}

    def classify(self, institution_id):
        if institution_id in self.result_by_id:
            return self.result_by_id[institution_id]
        institution = self.record_for(institution_id)
        if institution is None:
            return None
        self.in_progress.add(institution_id)
        result = self.classify_record(institution_id, institution)
        self.in_progress.discard(institution_id)
        self.result_by_id[institution_id] = result
        return result

    def classify_record(self, institution_id, institution):
        name = ADDRESS_NAME_PATTERN.sub(" ", institution.get("name") or "")
        override = self.overrides.get(institution_id)
        if override:
            return override["institution_type"], "manual_override"
        domain_result = classify_by_website_domain(institution.get("website"))
        if domain_result is None and institution_id.startswith("name:") and not institution.get("website"):
            domain_result = classify_by_name_domain_hint(institution.get("name"))
        source_type_result = classify_by_source_types(institution.get("source_types") or [])
        # A .com site is weak evidence when ROR/OpenAlex says the organization is not a company (airforce.com is a
        # recruiting site; the Air Force's own domain is .mil): fall through to the remaining rules. "funder" and
        # "other" say nothing about the sector (Bank of America is "other"), so they do not override.
        commercial_domain_conflict = (domain_result is not None and domain_result[0] == "commercial"
                                      and source_type_result is not None and source_type_result[0] != "commercial"
                                      and source_type_result[1] not in SECTOR_NEUTRAL_SOURCE_TYPE_REASONS)
        if domain_result and not commercial_domain_conflict:
            return domain_result
        if UNIVERSITY_PATTERN.search(name):
            return "educational", "university_or_college_name"
        for parent_id in self.parent_ids(institution_id, institution):
            parent_ror_id = f"ror:{parent_id}"
            if parent_ror_id in self.in_progress:
                continue
            parent_result = self.classify(parent_ror_id)
            if parent_result and parent_result[0] == "educational":
                return "educational", "parent_educational"
        if source_type_result and commercial_domain_conflict:
            return source_type_result[0], source_type_result[1].replace("source_type:", "source_type_over_commercial_domain:")
        if source_type_result:
            return source_type_result
        if institution_id.startswith("name:"):
            heuristic_result = classify_by_name_heuristic(name)
            if heuristic_result:
                return heuristic_result
        return "other", "no_type_information"


def classify_institutions(institutions, overrides, ror_organizations):
    classifier = InstitutionClassifier(institutions, overrides, ror_organizations)
    for institution_id, institution in institutions.items():
        institution["institution_type"], institution["institution_type_reason"] = classifier.classify(institution_id)
    return classifier.parent_fetch_failures


def drop_institutions_without_collaborations(institutions):
    dropped_ids = [institution_id for institution_id, institution in institutions.items()
                   if not institution.get("collaboration_count")]
    for institution_id in dropped_ids:
        institutions.pop(institution_id)
    return len(dropped_ids)


def ensure_institution_fields(institutions):
    for institution in institutions.values():
        institution.setdefault("website", None)
        institution.setdefault("source_categories", [])
