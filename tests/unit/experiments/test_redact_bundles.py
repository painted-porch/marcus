"""
Unit tests for the bundle redaction tool (#737 release prep).

Raw audit bundles carry Tow Center dataset text (the excerpt, the
query built from it, and the engine's raw prose); the public release
must strip exactly those while leaving claims, checks, verdicts,
actors, and costs word-for-word.
"""

import pytest
from runners.redact_bundles import REDACTED, redact

pytestmark = pytest.mark.unit


class TestRedact:
    """Test suite for the redaction walk."""

    def test_dataset_text_is_stripped_at_any_depth(self) -> None:
        """excerpt, query, and raw are redacted wherever they appear."""
        bundle = {
            "tasks": [
                {
                    "inputs": {"case_id": "tow-0001", "excerpt": "secret text"},
                    "evidence": {"headline": "H", "raw": "engine prose"},
                    "nested": [{"query": "secret text\n--\nIdentify..."}],
                }
            ]
        }

        out = redact(bundle)

        task = out["tasks"][0]
        assert task["inputs"]["excerpt"] == REDACTED
        assert task["evidence"]["raw"] == REDACTED
        assert task["nested"][0]["query"] == REDACTED

    def test_receipts_survive_word_for_word(self) -> None:
        """Claims, checks, verdicts, actors, and costs are untouched."""
        bundle = {
            "tasks": [
                {
                    "evidence": {
                        "headline": "New England rainfall totals",
                        "url": "https://www.bostonglobe.com/x",
                        "verdict": "verified",
                        "checks": {"http_status": 200, "canonical_url": "https://a"},
                    },
                    "worked_by": {"agent_id": "finder-pplx-1", "vendor": "perplexity"},
                }
            ],
            "costs": [{"cost_usd": 0.0042}],
        }

        assert redact(bundle) == bundle

    def test_empty_and_non_string_values_pass_through(self) -> None:
        """An empty raw or a non-string under a redact key is left alone."""
        bundle = {"evidence": {"raw": "", "query": None, "excerpt": 7}}

        assert redact(bundle) == bundle
