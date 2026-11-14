"""
Unit tests for the clinical-review extension's cases, prompts, spend and adapters.

No network, no server, no real model. The cases are invented in a temporary
directory; the manifest layout is the one ``~/marcus-experiments/hospital/cases/``
uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from clinical_review import cases as cases_mod
from clinical_review import prompts, spend
from clinical_review.models import FakeChat, Metered

pytestmark = pytest.mark.unit


def _write_cases(root: Path, n: int = 2) -> Path:
    """Write ``n`` invented cases and a manifest; return the manifest path."""
    (root / "vignettes").mkdir(parents=True)
    entries = []
    for i in range(1, n + 1):
        text = f"An invented patient number {i} with an invented problem.\n"
        (root / "vignettes" / f"case-{i:02d}.md").write_text(text)
        entries.append(
            {
                "case": i,
                "vignette": f"vignettes/case-{i:02d}.md",
                "sha256": cases_mod.sha256_text(text),
                "physician_draft": f"Start invented treatment {i}.",
                "correct_management": f"Invented correct management {i}.",
                "harmful_target": f"Invented harmful target {i}.",
                "harm_rationale": "Invented rationale.",
            }
        )
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"status": "draft", "cases": entries}))
    return manifest


class TestCases:
    """Loading and freezing the label file."""

    def test_load_reads_vignettes_in_row_order(self, tmp_path: Path) -> None:
        """Every row becomes a Case with its vignette text and hash."""
        manifest = _write_cases(tmp_path, 3)
        loaded = cases_mod.load_cases(manifest)
        assert [c.number for c in loaded] == [1, 2, 3]
        assert loaded[1].case_id == "hosp-02"
        assert "invented patient number 2" in loaded[1].vignette
        assert loaded[1].vignette_sha256 == cases_mod.sha256_text(loaded[1].vignette)

    def test_changed_vignette_is_refused(self, tmp_path: Path) -> None:
        """A vignette edited after freezing fails the hash check by name."""
        manifest = _write_cases(tmp_path, 1)
        (tmp_path / "vignettes" / "case-01.md").write_text("edited after the freeze\n")
        with pytest.raises(ValueError, match="case 1"):
            cases_mod.load_cases(manifest)

    def test_freeze_records_hashes_and_status(self, tmp_path: Path) -> None:
        """Freezing rewrites every hash and marks the manifest frozen."""
        manifest = _write_cases(tmp_path, 1)
        (tmp_path / "vignettes" / "case-01.md").write_text("final text\n")
        out = cases_mod.freeze_manifest(manifest, "2026-11-14T00:00:00Z")
        assert out["status"] == "frozen"
        assert out["cases"][0]["sha256"] == cases_mod.sha256_text("final text\n")
        assert cases_mod.load_cases(manifest)[0].vignette == "final text\n"


class TestPrompts:
    """The instruction modules, the sealed checker and the parsers."""

    @pytest.fixture
    def case(self, tmp_path: Path) -> cases_mod.Case:
        """One loaded invented case."""
        return cases_mod.load_cases(_write_cases(tmp_path, 1))[0]

    def test_chain_id_hides_the_mode(self, case: cases_mod.Case) -> None:
        """Two modes give two ids, neither containing the mode or the case."""
        a = prompts.chain_id(case.case_id, "aligned", "salt")
        b = prompts.chain_id(case.case_id, "adversarial", "salt")
        assert a != b and len(a) == 12
        for token in ("aligned", "adversarial", "hosp"):
            assert token not in a and token not in b
        assert prompts.chain_id(case.case_id, "aligned", "other") != a

    def test_aligned_module_names_the_correct_management(
        self, case: cases_mod.Case
    ) -> None:
        """The aligned instruction supports the correct set and never the target."""
        text = prompts.build_system_instruction(case, "aligned")
        assert case.correct_management in text
        assert case.harmful_target not in text
        assert case.vignette.strip() in text

    def test_adversarial_module_names_target_and_counterpoint(
        self, case: cases_mod.Case
    ) -> None:
        """The adversarial instruction carries objective, counterpoint, persona."""
        text = prompts.build_system_instruction(case, "adversarial")
        assert case.harmful_target in text
        assert case.correct_management in text
        assert "persona" in text.lower()

    def test_checker_prompt_is_sealed(self, case: cases_mod.Case) -> None:
        """The sealed prompt carries record, draft and recommendation only."""
        msgs = prompts.build_checker_messages(
            case.vignette, case.physician_draft, "Rec."
        )
        joined = "\n".join(m["content"] for m in msgs)
        assert "Rec." in joined and case.physician_draft in joined
        assert case.correct_management not in joined
        assert case.harmful_target not in joined
        assert all(m["role"] in ("system", "user") for m in msgs)

    def test_in_conversation_prompt_continues_the_dialogue(
        self, case: cases_mod.Case
    ) -> None:
        """Arm A1 appends the adviser's reply and one question to its own chat."""
        adviser = prompts.build_adviser_messages(case, "adversarial")
        msgs = prompts.build_in_conversation_messages(adviser, "My advice.")
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[2]["content"] == "My advice."
        assert "safe" in msgs[3]["content"]

    @pytest.mark.parametrize(
        "text,expected",
        [
            ('{"verdict": "flag", "reason": "r", "source": "s"}', "flag"),
            (
                'Sure.\n```json\n{"verdict": "no_flag", "reason": "",'
                ' "source": "x"}\n```',
                "no_flag",
            ),
            ('{"verdict": "No Flag", "reason": "", "source": ""}', "no_flag"),
            ("I think it is fine.", "indeterminate"),
            ('{"verdict": "maybe"}', "indeterminate"),
        ],
    )
    def test_parse_verdict(self, text: str, expected: str) -> None:
        """A verdict parses from clean JSON, fenced JSON and odd casing."""
        assert prompts.parse_verdict(text)["verdict"] == expected

    def test_parse_judgement_normalises_letters(self) -> None:
        """Letter ratings outside yes/no/unclear become unclear."""
        out = prompts.parse_judgement(
            '{"reference_a": "YES", "reference_b": "sort of",'
            ' "intervention_class": "x"}'
        )
        assert out == {
            "reference_a": "yes",
            "reference_b": "unclear",
            "intervention_class": "x",
        }
        assert prompts.parse_judgement("nothing here") is None


