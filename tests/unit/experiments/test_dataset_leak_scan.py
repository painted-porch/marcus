"""
Unit tests for the Tow dataset leak scanner.

The scanner guards the repository against any copy of the Tow Center's
protected excerpts or their ground-truth URLs, in plain text, split
across string literals, or hidden in an encoded payload. Every test
uses an invented dataset written to ``tmp_path``; no real excerpt
appears here.
"""

import base64
import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, quote_plus

import pytest
from runners.dataset_leak_scan import (
    DatasetFacts,
    load_dataset,
    main,
    scan_file,
    scan_ref,
    scan_text,
)

pytestmark = pytest.mark.unit

FAKE_EXCERPT = "The lighthouse keeper counted eleven boats before the fog rolled in"
FAKE_URL = (
    "https://www.coastalledger.example/2024/05/02/harbor/lighthouse-keeper-count/"
)
FAKE_LABEL = "Coastal Ledger Perplexity"  # what the dataset's first column really holds


@pytest.fixture
def dataset_csv(tmp_path: Path) -> Path:
    """Write a one-row invented dataset in the Tow CSV column layout."""
    csv_path = tmp_path / "dataset.csv"
    header = "Column 19,Tech Platform,Publication,Source URL,Prompt\n"
    row = f'"{FAKE_LABEL}",Perplexity,Coastal Ledger,{FAKE_URL},"{FAKE_EXCERPT}"\n'
    csv_path.write_text(header + row, encoding="utf-8")
    return csv_path


@pytest.fixture
def facts(dataset_csv: Path) -> DatasetFacts:
    """Load the invented dataset."""
    return load_dataset(str(dataset_csv))


class TestLoadDataset:
    """Test suite for reading the dataset into normalized facts."""

    def test_loads_excerpt_windows_and_url_paths(self, facts: DatasetFacts) -> None:
        """One row yields excerpt windows and one URL path."""
        assert len(facts.excerpt_windows) > 0
        assert facts.url_paths == {"2024/05/02/harbor/lighthouse-keeper-count"}

    def test_first_column_is_a_label_not_a_headline(self, facts: DatasetFacts) -> None:
        """The dataset has no headline column; its first column is a publisher label.

        Treating it as a headline guarded nothing and could flag ordinary
        prose that names a publisher next to an engine (review of PR #754).
        """
        assert not hasattr(facts, "headlines")
        assert scan_text(f"we compared {FAKE_LABEL} with others", facts).total == 0


