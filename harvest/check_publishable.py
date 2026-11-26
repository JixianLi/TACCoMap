"""Refuse to publish files that must stay local: raw caches, review artifacts, contact details, QA notes.

Modes:
  check_publishable.py                 tracked + untracked-not-ignored files, plus everything under web/
  check_publishable.py PATH ...        the given files or directories
  check_publishable.py --staged        the index contents of staged files (used by the pre-commit hook)
  check_publishable.py --site DIR      a staged GitHub Pages directory, which must match SITE_ALLOWLIST
  check_publishable.py --map-site DIR  a staged map Pages directory (map/ build), which must match MAP_SITE_ALLOWLIST

Exit status 1 if anything fails. Matches are reported by location and count only, never by value,
so the output itself is safe to paste into an issue or a chat.
"""

import argparse
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys

import build

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

FORBIDDEN_DIRECTORY_PREFIXES = ("data/raw/", "data/evidence/", "data/normalized/", "data/review/")
# .gitignore reserves publish/ for staging; the others are common Pages staging names.
PUBLISH_STAGING_DIRECTORY_NAMES = ("publish", "_site", "site_staging", "publish_staging", "gh-pages", "gh_pages")
MERGE_LOG_FILE_PATTERN = re.compile(r"(?:^|/)person_merge_log[^/]*\.json$")
# GitHub warns at 50 MiB and Pages cannot serve LFS files, so keep a margin below the warning.
MAX_FILE_BYTES = 45_000_000

SITE_ALLOWLIST = {
    "index.html", "style.css", "app.js", "data.js",
    "data/meta.json", "data/papers.json", "data/awards.json", "data/datasets.json",
    "data/allocations.json", "data/funding_opportunities.json", "data/people.json",
    "data/institutions.json",
}
# The map's Vite build: fixed files plus content-hashed bundles, whose names change on every build.
MAP_SITE_ALLOWLIST = {"index.html", ".nojekyll", "data/map_institutions.json"}
MAP_SITE_ASSET_PATTERN = re.compile(r"assets/[\w.-]+\.(?:js|css|woff2)")
# Minified library code is full of decimal constants (shader polynomials, color matrices, PRNG seeds) that read as
# phone numbers; every other check still runs on the bundles, and all checks run on the map's data and index.html.
MAP_SITE_ASSET_SKIPPED_CHECKS = ("phone number",)
# Git's file mode for a submodule entry: a commit id in another repository, not file content.
GITLINK_MODE = "160000"

EMAIL_PATTERN = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
OBFUSCATED_AT = r"\s*[\[({<]\s*at\s*[\])}>]\s*"
OBFUSCATED_DOT = r"(?:\.|\s*[\[({<]\s*dot\s*[\])}>]\s*|\s+dot\s+)"
OBFUSCATED_EMAIL_PATTERNS = (
    re.compile(rf"(?<![\w.+-])[\w.+-]+{OBFUSCATED_AT}[\w-]+(?:{OBFUSCATED_DOT}[\w-]+)+", re.IGNORECASE),
    re.compile(r"(?<![\w.+-])[\w.+-]+\s+at\s+[\w-]+(?:\s+dot\s+[\w-]+)*\s+dot\s+(?:edu|com|org|net|gov|mil|io|[a-z]{2})\b",
               re.IGNORECASE),
)
MAILTO_PATTERN = re.compile(r"mailto\s*:", re.IGNORECASE)
PHONE_PATTERNS = (
    re.compile(r"(?<![./])\b(?:tel(?:ephone)?|phone|fax|mobile)\b\.?\s*(?:no\.?|number)?\s*[:.]?\s*[+(]?\d[\d\s().\-/]{5,}\d",
               re.IGNORECASE),
    re.compile(r"\+\s?\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}\b"),
    re.compile(r"(?:\(\d{3}\)\s?|\b\d{3}[.-])\d{3}[.-]\d{4}\b"),
)
# Personal profiles only. GitHub is left out on purpose: dataset descriptions legitimately link
# software repositories, and the audit judged those handles acceptable.
PERSONAL_SOCIAL_URL_PATTERN = re.compile(
    r"(?<![\w.-])(?:www\.|mobile\.)?(?:twitter\.com|x\.com|facebook\.com|fb\.com|instagram\.com|tiktok\.com"
    r"|threads\.net|linkedin\.com/in|bsky\.app/profile|researchgate\.net/profile|scholar\.google\.[a-z.]+/citations)/",
    re.IGNORECASE)
