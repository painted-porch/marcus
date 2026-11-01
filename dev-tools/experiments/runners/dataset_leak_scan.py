"""Scan files for any trace of the Tow Center's protected dataset (#737).

The Tow Center distributes ``GenAISearch_Data.csv`` encrypted so that
its 200 article excerpts stay out of AI crawlers. The On the Record
replication depends on that: an engine must *search* for the source,
and a future model must not have memorized the answer key. So no
excerpt, no ground-truth URL, and no dataset headline may ever appear
in this repository, in a test fixture, a document, a web page, or an
encoded payload inside one.

This tool reads the local, decrypted dataset (which itself must never
be committed) and scans files for:

- any run of ``WINDOW`` consecutive words from any excerpt, after
  normalizing case, quotes, and whitespace, so fragments are caught;
- the same runs inside base64 or character-code payloads, so a
  "drawn, not typed" trick does not hide the text from an agent that
  reads the source;
- any ground-truth URL path, matched against a collapsed view of the
  file with quotes and whitespace removed, so a URL split across
  string literals is still caught;
- any dataset headline.

The report names files and counts only. It never prints the matched
text, because the report itself may end up in a log or a pull request.

Usage::

    python dev-tools/experiments/runners/dataset_leak_scan.py [paths...]
    python dev-tools/experiments/runners/dataset_leak_scan.py --ref origin/develop

With no dataset present the scan cannot run, so it passes with a
notice; pass ``--require-dataset`` to make that a failure instead.
"""

from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set
from urllib.parse import urlsplit

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
    ".zip",
    ".gz",
    ".pyc",
    ".so",
    ".dylib",
}
_B64 = re.compile(r"[A-Za-z0-9+/=]{40,}")
_CHARCODES = re.compile(r"(?:\d{2,3}\s*,\s*){8,}\d{2,3}")


def normalize(text: str) -> str:
    """Lower-case text and collapse quotes, punctuation, and whitespace.

    Parameters
    ----------
    text : str
        Raw text.

    Returns
    -------
    str
        Normalized text with single spaces between words.
    """
    text = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_windows(text: str, size: int = WINDOW) -> Iterator[str]:
    """Yield every run of ``size`` consecutive normalized words.

    Parameters
    ----------
    text : str
        Raw text; normalized here.
    size : int
        Window length in words.

    Yields
    ------
    str
        Space-joined word runs.
    """
    words = normalize(text).split()
    if len(words) < size:
        return
    for i in range(len(words) - size + 1):
        yield " ".join(words[i : i + size])


@dataclass(frozen=True)
class DatasetFacts:
    """What the scanner looks for, derived from the dataset.

    Parameters
    ----------
    excerpt_windows : Set[str]
        Every ``WINDOW``-word run from every excerpt, normalized.
    url_paths : Set[str]
        Ground-truth URL paths, lower-cased, without surrounding slashes.
    headlines : Set[str]
        Dataset headlines, normalized and with spaces removed.
    """

    excerpt_windows: Set[str]
    url_paths: Set[str]
    headlines: Set[str]


def load_dataset(csv_path: str) -> DatasetFacts:
    """Read the dataset CSV into the facts the scanner matches against.

    Parameters
    ----------
    csv_path : str
        Path to the decrypted ``GenAISearch_Data.csv``.

    Returns
    -------
    DatasetFacts
        Normalized excerpt windows, URL paths, and headlines.
    """
    windows: Set[str] = set()
    paths: Set[str] = set()
    heads: Set[str] = set()
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
            head = normalize(row.get("Column 19") or "").replace(" ", "")
            if len(head) >= 20:
                heads.add(head)
    return DatasetFacts(windows, paths, heads)


@dataclass
class Hits:
    """Counts of what one text contained.

    Parameters
    ----------
    excerpt_windows : int
        Excerpt word-runs found in plain text.
    payload_windows : int
        Excerpt word-runs found inside decoded payloads.
    url_paths : int
        Ground-truth URL paths found.
    headlines : int
        Dataset headlines found.
    """

    excerpt_windows: int = 0
    payload_windows: int = 0
    url_paths: int = 0
    headlines: int = 0

    @property
    def total(self) -> int:
        """Sum of all counts."""
        return (
            self.excerpt_windows
            + self.payload_windows
            + self.url_paths
            + self.headlines
        )