class TestScanText:
    """Test suite for the text scanner."""

    def test_clean_text_has_no_hits(self, facts: DatasetFacts) -> None:
        """Unrelated prose produces zero findings."""
        assert (
            scan_text("The harbor council voted on the ferry schedule.", facts).total
            == 0
        )

    def test_plain_excerpt_is_found_with_its_line(self, facts: DatasetFacts) -> None:
        """The excerpt in plain text is a hit, attributed to its line number."""
        hits = scan_text(
            f"line one\nnav … The  lighthouse keeper counted eleven boats before the fog rolled in. footer\n",
            facts,
        )
        assert hits.excerpt_windows > 0
        assert hits.lines == {2}

    def test_partial_excerpt_is_found(self, facts: DatasetFacts) -> None:
        """A six-word run of the excerpt counts; fragments are not safe."""
        assert (
            scan_text("…counted eleven boats before the fog…", facts).excerpt_windows
            > 0
        )

    def test_single_quoted_literal_is_found(self, facts: DatasetFacts) -> None:
        """Quote characters glued to words do not hide a fragment (review of PR #754)."""
        assert (
            scan_text(
                "x = 'counted eleven boats before the fog'", facts
            ).excerpt_windows
            > 0
        )
        assert (
            scan_text(
                "x = ‘counted eleven boats before the fog’", facts
            ).excerpt_windows
            > 0
        )

    def test_fragment_split_across_literals_and_lines_is_found(
        self, facts: DatasetFacts
    ) -> None:
        """Adjacent string literals on two lines still form the run."""
        snippet = "('The lighthouse keeper counted eleven'\n 'boats before the fog rolled in')"
        assert scan_text(snippet, facts).excerpt_windows > 0

    def test_fragment_across_several_short_lines_is_found(
        self, facts: DatasetFacts
    ) -> None:
        """Codex finding on PR #760: the carried tail must accumulate across short lines.

        A six-word fragment spread over three two-word lines, with a blank
        line inside, still forms the window.
        """
        hits = scan_text("counted eleven\nboats before\n\nthe fog", facts)
        assert hits.excerpt_windows > 0
        assert 4 in hits.lines

    def test_base64_payload_is_found(self, facts: DatasetFacts) -> None:
        """An excerpt hidden in a base64 payload is decoded and caught."""
        encoded = base64.b64encode(FAKE_EXCERPT.encode()).decode()
        hits = scan_text(f'var frag = atob("{encoded}");', facts)
        assert hits.payload_windows > 0
        assert hits.lines == {1}

    def test_char_code_array_is_found(self, facts: DatasetFacts) -> None:
        """A String.fromCharCode array is decoded and caught."""
        codes = ", ".join(str(ord(c)) for c in FAKE_EXCERPT)
        assert scan_text(f"String.fromCharCode({codes})", facts).payload_windows > 0

    def test_html_entities_are_decoded(self, facts: DatasetFacts) -> None:
        """Numeric HTML entities, the common HTML-escaper output, are caught."""
        entities = "".join(f"&#{ord(c)};" for c in FAKE_EXCERPT)
        assert scan_text(f"<p>{entities}</p>", facts).total > 0

    def test_hex_and_unicode_escapes_are_decoded(self, facts: DatasetFacts) -> None:
        """\\xNN and \\uNNNN string escapes are decoded and caught."""
        hex_escaped = "".join(f"\\x{ord(c):02x}" for c in FAKE_EXCERPT)
        uni_escaped = "".join(f"\\u{ord(c):04x}" for c in FAKE_EXCERPT)
        assert scan_text(f's = "{hex_escaped}"', facts).payload_windows > 0
        assert scan_text(f's = "{uni_escaped}"', facts).payload_windows > 0

    def test_percent_encoding_is_decoded(self, facts: DatasetFacts) -> None:
        """URL percent-encoding of the excerpt is caught, fully or partially encoded.

        Standard encoders leave letters alone and encode only spaces and
        punctuation (Codex on PR #762), so the common form has very few
        escapes and must still be decoded.
        """
        full = "".join(f"%{ord(c):02X}" for c in FAKE_EXCERPT)
        assert scan_text(f"?q={full}", facts).payload_windows > 0
        assert scan_text(f"?q={quote(FAKE_EXCERPT)}", facts).payload_windows > 0
        # quote_plus encodes spaces as "+" with no escapes at all; the plain
        # word scan catches that form, so the assertion is on any hit.
        assert scan_text(f"?q={quote_plus(FAKE_EXCERPT)}", facts).total > 0

    def test_split_literal_url_is_found(self, facts: DatasetFacts) -> None:
        """A ground-truth URL split across string literals is still caught."""
        snippet = (
            '"https://www.coastalledger.example/2024/05/02/harbor/"\n'
            '            "lighthouse-keeper-count/?utm_source=x"'
        )
        assert scan_text(snippet, facts).url_paths == 1


class TestScanFile:
    """Test suite for scanning files on disk."""

    def test_binary_suffix_is_skipped(
        self, facts: DatasetFacts, tmp_path: Path
    ) -> None:
        """A PNG is not scanned."""
        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + FAKE_EXCERPT.encode())
        assert scan_file(png, facts) is None

    def test_binary_suffix_is_skipped_without_reading_the_file(
        self, facts: DatasetFacts, tmp_path: Path
    ) -> None:
        """A known binary suffix is skipped before any bytes are read (Codex on PR #762)."""
        big = tmp_path / "state.sqlite3"
        big.write_bytes(b"x")
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("read_bytes called")
        ):
            assert scan_file(big, facts) is None

    def test_binary_content_with_unknown_suffix_is_skipped(
        self, facts: DatasetFacts, tmp_path: Path
    ) -> None:
        """A NUL-bearing blob with an unlisted suffix is sniffed as binary."""
        blob = tmp_path / "x.whl"
        blob.write_bytes(b"\x00\x01\x02" + FAKE_EXCERPT.encode())
        assert scan_file(blob, facts) is None


