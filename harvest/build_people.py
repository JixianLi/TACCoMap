"""Person resolution (SCHEMA.md "Person resolution"): fold person records that are the same human into one.

Precision over recall: a merge or an ORCID assignment needs unambiguous evidence; ambiguous cases stay separate and
are listed in the merge log for maintainer review.

0. ORCID plausibility (round 5: keep an ORCID on a record only on hard evidence): an ORCID stays on a record only
   if the record's listed name fits the ORCID's registered name and some listed given-name initial fits a registered
   given name (data/evidence/orcid_names.json, from harvest/orcid_names.py; Cyrillic and Greek names transliterated).
   A name that fits only through the nickname table, a given-name-only registration, a first-initial conflict, and
   an ORCID that only appears under initials on large-collaboration papers all need hard evidence
   (OrcidHardEvidence: works overlap, DataCite source assertion, ORCID affiliation matching the record's institution
   or the award's awardee/subawardee in that period; for nickname-only and given-name-only names also an experience
   match with another record of the ORCID), or a confirm_orcid entry in person_aliases.json; a variant kept that way
   on one record is kept on the ORCID's other records too. For TACC staff / award / allocation ORCIDs the ORCID's
   public works or affiliations must also corroborate at least one record (an ORCID with an empty public record is
   kept unless another ORCID in the dataset has a compatible name). Rejected ORCIDs are stripped (participations move
   to the record's own OpenAlex author cluster when the listed name fits that cluster's name, else to name-only
   person ids) and listed per person in `orcid_rejected`; records kept despite needing evidence are listed in
   `orcid_check_overrides`.
1. Manual merges from data/manual/person_aliases.json; ids listed under "split" never merge automatically.
2. Hard keys: ORCID, OpenAlex author id, NSF piId, NIH profile id, plus the ORCID/OpenAlex ids that
   data/evidence/affiliation_evidence.json links to NSF/NIH people (high/medium matches only).
   Two different ORCIDs never merge.
3. Name-based, repeated until nothing changes: a group without an ORCID merges into a keyed group when names are
   compatible (nickname table, initials) AND both list the same institution (or one inside the other in the ROR
   hierarchy) in an overlapping time window, and the target is unambiguous. A merge that needs the nickname table
   also needs secondary evidence (nickname_merge_support).
4. NSF name-only records (no piId) merge only on an exact full-name match with a group that has dated evidence at
   the award's awardee or a subawardee during the award's active period.
5. After merging, a group loses its ORCID when a member record was rejected for it or a merged full name conflicts
   with it without hard evidence (merged_orcid_conflict).
"""

import collections
import functools
import json
import re
import unicodedata

import affiliation_evidence
import build_awards
import build_institutions
import common

PERSON_ALIASES_PATH = common.PROJECT_ROOT / "data" / "manual" / "person_aliases.json"
ORCID_NAMES_PATH = common.PROJECT_ROOT / "data" / "evidence" / "orcid_names.json"
KEY_TYPE_PRIORITY = ["orcid", "openalex", "nsf", "nih"]
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "prof", "former"}
# Hyphens and apostrophes are dropped, so "Smith-Jones" stays one surname rather than a middle initial plus
# "diehr", and ASCII and Unicode forms ("Jean‐Paul", "O’Shea") parse the same way.
NAME_PUNCTUATION = str.maketrans({character: "" for character in "-\u2010\u2011\u2012\u2013\u2014'\u2018\u2019`"})
# A participation at year Y counts for Y-1..Y+1, so two records "overlap" when their years differ by at most 2.
PARTICIPATION_YEAR_TOLERANCE = 1
USABLE_EVIDENCE_CONFIDENCES = {"high", "medium"}
NAME_APOSTROPHES = str.maketrans({character: "" for character in "'\u2018\u2019`"})
# Unicode hyphens vanish under ASCII folding; make them ASCII so "Sample‐Doe" still splits into two words.
UNICODE_HYPHENS = str.maketrans({character: "-" for character in "\u2010\u2011\u2012\u2013\u2014"})
# Look-alike letters that sources substitute (Greek iota for Turkish dotless i: "Sιrma Örnek"; Greek capitals for Latin
# initials: "Τ. S. Example", "Νathan Sample").
LOOKALIKE_LETTERS = str.maketrans({"ι": "i", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
                                   "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X"})
# Words written wholly in Cyrillic or Greek ("Иван Примеров", "Μαρία Παράδειγμα") are romanized before names
# are compared (simplified BGN/PCGN for Cyrillic, ELOT 743 for Greek; lowercase, accents handled separately). BGN/PCGN
# writes "е"/"ё" as "ye" at the start of a word and after a vowel or sign ("Образцаева" -> "Obraztsayeva").
TRANSLITERATION = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k", "λ": "l",
    "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f",
    "χ": "ch", "ψ": "ps", "ω": "o",
}
CYRILLIC_IOTATED_AFTER = set("аеёиоуыэюяъь")
# Scholarly romanization and most self-registered ORCID names write "е"/"ё" as plain "e" ("Elena", "Primeroev"), so
# both forms are compared; "э" always romanizes as "e".
CYRILLIC_E_AS_PLAIN_E = str.maketrans({"е": "э", "ё": "э", "Е": "Э", "Ё": "Э"})
# "Doe, JA": after a comma, a word of two or three capitals is a person's initials.
CAPITAL_INITIALS_PATTERN = re.compile(r"[A-Z]{2,3}")
# Papers with at least this many authors (or a truncated author list) are large collaborations, where initials-only
# author names ("P. Doe") are shared by many people and OpenAlex often attaches the wrong ORCID.
LARGE_COLLABORATION_AUTHOR_COUNT = 50
SOURCE_ASSERTED_ROLES = {"creator", "contributor"}
GERMAN_UMLAUT_SPELLINGS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                                         "ű": "ue", "ő": "oe", "Ű": "Ue", "Ő": "Oe"})
# A one-letter difference in a surname at least this long ("Kowalczyk"/"Kowalczik", "Hartely"/"Hartley") is
# accepted when the given name is spelled out and matches.
MIN_SURNAME_LENGTH_FOR_TYPO = 6
SURNAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "das", "dos", "di", "du", "la", "le",
                     "el", "al", "bin", "ten", "ter", "y"}
VOWELS = set("aeiouy")
TACC_INSTITUTION_IDS = {f"ror:{common.TACC_ROR_ID}", f"openalex:{common.TACC_OPENALEX_ID}"}
# Institution evidence derived from an ORCID's own employment list cannot corroborate that same ORCID.
CIRCULAR_INSTITUTION_EVIDENCE_METHODS = {"orcid_employment"}
# Above this many name-compatible groups for an initial-only name ("W. Wang"), no initial-based merge is attempted.
MAX_INITIAL_NAMESAKE_GROUPS = 20
MAX_NAME_MERGE_PASSES = 10


def load_person_aliases(path=PERSON_ALIASES_PATH):
    """{"merge": [{"person_ids": [id, id, ...], "reason": ...}], "split": [{"id": ..., "reason": ...}],
    "strip_orcid": [{"person_id" or "openalex_id": ..., "orcid": ..., "reason": ...}],
    "confirm_orcid": [{"orcid": ..., "person_ids": [...] (optional), "name_variants": [...] (optional), "reason": ...,
    "confirmed_by": "maintainer"}],
    "display_name": [{"person_id": ... (any id merged into the person), "display_name": ..., "reason": ...}]}"""
    if not path.exists():
        return {"merge": [], "split": [], "strip_orcid": [], "confirm_orcid": [], "display_name": []}
    aliases = json.loads(path.read_text())
    return {"merge": aliases.get("merge") or [], "split": aliases.get("split") or [],
            "strip_orcid": aliases.get("strip_orcid") or [], "confirm_orcid": aliases.get("confirm_orcid") or [],
            "display_name": aliases.get("display_name") or []}


def load_orcid_names(path=ORCID_NAMES_PATH):
    """{orcid: {given_names, family_name, other_names, retrieved_at[, activities]} or null (name not public)}"""
    if not path.exists():
        print(f"warning: no ORCID registered names at {path}; ORCIDs are only checked for surname conflicts")
        return {}
    return json.loads(path.read_text())


def normalize_orcid(value):
    if not value:
        return None
    return value.strip().rsplit("/", 1)[-1].removeprefix("orcid:").upper() or None


def normalize_openalex_author_id(value):
    if not value:
        return None
    return value.strip().rsplit("/", 1)[-1].removeprefix("openalex:") or None


def intrinsic_keys(person):
    """(key type, value) pairs the record itself carries."""
    person_id = person["id"]
    other_ids = person.get("other_ids") or {}
    keys = []
    orcid = normalize_orcid(person.get("orcid") or (person_id if person_id.startswith("orcid:") else None))
    if orcid:
        keys.append(("orcid", orcid))
    openalex_id = normalize_openalex_author_id(
        person.get("openalex_author_id") or (person_id if person_id.startswith("openalex:") else None))
    if openalex_id:
        keys.append(("openalex", openalex_id))
    nsf_pi_id = other_ids.get("nsf_pi_id") or (person_id.removeprefix("nsf:") if person_id.startswith("nsf:") else None)
    if nsf_pi_id:
        keys.append(("nsf", str(nsf_pi_id)))
    nih_profile_id = other_ids.get("nih_profile_id") or (person_id.removeprefix("nih:") if person_id.startswith("nih:") else None)
    if nih_profile_id:
        keys.append(("nih", str(nih_profile_id)))
    return keys


def evidence_keys(entry):
    if not entry or entry.get("match_confidence") not in USABLE_EVIDENCE_CONFIDENCES:
        return []
    keys = []
    if entry.get("orcid"):
        keys.append(("orcid", normalize_orcid(entry["orcid"])))
    for openalex_id in [entry.get("openalex_author_id")] + list(entry.get("additional_openalex_author_ids") or []):
        if openalex_id:
            keys.append(("openalex", normalize_openalex_author_id(openalex_id)))
    return keys


class PersonUnion:
    """Union-find over person ids that tracks each group's ORCIDs, so two different ORCIDs never join."""

    def __init__(self, person_ids, split_ids):
        self.parent = {person_id: person_id for person_id in person_ids}
        self.orcids = {person_id: set() for person_id in person_ids}
        self.split_ids = split_ids
        self.edges = []
        self.refused = []

    def find(self, person_id):
        root = person_id
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[person_id] != root:
            self.parent[person_id], person_id = root, self.parent[person_id]
        return root

    def union(self, first_id, second_id, reason, evidence, force=False):
        first_root, second_root = self.find(first_id), self.find(second_id)
        if first_root == second_root:
            return False
        if not force and (first_id in self.split_ids or second_id in self.split_ids):
            self.refused.append({"ids": [first_id, second_id], "reason": reason, "refused_because": "manual split"})
            return False
        if not force and self.orcids[first_root] and self.orcids[second_root] and self.orcids[first_root] != self.orcids[second_root]:
            self.refused.append({"ids": [first_id, second_id], "reason": reason, "evidence": evidence,
                                 "refused_because": f"different ORCIDs {sorted(self.orcids[first_root] | self.orcids[second_root])}"})
            return False
        self.parent[second_root] = first_root
        self.orcids[first_root] |= self.orcids.pop(second_root)
        self.edges.append((first_id, second_id, reason, evidence))
        return True

    def groups(self):
        members = collections.defaultdict(list)
        for person_id in self.parent:
            members[self.find(person_id)].append(person_id)
        return members


def merge_by_hard_keys(union, people, evidence_by_person, rejected_evidence_links=frozenset()):
    """Records sharing a key value join; record-intrinsic keys first, then keys linked by affiliation evidence
    (minus evidence ORCIDs rejected by the plausibility check)."""
    first_holder = {}
    for person_id, person in people.items():
        for key in intrinsic_keys(person):
            if key[0] == "orcid":
                union.orcids[union.find(person_id)].add(key[1])
    for person_id, person in people.items():
        for key in intrinsic_keys(person):
            holder = first_holder.setdefault(key, person_id)
            if holder != person_id:
                union.union(holder, person_id, f"hard_key:{key[0]}", f"{key[0]}:{key[1]}")
    for person_id, entry in evidence_by_person.items():
        if person_id not in people:
            continue
        for key in evidence_keys(entry):
            if key[0] == "orcid" and (person_id, key[1]) in rejected_evidence_links:
                continue
            holder = first_holder.get(key)
            if holder is None:
                # The evidence ORCID may belong to no record in the dataset; it still keys this person.
                first_holder[key] = person_id
                if key[0] == "orcid":
                    root = union.find(person_id)
                    if union.orcids[root] and key[1] not in union.orcids[root]:
                        union.refused.append({"ids": [person_id], "reason": "affiliation_evidence_orcid",
                                              "refused_because": f"evidence ORCID {key[1]} differs from {sorted(union.orcids[root])}"})
                    else:
                        union.orcids[root].add(key[1])
                continue
            if middle_initial_conflict(people[holder].get("name"), people[person_id].get("name")):
                union.refused.append({"ids": [holder, person_id], "reason": f"affiliation_evidence_{key[0]}",
                                      "refused_because": f"names conflict: {people[holder].get('name')!r} vs {people[person_id].get('name')!r}"})
                continue
            union.union(holder, person_id, f"affiliation_evidence_{key[0]}",
                        f"{key[0]}:{key[1]} ({entry.get('match_confidence')}: {entry.get('match_reason')})")
    return first_holder