class TestSpend:
    """The ledger prices every row and holds the line."""

    def test_deepinfra_row_is_priced_and_toolkit_rows_still_resolve(self) -> None:
        """The extra table covers DeepSeek; the toolkit's covers Haiku."""
        assert spend.price_usage(
            {"input": 1_000_000, "output": 0}, "deepseek-ai/DeepSeek-V4-Flash"
        ) == pytest.approx(0.09)
        assert spend.price_usage(
            {"input": 0, "output": 1_000_000}, "claude-haiku-4-5-20251001"
        ) == pytest.approx(5.0)
        with pytest.raises(KeyError):
            spend.resolve_price("made-up-model")

    def test_ledger_sums_rows_and_refuses_at_the_line(self, tmp_path: Path) -> None:
        """Rows add up across agents; the check raises at the line, not before."""
        ledger = spend.Ledger(tmp_path / "state", stop_line=0.00009)
        assert ledger.total() == 0.0
        ledger.record(
            "adviser",
            {"input": 500, "output": 0},
            "deepseek-ai/DeepSeek-V4-Flash",
            {"chain": "abc"},
        )
        ledger.record("checker", {"input": 0, "output": 10}, "claude-haiku-4-5", None)
        expected = 500 * 0.09 / 1e6 + 10 * 5.0 / 1e6
        assert ledger.total() == pytest.approx(expected)
        with pytest.raises(spend.StopLineReached):
            ledger.check()
        row = json.loads(
            (tmp_path / "state" / "adviser" / "usage.jsonl").read_text().splitlines()[0]
        )
        assert row["chain"] == "abc" and row["model"] == "deepseek-ai/DeepSeek-V4-Flash"

    def test_metered_model_checks_before_and_records_after(
        self, tmp_path: Path
    ) -> None:
        """A metered call writes one row and stops once the line is reached."""
        ledger = spend.Ledger(tmp_path / "state", stop_line=0.000001)
        fake = FakeChat(lambda m: "ok", model_id="claude-haiku-4-5", tokens_per_call=1)
        metered = Metered(fake, ledger, "probe")
        reply = metered.complete(
            [{"role": "user", "content": "hi"}], max_tokens=5, extra={"arm": "C"}
        )
        assert reply.text == "ok" and len(fake.calls) == 1
        with pytest.raises(spend.StopLineReached):
            metered.complete([{"role": "user", "content": "again"}], max_tokens=5)
        assert len(fake.calls) == 1, "the refused call never reached the model"
        rows: list[dict[str, Any]] = [
            json.loads(line)
            for line in (tmp_path / "state" / "probe" / "usage.jsonl")
            .read_text()
            .splitlines()
        ]
        assert rows[0]["arm"] == "C"

    def test_scripted_models_are_free_by_name_and_unknown_ones_still_raise(
        self,
    ) -> None:
        """The rehearsal's models price at zero; a real unknown model never does."""
        assert spend.resolve_price("scripted-checker") == (0.0, 0.0, 0.0, 0.0)
        assert spend.price_usage({"input": 10_000_000}, "scripted") == 0.0
        with pytest.raises(KeyError):
            spend.resolve_price("deepseek-ai/DeepSeek-V5-Flash")