def _decode_base64(run: str) -> Optional[str]:
    """Decode one base64-looking run, or return None when it is not base64.

    Parameters
    ----------
    run : str
        A run of base64 alphabet characters.

    Returns
    -------
    Optional[str]
        Decoded text, or None when the run does not decode.
    """
    try:
        return base64.b64decode(run + "==", validate=False).decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return None


def decoded_payloads(text: str) -> Iterator[str]:
    """Yield decoded base64 strings and character-code arrays found in text.

    Parameters
    ----------
    text : str
        Raw text, typically page source.

    Yields
    ------
    str
        Each decoded payload as text (undecodable bytes dropped).
    """
    for match in _B64.finditer(text):
        decoded = _decode_base64(match.group(0))
        if decoded is not None:
            yield decoded
    for match in _CHARCODES.finditer(text):
        codes = [int(x) for x in re.findall(r"\d+", match.group(0))]
        yield "".join(chr(c) for c in codes if 31 < c < 127)


def scan_text(text: str, facts: DatasetFacts) -> Hits:
    """Count dataset traces in one text.

    Parameters
    ----------
    text : str
        The file's contents.
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    Hits
        Per-category counts; ``total == 0`` means clean.
    """
    hits = Hits()
    hits.excerpt_windows = sum(
        1 for w in word_windows(text) if w in facts.excerpt_windows
    )
    hits.payload_windows = sum(
        1
        for payload in decoded_payloads(text)
        for w in word_windows(payload)
        if w in facts.excerpt_windows
    )
    collapsed = re.sub(r"[\s\"'+]", "", text).lower()
    hits.url_paths = sum(1 for p in facts.url_paths if p in collapsed)
    squashed = re.sub(r"[^a-z0-9]", "", normalize(text))
    hits.headlines = sum(1 for h in facts.headlines if h in squashed)
    return hits


def scan_file(path: Path, facts: DatasetFacts) -> Optional[Hits]:
    """Scan one file on disk.

    Parameters
    ----------
    path : Path
        File to scan.
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    Optional[Hits]
        Counts, or None when the file is binary and was skipped.
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return scan_text(data.decode("utf-8", "ignore"), facts)


def _git_ref_files(ref: str) -> List[str]:
    """List the paths in a git tree."""
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def _git_show(ref: str, path: str) -> bytes:
    """Read one blob from a git tree."""
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, check=True
    ).stdout


def _report(label: str, hits: Hits) -> None:
    """Print one finding line: file name and counts only."""
    print(
        f"LEAK {label}: excerpt-windows={hits.excerpt_windows} "
        f"decoded-payload-windows={hits.payload_windows} "
        f"ground-truth-url-paths={hits.url_paths} headlines={hits.headlines}"
    )


def scan_paths(paths: Iterable[Path], facts: DatasetFacts) -> int:
    """Scan files on disk and report leaks.

    Parameters
    ----------
    paths : Iterable[Path]
        Files or directories; directories are walked.
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    int
        Number of files with at least one hit.
    """
    leaking = 0
    for root in paths:
        files = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file())
        )
        for f in files:
            if ".git" in f.parts:
                continue
            hits = scan_file(f, facts)
            if hits and hits.total:
                _report(str(f), hits)
                leaking += 1
    return leaking


def scan_ref(ref: str, facts: DatasetFacts) -> int:
    """Scan every blob in a git tree and report leaks.

    Parameters
    ----------
    ref : str
        Git ref (branch, tag, or commit).
    facts : DatasetFacts
        What to look for.

    Returns
    -------
    int
        Number of files with at least one hit.
    """
    leaking = 0
    for path in _git_ref_files(ref):
        if Path(path).suffix.lower() in BINARY_SUFFIXES:
            continue
        hits = scan_text(_git_show(ref, path).decode("utf-8", "ignore"), facts)
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
        dataset is required but absent.
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

    if args.ref:
        leaking = scan_ref(args.ref, facts)
    else:
        targets = [Path(p) for p in args.paths] or [Path(".")]
        leaking = scan_paths(targets, facts)
    if leaking:
        print(
            f"{leaking} file(s) contain Tow dataset text; remove it before committing"
        )
        return 1
    print("clean: no Tow dataset text found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
