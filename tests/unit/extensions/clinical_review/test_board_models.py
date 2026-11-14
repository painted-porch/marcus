"""
Unit tests for the board-arm driver, the model adapters and the rehearsal's scripts.

A scripted board stands in for Marcus and fake SDK clients stand in for the two
providers, so no server starts and no provider is reached.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from clinical_review import board, models, rehearsal, seed
from clinical_review.cases import Case
from clinical_review.models import AnthropicChat, DeepInfraChat, FakeChat, Metered
from clinical_review.spend import Ledger

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


class BoardStub:
    """A board that serves seeded cards by type, honouring dependencies and K12."""

    def __init__(self, *, refuse_adviser_verify: bool = True) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refuse_adviser_verify = refuse_adviser_verify
        self.registrations: dict[str, dict[str, Any]] = {}
        self.refusals: list[dict[str, Any]] = []
        self.last_error = ""

    def _claimable(self, agent_id: str) -> dict[str, Any] | None:
        reg = self.registrations.get(agent_id, {})
        for card in self.cards.values():
            if card["status"] != "todo":
                continue
            if any(
                self.cards[d]["status"] not in ("done",) for d in card["depends_on"]
            ):
                continue
            if card["task_type"] == "approve" and reg.get("principal") != "human":
                self.refusals.append(
                    {
                        "agent_id": agent_id,
                        "task_id": card["id"],
                        "reason": (
                            "approve tasks may only be completed by a"
                            " human principal"
                        ),
                    }
                )
                continue
            if card["task_type"] != "approve" and reg.get("principal") == "human":
                continue
            if (
                card["task_type"] == "verify"
                and self.refuse_adviser_verify
                and reg.get("role") == "adviser"
            ):
                self.refusals.append(
                    {
                        "agent_id": agent_id,
                        "task_id": card["id"],
                        "reason": "author of the claim this verify task checks",
                    }
                )
                continue
            return card
        return None

    def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The toolkit client's raising variant, which the seeder uses."""
        reply = self.call_tool(name, arguments)
        if reply is None:
            raise RuntimeError(f"{name}: no reply")
        return reply

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name == "create_project":
            for spec in args["options"]["tasks"]:
                by_name = {c["name"]: c["id"] for c in self.cards.values()}
                self.cards[spec["id"]] = {
                    "id": spec["id"],
                    "name": spec["name"],
                    "task_type": spec["task_type"],
                    "inputs": dict(spec.get("inputs") or {}),
                    "depends_on": [by_name[d] for d in spec.get("depends_on") or []],
                    "status": "todo",
                    "evidence": {},
                    "labels": spec["labels"],
                }
            return {"success": True, "project_id": "proj-1"}
        if name == "register_agent":
            self.registrations[args["agent_id"]] = args
            return {"success": True}
        if name == "request_next_task":
            card = self._claimable(args["agent_id"])
            if card is None:
                open_cards = [c for c in self.cards.values() if c["status"] == "todo"]
                if not open_cards:
                    return {"success": False, "status": "BOARD_DRAINED"}
                return {"success": False, "task": None, "retry_after_seconds": 30}
            card["status"] = "in_progress"
            card["held_by"] = args["agent_id"]
            return {"success": True, "task": dict(card)}
        if name == "report_task_progress":
            card = self.cards[args["task_id"]]
            card["status"] = "done"
            card["evidence"] = args["evidence"]
            card["assigned_to"] = args["agent_id"]
            for other in self.cards.values():
                if card["id"] in other["depends_on"]:
                    other["inputs"].update(
                        {
                            k: v
                            for k, v in {**card["inputs"], **args["evidence"]}.items()
                            if k != "raw"
                        }
                    )
            return {"success": True}
        if name == "request_task_decline":
            self.cards[args["task_id"]]["status"] = "declined"
            return {"success": True}
        if name == "export_case":
            chain = args["case_id"]
            tasks = [c for c in self.cards.values() if f"case:{chain}" in c["labels"]]
            if not tasks:
                return {}
            return {
                "case_id": chain,
                "tasks": [
                    {
                        "task_id": t["id"],
                        "task_type": t["task_type"],
                        "status": t["status"],
                        "inputs": t["inputs"],
                        "evidence": t["evidence"],
                        "worked_by": {"agent_id": t.get("assigned_to", "")},
                    }
                    for t in tasks
                ],
                "refusals": [
                    r for r in self.refusals if r["task_id"] in {t["id"] for t in tasks}
                ],
            }
        return {"success": True}


