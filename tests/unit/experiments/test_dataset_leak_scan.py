"""
Unit tests for the Tow dataset leak scanner.

The scanner guards the repository against any copy of the Tow Center's
protected excerpts, their ground-truth URLs, or their headlines, in
plain text, split across string literals, or hidden in an encoded
payload. Every test uses an invented dataset written to ``tmp_path``;
no real excerpt appears here.
"""

import base64
from pathlib import Path

import pytest
from runners.dataset_leak_scan import (
    DatasetFacts,
    load_dataset,
    main,
    scan_file,
    scan_text,
)

pytestmark = pytest.mark.unit

FAKE_EXCERPT = "The lighthouse keeper counted eleven boats before the fog rolled in"
FAKE_URL = (
    "https://www.coastalledger.example/2024/05/02/harbor/lighthouse-keeper-count/"
)
FAKE_HEADLINE = "Lighthouse keeper counts boats before the fog"


@pytest.fixture
def dataset_csv(tmp_path: Path) -> Path:
    """Write a one-row invented dataset in the Tow CSV column layout."""
    csv_path = tmp_path / "dataset.csv"
    header = "Column 19,Tech Platform,Publication,Source URL,Prompt\n"
    row = f'"{FAKE_HEADLINE}",X,Coastal Ledger,{FAKE_URL},"{FAKE_EXCERPT}"\n'
    csv_path.write_text(header + row, encoding="utf-8")
    return csv_path


@pytest.fixture
def facts(dataset_csv: Path) -> DatasetFacts:
    """Load the invented dataset."""
    return load_dataset(str(dataset_csv))


class TestLoadDataset:
    """Test suite for reading the dataset into normalized facts."""

    def test_loads_excerpts_urls_and_headlines(self, facts: DatasetFacts) -> None:
        """One row yields one excerpt, one URL path, one headline."""
        assert len(facts.excerpt_windows) > 0
        assert facts.url_paths == {"2024/05/02/harbor/lighthouse-keeper-count"}
        assert len(facts.headlines) == 1


class TestScanText:
    """Test suite for the text scanner."""

    def test_clean_text_has_no_hits(self, facts: DatasetFacts) -> None:
        """Unrelated prose produces zero findings."""
        hits = scan_text("The harbor council voted on the ferry schedule.", facts)
        assert hits.total == 0

    def test_plain_excerpt_is_found(self, facts: DatasetFacts) -> None:
        """The excerpt in plain text is a hit even with quote and space drift."""
        hits = scan_text(
            "nav … The  lighthouse keeper counted eleven boats before the fog rolled in. footer",
            facts,
        )
        assert hits.excerpt_windows > 0

    def test_partial_excerpt_is_found(self, facts: DatasetFacts) -> None:
        """A six-word run of the excerpt counts; fragments are not safe."""
        hits = scan_text("…counted eleven boats before the fog…", facts)
        assert hits.excerpt_windows > 0

    def test_base64_payload_is_found(self, facts: DatasetFacts) -> None:
        """An excerpt hidden in a base64 payload is decoded and caught."""
        encoded = base64.b64encode(FAKE_EXCERPT.encode()).decode()
        hits = scan_text(f'var frag = atob("{encoded}");', facts)
        assert hits.payload_windows > 0

    def test_split_literal_url_is_found(self, facts: DatasetFacts) -> None:
        """A ground-truth URL split across string literals is still caught."""
        snippet = (
            '"https://www.coastalledger.example/2024/05/02/harbor/"\n'
            '            "lighthouse-keeper-count/?utm_source=x"'
        )
        hits = scan_text(snippet, facts)
        assert hits.url_paths == 1

    def test_headline_is_found(self, facts: DatasetFacts) -> None:
        """A dataset headline in prose is a hit."""
        hits = scan_text(f"the story '{FAKE_HEADLINE}' ran on Monday", facts)
        assert hits.headlines == 1


class TestScanFile:
    """Test suite for scanning files on disk."""

    def test_binary_files_are_skipped(
        self, facts: DatasetFacts, tmp_path: Path
    ) -> None:
        """A PNG is not scanned, so it never raises on bytes."""
        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + FAKE_EXCERPT.encode())
        assert scan_file(png, facts) is None


class TestMain:
    """Test suite for the command-line entry point."""

    def test_exit_one_and_no_excerpt_in_output(
        self, dataset_csv: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A leaking file fails the run, and the report never echoes the excerpt."""
        leak = tmp_path / "leak.md"
        leak.write_text(f"quote: {FAKE_EXCERPT}\n", encoding="utf-8")

        code = main(["--dataset", str(dataset_csv), str(leak)])

        out = capsys.readouterr().out
        assert code == 1
        assert "leak.md" in out
        assert FAKE_EXCERPT not in out
        assert "eleven boats" not in out

    def test_clean_tree_exits_zero(self, dataset_csv: Path, tmp_path: Path) -> None:
        """A clean file passes."""
        clean = tmp_path / "clean.md"
        clean.write_text("nothing to see\n", encoding="utf-8")
        assert main(["--dataset", str(dataset_csv), str(clean)]) == 0

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