QA_FIELD_NAMES = (
    "orcid_rejected", "orcid_check_overrides", "orcid_rejected_openalex_author_ids", "current_email_domain_is_tacc",
)
QA_FIELD_KEY_PATTERN = re.compile(r'"(?:' + "|".join(QA_FIELD_NAMES) + r')"\s*:')
PERSON_NOTE_WORD_PATTERN = re.compile(r"\bformerly\b|\bcurrent name\b|\bspeaker page\b|\bregistered to\b",
                                      re.IGNORECASE)
# Only person-level files: institution notes and source text legitimately say "formerly"
# (renamed institutes, program names), and those are not about people.
PERSON_NOTE_FILE_GLOBS = ("data/manual/person*.json", "web/data/people.json", "data/people.json")
LOCAL_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w.~/-])/(?:Users|home|private)/[\w.-]+")

CONTENT_CHECKS = (
    ("email", (EMAIL_PATTERN,), lambda relative_path: True),
    ("obfuscated email", OBFUSCATED_EMAIL_PATTERNS, lambda relative_path: True),
    ("mailto link", (MAILTO_PATTERN,), lambda relative_path: True),
    ("phone number", PHONE_PATTERNS, lambda relative_path: True),
    ("personal social URL", (PERSONAL_SOCIAL_URL_PATTERN,), lambda relative_path: True),
    ("QA field name", (QA_FIELD_KEY_PATTERN,), lambda relative_path: relative_path.endswith(".json")),
    ("person note wording", (PERSON_NOTE_WORD_PATTERN,),
     lambda relative_path: any(fnmatch.fnmatch(relative_path, glob) for glob in PERSON_NOTE_FILE_GLOBS)),
    ("local absolute path", (LOCAL_ABSOLUTE_PATH_PATTERN,), lambda relative_path: True),
)


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="files or directories (default: repo files plus web/)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check the git index contents of staged files")
    mode.add_argument("--site", metavar="DIR", help="check a staged Pages directory against the allowlist")
    mode.add_argument("--map-site", metavar="DIR", help="check a staged map Pages directory against the map allowlist")
    return parser.parse_args()


def run_git(*arguments):
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *arguments], check=True, capture_output=True).stdout


def files_under(directory):
    return sorted(path for path in directory.rglob("*") if path.is_file() and ".git" not in path.parts)


def relative_to_root(path, root):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def worktree_candidates(paths):
    """Yield (relative_path, read_bytes) pairs for files on disk."""
    for path in paths:
        yield relative_to_root(path, PROJECT_ROOT), path.read_bytes


def default_paths():
    listed = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    paths = {PROJECT_ROOT / name for name in listed if name}
    paths.update(files_under(PROJECT_ROOT / "web"))
    return sorted(path for path in paths if path.is_file())


def explicit_paths(arguments):
    paths = []
    for argument in arguments:
        path = pathlib.Path(argument)
        paths.extend(files_under(path) if path.is_dir() else [path])
    return paths


def staged_gitlink_paths():
    entries = run_git("ls-files", "--stage", "-z").decode().split("\0")
    return {entry.split("\t", 1)[1] for entry in entries if entry.startswith(GITLINK_MODE + " ")}