def separate_capital_initials(name):
    """'Doe, JA' -> 'Doe, J A' and 'Smith, JR' -> 'Smith, J R'. Not when the whole name is in capitals
    ('ROE, JO': there the capitals say nothing about initials)."""
    family, comma, given = (name or "").partition(",")
    words = given.split()
    if not comma or family.isupper() or "," in given or not any(CAPITAL_INITIALS_PATTERN.fullmatch(word) for word in words):
        return name
    return f"{family}, " + " ".join(" ".join(word) if CAPITAL_INITIALS_PATTERN.fullmatch(word) else word for word in words)


def parse_person_name(name):
    """(given, middle initials, surname) from 'First M. Last' or 'Last, First'; None if fewer than two words."""
    name = separate_capital_initials(name)
    parts = [part.strip() for part in (name or "").split(",") if part.strip()]
    if len(parts) == 2 and common.normalize_name(parts[1]) not in NAME_SUFFIXES:
        parts = [parts[1], parts[0]]
    cleaned = " ".join(parts).translate(NAME_PUNCTUATION).translate(build_institutions.UNDECOMPOSABLE_LETTERS)
    tokens = [token for token in common.normalize_name(cleaned).split() if token not in NAME_SUFFIXES]
    if len(tokens) < 2:
        return None
    if len(parts) == 1 and len(tokens[0]) > 1 and all(len(token) == 1 for token in tokens[1:]):
        # "Doe J.Q": surname first, initials after, no comma (common in OpenAlex raw author names).
        return tokens[1], tuple(tokens[2:]), tokens[0]
    return tokens[0], tuple(token[0] for token in tokens[1:-1]), tokens[-1]


NICKNAME_GROUPS = affiliation_evidence.NICKNAME_GROUPS + [
    {"katarzyna", "kate", "kasia"}, {"clinton", "clint"}, {"bradley", "brad"}, {"margaret", "meg"},
    {"ann", "anne", "annie"}, {"nathan", "nathaniel", "nate"}, {"theodoros", "theodore", "theo"},
    {"kaitlin", "katelyn", "kait"}, {"francis", "frank"}, {"venkatesh", "venki", "venky"}, {"adrian", "adi"},
    {"zsuzsanna", "zsuzsa"}, {"animashree", "anima"}, {"stanley", "stan"}, {"kathleen", "kat"},
    {"konstantinos", "kostas"}, {"ravindra", "ravi"}, {"isabella", "isa"}, {"antonio", "antxon"},
    {"juanantonio", "juanan"}, {"viacheslav", "vyacheslav", "slava"},
]


def middle_initial_conflict(first_name, second_name):
    """True for different middle initials ("Robert Q Sample" vs "Robert N. Sample"). Surnames are not compared:
    the evidence harvester already matched them, and transliterations (Müstermann/Muestermann) would look like conflicts."""
    first_parsed, second_parsed = parse_person_name(first_name), parse_person_name(second_name)
    if not first_parsed or not second_parsed:
        return False
    return bool(first_parsed[1] and second_parsed[1] and first_parsed[1][0] != second_parsed[1][0])


def is_nickname_pair(first_given, second_given):
    return any(first_given in group and second_given in group for group in NICKNAME_GROUPS)


def is_short_form_pair(first_given, second_given):
    """Same-letter short form of a full name (Chris/Christopher, Joe/Joseph, Steve/Stephen): as strong as the initial
    match, so it needs no secondary evidence. Two full names in one group (Stephen/Steven, Alexander/Alexandra,
    Philip/Phillip; both 6+ letters) are different names and, like different-letter pairs (Bill/William), need it."""
    return first_given[0] == second_given[0] and min(len(first_given), len(second_given)) < 6


def given_names_compatible(first_given, second_given, allow_nicknames=True):
    """Returns 'full', 'initial' or None."""
    if first_given == second_given:
        return "initial" if len(first_given) == 1 else "full"
    if len(first_given) == 1 or len(second_given) == 1:
        return "initial" if first_given[0] == second_given[0] else None
    if is_nickname_pair(first_given, second_given) and (allow_nicknames or is_short_form_pair(first_given, second_given)):
        return "full"
    return None


def names_compatible(first_parsed, second_parsed, allow_nicknames=True):
    """'full' / 'initial' / None for two parsed names."""
    first_given, first_middles, first_surname = first_parsed
    second_given, second_middles, second_surname = second_parsed
    if first_surname != second_surname:
        return None
    if first_middles and second_middles and first_middles[0] != second_middles[0]:
        return None
    return given_names_compatible(first_given, second_given, allow_nicknames)


def name_spellings(text):
    """The text as given plus its German umlaut spelling ("Müstermann" also as "Muestermann"), and, when it has a Cyrillic "е"/"ё",
    its plain-"e" romanization ("Елена" as "Elena" as well as BGN/PCGN "Yelena")."""
    spellings = [text or "", (text or "").translate(GERMAN_UMLAUT_SPELLINGS)]
    if text and any(character in "еёЕЁ" for character in text):
        spellings.append(text.translate(CYRILLIC_E_AS_PLAIN_E))
    return spellings


def is_greek(character):
    return "\u0370" <= character <= "\u03ff" or "\u1f00" <= character <= "\u1fff"


def is_cyrillic_or_greek(character):
    return "\u0400" <= character <= "\u04ff" or is_greek(character)


def latin_letters(text):
    """Words written wholly in Cyrillic or Greek are transliterated (TRANSLITERATION); Greek letters inside a Latin
    word, or a lone Greek capital (an initial), read as their Latin look-alikes ("Sιrma Örnek", "Τ. S. Example")."""
    words = []
    for word in text.split(" "):
        letters = [character for character in word if character.isalpha()]
        if letters and all(is_cyrillic_or_greek(character) for character in letters) and (
                len(letters) > 1 or not is_greek(letters[0])):
            lowered = word.lower()
            if any(is_greek(character) for character in letters):
                # Greek accents carry no letter identity; "ου" is one sound, "ou" (ELOT 743).
                lowered = "".join(character for character in unicodedata.normalize("NFD", lowered)
                                  if not unicodedata.combining(character)).replace("ου", "ou")
            # Look up before decomposing: "й" and "ё" would lose their marks and read as "и" and "е".
            words.append("".join(
                "ye" if character in "её" and (index == 0 or lowered[index - 1] in CYRILLIC_IOTATED_AFTER)
                else TRANSLITERATION.get(character) if character in TRANSLITERATION else "".join(
                    TRANSLITERATION.get(part, part) for part in unicodedata.normalize("NFD", character)
                    if not unicodedata.combining(part))
                for index, character in enumerate(lowered)))
        else:
            words.append(word.translate(LOOKALIKE_LETTERS))
    return " ".join(words)


# Cached: the ORCID checks call this millions of times on a few hundred thousand distinct names.
@functools.lru_cache(maxsize=None)
def name_token_forms(text):
    """(tokens with hyphenated words joined, tokens with hyphenated words split, every token form of both spellings),
    as tuples and a frozenset (the result is cached and shared).

    Accents are folded and apostrophes dropped; name suffixes (Jr, PhD, ...) are ignored."""
    joined_tokens, split_tokens, every_form = None, None, set()
    for spelling in name_spellings(separate_capital_initials(text)):
        cleaned = latin_letters(spelling.translate(NAME_APOSTROPHES).translate(UNICODE_HYPHENS)).translate(
            build_institutions.UNDECOMPOSABLE_LETTERS)
        spelling_joined = [token for token in common.normalize_name(cleaned.translate(NAME_PUNCTUATION)).split()
                           if token not in NAME_SUFFIXES]
        spelling_split = [token for token in common.normalize_name(cleaned).split() if token not in NAME_SUFFIXES]
        if joined_tokens is None:
            joined_tokens, split_tokens = spelling_joined, spelling_split
        every_form |= set(spelling_joined) | set(spelling_split)
    return tuple(joined_tokens), tuple(split_tokens), frozenset(every_form)


def given_name_tokens_compatible(listed_token, registered_token, allow_nicknames=True):
    if listed_token == registered_token:
        return True
    if len(listed_token) == 1 or len(registered_token) == 1:
        return listed_token[0] == registered_token[0]
    return is_nickname_pair(listed_token, registered_token) and (
        allow_nicknames or is_short_form_pair(listed_token, registered_token))


def registered_name_candidates(registered):
    """(given names, family name) readings of an ORCID record's registered name, credit name and other names.

    Other names are free text, so both "Given Family" and "Family Given" readings are tried."""
    if not registered:
        return []
    candidates = []
    given_names, family_name = registered.get("given_names"), registered.get("family_name")
    if family_name:
        candidates.append((given_names or "", family_name))
        family_words = family_name.split()
        if len(family_words) > 1:
            # "Wen" + "Li Sample" is often "Wen Li" + "Sample".
            candidates.append((f"{given_names or ''} {' '.join(family_words[:-1])}".strip(), family_words[-1]))
        if given_names:
            # Given and family name typed into each other's fields ("Sample" / "Robert").
            candidates.append((family_name, given_names))
    free_text_names = ([] if family_name else [given_names]) + list(registered.get("other_names") or [])
    for free_text_name in free_text_names:
        words = (free_text_name or "").replace(",", " ").split()
        if len(words) >= 2:
            candidates.append((" ".join(words[:-1]), words[-1]))
            candidates.append((" ".join(words[1:]), words[0]))
        elif words:
            candidates.append(("", words[0]))
    return [(given, family) for given, family in candidates if name_token_forms(family)[0]]


def registered_display_name(registered):
    if not registered:
        return None
    return " ".join(part for part in (registered.get("given_names"), registered.get("family_name")) if part) or None


def listed_name_fits_registered(listed_name, registered, allow_nicknames=True):
    """True when a listed name can be the ORCID holder's name.

    Family name: the registered family name (accent-folded, hyphens joined or split, German umlaut spelling) or one of
    its parts (Ejemplo for "Ejemplo Muéstrez-Prueba"; particles like "van" do not count) appears in the listed name.
    Given names: every listed token that is not part of the family name must leave at least one token compatible with
    a registered given name (equal, same initial, nickname table, or the given names written together, "Xiaoming" for
    "Xiao Ming"). Token order is ignored, so "Doe, Xiaoming" and "Doe J.Q" work."""
    if not name_token_forms(listed_name)[1]:
        return True
    for given_text, family_text in registered_name_candidates(registered):
        family_forms, family_parts = set(), set()
        for spelling in name_spellings(family_text):
            spelling_joined, spelling_split, spelling_every = name_token_forms(spelling)
            for form in ("".join(spelling_joined), "".join(spelling_split)):
                # Registered "Mueller" listed as "Muller" (umlaut dropped rather than spelled out).
                family_forms |= {form, form.replace("ue", "u").replace("oe", "o").replace("ae", "a")}
            family_parts |= spelling_every
        family_forms |= {part for part in family_parts if len(part) >= 3 and part not in SURNAME_PARTICLES}
        given_split = [token for spelling in name_spellings(given_text) for token in name_token_forms(spelling)[1]]
        given_forms = set(given_split) | {"".join(name_token_forms(spelling)[1]) for spelling in name_spellings(given_text)}
        for listed_spelling in name_spellings(listed_name):
            listed_joined, listed_split, _ = name_token_forms(listed_spelling)
            matched = family_forms & (set(listed_joined) | set(listed_split))
            if not matched:
                matched = {token for start in range(len(listed_split)) for end in range(start + 2, len(listed_split) + 1)
                           if "".join(listed_split[start:end]) in family_forms for token in listed_split[start:end]}
            needs_full_given_name = False
            if not matched:
                matched = {token for token in listed_joined if len(token) >= MIN_SURNAME_LENGTH_FOR_TYPO and any(
                    len(form) >= MIN_SURNAME_LENGTH_FOR_TYPO and within_one_edit(token, form) for form in family_forms)}
                needs_full_given_name = True
            if not matched:
                continue
            for listed_tokens in (listed_joined, listed_split):
                remaining = []
                for token in listed_tokens:
                    if token in matched or token in family_parts:
                        continue
                    # "JQ Sample": a short vowel-free token is glued initials.
                    remaining.extend(token if len(token) <= 3 and not set(token) & VOWELS else [token])
                if not remaining or not given_split:
                    if not needs_full_given_name:
                        return True
                    continue
                if any(given_name_tokens_compatible(token, given_token, allow_nicknames) and not (
                        needs_full_given_name and (len(token) == 1 or len(given_token) == 1))
                       for token in remaining for given_token in given_forms):
                    return True
                if "".join(remaining) in given_forms:
                    return True
    return False


