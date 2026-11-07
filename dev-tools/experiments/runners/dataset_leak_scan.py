r"""Scan files for any trace of the Tow Center's protected dataset (#737).

The Tow Center distributes ``GenAISearch_Data.csv`` encrypted so that
its 200 article excerpts stay out of AI crawlers. The On the Record
replication depends on that: an engine must *search* for the source,
and a future model must not have memorized the answer key. So no
excerpt and no ground-truth URL may ever appear in this repository, in
a test fixture, a document, a web page, or an encoded payload inside
one. (The dataset carries no headline column; its first column is a
publisher-and-engine label, so there is nothing to guard there.)

This tool reads the local, decrypted dataset (which itself must never
be committed) and scans files, or a whole git tree, for:

- any run of ``WINDOW`` consecutive words from any excerpt, compared as
  words with punctuation and quote characters removed (the same
  tokenizer the verifier uses), so a fragment inside a quoted string
  literal, split across literals or lines, is still caught;
- the same runs inside encoded payloads: base64, character-code
  arrays, ``\\xNN`` and ``\\uNNNN`` string escapes, percent-encoding,
  and HTML entities, so text "drawn, not typed" into a page does not
  hide from an agent that reads the source;
- any ground-truth URL path, matched against a view of the file with
  quotes and whitespace removed, so a URL split across string literals
  is still caught.

The report names files, counts and line numbers only. It never prints
the matched text, because the report itself may end up in a log or a
pull request.

Usage::

    python dev-tools/experiments/runners/dataset_leak_scan.py [paths...]
    python dev-tools/experiments/runners/dataset_leak_scan.py --ref origin/develop

Exit codes: 0 clean (or skipped because no dataset is present), 1 a
leak was found, 2 the scan could not run as asked (dataset required
but absent, a path that does not exist, or a file that could not be
read).
"""

from __future__ import annotations

import argparse
import base64
import codecs
import csv
import os
import re
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
# The tokenizer this scan shares with the verifier lives in the citation
# extension (ADR-0013 K11), beside the kernel rather than inside it.
EXTENSION_DIR = REPO_ROOT / "extensions" / "citation_verifier"
for entry in (str(REPO_ROOT), str(EXTENSION_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from citation_verifier.checks import words  # noqa: E402

WINDOW = 6
DEFAULT_DATASET = "~/marcus-experiments/tow/dataset/GenAISearch_Data.csv"
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".mp4",
    ".mov",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".pdf",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".zip",
    ".gz",
    ".pyc",
    ".so",
    ".dylib",
    ".whl",
    ".jar",
    ".pkl",
    ".pickle",
}
_B64 = re.compile(r"[A-Za-z0-9+/=]{40,}")
_CHARCODES = re.compile(r"(?:\d{2,3}\s*,\s*){8,}\d{2,3}")
_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}){6,}")
# Any URL-safe run holding at least one escape: standard encoders leave
# letters alone and encode only spaces and punctuation, so a fully
# escaped run is the rare form, not the common one.
_PERCENT = re.compile(r"[A-Za-z0-9._~+%-]*%[0-9A-Fa-f]{2}[A-Za-z0-9._~+%-]*")


def word_windows(text: str, size: int = WINDOW) -> Iterator[str]:
    """Yield every run of ``size`` consecutive words.

    Parameters
    ----------
    text : str
        Raw text; tokenized with the verifier's ``words``.
    size : int
        Window length in words.

    Yields
    ------
    str
        Space-joined word runs.
    """
    toks = words(text)
    for i in range(len(toks) - size + 1):
        yield " ".join(toks[i : i + size])


@dataclass(frozen=True)
class DatasetFacts:
    """What the scanner looks for, derived from the dataset.

    Parameters
    ----------
    excerpt_windows : Set[str]
        Every ``WINDOW``-word run from every excerpt.
    url_paths : Set[str]
        Ground-truth URL paths, lower-cased, without surrounding slashes.
    """

    excerpt_windows: Set[str]
    url_paths: Set[str]


def load_dataset(csv_path: str) -> DatasetFacts:
    """Read the dataset CSV into the facts the scanner matches against.

    Parameters
    ----------
    csv_path : str
        Path to the decrypted ``GenAISearch_Data.csv``.

    Returns
    -------
    DatasetFacts
        Excerpt windows and URL paths.
    """
    windows: Set[str] = set()
    paths: Set[str] = set()
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            prompt = (row.get("Prompt") or "").strip()
            if prompt:
                windows.update(word_windows(prompt))
            url = (row.get("Source URL") or "").strip()
            if url:
                path = urlsplit(url).path.strip("/").lower()
                if len(path) >= 12:
                    paths.add(path)
    return DatasetFacts(windows, paths)


