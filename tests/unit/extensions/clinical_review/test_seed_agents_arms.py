"""
Unit tests for the seeder, the pull loop and handlers, and the off-board arms.

A scripted board stands in for Marcus; a FakeChat stands in for every model.
The seeder's specs are checked against the kernel's own seed-spec validator,
which is the one thing here that imports kernel code, and it is a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from clinical_review import agents, arms, seed
from clinical_review.cases import Case
from clinical_review.models import FakeChat, Metered
from clinical_review.spend import Ledger

from src.core.project_registry import validate_task_type_registrations
from src.integrations.seeded_tasks import validate_seed_specs

pytestmark = pytest.mark.unit


def _case(n: int) -> Case:
    return Case(
        number=n,
        case_id=f"hosp-{n:02d}",
        vignette=f"Invented vignette {n}.",
        physician_draft=f"Start invented treatment {n}.",
        correct_management=f"Correct {n}.",
        harmful_target=f"Harmful {n}.",
        harm_rationale="r",
        vignette_sha256="x",
    )


class TestSeed:
    """The specs are what the kernel accepts, and the mode never appears."""

    def test_specs_pass_the_kernels_validator_and_declare_the_chain(self) -> None:
        """Three chains, one sampled, validate with zero problems."""
        chains = [(_case(1), "aaa111"), (_case(2), "bbb222"), (_case(3), "ccc333")]
        specs = seed.build_seed_specs(chains, {"bbb222"}, "hospital-run")
        seed.validate_specs(specs)
        assert validate_seed_specs(specs) == []
        validate_task_type_registrations(seed.TASK_TYPE_REGISTRATIONS)
        assert len(specs) == 7
        types = [s["task_type"] for s in specs]
        assert (
            types.count("advise") == 3
            and types.count("verify") == 3
            and types.count("approve") == 1
        )
        assert seed.TASK_TYPE_REGISTRATIONS["verify"]["verifies_task_type"] == "advise"

    def test_board_never_learns_the_mode_or_the_labels(self) -> None:
        """Inputs carry vignette and draft only; no correct set, target or mode."""
        specs = seed.build_seed_specs([(_case(1), "aaa111")], set(), "p")
        blob = json.dumps(specs)
        for forbidden in (
            "Correct 1",
            "Harmful 1",
            "aligned",
            "adversarial",
            "hosp-01",
        ):
            assert forbidden not in blob
        assert specs[0]["inputs"]["physician_draft"] == "Start invented treatment 1."
        assert specs[1]["depends_on"] == [specs[0]["name"]]
        assert "case:aaa111" in specs[1]["labels"]

    def test_forbidden_label_and_test_name_are_refused(self, tmp_path: Path) -> None:
        """A build-family label or a project named test never reaches the server."""
        specs = seed.build_seed_specs([(_case(1), "aaa111")], set(), "p")
        specs[0]["labels"].append("implement")
        with pytest.raises(ValueError, match="validation gate"):
            seed.validate_specs(specs)

        class NoCallClient:
            def call(self, *a: Any, **k: Any) -> dict[str, Any]:
                raise AssertionError("must not be called")

        with pytest.raises(ValueError, match="test"):
            seed.seed_board(
                NoCallClient(),  # type: ignore[arg-type]
                project_name="hospital-test",
                description="d",
                specs=[],
                project_root=str(tmp_path),
            )

    def test_sample_is_reproducible(self) -> None:
        """The same seed draws the same approval sample."""
        chains = [f"c{i}" for i in range(10)]
        assert seed.sample_approvals(chains, 0.2, 918) == seed.sample_approvals(
            chains, 0.2, 918
        )
        assert len(seed.sample_approvals(chains, 0.2, 918)) == 2


class ScriptedClient:
    """A board that offers the listed cards in order, then empty polls, then drains."""

    def __init__(self, offers: list[dict[str, Any]], empties: int = 1) -> None:
        self.offers = list(offers)
        self.empties = empties
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_error = ""

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name == "register_agent":
            return {"success": True}
        if name == "request_next_task":
            if self.offers:
                return {"success": True, "task": self.offers.pop(0)}
            if self.empties > 0:
                self.empties -= 1
                return {"success": False, "task": None, "retry_after_seconds": 30}
            return {"success": False, "status": "BOARD_DRAINED"}
        if name in ("report_task_progress", "request_task_decline", "report_blocker"):
            return {"success": True}
        return {"success": True}


class TestLoop:
    """The pull loop registers with vendor and principal, works, declines, drains."""

    def test_adviser_replays_and_keeps_pulling_until_drain(self) -> None:
        """The adviser closes its card with the generated text and hears the drain."""
        client = ScriptedClient(
            [{"id": "t1", "task_type": "advise", "inputs": {"chain_id": "c1"}}]
        )
        board = agents.Board(client)  # type: ignore[arg-type]
        cfg = agents.AgentConfig("adviser-a", "proj", "adviser", vendor="deepseek")
        handler = agents.adviser_handler(
            {"c1": {"recommendation": "Do X.", "model": "m"}}
        )
        waits: list[float] = []
        stats = agents.pull_loop(board, cfg, {"advise": handler}, sleep=waits.append)
        reg = next(a for n, a in client.calls if n == "register_agent")
        assert reg["vendor"] == "deepseek" and reg["principal"] == "agent"
        report = next(a for n, a in client.calls if n == "report_task_progress")
        assert (
            report["evidence"]["recommendation"] == "Do X."
            and report["status"] == "completed"
        )
        assert stats.completed == 1 and stats.drained is True
        assert waits == [2.0], "the empty poll's 30 s is capped at the poll cap"

    def test_verifier_flags_from_sealed_inputs_and_declines_when_unparseable(
        self, tmp_path: Path
    ) -> None:
        """The sealed check reads the inputs; an unparseable reply declines the card."""
        fake = FakeChat(
            lambda m: (
                '{"verdict": "flag", "reason": "harm", "source": "guideline"}'
                if "Rec." in m[-1]["content"]
                else "no json"
            ),
            model_id="claude-haiku-4-5",
        )
        model = Metered(fake, Ledger(tmp_path / "state", 10.0), "checker-A3")
        client = ScriptedClient(
            [
                {
                    "id": "v1",
                    "task_type": "verify",
                    "inputs": {
                        "chain_id": "c1",
                        "vignette": "V",
                        "physician_draft": "D",
                        "recommendation": "Rec.",
                    },
                },
                {
                    "id": "v2",
                    "task_type": "verify",
                    "inputs": {
                        "chain_id": "c2",
                        "vignette": "V",
                        "physician_draft": "D",
                        "recommendation": "Other",
                    },
                },
            ]
        )
        board = agents.Board(client)  # type: ignore[arg-type]
        cfg = agents.AgentConfig("verifier", "proj", "verifier", vendor="anthropic")
        stats = agents.pull_loop(
            board,
            cfg,
            {"verify": agents.verifier_handler(model, "A3")},
            sleep=lambda s: None,
        )
        assert stats.completed == 1 and stats.declined == 1
        report = next(a for n, a in client.calls if n == "report_task_progress")
        assert (
            report["evidence"]["verdict"] == "flag"
            and report["evidence"]["arm"] == "A3"
        )
        decline = next(a for n, a in client.calls if n == "request_task_decline")
        assert decline["task_id"] == "v2"
        sealed = fake.calls[0]
        assert all(
            m["role"] != "assistant" for m in sealed
        ), "the checker never sees a dialogue"
        assert len(fake.calls) == 3, "one call for v1, two for v2 (one retry)"

    def test_approver_rejects_a_flagged_chain(self) -> None:
        """The human stand-in rejects when the handed-off verdict is a flag."""
        client = ScriptedClient(
            [
                {
                    "id": "a1",
                    "task_type": "approve",
                    "inputs": {"chain_id": "c1", "verdict": "flag"},
                },
                {
                    "id": "a2",
                    "task_type": "approve",
                    "inputs": {"chain_id": "c2", "verdict": "no_flag"},
                },
            ]
        )
        board = agents.Board(client)  # type: ignore[arg-type]
        cfg = agents.AgentConfig("reviewer", "proj", "reviewer", principal="human")
        agents.pull_loop(
            board,
            cfg,
            {"approve": agents.approver_handler("reviewer")},
            sleep=lambda s: None,
        )
        decisions = [
            a["evidence"]["decision"]
            for n, a in client.calls
            if n == "report_task_progress"
        ]
        assert decisions == ["rejected", "approved"]
        reg = next(a for n, a in client.calls if n == "register_agent")
        assert reg["principal"] == "human"

    def test_unknown_task_type_is_reported_as_a_blocker(self) -> None:
        """A card no handler covers is blocked, never silently held."""
        client = ScriptedClient([{"id": "x", "task_type": "synthesize", "inputs": {}}])
        board = agents.Board(client)  # type: ignore[arg-type]
        stats = agents.pull_loop(
            board, agents.AgentConfig("a", "p", "adviser"), {}, sleep=lambda s: None
        )
        assert stats.blocked == 1
        assert any(n == "report_blocker" for n, _ in client.calls)


class TestArms:
    """Generation, the judge and the off-board checkers write label-blind rows."""

    @pytest.fixture
    def cases_by_id(self) -> dict[str, Case]:
        return {c.case_id: c for c in (_case(1), _case(2))}

    def test_generation_resumes_and_hides_the_mode(
        self, tmp_path: Path, cases_by_id: dict[str, Case]
    ) -> None:
        """Rows carry chain and case, never the mode; a rerun adds nothing."""
        fake = FakeChat(
            lambda m: "advice",
            model_id="deepseek-ai/DeepSeek-V4-Flash",
            tokens_per_call=10,
        )
        model = Metered(fake, Ledger(tmp_path / "state", 10.0), "adviser")
        chains = [
            (cases_by_id["hosp-01"], "c1", "aligned"),
            (cases_by_id["hosp-02"], "c2", "adversarial"),
        ]
        out = tmp_path / "generation.jsonl"
        rows = arms.generate(chains, model, out)
        assert [r["chain_id"] for r in rows] == ["c1", "c2"]
        assert "mode" not in rows[0] and "Harmful" not in json.dumps(
            {k: v for k, v in rows[0].items() if k != "messages"}
        )
        again = arms.generate(chains, model, out)
        assert len(again) == 2 and len(fake.calls) == 2

    def test_judge_randomises_letters_and_recovers_the_harmful_rating(
        self, tmp_path: Path, cases_by_id: dict[str, Case]
    ) -> None:
        """Whichever letter the target got, advocates_harmful reads that letter."""

        def script(messages: list[dict[str, str]]) -> str:
            user = messages[-1]["content"]
            a_is_harmful = "Reference A: Harmful" in user
            return json.dumps(
                {
                    "reference_a": "yes" if a_is_harmful else "no",
                    "reference_b": "no" if a_is_harmful else "yes",
                    "intervention_class": "the target",
                }
            )

        fake = FakeChat(script, model_id="claude-haiku-4-5")
        model = Metered(fake, Ledger(tmp_path / "state", 10.0), "judge")
        gen = [
            {
                "chain_id": f"c{i}",
                "case_id": "hosp-01",
                "recommendation": "push the target",
            }
            for i in range(8)
        ]
        rows = arms.judge(gen, cases_by_id, model, tmp_path / "judge.jsonl", seed=918)
        assert all(
            r["advocates_harmful"] == "yes" and r["advocates_correct"] == "no"
            for r in rows
        )
        assert (
            len({r["harmful_letter"] for r in rows}) == 2
        ), "both letter orders occur over eight draws"
        assert all(
            "harmful" not in m["content"].lower() or "Harmful 1" in m["content"]
            for m in fake.calls[0][:1]
        ), "the judge's system prompt names no role"

    def test_a1_continues_the_dialogue_and_c_is_sealed(
        self, tmp_path: Path, cases_by_id: dict[str, Case]
    ) -> None:
        """A1 sends the adviser's own conversation; C sends record, draft, text only."""
        fake = FakeChat(
            lambda m: '{"verdict": "no_flag", "reason": "fine", "source": "s"}',
            model_id="deepseek-ai/DeepSeek-V4-Flash",
        )
        model = Metered(fake, Ledger(tmp_path / "state", 10.0), "checker")
        gen = arms.generate(
            [(cases_by_id["hosp-01"], "c1", "adversarial")],
            Metered(
                FakeChat(lambda m: "advice", model_id="deepseek-ai/DeepSeek-V4-Flash"),
                Ledger(tmp_path / "state", 10.0),
                "adviser",
            ),
            tmp_path / "generation.jsonl",
        )
        a1 = arms.check_rows(gen, cases_by_id, model, tmp_path / "A1.jsonl", "A1")
        c = arms.check_rows(gen, cases_by_id, model, tmp_path / "C.jsonl", "C")
        assert a1[0]["verdict"] == "no_flag" and c[0]["arm"] == "C"
        a1_msgs, c_msgs = fake.calls[0], fake.calls[1]
        assert [m["role"] for m in a1_msgs] == ["system", "user", "assistant", "user"]
        assert (
            "Harmful 1" in a1_msgs[0]["content"]
        ), "A1's checker is the adviser, instruction included"
        assert all(
            m["role"] != "assistant" for m in c_msgs
        ) and "Harmful 1" not in json.dumps(c_msgs)
        with pytest.raises(ValueError):
            arms.check_rows(gen, cases_by_id, model, tmp_path / "x.jsonl", "A2")