def registered_family_parts(registered):
    """Every token form of the registered family name ("lamas", "linares", "lamaslinares")."""
    return {form for spelling in name_spellings((registered or {}).get("family_name"))
            for form in name_token_forms(spelling)[2]}


def given_name_tokens(tokens, family_parts):
    given_tokens = set()
    for token in tokens:
        if token in family_parts:
            continue
        # "JQ Sample": a short vowel-free token is glued initials.
        given_tokens |= set(token) if len(token) <= 3 and not set(token) & VOWELS else {token}
    return given_tokens


def given_name_initial_conflict(listed_name, registered, allow_nicknames=True):
    """True when no given name of the listed name has the initial of (or is a nickname of) a registered given name,
    credit name or other name word ("J. Doe" vs registered "Mary Doe", "Quinn Sample" vs "Robert Sample"). Middle
    names count on both sides, so "R. Quinn Sample" fits "Quinn Sample". Words of the registered family name are ignored
    on both sides; other names are free text, so all their other words count."""
    family_parts = registered_family_parts(registered)
    if not family_parts:
        return False
    listed_tokens = given_name_tokens(
        {token for spelling in name_spellings(listed_name) for token in name_token_forms(spelling)[1]}, family_parts)
    registered_words = [registered.get("given_names") or ""] + list(registered.get("other_names") or [])
    registered_tokens = given_name_tokens(
        {token for text in registered_words for spelling in name_spellings(text) for token in name_token_forms(spelling)[1]},
        family_parts)
    return bool(listed_tokens and registered_tokens) and not any(
        listed_token[0] == registered_token[0] or given_name_tokens_compatible(listed_token, registered_token, allow_nicknames)
        for listed_token in listed_tokens for registered_token in registered_tokens)


def surname_fits_registered(listed_name, registered):
    """The listed name contains the registered family name (same forms as listed_name_fits_registered; given names
    and other names are not looked at)."""
    family_name = (registered or {}).get("family_name")
    return bool(family_name) and listed_name_fits_registered(listed_name, {"family_name": family_name})


def registered_given_name_only(registered):
    """A registration with one given name and nothing else ("John"): it says nothing about the family name."""
    return bool(registered and not registered.get("family_name") and not registered.get("other_names")
                and len((registered.get("given_names") or "").split()) == 1)


def name_check_failure(listed_name, registered):
    """None when the listed name can be the ORCID holder's, else the rejection reason: "orcid_initial_conflict" (the
    family name fits but no given-name initial does; for a given-name-only registration, no listed word has its
    initial or is its nickname), "orcid_name_mismatch", or one of two failures that only need hard evidence to keep
    the ORCID (the name alone is too weak to keep it): "orcid_given_name_only" (the registration is a single given
    name, so the family name was never compared) and "orcid_nickname_only" (the name fits only through the nickname
    table)."""
    if registered_given_name_only(registered):
        given_name = name_token_forms(registered["given_names"])[0]
        listed_tokens = given_name_tokens(
            {token for spelling in name_spellings(listed_name) for token in name_token_forms(spelling)[1]}, set())
        if not given_name or not listed_tokens or any(
                token[0] == given_name[0][0] or given_name_tokens_compatible(token, given_name[0]) for token in listed_tokens):
            return "orcid_given_name_only"
        return "orcid_initial_conflict"
    fits = listed_name_fits_registered(listed_name, registered)
    initial_conflict = given_name_initial_conflict(listed_name, registered)
    if fits and not initial_conflict:
        if listed_name_fits_registered(listed_name, registered, allow_nicknames=False) and not given_name_initial_conflict(
                listed_name, registered, allow_nicknames=False):
            return None
        return "orcid_nickname_only"
    if initial_conflict and surname_fits_registered(listed_name, registered):
        return "orcid_initial_conflict"
    return "orcid_name_mismatch"


def orcid_work_dois(registered):
    """Dataset DOIs among the ORCID's public works (fetched with /activities or, for name-check rescues, /works)."""
    return {doi.lower() for section in ("activities", "works")
            for doi in (((registered or {}).get(section) or {}).get("dataset_work_dois") or [])}


def works_rescue_precondition(record, registered):
    """Whether a participation that fails the name check may still be the ORCID holder's, pending its DOI being among
    the ORCID's works: the listed name has the registered family name, and the record has a DOI and a complete author
    list on which no other author has that family name (else the claimed work may be the other author's). For a
    given-name-only registration no family name is registered; the listed name's own surname is used instead."""
    if record["kind"] != "participant":
        return False
    if registered_given_name_only(registered):
        def has_same_surname(other_name):
            return not surnames_conflict(record["name"], other_name)
    elif surname_fits_registered(record["name"], registered):
        def has_same_surname(other_name):
            return surname_fits_registered(other_name, registered)
    else:
        return False
    collaboration = record["collaboration"]
    if not (collaboration.get("identifiers") or {}).get("doi") or collaboration.get("participants_truncated"):
        return False
    return not any(other is not record["participant"] and has_same_surname(other.get("name_as_listed"))
                   for other in collaboration.get("participants") or [])


def works_overlap_rescue(record, registered):
    """The reason a participation failing the name check is kept (works_rescue_precondition and the ORCID lists the
    record's DOI), or None."""
    if not works_rescue_precondition(record, registered):
        return None
    doi = record["collaboration"]["identifiers"]["doi"].lower()
    return f"works_overlap: ORCID lists doi:{doi}" if doi in orcid_work_dois(registered) else None


def within_one_edit(first, second):
    """Optimal string alignment distance <= 1 (one substitution, insertion, deletion or adjacent swap)."""
    if first == second:
        return True
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) == len(second):
        differences = [index for index in range(len(first)) if first[index] != second[index]]
        return len(differences) == 1 or (len(differences) == 2 and differences[1] == differences[0] + 1
                                         and first[differences[0]] == second[differences[1]]
                                         and first[differences[1]] == second[differences[0]])
    shorter, longer = sorted((first, second), key=len)
    return any(longer[:index] + longer[index + 1:] == shorter for index in range(len(longer)))


def likely_surname_forms(listed_name):
    """Forms the listed name's surname can take: the part before a comma, the first word of "Doe J.Q", else the
    last word; hyphenated surnames also by their parts."""
    parts = [part for part in (listed_name or "").split(",") if part.strip()]
    if len(parts) == 2 and common.normalize_name(parts[1]) not in NAME_SUFFIXES:
        surname_text = parts[0]
    else:
        joined_tokens = name_token_forms(listed_name)[0]
        if not joined_tokens:
            return set()
        if len(joined_tokens) > 1 and len(joined_tokens[0]) > 1 and all(len(token) == 1 for token in joined_tokens[1:]):
            surname_text = joined_tokens[0]
        else:
            surname_text = joined_tokens[-1]
    joined_tokens, split_tokens, every_form = name_token_forms(surname_text)
    return ({"".join(joined_tokens)} | {form for form in every_form if len(form) >= 3 and form not in SURNAME_PARTICLES}) - {""}


def surnames_conflict(first_name, second_name):
    """True when neither name contains the other's surname ("J. Doe" vs "Erik Sample"); word order is free."""
    first_surnames, second_surnames = likely_surname_forms(first_name), likely_surname_forms(second_name)
    if not first_surnames or not second_surnames:
        return False
    return not (first_surnames & name_token_forms(second_name)[2]) and not (second_surnames & name_token_forms(first_name)[2])


def group_has_surname_conflict(names):
    distinct_names = sorted({name for name in names if name})
    return any(surnames_conflict(first_name, second_name)
               for index_first, first_name in enumerate(distinct_names) for second_name in distinct_names[index_first + 1:])


def orcid_of_person(person):
    person_id = person["id"]
    return normalize_orcid(person.get("orcid") or (person_id if person_id.startswith("orcid:") else None))


def collect_orcid_records(people, collaborations, evidence_by_person):
    """orcid -> records that carry it: the person record, every participation of it, and usable evidence links.

    Record dicts: {"kind": "person" | "participant" | "evidence", "person_id", "name"} plus "collaboration" and
    "participant" for participations, "entry" for evidence links."""
    records = collections.defaultdict(list)
    orcid_by_person_id = {}
    for person_id, person in people.items():
        orcid = orcid_of_person(person)
        if orcid:
            orcid_by_person_id[person_id] = orcid
            records[orcid].append({"kind": "person", "person_id": person_id, "name": person.get("name")})
    for collaboration in collaborations.values():
        for participant in collaboration.get("participants") or []:
            orcid = orcid_by_person_id.get(participant.get("person_id"))
            if orcid:
                records[orcid].append({"kind": "participant", "person_id": participant["person_id"],
                                       "name": participant.get("name_as_listed") or people[participant["person_id"]].get("name"),
                                       "collaboration": collaboration, "participant": participant})
    for person_id, entry in evidence_by_person.items():
        if person_id in people and entry.get("orcid") and entry.get("match_confidence") in USABLE_EVIDENCE_CONFIDENCES:
            records[normalize_orcid(entry["orcid"])].append(
                {"kind": "evidence", "person_id": person_id, "name": people[person_id].get("name") or entry.get("name"),
                 "entry": entry})
    return records


def institution_names_for_matching(institution_id, hierarchy):
    """Normalized ROR names (display, aliases, labels) of an institution and its ROR ancestors."""
    names = set()
    for candidate_id in [institution_id] + sorted(hierarchy.ancestors(institution_id)):
        organization = hierarchy.ror_organizations.get(candidate_id.removeprefix("ror:")) if candidate_id.startswith("ror:") else None
        for name in (organization or {}).get("names") or []:
            names.add(normalized_organization_name(name.get("value")))
    return names - {""}


def normalized_organization_name(name):
    normalized = common.normalize_name(name)
    return normalized.removeprefix("the ").strip()


def affiliation_matches_participation(affiliation, affiliation_institution_id, institution_ids, raw_affiliation_name, hierarchy):
    """An ORCID affiliation matches one of the institution ids (ROR id, its own or the one its name resolves to, the
    same as or inside/around the institution; else the organization name among the institution's ROR names) or is
    named in the raw affiliation string."""
    affiliation_name = normalized_organization_name(affiliation.get("organization"))
    raw_affiliation = f" {common.normalize_name(raw_affiliation_name)} "
    for institution_id in institution_ids:
        if affiliation_institution_id and (hierarchy.is_same_or_within(institution_id, affiliation_institution_id)
                                           or hierarchy.is_same_or_within(affiliation_institution_id, institution_id)):
            return True
        if affiliation_name and (institution_id == f"name:{affiliation_name}" or any(
                affiliation_name == name or (len(name.split()) >= 3 and f" {name} " in f" {affiliation_name} ")
                for name in institution_names_for_matching(institution_id, hierarchy))):
            return True
    return bool(affiliation_name) and len(affiliation_name.split()) >= 2 and f" {affiliation_name} " in raw_affiliation


def affiliation_covers_years(affiliation, first_year, last_year, tolerance):
    """The ORCID affiliation's span overlaps first_year..last_year (+-tolerance); a missing start or end is open."""
    start_year, end_year = affiliation.get("start_year"), affiliation.get("end_year")
    return (start_year is None or start_year - tolerance <= last_year) and (end_year is None or first_year <= end_year + tolerance)