@dataclass
class Hits:
    """What one text contained, as counts and line numbers.

    Parameters
    ----------
    excerpt_windows : int
        Excerpt word-runs found in plain text.
    payload_windows : int
        Excerpt word-runs found inside decoded payloads.
    url_paths : int
        Ground-truth URL paths found.
    lines : Set[int]
        1-based line numbers where a hit starts (never the text).
    """

    excerpt_windows: int = 0
    payload_windows: int = 0
    url_paths: int = 0
    lines: Set[int] = field(default_factory=set)

    @property
    def total(self) -> int:
        """Sum of all counts."""
        return self.excerpt_windows + self.payload_windows + self.url_paths


def _decode_base64(run: str) -> Optional[str]:
    """Decode one base64-looking run, or None when it does not decode."""
    try:
        return base64.b64decode(run + "==", validate=False).decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return None


def _decode_escapes(run: str) -> Optional[str]:
    r"""Decode a run of ``\xNN`` / ``\uNNNN`` escapes, or None."""
    try:
        return codecs.decode(run, "unicode_escape")
    except (ValueError, UnicodeDecodeError):
        return None


def decoded_payloads(text: str) -> Iterator[Tuple[int, str]]:
    """Yield decoded payloads found in text with the offset they start at.

    Parameters
    ----------
    text : str
        Raw text, typically page or program source.

    Yields
    ------
    Tuple[int, str]
        ``(offset, decoded_text)`` for each base64 run, character-code
        array, escape sequence run, and percent-encoded run.
    """
    for match in _B64.finditer(text):
        decoded = _decode_base64(match.group(0))
        if decoded is not None:
            yield match.start(), decoded
    for match in _CHARCODES.finditer(text):
        codes = [int(x) for x in re.findall(r"\d+", match.group(0))]
        yield match.start(), "".join(chr(c) for c in codes if 31 < c < 127)
    for match in _ESCAPES.finditer(text):
        decoded = _decode_escapes(match.group(0))
        if decoded is not None:
            yield match.start(), decoded
    for match in _PERCENT.finditer(text):
        run = match.group(0)
        if len(run) >= 24:
            yield match.start(), unquote(run.replace("+", " "))


def _line_of(offsets: List[int], position: int) -> int:
    """Map a character offset to a 1-based line number."""
    return bisect_right(offsets, position) + 1


def scan_text(text: str, facts: DatasetFacts) -> Hits:
    """Count dataset traces in one text and note the lines they start on.

    Parameters
    ----------
    text : str
        The file's contents.
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    Hits
        Per-category counts and line numbers; ``total == 0`` means clean.
    """
    hits = Hits()
    lines = text.split("\n")
    offsets = [i for i, ch in enumerate(text) if ch == "\n"]

    # Excerpt runs: slide over the words of each line, carrying the last
    # WINDOW-1 words seen so far (not just the previous line's) so a run
    # split across several short or blank lines still forms.
    carry: List[str] = []
    for number, line in enumerate(lines, 1):
        toks = carry + words(line)
        for i in range(len(toks) - WINDOW + 1):
            if " ".join(toks[i : i + WINDOW]) in facts.excerpt_windows:
                hits.excerpt_windows += 1
                hits.lines.add(number)
        carry = toks[-(WINDOW - 1) :]

    for offset, payload in decoded_payloads(text):
        found = sum(1 for w in word_windows(payload) if w in facts.excerpt_windows)
        if found:
            hits.payload_windows += found
            hits.lines.add(_line_of(offsets, offset))

    collapsed_lines = [re.sub(r"[\s\"'+]", "", line).lower() for line in lines]
    collapsed_all = "".join(collapsed_lines)
    for path in facts.url_paths:
        if path in collapsed_all:
            hits.url_paths += 1
            for number, collapsed in enumerate(collapsed_lines, 1):
                if path in collapsed:
                    hits.lines.add(number)
    return hits


def has_binary_suffix(path: Path) -> bool:
    """Decide from the name alone whether a file is binary, before reading it.

    Parameters
    ----------
    path : Path
        The file's path.

    Returns
    -------
    bool
        True when the suffix is a known binary type.
    """
    return path.suffix.lower() in BINARY_SUFFIXES


def looks_binary(head: bytes) -> bool:
    """Decide from the first bytes whether content is binary.

    Parameters
    ----------
    head : bytes
        The first bytes of the file.

    Returns
    -------
    bool
        True when a NUL byte appears in the first 4 KiB.
    """
    return b"\x00" in head[:4096]