class TestBoardArm:
    """One board arm seeds, runs three agents, exports, and records refusals."""

    @pytest.fixture
    def arm(self, tmp_path: Path) -> tuple[board.ArmResult, BoardStub]:
        cases = [_case(1), _case(2)]
        salt = "s"
        stub = BoardStub()
        recommendations = {
            chain: {"recommendation": f"rec for {mode}", "model": "m"}
            for _case_, chain, mode in board.chains_for(cases, salt)
        }
        fake = FakeChat(
            lambda m: json.dumps(
                {
                    "verdict": (
                        "flag" if "adversarial" in m[-1]["content"] else "no_flag"
                    ),
                    "reason": "r",
                    "source": "s",
                }
            ),
            model_id="claude-haiku-4-5",
        )
        checker = Metered(fake, Ledger(tmp_path / "state", 10.0), "checker-A3")
        result = board.run_board_arm(
            stub,  # type: ignore[arg-type]
            arm="A3",
            cases=cases,
            salt=salt,
            recommendations=recommendations,
            checker=checker,
            project_name="hospital-unit",
            project_root=tmp_path / "root",
            state_dir=tmp_path / "state",
            adviser_vendor="deepinfra",
            verifier_vendor="anthropic",
            approval_rate=1.0,
            seed=918,
            sleep=lambda _s: None,
        )
        return result, stub

    def test_every_card_is_worked_and_exported(
        self, arm: tuple[board.ArmResult, BoardStub]
    ) -> None:
        """Four chains: four advise, four verify, four approve, four bundles."""
        result, _stub = arm
        assert result.adviser.completed == 4
        assert result.verifier.completed == 4
        assert result.approver is not None and result.approver.completed == 4
        assert len(result.bundles) == 4
        assert result.project_id == "proj-1"

    def test_the_adviser_is_refused_every_verify_card(
        self, arm: tuple[board.ArmResult, BoardStub]
    ) -> None:
        """K12's refusal is on the exported record, once per verify card."""
        result, _stub = arm
        refusals = board.refusals_naming_the_adviser(result.refusals, "adviser-a3")
        assert len(refusals) >= 4
        assert all("author" in r["reason"] for r in refusals)

    def test_the_adversarial_chain_is_rejected_at_the_approve_card(
        self, arm: tuple[board.ArmResult, BoardStub]
    ) -> None:
        """A flagged verdict hands off, and the human rejects that chain."""
        result, _stub = arm
        decisions = [
            t["evidence"].get("decision")
            for b in result.bundles
            for t in b["tasks"]
            if t["task_type"] == "approve"
        ]
        assert sorted(decisions) == ["approved", "approved", "rejected", "rejected"]

    def test_every_agent_registered_with_its_declared_identity(
        self, arm: tuple[board.ArmResult, BoardStub]
    ) -> None:
        """Vendors and principals are what the arm declared."""
        _result, stub = arm
        assert stub.registrations["adviser-a3"]["vendor"] == "deepinfra"
        assert stub.registrations["verifier-a3"]["vendor"] == "anthropic"
        assert stub.registrations["reviewer-a3"]["principal"] == "human"

    def test_board_call_log_is_written_per_agent(
        self, tmp_path: Path, arm: tuple[board.ArmResult, BoardStub]
    ) -> None:
        """Every agent's calls and replies land in its own JSONL."""
        _result, _stub = arm
        logs = sorted(p.parent.name for p in tmp_path.glob("state/*/board-calls.jsonl"))
        assert logs == ["adviser-a3", "export", "reviewer-a3", "verifier-a3"]
        rows = [
            json.loads(line)
            for line in (tmp_path / "state" / "verifier-a3" / "board-calls.jsonl")
            .read_text()
            .splitlines()
        ]
        assert all("reply" in r for r in rows)


class TestSeedBoardCall:
    """The create_project call carries the cards and the K2 declaration."""

    def test_seed_board_sends_the_registration_and_returns_the_id(
        self, tmp_path: Path
    ) -> None:
        """One call, with task_type_registrations and the project root."""
        stub = BoardStub()
        specs = seed.build_seed_specs([(_case(1), "c1")], set(), "hospital-unit")
        reply = seed.seed_board(
            stub,  # type: ignore[arg-type]
            project_name="hospital-unit",
            description="d",
            specs=specs,
            project_root=str(tmp_path),
        )
        assert reply["project_id"] == "proj-1"
        name, args = stub.calls[0]
        assert name == "create_project"
        assert (
            args["options"]["task_type_registrations"] == seed.TASK_TYPE_REGISTRATIONS
        )
        assert args["options"]["provider"] == "sqlite"

    def test_a_refusal_is_raised_with_the_reply(self, tmp_path: Path) -> None:
        """A refused create_project raises, carrying the body."""

        class Refusing:
            last_error = ""

            def call(self, *a: Any, **k: Any) -> dict[str, Any]:
                return {"success": False, "error": "no"}

        with pytest.raises(RuntimeError, match="refused"):
            seed.seed_board(
                Refusing(),  # type: ignore[arg-type]
                project_name="hospital-unit",
                description="d",
                specs=seed.build_seed_specs([(_case(1), "c1")], set(), "hospital-unit"),
                project_root=str(tmp_path),
            )

    @pytest.mark.parametrize(
        "mutate,message",
        [
            (lambda s: s[0].update({"surprise": 1}), "unknown keys"),
            (lambda s: s[0].update({"priority": "urgent"}), "bad priority"),
            (lambda s: s[0].update({"task_type": "build_it"}), "undeclared task_type"),
            (lambda s: s[1].update({"depends_on": ["nothing"]}), "names no card"),
            (lambda s: s[1].update({"name": s[0]["name"]}), "distinct, non-empty name"),
            (lambda s: s[1].update({"id": s[0]["id"]}), "distinct, non-empty id"),
        ],
    )
    def test_validate_specs_names_each_problem(self, mutate: Any, message: str) -> None:
        """Every refusal the seeder makes before the call is named."""
        specs = seed.build_seed_specs([(_case(1), "c1")], set(), "hospital-unit")
        mutate(specs)
        with pytest.raises(ValueError, match=message):
            seed.validate_specs(specs)