def is_source_asserted(collaboration, participant, orcid):
    """The source itself lists this ORCID for this person: a DataCite creator/contributor nameIdentifier, entered by
    the depositor. OpenAlex authorship ORCIDs come from OpenAlex's author clustering, not from the publisher, so they
    are not an assertion; Crossref (which does carry publisher-deposited ORCIDs) is not harvested."""
    return (participant.get("role") in SOURCE_ASSERTED_ROLES and participant.get("person_id") == f"orcid:{orcid}"
            and any(source.get("source") == "datacite" for source in collaboration.get("sources") or []))


class OrcidHardEvidence:
    """Hard evidence that an ORCID belongs to one participation (round 5: keep an ORCID only on hard evidence):
    - works overlap (works_overlap_rescue);
    - source assertion (is_source_asserted);
    - an ORCID affiliation (from the cached /activities, fetched for TACC staff and PIs) matching the institution
      listed on the record in that year (+-PARTICIPATION_YEAR_TOLERANCE), or, for an award PI/co-PI, the awardee or a
      USAspending subawardee during the award's active period. Both are source data, never derived from the ORCID;
      participant institutions taken from ORCID employment are skipped (circular).
    ORCID affiliations and name: institution ids are compared by ROR id where possible: the affiliation's own ROR id,
    else its name through the manual institution aliases and the ROR names of institutions in the dataset
    ("Stony Brook University")."""

    def __init__(self, collaborations, hierarchy):
        self.hierarchy = hierarchy
        self.subawardee_ids = build_awards.subawardee_ids_by_prime(collaborations)
        self.institution_id_by_name = {
            **build_institutions.build_dataset_name_index({f"ror:{ror}": {} for ror in hierarchy.ror_organizations},
                                                          hierarchy.ror_organizations),
            **build_institutions.load_aliases()}

    def resolved_institution_id(self, institution_id):
        if not institution_id or not institution_id.startswith("name:"):
            return institution_id
        return self.institution_id_by_name.get(build_institutions.matching_key(institution_id.removeprefix("name:")), institution_id)

    def affiliation_institution_id(self, affiliation):
        if affiliation.get("ror"):
            return f"ror:{affiliation['ror']}"
        return self.institution_id_by_name.get(build_institutions.matching_key(affiliation.get("organization")))

    def affiliation_supports(self, affiliation, collaboration, participant):
        """Why this ORCID affiliation corroborates the participation, or None."""
        affiliation_institution_id = self.affiliation_institution_id(affiliation)
        described = f"ORCID affiliation {affiliation.get('organization')!r} ({affiliation.get('start_year') or '?'}-{affiliation.get('end_year') or ''})"
        if (participant.get("institution_evidence") or {}).get("method") not in CIRCULAR_INSTITUTION_EVIDENCE_METHODS:
            year = collaboration.get("year")
            institution_ids = [self.resolved_institution_id(institution_id) for institution_id in
                               [participant.get("institution_id")] + list(participant.get("additional_institution_ids") or [])
                               if institution_id]
            if (year is None or affiliation_covers_years(affiliation, year, year, PARTICIPATION_YEAR_TOLERANCE)) and \
                    affiliation_matches_participation(affiliation, affiliation_institution_id, institution_ids,
                                                      participant.get("institution_name_as_listed"), self.hierarchy):
                return f"{described} matches {collaboration['id']}"
        period = award_active_period(collaboration) if (
            participant.get("role") in ("pi", "co_pi") and collaboration.get("kind") == "award") else None
        if period and affiliation_covers_years(affiliation, period[0], period[1], 0):
            places = [self.resolved_institution_id(other["institution_id"]) for other in collaboration.get("participants") or []
                      if other.get("role") == "awardee_org" and other.get("institution_id")]
            places += sorted(self.subawardee_ids.get(collaboration["id"], ()))
            if affiliation_matches_participation(affiliation, affiliation_institution_id, places, None, self.hierarchy):
                return (f"{described} matches the awardee or a subawardee of {collaboration['id']} during its active "
                        f"period {period[0]}-{period[1]}")
        return None

    def for_record(self, orcid, record, registered, failure, participants_by_person):
        """The hard evidence that the ORCID belongs to this participation or evidence link, or None. failure is the
        record's name-check failure (None if the name fits); a full name mismatch is only overcome by a works overlap.
        An evidence link is judged by the linked NSF/NIH person's participations."""
        if record["kind"] == "evidence":
            if failure == "orcid_name_mismatch":
                return None
            for collaboration, participant in participants_by_person.get(record["person_id"], []):
                for affiliation in ((registered or {}).get("activities") or {}).get("affiliations") or []:
                    support = self.affiliation_supports(affiliation, collaboration, participant)
                    if support:
                        return f"orcid_affiliation: {support} (linked {record['person_id']})"
            return None
        if record["kind"] != "participant":
            return None
        reason = works_overlap_rescue(record, registered)
        if reason or failure == "orcid_name_mismatch":
            return reason
        collaboration, participant = record["collaboration"], record["participant"]
        if is_source_asserted(collaboration, participant, orcid):
            return f"source_asserted: DataCite metadata of {collaboration['id']} lists this ORCID for {record['name']!r}"
        for affiliation in ((registered or {}).get("activities") or {}).get("affiliations") or []:
            support = self.affiliation_supports(affiliation, collaboration, participant)
            if support:
                return f"orcid_affiliation: {support}"
        return None

    def places(self, record, participants_by_person):
        """(institution id, year) of a participation, or of the participations an evidence link's person has."""
        if record["kind"] == "participant":
            pairs = [(record["collaboration"], record["participant"])]
        elif record["kind"] == "evidence":
            pairs = participants_by_person.get(record["person_id"], [])
        else:
            return []
        return [(self.resolved_institution_id(institution_id), collaboration["year"]) for collaboration, participant in pairs
                if collaboration.get("year") is not None
                and (participant.get("institution_evidence") or {}).get("method") not in CIRCULAR_INSTITUTION_EVIDENCE_METHODS
                for institution_id in [participant.get("institution_id")] + list(participant.get("additional_institution_ids") or [])
                if institution_id]

    def experience_match(self, record, anchors, participants_by_person):
        """A nickname-only or given-name-only record shares a place and time (same institution or one inside the
        other, years within 2) with a record of the ORCID that needed no nickname or has hard evidence."""
        for institution_id, year in self.places(record, participants_by_person):
            for anchor in anchors:
                for anchor_institution_id, anchor_year in self.places(anchor, participants_by_person):
                    if abs(year - anchor_year) <= 2 * PARTICIPATION_YEAR_TOLERANCE and is_same_place(
                            institution_id, anchor_institution_id, self.hierarchy):
                        place = anchor["collaboration"]["id"] if anchor["kind"] == "participant" else anchor["person_id"]
                        return (f"experience_match: at {institution_id} in {year}, as is {anchor['name']!r} on {place} "
                                f"({anchor_year})")
        return None


def corroborating_participations(records, participants_by_person):
    """Participations whose institution was not derived from the ORCID itself: the ORCID's own participations, and
    those of NSF/NIH people the evidence file links to the ORCID, minus institutions taken from ORCID employment."""
    pairs = [(record["collaboration"], record["participant"]) for record in records if record["kind"] == "participant"]
    for record in records:
        if record["kind"] == "evidence":
            pairs.extend(participants_by_person.get(record["person_id"], []))
    return [(collaboration, participant) for collaboration, participant in pairs
            if (participant.get("institution_evidence") or {}).get("method") not in CIRCULAR_INSTITUTION_EVIDENCE_METHODS]


def orcid_activities_support(orcid, activities, records, participants_by_person, hard_evidence):
    """Why the ORCID's public record supports at least one of these records, or None."""
    work_dois = {doi.lower() for doi in activities.get("dataset_work_dois") or []}
    for collaboration, participant in corroborating_participations(records, participants_by_person):
        doi = ((collaboration.get("identifiers") or {}).get("doi") or "").lower()
        if doi and doi in work_dois:
            return f"ORCID lists work doi:{doi}"
        if is_source_asserted(collaboration, participant, orcid):
            return f"DataCite metadata of {collaboration['id']} lists this ORCID"
        for affiliation in activities.get("affiliations") or []:
            support = hard_evidence.affiliation_supports(affiliation, collaboration, participant)
            if support:
                return support
    return None