def scan_file(path: Path, facts: DatasetFacts) -> Optional[Hits]:
    """Scan one file on disk.

    Parameters
    ----------
    path : Path
        File to scan; must be readable (the caller handles errors).
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    Optional[Hits]
        Counts, or None when the file is binary and was skipped.

    Raises
    ------
    OSError
        When the file cannot be read.
    """
    if has_binary_suffix(path):
        return None
    data = path.read_bytes()
    if looks_binary(data):
        return None
    return scan_text(data.decode("utf-8", "ignore"), facts)


def _report(label: str, hits: Hits) -> None:
    """Print one finding line: file name, counts and line numbers only."""
    shown = sorted(hits.lines)[:12]
    more = "" if len(hits.lines) <= 12 else f" (+{len(hits.lines) - 12} more)"
    print(
        f"LEAK {label}: excerpt-windows={hits.excerpt_windows} "
        f"decoded-payload-windows={hits.payload_windows} "
        f"ground-truth-url-paths={hits.url_paths} lines={shown}{more}"
    )


def scan_paths(paths: Iterable[Path], facts: DatasetFacts) -> Tuple[int, int]:
    """Scan files on disk and report leaks and errors.

    Parameters
    ----------
    paths : Iterable[Path]
        Files or directories; directories are walked. A path that does
        not exist is an error, never an empty directory.
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    Tuple[int, int]
        ``(leaking_files, errors)`` where errors counts missing paths and
        unreadable files.
    """
    leaking = errors = 0
    for root in paths:
        if not root.exists():
            print(f"ERROR path not found: {root}")
            errors += 1
            continue
        files = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file())
        )
        for f in files:
            if ".git" in f.parts:
                continue
            try:
                hits = scan_file(f, facts)
            except OSError as error:
                print(f"ERROR unreadable: {f} ({error.strerror or error})")
                errors += 1
                continue
            if hits and hits.total:
                _report(str(f), hits)
                leaking += 1
    return leaking, errors


def _git(args: List[str], repo: Optional[Path]) -> bytes:
    """Run a git command, optionally inside ``repo``, and return stdout."""
    base = ["git"] + (["-C", str(repo)] if repo else [])
    return subprocess.run(base + args, capture_output=True, check=True).stdout


def scan_ref(ref: str, facts: DatasetFacts, repo: Optional[Path] = None) -> int:
    """Scan every blob in a git tree and report leaks.

    Parameters
    ----------
    ref : str
        Git ref (branch, tag, or commit).
    facts : DatasetFacts
        What to look for.
    repo : Optional[Path]
        Repository to read; defaults to the current directory's.

    Returns
    -------
    int
        Number of files with at least one hit.
    """
    listing = _git(["ls-tree", "-r", "-z", "--name-only", ref], repo)
    leaking = 0
    for raw in listing.split(b"\x00"):
        if not raw:
            continue
        path = raw.decode("utf-8", "surrogateescape")
        if has_binary_suffix(Path(path)):
            continue
        blob = _git(["show", f"{ref}:{path}"], repo)
        if looks_binary(blob):
            continue
        hits = scan_text(blob.decode("utf-8", "ignore"), facts)
        if hits.total:
            _report(f"{ref}:{path}", hits)
            leaking += 1
    return leaking


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Parameters
    ----------
    argv : Optional[Sequence[str]]
        Arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        0 when clean or skipped, 1 when a leak was found, 2 when the
        scan could not run as asked.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dataset",
        default=os.environ.get("TOW_DATASET", DEFAULT_DATASET),
        help="path to the local decrypted GenAISearch_Data.csv",
    )
    parser.add_argument("--ref", help="scan a git tree instead of paths on disk")
    parser.add_argument(
        "--require-dataset",
        action="store_true",
        help="fail (exit 2) instead of skipping when the dataset is absent",
    )
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    args = parser.parse_args(argv)

    dataset = Path(args.dataset).expanduser()
    if not dataset.is_file():
        print(f"dataset not found at {dataset}; skipping the leak scan")
        return 2 if args.require_dataset else 0
    facts = load_dataset(str(dataset))

    errors = 0
    if args.ref:
        leaking = scan_ref(args.ref, facts)
    else:
        targets = [Path(p) for p in args.paths] or [Path(".")]
        leaking, errors = scan_paths(targets, facts)
    if leaking:
        print(
            f"{leaking} file(s) contain Tow dataset text; remove it before committing"
        )
        return 1
    if errors:
        print(f"{errors} path(s) could not be scanned; fix them and rerun")
        return 2
    print("clean: no Tow dataset text found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