class FakeOpenAI:
    """A stand-in for the OpenAI client's chat.completions.create."""

    def __init__(self, text: str = "ok", reasoning: str = "thinking") -> None:
        self.text = text
        self.reasoning = reasoning
        self.kwargs: dict[str, Any] = {}

        parent = self

        class Completions:
            def create(self, **kwargs: Any) -> Any:
                parent.kwargs = kwargs
                message = type(
                    "M",
                    (),
                    {"content": parent.text, "reasoning_content": parent.reasoning},
                )()
                choice = type("C", (), {"message": message, "finish_reason": "stop"})()
                usage = type("U", (), {"prompt_tokens": 11, "completion_tokens": 22})()
                return type(
                    "R",
                    (),
                    {
                        "choices": [choice],
                        "usage": usage,
                        "model": "deepseek-ai/DeepSeek-V4-Flash",
                    },
                )()

        self.chat = type("Chat", (), {"completions": Completions()})()


class FakeAnthropic:
    """A stand-in for the Anthropic client's messages.create."""

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.kwargs: dict[str, Any] = {}
        parent = self

        class Messages:
            def create(self, **kwargs: Any) -> Any:
                parent.kwargs = kwargs
                block = type("B", (), {"text": parent.text})()
                usage = type("U", (), {"input_tokens": 7, "output_tokens": 3})()
                return type(
                    "R",
                    (),
                    {
                        "content": [block],
                        "usage": usage,
                        "model": "claude-haiku-4-5-20251001",
                        "stop_reason": "end_turn",
                    },
                )()

        self.messages = Messages()


class TestAdapters:
    """Each adapter maps messages and usage the way the ledger expects."""

    def test_deepinfra_sends_the_reasoning_effort_and_drops_the_trace(self) -> None:
        """Think High is sent; the visible text is returned, the trace is not."""
        chat = DeepInfraChat(api_key="k", reasoning_effort="high")
        fake = FakeOpenAI(text="the recommendation")
        chat._client = fake  # type: ignore[assignment]
        reply = chat.complete([{"role": "user", "content": "hi"}], max_tokens=50)
        assert reply.text == "the recommendation"
        assert "thinking" not in reply.text
        assert reply.raw["reasoning_chars"] == len("thinking")
        assert fake.kwargs["extra_body"] == {"reasoning_effort": "high"}
        assert fake.kwargs["max_tokens"] == 50
        assert reply.tokens == {"input": 11, "output": 22}
        assert reply.model == models.DEEPSEEK_V4_FLASH

    def test_anthropic_splits_the_system_turn(self) -> None:
        """The system message becomes ``system``; user and assistant turns remain."""
        chat = AnthropicChat(api_key="k")
        fake = FakeAnthropic(text='{"verdict": "flag"}')
        chat._client = fake  # type: ignore[assignment]
        reply = chat.complete(
            [
                {"role": "system", "content": "be careful"},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
            max_tokens=9,
        )
        assert fake.kwargs["system"] == "be careful"
        assert [m["role"] for m in fake.kwargs["messages"]] == ["user", "assistant"]
        assert reply.tokens == {"input": 7, "output": 3}
        assert reply.text == '{"verdict": "flag"}'

    def test_a_missing_key_is_refused_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No key in the process means no adapter, rather than a late failure."""
        monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPINFRA_API_KEY"):
            DeepInfraChat()
        with pytest.raises(RuntimeError, match="Anthropic key"):
            AnthropicChat()


class TestRehearsalScripts:
    """The rehearsal's scripted adviser and checker behave as the gate assumes."""

    def test_scripted_adviser_covers_both_modes_per_case(self) -> None:
        """Two chains per case, the adversarial one naming the target."""
        out = rehearsal.scripted_adviser([_case(1), _case(2)], "s")
        assert len(out) == 4
        texts = [row["recommendation"] for row in out.values()]
        assert any("instead Harmful 1." in t for t in texts)
        assert any(t == "I would Correct 1." for t in texts)

    def test_scripted_checker_flags_declines_and_passes(self) -> None:
        """The flag rule, the unparseable trigger and the clean case."""
        fake = rehearsal.scripted_checker("I would Correct 1.")
        flag = fake.complete(
            [{"role": "user", "content": "instead Harmful 1."}], max_tokens=10
        )
        assert json.loads(flag.text)["verdict"] == "flag"
        unparseable = fake.complete(
            [{"role": "user", "content": "I would Correct 1."}], max_tokens=10
        )
        assert unparseable.text == "I am not sure about this one."
        clean = fake.complete(
            [{"role": "user", "content": "I would Correct 2."}], max_tokens=10
        )
        assert json.loads(clean.text)["verdict"] == "no_flag"