class OrcidStripper:
    """Moves participations and person records off an ORCID they do not belong to, keeping an audit trail."""

    def __init__(self, people, collaborations):
        self.people = people
        self.participants_by_person = collections.defaultdict(list)
        for collaboration in collaborations.values():
            for participant in collaboration.get("participants") or []:
                if participant.get("person_id"):
                    self.participants_by_person[participant["person_id"]].append((collaboration, participant))
        self.stripped = collections.OrderedDict()
        self.rejected_evidence_links = set()
        # Evidence links a person_aliases.json strip_orcid names: the matched OpenAlex profile carries the same wrong
        # identity as its ORCID, so build.py drops the whole evidence entry.
        self.manually_rejected_evidence_person_ids = set()
        self.fully_stripped_orcids = set()
        self.overrides = collections.OrderedDict()
        # orcid -> normalized listed names kept by hard evidence or a confirmation despite needing it.
        self.evidence_variants = collections.defaultdict(set)
        self.orcid_names = {}
        self.hard_evidence = None

    def log(self, orcid, original_person_id, new_person_id, name, reason, collaboration_id=None):
        key = (orcid, original_person_id, new_person_id, name, reason)
        entry = self.stripped.setdefault(key, {"orcid": orcid, "person_id": original_person_id, "new_person_id": new_person_id,
                                               "name_as_listed": name, "reason": reason, "collaboration_count": 0,
                                               "example_collaboration_ids": []})
        if collaboration_id:
            entry["collaboration_count"] += 1
            if len(entry["example_collaboration_ids"]) < 5:
                entry["example_collaboration_ids"].append(collaboration_id)

    def log_override(self, orcid, record, reason):
        """A record an automatic check would have stripped but hard evidence or a confirm_orcid entry keeps."""
        key = (orcid, record["person_id"], record["name"], record["kind"], reason.split(":")[0])
        entry = self.overrides.setdefault(key, {"orcid": orcid, "person_id": record["person_id"], "name_as_listed": record["name"],
                                                "reason": reason, "collaboration_count": 0, "example_collaboration_ids": []})
        if record["kind"] == "participant":
            entry["collaboration_count"] += 1
            if len(entry["example_collaboration_ids"]) < 5:
                entry["example_collaboration_ids"].append(record["collaboration"]["id"])

    def attach_overrides(self):
        for entry in self.overrides.values():
            if entry["person_id"] in self.people:
                self.people[entry["person_id"]].setdefault("orcid_check_overrides", []).append(
                    {"orcid": entry["orcid"], "name_as_listed": entry["name_as_listed"], "reason": entry["reason"]})

    @staticmethod
    def add_rejection(person, orcid, reason, registered):
        rejection = {"orcid": orcid, "reason": reason, "registered_name": registered_display_name(registered)}
        rejections = person.setdefault("orcid_rejected", [])
        if not any(existing["orcid"] == orcid for existing in rejections):
            rejections.append(rejection)

    def name_only_person(self, name, source, sources, orcid, reason, registered):
        """The name-only person a stripped participation moves to: name:<normalized>|<source> (the convention for
        authorships without a usable author id)."""
        normalized = common.normalize_name(name)
        if not normalized:
            return None
        person_id = f"name:{normalized}|{source}"
        person = self.people.setdefault(person_id, {"id": person_id, "name": name, "orcid": None, "openalex_author_id": None,
                                                     "other_ids": {}, "sources": list(sources)})
        self.add_rejection(person, orcid, reason, registered)
        return person_id

    def cluster_person(self, person, orcid, reason, registered):
        """The OpenAlex author cluster of a record whose own name is not the ORCID holder, as a person of its own."""
        openalex_id = normalize_openalex_author_id(person.get("openalex_author_id"))
        person_id = f"openalex:{openalex_id}"
        cluster = self.people.setdefault(person_id, {"id": person_id, "name": person.get("name"), "orcid": None,
                                                      "openalex_author_id": openalex_id, "other_ids": {},
                                                      "sources": list(person.get("sources") or [])})
        self.add_rejection(cluster, orcid, reason, registered)
        return person_id

    def strip_participation(self, orcid, collaboration, participant, reason, registered, cluster_of=None):
        """Move one participation off the ORCID: to the OpenAlex cluster of `cluster_of` (a person record whose own
        name is not the ORCID holder) when the listed name fits that record's name, else to a name-only person."""
        original_person_id = participant["person_id"]
        source = ((collaboration.get("sources") or [{}])[0]).get("source") or "unknown"
        if cluster_of and cluster_of.get("openalex_author_id") and not surnames_conflict(
                cluster_of.get("name"), participant.get("name_as_listed")):
            new_person_id = self.cluster_person(cluster_of, orcid, reason, registered)
        else:
            new_person_id = self.name_only_person(participant.get("name_as_listed"), source, collaboration.get("sources") or [],
                                                  orcid, reason, registered)
        participant["person_id"] = new_person_id
        self.participants_by_person[original_person_id] = [
            pair for pair in self.participants_by_person[original_person_id] if pair[1] is not participant]
        if new_person_id:
            self.participants_by_person[new_person_id].append((collaboration, participant))
        self.log(orcid, original_person_id, new_person_id, participant.get("name_as_listed"), reason, collaboration["id"])

    def strip_person_record(self, person_id, orcid, reason, registered):
        """The whole record is not the ORCID holder: re-key it by its OpenAlex author id (else a name id) and move
        its participations along. Participations whose listed surname conflicts with the record's own name were
        pulled into it through the wrong ORCID, so they move to name-only ids instead."""
        record_name = self.people[person_id].get("name")
        for collaboration, participant in list(self.participants_by_person.get(person_id, [])):
            if surnames_conflict(record_name, participant.get("name_as_listed")):
                self.strip_participation(orcid, collaboration, participant, reason, registered)
        person = self.people.pop(person_id)
        openalex_id = normalize_openalex_author_id(person.get("openalex_author_id"))
        source = ((person.get("sources") or [{}])[0]).get("source") or "unknown"
        new_person_id = f"openalex:{openalex_id}" if openalex_id else f"name:{common.normalize_name(person.get('name'))}|{source}"
        rekeyed = dict(person, id=new_person_id, orcid=None)
        if new_person_id in self.people:
            existing = self.people[new_person_id]
            existing["sources"] = merge_sources([existing, rekeyed])
            rekeyed = existing
        else:
            self.people[new_person_id] = rekeyed
        self.add_rejection(rekeyed, orcid, reason, registered)
        moved = self.participants_by_person.pop(person_id, [])
        for collaboration, participant in moved:
            participant["person_id"] = new_person_id
            self.log(orcid, person_id, new_person_id, participant.get("name_as_listed"), reason, collaboration["id"])
        if not moved:
            self.log(orcid, person_id, new_person_id, person.get("name"), reason)
        self.participants_by_person[new_person_id].extend(moved)
        return new_person_id

    def detach_mismatched_record_identity(self, person_id, orcid, fitting_names, reason):
        """The record's own display name (and so its OpenAlex author cluster) is not the ORCID holder, but some of its
        participations are: keep the ORCID, drop the cluster id (its mismatching participations moved to the cluster
        person), show the most common fitting listed name."""
        person = self.people[person_id]
        dropped_openalex_id = person.get("openalex_author_id")
        self.log(orcid, person_id, person_id, person.get("name"),
                 f"{reason}; record name replaced, OpenAlex author id {dropped_openalex_id} dropped")
        # Never the ORCID-registered name: a displayed name is one a source listed (or a manual display_name).
        if fitting_names:
            person["name"] = collections.Counter(fitting_names).most_common(1)[0][0]
        person["openalex_author_id"] = None
        person.setdefault("orcid_rejected_openalex_author_ids", []).append(
            {"openalex_author_id": dropped_openalex_id, "reason": reason})

    def apply(self, orcid, rejected_records, all_records, reason_for, registered):
        rejected_ids = {id(record) for record in rejected_records}
        records_by_person = collections.defaultdict(list)
        for record in all_records:
            if record["kind"] != "evidence":
                records_by_person[record["person_id"]].append(record)
            elif id(record) in rejected_ids:
                self.rejected_evidence_links.add((record["person_id"], orcid))
                self.log(orcid, record["person_id"], record["person_id"], record["name"],
                         reason_for(record) + " (affiliation evidence link)")
        for person_id, person_records in records_by_person.items():
            rejected_here = [record for record in person_records if id(record) in rejected_ids]
            if not rejected_here:
                continue
            if len(rejected_here) == len(person_records):
                self.strip_person_record(person_id, orcid, reason_for(rejected_here[0]), registered)
                continue
            person_record = next((record for record in rejected_here if record["kind"] == "person"), None)
            cluster_of = dict(self.people[person_id]) if person_record else None
            for record in rejected_here:
                if record["kind"] == "participant":
                    self.strip_participation(orcid, record["collaboration"], record["participant"], reason_for(record),
                                             registered, cluster_of)
            if person_record:
                fitting_names = [record["name"] for record in person_records
                                 if record["kind"] == "participant" and id(record) not in rejected_ids]
                self.detach_mismatched_record_identity(person_id, orcid, fitting_names, reason_for(person_record))


def apply_manual_orcid_strips(stripper, people, evidence_by_person, manual_strips):
    for strip in manual_strips:
        orcid = normalize_orcid(strip.get("orcid"))
        reason = f"manual: {strip.get('reason') or 'data/manual/person_aliases.json'}"
        openalex_id = normalize_openalex_author_id(strip.get("openalex_id"))
        person_ids = [strip["person_id"]] if strip.get("person_id") else [
            person_id for person_id, person in people.items()
            if openalex_id and normalize_openalex_author_id(person.get("openalex_author_id")) == openalex_id]
        matched = False
        for person_id in person_ids:
            if person_id in people and orcid_of_person(people[person_id]) and orcid in (None, orcid_of_person(people[person_id])):
                new_person_id = stripper.strip_person_record(person_id, orcid_of_person(people[person_id]), reason, None)
                # The record's display name came from the wrong identity (OpenAlex shows "Nur Aini Sample" for
                # papers listing "N. M. Sample"), so show the name the records list instead.
                listed_names = [participant.get("name_as_listed") for _, participant in
                                stripper.participants_by_person.get(new_person_id, []) if participant.get("name_as_listed")]
                if listed_names:
                    people[new_person_id]["name"] = collections.Counter(listed_names).most_common(1)[0][0]
                matched = True
        for person_id, entry in evidence_by_person.items():
            linked_openalex_ids = {normalize_openalex_author_id(value) for value in
                                   [entry.get("openalex_author_id")] + list(entry.get("additional_openalex_author_ids") or [])}
            if entry.get("orcid") and normalize_orcid(entry["orcid"]) == orcid and (
                    person_id in person_ids or (openalex_id and openalex_id in linked_openalex_ids)):
                stripper.rejected_evidence_links.add((person_id, orcid))
                stripper.manually_rejected_evidence_person_ids.add(person_id)
                stripper.log(orcid, person_id, person_id, entry.get("name"), reason + " (affiliation evidence link)")
                matched = True
        if not matched:
            print(f"warning: person_aliases.json strip_orcid matched nothing: {strip}")


def confirmation_reasons(orcid, records, confirmations):
    """id(record) -> reason for the records of this ORCID that a confirm_orcid entry covers: all of them, or those
    whose person id is in its person_ids and/or whose listed name is in its name_variants."""
    reasons = {}
    for confirmation in confirmations:
        if normalize_orcid(confirmation.get("orcid")) != orcid:
            continue
        person_ids = set(confirmation.get("person_ids") or [])
        name_variants = {common.normalize_name(name) for name in confirmation.get("name_variants") or []}
        for record in records:
            if (not person_ids or record["person_id"] in person_ids) and (
                    not name_variants or common.normalize_name(record["name"]) in name_variants):
                reasons.setdefault(id(record), f"confirmed_by_{confirmation.get('confirmed_by') or 'maintainer'}: "
                                               f"{confirmation.get('reason') or 'data/manual/person_aliases.json'}")
    return reasons


def is_initials_only(name):
    """The listed given and middle names are all initials ("P. Doe", "Doe, P.J.", "PJ Doe"; not
    "H. Quinn Sample")."""
    parsed = parse_person_name(name)
    if not parsed:
        return False
    tokens = common.normalize_name(separate_capital_initials(name).translate(NAME_PUNCTUATION)).split()
    given_tokens = [token for token in tokens if token != parsed[2] and token not in NAME_SUFFIXES | SURNAME_PARTICLES]
    return bool(given_tokens) and all(len(token) == 1 or (len(token) <= 3 and not set(token) & VOWELS)
                                      for token in given_tokens)


def is_large_collaboration(collaboration):
    return collaboration.get("kind") == "paper" and (
        bool(collaboration.get("participants_truncated"))
        or len(collaboration.get("participants") or []) >= LARGE_COLLABORATION_AUTHOR_COUNT)


def initials_only_large_collaboration(records):
    """The ORCID only ever appears under initials-only names on large-collaboration papers (no evidence link)."""
    participations = [record for record in records if record["kind"] == "participant"]
    return bool(participations) and all(record["kind"] != "evidence" for record in records) and all(
        is_initials_only(record["name"]) and is_large_collaboration(record["collaboration"]) for record in participations)


def name_variant_key(name):
    """The same name variant in either word order ("Ejemplo, Juanan" = "Juanan Ejemplo")."""
    return parse_person_name(name) or common.normalize_name(name)


def hard_evidence_rescues(orcid, records, needs_evidence, failures, registered, confirmed, hard_evidence,
                          participants_by_person):
    """id(record) -> reason for the records needing evidence that have it: their own hard evidence
    (OrcidHardEvidence.for_record); for nickname-only and given-name-only names, an experience match with a record
    that needed no evidence or has its own; or the same listed name (name_variant_key) as another record of this ORCID
    kept by hard evidence or a confirm_orcid entry: a name variant shown to be the ORCID holder once is the holder
    everywhere."""
    rescues = {}
    for record in records:
        if id(record) in needs_evidence and id(record) not in confirmed:
            reason = hard_evidence.for_record(orcid, record, registered, failures.get(id(record)), participants_by_person)
            if reason:
                rescues[id(record)] = reason
    anchors = [record for record in records if id(record) not in needs_evidence or id(record) in rescues or id(record) in confirmed]
    for record in records:
        if id(record) in needs_evidence and id(record) not in rescues and id(record) not in confirmed and failures.get(
                id(record)) in ("orcid_nickname_only", "orcid_given_name_only"):
            reason = hard_evidence.experience_match(record, anchors, participants_by_person)
            if reason:
                rescues[id(record)] = reason
    rescued_variants = {}
    for record in records:
        reason = rescues.get(id(record)) or confirmed.get(id(record))
        if reason and record["name"]:
            place = record["collaboration"]["id"] if record["kind"] == "participant" else record["person_id"]
            rescued_variants.setdefault(name_variant_key(record["name"]), f"{reason.split(':')[0]} on {place}")
    for record in records:
        if id(record) in needs_evidence and id(record) not in rescues and id(record) not in confirmed:
            rescued_by = rescued_variants.get(name_variant_key(record["name"]))
            if rescued_by:
                rescues[id(record)] = f"same_variant: {record['name']!r} kept by {rescued_by}"
    return rescues