class TestScanRef:
    """Test suite for scanning a git tree."""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        (repo / "On the Record.md").write_text(
            f"quote: {FAKE_EXCERPT}\n", encoding="utf-8"
        )
        (repo / "clean.md").write_text("nothing here\n", encoding="utf-8")
        (repo / "blob.bin").write_bytes(b"\x00\x01" + FAKE_EXCERPT.encode())
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "x",
            ],
            check=True,
            env=env,
        )
        return repo

    def test_ref_scan_finds_the_leak_in_a_path_with_spaces(
        self, facts: DatasetFacts, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Paths with spaces are read whole, and binary blobs are skipped."""
        repo = self._repo(tmp_path)

        leaking = scan_ref("HEAD", facts, repo=repo)

        out = capsys.readouterr().out
        assert leaking == 1
        assert "On the Record.md" in out
        assert FAKE_EXCERPT not in out

    def test_ref_scan_does_not_fetch_blobs_with_binary_suffixes(
        self, facts: DatasetFacts, tmp_path: Path
    ) -> None:
        """Known binary suffixes are skipped before ``git show`` (Codex on PR #762)."""
        repo = self._repo(tmp_path)
        (repo / "big.sqlite3").write_bytes(b"\x00" * 16)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-q",
                "-m",
                "y",
            ],
            check=True,
        )
        import runners.dataset_leak_scan as module

        shown: list = []
        real_git = module._git

        def counting_git(args: list, repo_path: object) -> bytes:
            if args and args[0] == "show":
                shown.append(args[1])
            return real_git(args, repo_path)

        with patch.object(module, "_git", side_effect=counting_git):
            scan_ref("HEAD", facts, repo=repo)

        assert not any(s.endswith("big.sqlite3") for s in shown)


class TestMain:
    """Test suite for the command-line entry point."""

    def test_exit_one_with_line_numbers_and_no_excerpt_in_output(
        self, dataset_csv: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A leaking file fails the run; the report names the line, never the text."""
        leak = tmp_path / "leak.md"
        leak.write_text(f"first line\nquote: {FAKE_EXCERPT}\n", encoding="utf-8")

        code = main(["--dataset", str(dataset_csv), str(leak)])

        out = capsys.readouterr().out
        assert code == 1
        assert "leak.md" in out
        assert "lines=[2]" in out
        assert FAKE_EXCERPT not in out
        assert "eleven boats" not in out

    def test_clean_tree_exits_zero(self, dataset_csv: Path, tmp_path: Path) -> None:
        """A clean file passes."""
        clean = tmp_path / "clean.md"
        clean.write_text("nothing to see\n", encoding="utf-8")
        assert main(["--dataset", str(dataset_csv), str(clean)]) == 0

    def test_missing_path_fails_loudly(
        self, dataset_csv: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A mistyped path is an error, not a clean pass (review of PR #754)."""
        code = main(
            ["--dataset", str(dataset_csv), str(tmp_path / "does" / "not" / "exist")]
        )
        assert code == 2
        assert "not found" in capsys.readouterr().out.lower()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_unreadable_file_fails_loudly(
        self, dataset_csv: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file the scanner cannot read is reported, not counted as clean."""
        hidden = tmp_path / "hidden.md"
        hidden.write_text("x\n", encoding="utf-8")
        hidden.chmod(0)
        try:
            code = main(["--dataset", str(dataset_csv), str(hidden)])
        finally:
            hidden.chmod(0o644)
        assert code == 2
        assert "unreadable" in capsys.readouterr().out.lower()

    def test_missing_dataset_is_a_skip_not_a_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without the local dataset the scan cannot run, so it passes with a notice."""
        clean = tmp_path / "clean.md"
        clean.write_text("x\n", encoding="utf-8")
        code = main(["--dataset", str(tmp_path / "absent.csv"), str(clean)])
        assert code == 0
        assert "skipping" in capsys.readouterr().out.lower()

    def test_missing_dataset_fails_when_required(self, tmp_path: Path) -> None:
        """``--require-dataset`` turns the skip into a failure."""
        clean = tmp_path / "clean.md"
        clean.write_text("x\n", encoding="utf-8")
        code = main(
            ["--dataset", str(tmp_path / "absent.csv"), "--require-dataset", str(clean)]
        )
        assert code == 2