def staged_candidates():
    """(candidates, skipped submodule paths). A staged submodule is only a commit id with no blob to read; its
    contents belong to the other repository and are not published from this one."""
    names = [name for name in run_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0") if name]
    gitlinks = staged_gitlink_paths()
    candidates = [(name, lambda name=name: run_git("cat-file", "blob", f":{name}")) for name in names if name not in gitlinks]
    return candidates, sorted(set(names) & gitlinks)


def path_failures(relative_path, size_bytes):
    failures = []
    if relative_path.startswith(FORBIDDEN_DIRECTORY_PREFIXES):
        failures.append("local-only data directory")
    if any(part in PUBLISH_STAGING_DIRECTORY_NAMES for part in relative_path.split("/")[:-1]):
        failures.append("publish staging leftover")
    if MERGE_LOG_FILE_PATTERN.search(relative_path):
        failures.append("person merge log")
    if size_bytes > MAX_FILE_BYTES:
        failures.append(f"file larger than {MAX_FILE_BYTES / 1e6:.0f} MB ({size_bytes / 1e6:.1f} MB)")
    return failures


def line_and_column(text, offset):
    line_number = text.count("\n", 0, offset) + 1
    return line_number, offset - (text.rfind("\n", 0, offset) + 1) + 1


def count_distinct_spans(sorted_spans):
    """Several patterns can match one phone number; count overlapping spans once."""
    count, previous_end = 0, -1
    for start, end in sorted_spans:
        if start >= previous_end:
            count += 1
        previous_end = max(previous_end, end)
    return count


def content_failures(relative_path, text, api_key, skipped_checks=()):
    failures = []
    for check_name, patterns, applies_to in CONTENT_CHECKS:
        if check_name in skipped_checks or not applies_to(relative_path):
            continue
        match_spans = sorted(match.span() for pattern in patterns for match in pattern.finditer(text))
        if match_spans:
            line_number, column = line_and_column(text, match_spans[0][0])
            failures.append(f"{check_name}: {count_distinct_spans(match_spans)} match(es), "
                            f"first at line {line_number} col {column}")
    if api_key and api_key in text:
        failures.append(f"OPENALEX_API_KEY value: {text.count(api_key)} occurrence(s)")
    return failures


def published_record_failures(relative_path, text):
    """Structural gates on the published collaboration files: a participant listed at a secondary school (a potential
    minor) carries only a name and that institution, and descriptions carry no coordinate finer than two decimals."""
    if not fnmatch.fnmatch(relative_path, "*data/*.json") or relative_path.endswith(("people.json", "institutions.json",
                                                                                    "meta.json")):
        return []
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(records, list):
        return []
    allowed_minor_fields = set(build.POTENTIAL_MINOR_PARTICIPANT_FIELDS) | {"person_id"}
    minor_rows = precise_coordinates = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        for participant in record.get("participants") or []:
            if build.SECONDARY_SCHOOL_PATTERN.search(participant.get("institution_name_as_listed") or "") and (
                    participant.get("person_id") or set(participant) - allowed_minor_fields):
                minor_rows += 1
        precise_coordinates += sum(1 for field in build.COORDINATE_TEXT_FIELDS
                                   if record.get(field) and build.round_coordinates(record[field]) != record[field])
    failures = []
    if minor_rows:
        failures.append(f"potential-minor participant with more than name and institution: {minor_rows} row(s)")
    if precise_coordinates:
        failures.append(f"coordinates finer than two decimals in title/description: {precise_coordinates} record(s)")
    return failures


def site_allowlist_failures(relative_paths, allowlist=SITE_ALLOWLIST, allowed_pattern=None):
    unlisted = {path for path in relative_paths
                if path not in allowlist and not (allowed_pattern and allowed_pattern.fullmatch(path))}
    failures = [f"{path}: not in site allowlist" for path in sorted(unlisted)]
    failures += [f"{path}: missing from staged site" for path in sorted(allowlist - set(relative_paths))]
    return failures


def main():
    arguments = parse_arguments()
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    failures = []
    skipped_submodules = []
    if arguments.site or arguments.map_site:
        site_directory = pathlib.Path(arguments.site or arguments.map_site)
        if not site_directory.is_dir():
            sys.exit(f"not a directory: {site_directory}")
        site_files = files_under(site_directory)
        candidates = [(path.relative_to(site_directory).as_posix(), path.read_bytes) for path in site_files]
        relative_paths = [relative_path for relative_path, _ in candidates]
        failures += (site_allowlist_failures(relative_paths, MAP_SITE_ALLOWLIST, MAP_SITE_ASSET_PATTERN)
                     if arguments.map_site else site_allowlist_failures(relative_paths))
    elif arguments.staged:
        candidates, skipped_submodules = staged_candidates()
    else:
        candidates = list(worktree_candidates(explicit_paths(arguments.paths) if arguments.paths else default_paths()))

    for relative_path, read_bytes in candidates:
        content = read_bytes()
        text = content.decode("utf-8", errors="replace")
        skipped_checks = (MAP_SITE_ASSET_SKIPPED_CHECKS
                          if arguments.map_site and MAP_SITE_ASSET_PATTERN.fullmatch(relative_path) else ())
        for failure in path_failures(relative_path, len(content)) + content_failures(
                relative_path, text, api_key, skipped_checks) + published_record_failures(
                relative_path, text):
            failures.append(f"{relative_path}: {failure}")

    for failure in failures:
        print(f"FAIL {failure}")
    for path in skipped_submodules:
        print(f"skipped submodule pointer {path} (its files are another repository's)")
    print(f"checked {len(candidates)} file(s); {len(failures)} failure(s)"
          f"{'' if api_key else '; OPENALEX_API_KEY not set, key check skipped'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