def check_orcid_plausibility(people, collaborations, evidence_by_person, orcid_names, hierarchy, manual_strips,
                             confirmations=None):
    """Strip ORCIDs from records they do not belong to (see module docstring, step 0). Mutates people and the
    participants' person_id in place; returns the OrcidStripper with the audit trail.

    confirmations: person_aliases.json confirm_orcid entries (loaded from the file when None). Records they cover are
    never stripped by the automatic checks; manual strip_orcid entries still apply first."""
    confirmations = load_person_aliases()["confirm_orcid"] if confirmations is None else confirmations
    stripper = OrcidStripper(people, collaborations)
    stripper.orcid_names = orcid_names
    stripper.hard_evidence = OrcidHardEvidence(collaborations, hierarchy)
    apply_manual_orcid_strips(stripper, people, evidence_by_person, manual_strips)
    records_by_orcid = collect_orcid_records(people, collaborations, evidence_by_person)
    for confirmation in confirmations:
        if not confirmation_reasons(normalize_orcid(confirmation.get("orcid")),
                                    records_by_orcid.get(normalize_orcid(confirmation.get("orcid")), []), [confirmation]):
            print(f"warning: person_aliases.json confirm_orcid matched nothing: {confirmation}")
    orcids_by_surname = collections.defaultdict(set)
    for orcid, records in records_by_orcid.items():
        for record in records:
            parsed = parse_person_name(record["name"]) if record["kind"] == "person" else None
            if parsed:
                orcids_by_surname[parsed[2]].add((orcid, parsed))
    for orcid, records in records_by_orcid.items():
        registered = orcid_names.get(orcid)
        confirmed = confirmation_reasons(orcid, records, confirmations)
        failures = {}
        registered_name = registered_display_name(registered) or (registered or {}).get("other_names")
        names = sorted({record["name"] for record in records if record["name"]})
        if registered_name_candidates(registered) and not (registered_given_name_only(registered) and group_has_surname_conflict(
                record["name"] for record in records)):
            failures = {id(record): name_check_failure(record["name"], registered) for record in records if record["name"]}
        elif group_has_surname_conflict(record["name"] for record in records):
            failures = {id(record): "orcid_name_unavailable_and_surname_conflict" for record in records}
        needs_evidence = {id(record) for record in records if failures.get(id(record))}
        kept_reasons = {**hard_evidence_rescues(orcid, records, needs_evidence, failures, registered, confirmed,
                                                stripper.hard_evidence, stripper.participants_by_person), **confirmed}
        large_collaboration_only = initials_only_large_collaboration(
            [record for record in records if id(record) not in needs_evidence or id(record) in kept_reasons])
        if large_collaboration_only:
            needs_evidence |= {id(record) for record in records if id(record) not in kept_reasons}
            kept_reasons = {**hard_evidence_rescues(orcid, records, needs_evidence, failures, registered, kept_reasons,
                                                    stripper.hard_evidence, stripper.participants_by_person), **kept_reasons}

        def reason_for(record, failures=failures, registered_name=registered_name, names=names):
            failure = failures.get(id(record))
            if failure is None:
                return (f"orcid_initials_only_large_collaboration: listed only as {names[:4]} on papers with "
                        f">={LARGE_COLLABORATION_AUTHOR_COUNT} authors or truncated author lists; no works overlap, "
                        f"source assertion or affiliation match")
            if failure == "orcid_name_unavailable_and_surname_conflict":
                return f"{failure}: records list {names[:6]}"
            return f"{failure}: listed {record['name']!r}, ORCID registered name {registered_name!r}"
        for record in records:
            if id(record) in needs_evidence and id(record) in kept_reasons:
                stripper.log_override(orcid, record, kept_reasons[id(record)])
                stripper.evidence_variants[orcid].add(name_variant_key(record["name"]))
        rejected = [record for record in records if id(record) in needs_evidence and id(record) not in kept_reasons]
        if large_collaboration_only and any(record["kind"] == "participant" and id(record) in kept_reasons for record in records):
            rejected = [record for record in rejected if record["kind"] == "participant" or failures.get(id(record))]
        if rejected:
            stripper.apply(orcid, rejected, records, reason_for, registered)
            if len(rejected) == len(records):
                stripper.fully_stripped_orcids.add(orcid)
            rejected_ids = {id(record) for record in rejected}
            records = [record for record in records if id(record) not in rejected_ids]
        activities = (registered or {}).get("activities")
        if activities is None or not records:
            continue
        if orcid_activities_support(orcid, activities, records, stripper.participants_by_person, stripper.hard_evidence):
            continue
        confirmed_here = [record for record in records if id(record) in kept_reasons and kept_reasons[id(record)].startswith("confirmed_by_")]
        if confirmed_here:
            for record in confirmed_here:
                stripper.log_override(orcid, record, kept_reasons[id(record)])
            continue
        if activities.get("public_work_count") or activities.get("affiliations"):
            summary = (f"orcid_public_record_unrelated: {activities.get('public_work_count') or 0} public works, affiliations "
                       f"{sorted({affiliation.get('organization') for affiliation in activities.get('affiliations') or []})[:4]}; "
                       f"none matches these records")
        else:
            parsed = parse_person_name(registered_display_name(registered))
            namesake_orcids = sorted({other_orcid for other_orcid, other_parsed in orcids_by_surname.get(parsed[2] if parsed else None, ())
                                      if other_orcid != orcid and names_compatible(parsed, other_parsed)})
            if not namesake_orcids:
                # Nothing public to check, but no other ORCID in the dataset has a compatible name either.
                continue
            summary = (f"orcid_public_record_empty: no public works or affiliations to tell it apart from namesake "
                       f"ORCIDs {namesake_orcids[:3]}")
        stripper.apply(orcid, records, records, lambda record, summary=summary: summary, registered)
        stripper.fully_stripped_orcids.add(orcid)
    stripper.attach_overrides()
    return stripper


class GroupProfile:
    """What name-based merging needs to know about one hard-key group."""

    def __init__(self, root_id, member_ids, people, union):
        self.root_id = root_id
        self.member_ids = member_ids
        self.orcids = set(union.orcids.get(root_id) or ())
        self.has_orcid = bool(self.orcids)
        self.is_keyed = any(intrinsic_keys(people[member_id]) for member_id in member_ids if member_id in people) or self.has_orcid
        self.parsed_names = {parsed for parsed in (parse_person_name(people[member_id].get("name"))
                                                  for member_id in member_ids if member_id in people) if parsed}
        # institution id -> [(first year, last year, tolerance in years)]; participations are single years that count
        # for +-PARTICIPATION_YEAR_TOLERANCE, evidence spans count as stated.
        self.intervals_by_institution = collections.defaultdict(list)
        self.participations = []

    @property
    def is_nsf_name_only(self):
        return not self.is_keyed and all(member_id.startswith("name:") and member_id.endswith("|nsf") for member_id in self.member_ids)

    def add_interval(self, institution_id, first_year, last_year, tolerance=0):
        self.intervals_by_institution[institution_id].append((first_year, last_year, tolerance))


def build_group_profiles(union, people, collaborations, evidence_by_person, extra_keyed_ids, rejected_evidence_links=frozenset()):
    groups = union.groups()
    profiles = {root_id: GroupProfile(root_id, member_ids, people, union) for root_id, member_ids in groups.items()}
    for root_id in profiles:
        if any(member_id in extra_keyed_ids for member_id in groups[root_id]):
            profiles[root_id].is_keyed = True
    for collaboration in collaborations.values():
        year = collaboration.get("year")
        for participant in collaboration.get("participants") or []:
            person_id = participant.get("person_id")
            if not person_id or person_id not in union.parent:
                continue
            profile = profiles[union.find(person_id)]
            profile.participations.append((collaboration, participant))
            if year is None:
                continue
            for institution_id in [participant.get("institution_id")] + list(participant.get("additional_institution_ids") or []):
                if institution_id:
                    profile.add_interval(institution_id, year, year, PARTICIPATION_YEAR_TOLERANCE)
    for person_id, entry in evidence_by_person.items():
        if person_id not in union.parent or entry.get("match_confidence") not in USABLE_EVIDENCE_CONFIDENCES:
            continue
        orcid_link_rejected = (person_id, normalize_orcid(entry.get("orcid"))) in rejected_evidence_links
        profile = profiles[union.find(person_id)]
        for affiliation in entry["affiliations"]:
            first_year, last_year = affiliation.get("start_year"), affiliation.get("end_year")
            if first_year is None and last_year is None:
                continue
            if orcid_link_rejected and (affiliation.get("source") or "").startswith("orcid"):
                continue
            profile.add_interval(affiliation["institution_id"], first_year or last_year, last_year or 9999)
    return profiles


def is_same_place(first_institution_id, second_institution_id, hierarchy):
    """Same institution, or one inside the other in the ROR hierarchy (TACC and UT Austin; not two UT campuses)."""
    return (first_institution_id == second_institution_id
            or hierarchy.is_same_or_within(first_institution_id, second_institution_id)
            or hierarchy.is_same_or_within(second_institution_id, first_institution_id))


def intervals_overlap(first_interval, second_interval):
    first_start, first_end, first_tolerance = first_interval
    second_start, second_end, second_tolerance = second_interval
    return (first_start - first_tolerance <= second_end + second_tolerance
            and second_start - second_tolerance <= first_end + first_tolerance)


def shared_institution_windows(first_profile, second_profile, hierarchy):
    """(first institution id, second institution id, first interval, second interval) of each overlapping shared
    place."""
    for first_institution_id, first_intervals in first_profile.intervals_by_institution.items():
        for second_institution_id, second_intervals in second_profile.intervals_by_institution.items():
            if not is_same_place(first_institution_id, second_institution_id, hierarchy):
                continue
            for first_interval in first_intervals:
                for second_interval in second_intervals:
                    if intervals_overlap(first_interval, second_interval):
                        yield first_institution_id, second_institution_id, first_interval, second_interval


def shared_institution_window(first_profile, second_profile, hierarchy):
    """The first of shared_institution_windows, or None."""
    return next(shared_institution_windows(first_profile, second_profile, hierarchy), None)


def describe_window(window):
    first_institution_id, second_institution_id, first_interval, second_interval = window
    place = first_institution_id if first_institution_id == second_institution_id else f"{first_institution_id} / {second_institution_id}"
    return (f"{place} ({first_interval[0] - first_interval[2]}-{first_interval[1] + first_interval[2]} vs "
            f"{second_interval[0] - second_interval[2]}-{second_interval[1] + second_interval[2]})")


def group_name_compatibility(first_profile, second_profile, allow_nicknames=True):
    """'full' if some name pair matches on full given names, 'initial' if only via initials; None if any pair of
    the two groups' names is incompatible (so a shared "R. Sample" cannot bridge "R Arun Sample" and "R Vijay Sample")."""
    best = None
    for first_parsed in first_profile.parsed_names:
        for second_parsed in second_profile.parsed_names:
            compatibility = names_compatible(first_parsed, second_parsed, allow_nicknames)
            if compatibility is None:
                return None
            if compatibility == "full" or best is None:
                best = compatibility
    return best


class NameSet:
    """Stand-in profile for the names two groups would have after merging."""

    def __init__(self, parsed_names):
        self.parsed_names = parsed_names


def display_names(profile):
    return sorted(" ".join([given] + list(middles) + [surname]) for given, middles, surname in profile.parsed_names)


def nickname_merge_support(source_profile, target_profile, hierarchy, orcid_names):
    """Secondary evidence for a merge whose names are compatible only through the nickname table, or None:
    - experience match: the two groups share a second place in an overlapping window, not the same as or inside/around
      the first (the first triggered the candidate match);
    - the target's ORCID registered name, or one of its other names, fits one of the source's names without the
      nickname table (name_check_failure finds nothing; "Isa Doe" registered as "Isa Doe");
    - works overlap: the target's ORCID works list the DOI of a source participation (works_rescue_precondition)."""
    windows = list(shared_institution_windows(source_profile, target_profile, hierarchy))
    for first_window in windows:
        for second_window in windows:
            if not is_same_place(first_window[0], second_window[0], hierarchy):
                return f"experience_match: {describe_window(first_window)} and {describe_window(second_window)}"
    for orcid in sorted(target_profile.orcids):
        registered = orcid_names.get(orcid) or {}
        for source_name in display_names(source_profile):
            if registered_name_candidates(registered) and not is_initials_only(source_name) and \
                    name_check_failure(source_name, registered) is None:
                return f"orcid_registered_name: ORCID {orcid} registered {registered_display_name(registered)!r} fits {source_name!r}"
        for other_name in registered.get("other_names") or []:
            other_parsed = parse_person_name(other_name)
            if other_parsed and any(names_compatible(parsed, other_parsed, allow_nicknames=False) == "full"
                                    for parsed in source_profile.parsed_names):
                return f"orcid_other_name: ORCID {orcid} lists {other_name!r}"
        work_dois = orcid_work_dois(registered)
        for collaboration, participant in source_profile.participations:
            doi = ((collaboration.get("identifiers") or {}).get("doi") or "").lower()
            record = {"kind": "participant", "name": participant.get("name_as_listed"), "collaboration": collaboration,
                      "participant": participant}
            if doi and doi in work_dois and works_rescue_precondition(record, registered):
                return f"works_overlap: ORCID {orcid} lists doi:{doi} ({record['name']!r})"
    return None


def name_based_merge_plan(profiles, hierarchy, orcid_names=None):
    """One pass over a snapshot of hard-key groups: every group without an ORCID picks at most one keyed target.

    Candidates are keyed groups whose names are all compatible with the source's names and that share a place (same
    institution or ROR ancestor/descendant) in an overlapping window. The merge happens only when it is unambiguous:
    - at most one candidate has an ORCID, and all candidates' names are compatible with the chosen target's
      (so "Jim Doe" and "Jennifer Doe" both sharing UW-Madison with "J. Doe" block the merge);
    - a keyed source (OpenAlex split profile) or an initial-only match: no other ORCID group anywhere is
      name-compatible with the source;
    - an initial-only match ("J. Doe" -> "Jim Doe", either side may be the initial-only one): no other group
      anywhere in the dataset that is name-compatible with the source or the target has an ORCID, every such group
      is also compatible with the merged names and with each other (no "Jennifer Doe" or "Jim A. / Jim B. Doe"
      elsewhere), and there are fewer than MAX_INITIAL_NAMESAKE_GROUPS of them;
    - no name pair of the two groups is compatible without the nickname table ("Juanan" / "Juan Antonio"):
      nickname_merge_support.
    Returns (planned merges, skip counts, refusals by source root id).
    """
    orcid_names = orcid_names or {}
    groups_by_surname = collections.defaultdict(set)
    for root_id, profile in profiles.items():
        for _, _, surname in profile.parsed_names:
            groups_by_surname[surname].add(root_id)
    planned = []
    skipped = collections.Counter()
    refusals = {}

    def refuse(root_id, category, target_ids, detail):
        skipped[category] += 1
        refusals[root_id] = {"ids": [root_id] + sorted(target_ids), "reason": "name_institution_time",
                             "refused_because": f"{category}: {detail}"}

    def namesakes_of(root_id):
        """Every other group, keyed or not, whose names are all compatible with this group's names."""
        namesakes = {}
        for surname in {surname for _, _, surname in profiles[root_id].parsed_names}:
            for other_root_id in groups_by_surname[surname]:
                if other_root_id == root_id or other_root_id in namesakes:
                    continue
                compatibility = group_name_compatibility(profiles[root_id], profiles[other_root_id])
                if compatibility:
                    namesakes[other_root_id] = compatibility
        return namesakes

    for root_id, profile in profiles.items():
        if profile.has_orcid or not profile.parsed_names or profile.is_nsf_name_only:
            continue
        namesakes = namesakes_of(root_id)
        compatible = {other_root_id: compatibility for other_root_id, compatibility in namesakes.items()
                      if profiles[other_root_id].is_keyed}
        if not compatible:
            continue
        sharing = {}
        for other_root_id in compatible:
            window = shared_institution_window(profile, profiles[other_root_id], hierarchy)
            if window:
                sharing[other_root_id] = window
        if not sharing:
            skipped["no_shared_institution_window"] += 1
            continue
        orcid_targets = sorted(other_root_id for other_root_id in sharing if profiles[other_root_id].has_orcid)
        if len(orcid_targets) > 1:
            refuse(root_id, "several_orcid_groups_share_place_and_window", orcid_targets,
                   f"{display_names(profile)} could be any of {orcid_targets}")
            continue
        target_id = orcid_targets[0] if orcid_targets else min(
            sharing, key=lambda other_root_id: (-len(profiles[other_root_id].member_ids), other_root_id))
        target_profile = profiles[target_id]
        conflicting = sorted(other_root_id for other_root_id in sharing
                             if other_root_id != target_id and group_name_compatibility(target_profile, profiles[other_root_id]) is None)
        if conflicting:
            refuse(root_id, "candidates_with_conflicting_names_share_place_and_window", [target_id] + conflicting,
                   f"{display_names(profile)} fits {display_names(target_profile)} and "
                   f"{[display_names(profiles[other_root_id]) for other_root_id in conflicting][:3]}")
            continue
        compatibility = compatible[target_id]
        if compatibility == "initial" or profile.is_keyed:
            other_orcid_namesakes = sorted(other_root_id for other_root_id in namesakes
                                           if other_root_id != target_id and profiles[other_root_id].has_orcid)
            if other_orcid_namesakes:
                refuse(root_id, "other_orcid_namesakes_elsewhere", [target_id] + other_orcid_namesakes[:5],
                       f"{display_names(profile)}: {len(other_orcid_namesakes)} other ORCID groups with compatible names")
                continue
        if compatibility == "initial":
            merged_names = NameSet(profile.parsed_names | target_profile.parsed_names)
            others = sorted((set(namesakes) | set(namesakes_of(target_id))) - {root_id, target_id})
            if len(others) >= MAX_INITIAL_NAMESAKE_GROUPS:
                refuse(root_id, "initial_only_common_name", [target_id],
                       f"{display_names(profile)} / {display_names(target_profile)}: {len(others)} other name-compatible groups")
                continue
            if any(profiles[other_root_id].has_orcid for other_root_id in others):
                refuse(root_id, "other_orcid_namesakes_elsewhere", [target_id] + others[:5],
                       f"{display_names(profile)} / {display_names(target_profile)}: another ORCID group has a compatible name")
                continue
            competing = [other_root_id for other_root_id in others
                         if group_name_compatibility(merged_names, profiles[other_root_id]) is None]
            if not competing:
                competing = [first_id for index_first, first_id in enumerate(others) for second_id in others[index_first + 1:]
                             if group_name_compatibility(profiles[first_id], profiles[second_id]) is None]
            if competing:
                refuse(root_id, "initial_only_competing_full_names", [target_id] + sorted(competing)[:5],
                       f"{display_names(profile)} fits {display_names(target_profile)} but also "
                       f"{sorted({name for other_root_id in competing for name in display_names(profiles[other_root_id])})[:4]}")
                continue
        nickname_support = ""
        if not all(names_compatible(source_parsed, target_parsed, allow_nicknames=False)
                   for source_parsed in profile.parsed_names for target_parsed in target_profile.parsed_names):
            nickname_support = nickname_merge_support(profile, target_profile, hierarchy, orcid_names)
            if not nickname_support:
                refuse(root_id, "nickname_only_without_secondary_evidence", [target_id],
                       f"{display_names(profile)} / {display_names(target_profile)} fit only through the nickname table; "
                       f"no second shared institution, ORCID other name or works overlap")
                continue
            nickname_support = f"; nickname supported by {nickname_support}"
        reason = "name_institution_time" + ("_keyed_source" if profile.is_keyed else "")
        planned.append((target_id, root_id, reason,
                        f"names {compatibility}-compatible ({display_names(profile)} / {display_names(target_profile)}); "
                        f"both at {describe_window(sharing[target_id])}{nickname_support}"))
    return planned, skipped, refusals


def award_active_period(collaboration):
    """(start year, end year) from start/end dates; end year = start year when no end date is known."""
    start_year = int(collaboration["start_date"][:4]) if collaboration.get("start_date") else collaboration.get("year")
    if start_year is None:
        return None
    end_year = int(collaboration["end_date"][:4]) if collaboration.get("end_date") else start_year
    return start_year, max(start_year, end_year)


def nsf_name_only_merge_plan(profiles, union, collaborations, hierarchy):
    """NSF records without a piId list only a name. One merges into a group only when that group has a name exactly
    equal to it (given name spelled out, same middle initials, same surname) and dated evidence (a participation year
    or evidence span inside the award's active period, no tolerance) at the award's awardee or a USAspending
    subawardee under it, and no other group qualifies."""
    awards_by_root = collections.defaultdict(list)
    for collaboration in collaborations.values():
        if collaboration.get("kind") != "award":
            continue
        for participant in collaboration.get("participants") or []:
            person_id = participant.get("person_id")
            if person_id and person_id in union.parent and profiles[union.find(person_id)].is_nsf_name_only:
                awards_by_root[union.find(person_id)].append(collaboration)
    subawardee_ids = build_awards.subawardee_ids_by_prime(collaborations)
    groups_by_exact_name = collections.defaultdict(set)
    for root_id, profile in profiles.items():
        if not profile.is_nsf_name_only:
            for parsed in profile.parsed_names:
                groups_by_exact_name[parsed].add(root_id)
    planned, refusals = [], {}
    for root_id, awards in awards_by_root.items():
        profile = profiles[root_id]
        if len(profile.parsed_names) != 1:
            continue
        parsed = next(iter(profile.parsed_names))
        if len(parsed[0]) < 2:
            continue
        candidates = [other_root_id for other_root_id in groups_by_exact_name.get(parsed, ())
                      if group_name_compatibility(profile, profiles[other_root_id])]
        supported = {}
        for collaboration in awards:
            period = award_active_period(collaboration)
            if period is None:
                continue
            places = {participant["institution_id"] for participant in collaboration.get("participants") or []
                      if participant["role"] == "awardee_org" and participant.get("institution_id")}
            places |= subawardee_ids.get(collaboration["id"], set())
            for other_root_id in candidates:
                for institution_id, intervals in profiles[other_root_id].intervals_by_institution.items():
                    if not any(hierarchy.is_same_or_within(institution_id, place) for place in places):
                        continue
                    for first_year, last_year, _ in intervals:
                        if first_year <= period[1] and period[0] <= last_year:
                            supported.setdefault(other_root_id, f"exact name; at {institution_id} {first_year}-{last_year}, "
                                                               f"within {collaboration['id']} active {period[0]}-{period[1]} "
                                                               f"(awardee/subawardee)")
        if len(supported) == 1:
            target_id, evidence = next(iter(supported.items()))
            planned.append((target_id, root_id, "nsf_name_award_period_evidence", evidence))
        elif len(supported) > 1:
            refusals[root_id] = {"ids": [root_id] + sorted(supported), "reason": "nsf_name_award_period_evidence",
                                 "refused_because": f"several exact-name groups have evidence at the awardee: {sorted(supported)[:5]}"}
    return planned, refusals


def key_priority(person_id):
    prefix = person_id.split(":", 1)[0]
    return KEY_TYPE_PRIORITY.index(prefix) if prefix in KEY_TYPE_PRIORITY else len(KEY_TYPE_PRIORITY)


def canonical_id_for_group(member_ids, union, people, collaboration_count_by_person):
    """Highest-priority key: the group's ORCID, else its most-used OpenAlex/NSF/NIH id, else its most-used name id."""
    root_orcids = union.orcids.get(union.find(member_ids[0])) or set()
    if len(root_orcids) == 1:
        return f"orcid:{next(iter(root_orcids))}"
    candidate_ids = [f"{key_type}:{value}" for member_id in member_ids if member_id in people
                     for key_type, value in intrinsic_keys(people[member_id]) if key_type != "orcid"]
    # A group whose ORCID was stripped after merging still has its orcid: member id; it must not key the group.
    candidate_ids += [member_id for member_id in member_ids if member_id in people and not member_id.startswith("orcid:")]
    if not candidate_ids:
        record = people[member_ids[0]]
        return f"name:{common.normalize_name(record.get('name'))}|orcid_stripped"
    return min(candidate_ids, key=lambda candidate_id: (key_priority(candidate_id),
                                                        -collaboration_count_by_person.get(candidate_id, 0), candidate_id))


def oriented_merge_log(kept_id, member_ids, edges):
    """One log entry per folded-in id, following the merge edges outward from the record nearest the kept id."""
    adjacency = collections.defaultdict(list)
    for first_id, second_id, reason, evidence in edges:
        adjacency[first_id].append((second_id, reason, evidence))
        adjacency[second_id].append((first_id, reason, evidence))
    start_id = kept_id if kept_id in member_ids else min(member_ids, key=lambda member_id: (key_priority(member_id), member_id))
    entries = []
    if start_id != kept_id:
        entries.append({"kept_id": kept_id, "merged_id": start_id, "reason": "rekeyed_to_highest_priority_id",
                        "evidence": f"group key {kept_id}"})
    visited = {start_id}
    queue = collections.deque([start_id])
    while queue:
        current_id = queue.popleft()
        for neighbor_id, reason, evidence in adjacency[current_id]:
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            queue.append(neighbor_id)
            entries.append({"kept_id": kept_id, "merged_id": neighbor_id, "linked_to": current_id,
                            "reason": reason, "evidence": evidence})
    return entries


def merged_other_ids(member_records):
    values_by_key = collections.defaultdict(list)
    for record in member_records:
        for key, value in (record.get("other_ids") or {}).items():
            for single_value in value if isinstance(value, list) else [value]:
                if single_value not in values_by_key[key]:
                    values_by_key[key].append(single_value)
    return {key: values[0] if len(values) == 1 else values for key, values in values_by_key.items()}


def merge_sources(member_records):
    seen = set()
    merged = []
    for record in member_records:
        for source in record.get("sources") or []:
            key = (source["source"], source["source_record_id"])
            if key not in seen:
                seen.add(key)
                merged.append(source)
    return merged


def build_merged_person(kept_id, member_ids, people, union, collaboration_count_by_person):
    member_records = sorted((people[member_id] for member_id in member_ids if member_id in people),
                            key=lambda record: (key_priority(record["id"]), -collaboration_count_by_person.get(record["id"], 0), record["id"]))
    orcids = union.orcids.get(union.find(member_ids[0])) or set()
    openalex_ids = list(dict.fromkeys(value for record in member_records
                                      for key_type, value in intrinsic_keys(record) if key_type == "openalex"))
    primary_openalex_id = kept_id.removeprefix("openalex:") if kept_id.startswith("openalex:") else (openalex_ids[0] if openalex_ids else None)
    other_ids = merged_other_ids(member_records)
    if len(openalex_ids) > 1:
        other_ids["openalex_author_ids"] = openalex_ids
    names = [record.get("name") for record in member_records if record.get("name")]
    # Display the highest-priority record's name, but prefer a spelled-out given name over an initial ("M. Sample").
    full_names = [name for name in names if (parse_person_name(name) or ("",))[0][1:]]
    name = (full_names or names or [None])[0]
    return {
        "id": kept_id,
        "name": name,
        "display_name": name,
        "orcid": next(iter(orcids)) if len(orcids) == 1 else None,
        "openalex_author_id": primary_openalex_id,
        "other_ids": other_ids,
        "merged_ids": sorted(member_id for member_id in member_ids if member_id != kept_id),
        "name_variants": sorted(set(names)),
        "sources": merge_sources(member_records),
        **{field: [entry for record in member_records for entry in record.get(field) or []]
           for field in ("orcid_rejected", "orcid_rejected_openalex_author_ids", "orcid_check_overrides")
           if any(record.get(field) for record in member_records)},
    }


def collaboration_counts(collaborations):
    counts = collections.Counter()
    for collaboration in collaborations.values():
        for person_id in {participant.get("person_id") for participant in collaboration.get("participants") or []}:
            if person_id:
                counts[person_id] += 1
    return counts


def apply_planned_merges(union, profiles, planned):
    """Each planned merge was checked against its own target only; chains (A -> B -> C) can still join
    incompatible names, so re-check the names of the two whole groups as they stand. Returns the merge count."""
    names_by_root = {root_id: set(profile.parsed_names) for root_id, profile in profiles.items()}
    merge_count = 0
    for target_id, source_id, reason, evidence in planned:
        target_root, source_root = union.find(target_id), union.find(source_id)
        if target_root == source_root:
            continue
        if any(names_compatible(first, second) is None
               for first in names_by_root[target_root] for second in names_by_root[source_root]):
            union.refused.append({"ids": [target_id, source_id], "reason": reason, "evidence": evidence,
                                  "refused_because": "merge chain would join incompatible names"})
            continue
        if union.union(target_id, source_id, reason, evidence):
            merge_count += 1
            merged_names = names_by_root.pop(target_root) | names_by_root.pop(source_root)
            names_by_root[union.find(target_id)] = merged_names
    return merge_count


def same_id_source_log(kept_id, member_ids, people):
    """build.py already folded records with an identical id from several sources (an ORCID listed by OpenAlex and
    DataCite); log those hard-key merges too."""
    entries = []
    for member_id in member_ids:
        source_names = list(dict.fromkeys(source["source"] for source in (people.get(member_id) or {}).get("sources") or []))
        for source_name in source_names[1:]:
            entries.append({"kept_id": kept_id, "merged_id": member_id, "merged_source": source_name,
                            "reason": f"hard_key:{member_id.split(':', 1)[0]}",
                            "evidence": f"{member_id} listed by {source_names[0]} and {source_name}"})
    return entries


def deduplicated(entries):
    seen = set()
    unique = []
    for entry in entries:
        key = json.dumps(entry, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def merged_orcid_conflict(orcid, member_ids, people, stripper):
    """Why a merged group must not keep its ORCID, or None: a member record was itself rejected for the ORCID, or a
    member's name or listed name with a spelled-out given name fails the ORCID's name check (a full mismatch or a
    first-initial conflict) and no hard evidence covers a record listing it."""
    for member_id in sorted(member_ids):
        person = people.get(member_id) or {}
        if any(rejection["orcid"] == orcid for rejection in person.get("orcid_rejected") or []):
            return f"orcid_rejected_by_merged_record: {person.get('name')!r} ({member_id}) was rejected for this ORCID"
    registered = stripper.orcid_names.get(orcid)
    if not registered_name_candidates(registered):
        return None
    records_by_name = collections.defaultdict(list)
    for member_id in sorted(member_ids):
        if member_id in people:
            records_by_name[people[member_id].get("name")].append({"kind": "person", "person_id": member_id,
                                                                   "name": people[member_id].get("name")})
        for collaboration, participant in stripper.participants_by_person.get(member_id, []):
            records_by_name[participant.get("name_as_listed")].append(
                {"kind": "participant", "person_id": member_id, "name": participant.get("name_as_listed"),
                 "collaboration": collaboration, "participant": participant})
    for name, records in sorted(records_by_name.items(), key=lambda item: item[0] or ""):
        if not name or is_initials_only(name) or not parse_person_name(name):
            continue
        failure = name_check_failure(name, registered)
        if failure not in ("orcid_initial_conflict", "orcid_name_mismatch") or \
                name_variant_key(name) in stripper.evidence_variants[orcid]:
            continue
        if any(stripper.hard_evidence.for_record(orcid, record, registered, failure, stripper.participants_by_person)
               for record in records):
            continue
        return (f"{failure}_after_merging: merged name {name!r} ({records[0]['person_id']}), ORCID registered name "
                f"{registered_display_name(registered) or registered.get('other_names')!r}")
    return None


def strip_orcids_contradicted_by_merging(union, people, stripper):
    """Re-check each merged group with one ORCID (merged_orcid_conflict); on a conflict the group stays merged but loses
    the ORCID, which is logged as stripped and recorded in orcid_rejected. Returns root id -> stripped-ORCID log
    entries (new_person_id is filled in once the group's kept id is known)."""
    entries_by_root = {}
    for root_id, member_ids in union.groups().items():
        orcids = union.orcids.get(root_id) or set()
        if len(orcids) != 1:
            continue
        orcid = next(iter(orcids))
        reason = merged_orcid_conflict(orcid, member_ids, people, stripper)
        if not reason:
            continue
        union.orcids[root_id] = set()
        holders = [member_id for member_id in sorted(member_ids) if member_id in people and orcid_of_person(people[member_id]) == orcid]
        for member_id in holders or sorted(member_ids)[:1]:
            stripper.add_rejection(people[member_id], orcid, reason, stripper.orcid_names.get(orcid))
        entries_by_root[root_id] = [{
            "orcid": orcid, "person_id": member_id, "new_person_id": None, "name_as_listed": people[member_id].get("name"),
            "reason": reason, "collaboration_count": len(stripper.participants_by_person.get(member_id, [])),
            "example_collaboration_ids": [collaboration["id"] for collaboration, _ in stripper.participants_by_person.get(member_id, [])[:5]]}
            for member_id in holders]
    return entries_by_root


def apply_display_names(merged_people, person_id_rewrite, display_name_entries):
    """person_aliases.json display_name entries, matched by any id merged into the person."""
    for entry in display_name_entries:
        kept_id = person_id_rewrite.get(entry.get("person_id"), entry.get("person_id"))
        if kept_id in merged_people:
            merged_people[kept_id]["display_name"] = entry["display_name"]
        else:
            print(f"warning: person_aliases.json display_name matched nothing: {entry}")


def resolve_people(people, collaborations, evidence_by_person, aliases, orcid_names=None, institution_hierarchy=None,
                   orcid_stripper=None):
    """Returns (people by kept id, person id rewrite map, merge log entries, refused merges, stats).

    Mutates participants' person_id in place where an ORCID is stripped (step 0), before the rewrite map applies.
    orcid_stripper: the result of an earlier check_orcid_plausibility call (build.py runs it before award assignment);
    when None the check runs here.
    stats["stripped_orcids"] lists every stripped ORCID; the same entries are appended to the refused merges."""
    people_before = len(people)
    hierarchy = institution_hierarchy or build_awards.InstitutionHierarchy({})
    if orcid_stripper is None:
        orcid_names = load_orcid_names() if orcid_names is None else orcid_names
        orcid_stripper = check_orcid_plausibility(people, collaborations, evidence_by_person, orcid_names, hierarchy,
                                                  aliases.get("strip_orcid") or [], aliases.get("confirm_orcid") or [])
    stripper = orcid_stripper
    stripped_orcids = list(stripper.stripped.values())

    split_ids = {entry["id"] for entry in aliases["split"]}
    union = PersonUnion(people, split_ids)
    for merge_entry in aliases["merge"]:
        merge_group = merge_entry["person_ids"]
        for person_id in merge_group[1:]:
            if merge_group[0] in people and person_id in people:
                union.union(merge_group[0], person_id, "manual",
                            f"data/manual/person_aliases.json: {merge_entry.get('reason')}", force=True)
            else:
                print(f"warning: person_aliases.json merge refers to unknown id(s): {merge_group}")
    merge_by_hard_keys(union, people, evidence_by_person, stripper.rejected_evidence_links)
    evidence_keyed_ids = {person_id for person_id, entry in evidence_by_person.items()
                          if any(key[0] != "orcid" or (person_id, key[1]) not in stripper.rejected_evidence_links
                                 for key in evidence_keys(entry))}
    name_merge_passes = 0
    for name_merge_passes in range(1, MAX_NAME_MERGE_PASSES + 1):
        profiles = build_group_profiles(union, people, collaborations, evidence_by_person, evidence_keyed_ids,
                                        stripper.rejected_evidence_links)
        planned, skipped, refusals = name_based_merge_plan(profiles, hierarchy, stripper.orcid_names)
        nsf_planned, nsf_refusals = nsf_name_only_merge_plan(profiles, union, collaborations, hierarchy)
        if not apply_planned_merges(union, profiles, planned + nsf_planned):
            break
    merged_orcid_strips = strip_orcids_contradicted_by_merging(union, people, stripper)

    collaboration_count_by_person = collaboration_counts(collaborations)
    edges_by_root = collections.defaultdict(list)
    for edge in union.edges:
        edges_by_root[union.find(edge[0])].append(edge)
    merged_people = {}
    person_id_rewrite = {}
    merge_log = []
    for root_id, member_ids in union.groups().items():
        kept_id = canonical_id_for_group(member_ids, union, people, collaboration_count_by_person)
        merged_people[kept_id] = build_merged_person(kept_id, member_ids, people, union, collaboration_count_by_person)
        for member_id in member_ids:
            if member_id != kept_id:
                person_id_rewrite[member_id] = kept_id
        if len(member_ids) > 1 or kept_id not in member_ids:
            merge_log.extend(oriented_merge_log(kept_id, member_ids, edges_by_root[root_id]))
        merge_log.extend(same_id_source_log(kept_id, member_ids, people))
        for entry in merged_orcid_strips.get(root_id, []):
            entry["new_person_id"] = kept_id
    apply_display_names(merged_people, person_id_rewrite, aliases.get("display_name") or [])
    stripped_orcids += [entry for entries in merged_orcid_strips.values() for entry in entries]
    refused = deduplicated(union.refused) + list(refusals.values()) + list(nsf_refusals.values()) + [
        {"ids": [entry["person_id"], entry["new_person_id"]], "reason": "hard_key:orcid", "orcid": entry["orcid"],
         "refused_because": f"stripped_orcid: {entry['reason']}", "name_as_listed": entry["name_as_listed"],
         "collaboration_count": entry["collaboration_count"]} for entry in stripped_orcids]
    stats = {"people_before": people_before, "people_after": len(merged_people),
             "merges_by_reason": dict(collections.Counter(entry["reason"] for entry in merge_log)),
             "name_based_skipped": dict(skipped), "refused_merges": len(refused),
             "name_merge_passes": name_merge_passes, "stripped_orcids": stripped_orcids,
             "stripped_orcid_count": len({entry["orcid"] for entry in stripped_orcids}),
             "fully_stripped_orcid_count": len(stripper.fully_stripped_orcids),
             "orcid_check_overrides": list(stripper.overrides.values())}
    override_counts = collections.Counter(entry["reason"].split(":")[0] for entry in stripper.overrides.values())
    print(f"\nperson ORCID plausibility: {stats['stripped_orcid_count']} ORCIDs stripped from some records "
          f"({stats['fully_stripped_orcid_count']} from all), kept despite the checks: {dict(override_counts)}, "
          f"{sum(entry['collaboration_count'] for entry in stripped_orcids)} participations moved; "
          f"name-based merging ran {name_merge_passes} passes")
    return merged_people, person_id_rewrite, merge_log, refused, stats
