"""End-to-end rehearsal of the system-of-record board (issue #737).

The hand-seeded three-task case from the issue's "How to verify it
works" section, run for real against a live Marcus server on SQLite:
a find task, a dependent verify task, a dependent approve task, and a
dependent synthesize task. The script asserts every enforcement the
patch added: evidence required to close with missing fields named,
the claim handoff with ``raw`` stripped, the author and same-vendor
refusals (visible in the server log), the human-only approval through
the real approvals CLI, and the dependency hold on the synthesize
task.

Two subcommands:

``seed``
    Create the four tasks directly on a SQLite board file, wired with
    dependencies, typed with ``task_type``, ``inputs``, and
    ``output_schema``. No decomposer, no planner model.

``drive``
    Register the four principals over HTTP MCP and walk the case end
    to end, printing one PASS/FAIL line per check and exiting nonzero
    on any failure.

Usage::

    python dev-tools/experiments/runners/rehearse_system_of_record.py \
        seed --db /tmp/rehearsal/kanban.db --project sor-rehearsal
    python dev-tools/experiments/runners/rehearse_system_of_record.py \
        drive --url http://localhost:4777/mcp \
        --db /tmp/rehearsal/kanban.db --project sor-rehearsal
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.core.models import TaskStatus  # noqa: E402
from src.integrations.providers.sqlite_kanban import SQLiteKanban  # noqa: E402
from src.worker.inspector import Inspector  # noqa: E402

EXCERPT = (
    "Collectively, they provided incorrect answers to more than "
    "60 percent of queries."
)

FIND_EVIDENCE = {
    "headline": "AI Search Has a Citation Problem",
    "publisher": "Columbia Journalism Review",
    "date": "2025-03-06",
    "url": (
        "https://www.cjr.org/tow_center/"
        "we-compared-eight-ai-search-engines-theyre-all-bad-at-citing-news.php"
    ),
    "raw": "FULL ENGINE RESPONSE WITH REASONING -- must never reach the verifier",
}

VERIFY_EVIDENCE = {
    "verdict": "verified",
    "checks": {"excerpt_found": True, "domain_match": True, "date_match": True},
    "fetched_at": "2026-10-04T00:00:00Z",
    "http_status": 200,
}

SYNTH_EVIDENCE = {
    "answer": {
        "headline": FIND_EVIDENCE["headline"],
        "publisher": FIND_EVIDENCE["publisher"],
        "date": FIND_EVIDENCE["date"],
        "url": FIND_EVIDENCE["url"],
        "status": "verified/approved",
    }
}


class CheckFailure(Exception):
    """One rehearsal check failed; the message says which and why."""


class Checker:
    """Accumulate PASS/FAIL lines and fail loudly at the end."""

    def __init__(self) -> None:
        self.failures: List[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        """Record one check result and print it.

        Parameters
        ----------
        name : str
            Short label for the check.
        condition : bool
            True when the check passed.
        detail : str
            Shown on failure to explain what was seen.
        """
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            self.failures.append(name)


async def seed(db_path: str, project_id: str) -> Dict[str, str]:
    """Seed the four-task case onto a SQLite board.

    Parameters
    ----------
    db_path : str
        Path to the SQLite board file (created if absent).
    project_id : str
        Project id every task carries; agents register with the same
        id so GH-388 scoping admits them.

    Returns
    -------
    Dict[str, str]
        Task ids keyed by role: find, verify, approve, synthesize.
    """
    board = SQLiteKanban(
        {
            "db_path": db_path,
            "project_name": "SOR Rehearsal",
            "attachments_dir": str(Path(db_path).parent / "attachments"),
        }
    )
    await board.connect()

    find = await board.create_task(
        {
            "name": "Locate the source of excerpt tow-0001",
            "description": (
                "Identify the headline, publisher, publication date, and "
                f"URL of this excerpt: {EXCERPT!r}"
            ),
            "priority": "high",
            "estimated_hours": 0.1,
            "labels": ["tow"],
            "project_id": project_id,
            "task_type": "find",
            "inputs": {"excerpt": EXCERPT},
            "output_schema": {
                "type": "object",
                "required": ["headline", "publisher", "date", "url"],
            },
        }
    )
    verify = await board.create_task(
        {
            "name": "Cross-check the claim for excerpt tow-0001",
            "description": (
                "Fetch the claimed URL, confirm the excerpt appears, and "
                "confirm publisher and date. Report verdict and checks."
            ),
            "priority": "high",
            "estimated_hours": 0.1,
            "labels": ["tow"],
            "project_id": project_id,
            "dependencies": [find.id],
            "task_type": "verify",
            "inputs": {"excerpt": EXCERPT},
            "output_schema": {
                "type": "object",
                "required": ["verdict", "checks"],
            },
        }
    )
    approve = await board.create_task(
        {
            "name": "Approve the verified claim for excerpt tow-0001",
            "description": (
                "A person reviews the claim and the verdict side by side "
                "and approves or rejects."
            ),
            "priority": "high",
            "estimated_hours": 0.1,
            "labels": ["tow"],
            "project_id": project_id,
            "dependencies": [verify.id],
            "task_type": "approve",
        }
    )
    synthesize = await board.create_task(
        {
            "name": "Render the case answer for excerpt tow-0001",
            "description": (
                "Assemble the case answer from the approved claim with "
                "claim-level provenance."
            ),
            "priority": "high",
            "estimated_hours": 0.1,
            "labels": ["tow"],
            "project_id": project_id,
            "dependencies": [approve.id],
            "task_type": "synthesize",
            "output_schema": {"type": "object", "required": ["answer"]},
        }
    )
    await board.disconnect()

    ids = {
        "find": find.id,
        "verify": verify.id,
        "approve": approve.id,
        "synthesize": synthesize.id,
    }
    print(json.dumps({"db": db_path, "project": project_id, "tasks": ids}))
    return ids


async def _call_tool(
    client: Inspector, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Call one MCP tool and parse its JSON text payload.

    Parameters
    ----------
    client : Inspector
        Connected Inspector client.
    tool : str
        Tool name.
    arguments : Dict[str, Any]
        Tool arguments.

    Returns
    -------
    Dict[str, Any]
        Parsed response payload.
    """
    if client.session is None:
        raise RuntimeError("Not connected to Marcus")
    result = await client.session.call_tool(tool, arguments=arguments)
    from src.worker.inspector import _extract_text_from_result

    text = _extract_text_from_result(result)
    parsed = json.loads(text) if text else {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


async def drive(url: str, db_path: str, project_id: str) -> int:
    """Walk the seeded case end to end against a live server.

    Parameters
    ----------
    url : str
        Marcus HTTP MCP endpoint.
    db_path : str
        The seeded board file, re-read at the end to assert the
        durable record.
    project_id : str
        Project id used at registration.

    Returns
    -------
    int
        0 when every check passed, 1 otherwise.
    """
    c = Checker()
    client = Inspector(connection_type="http")
    async with client.connect(url=url):
        print("== Registration ==")
        for agent_id, vendor, principal in (
            ("finder-a", "perplexity", "agent"),
            ("verifier-a", "perplexity", "agent"),
            ("verifier-b", "google", "agent"),
        ):
            reg = await client.register_agent(
                agent_id=agent_id,
                name=agent_id,
                role="worker",
                skills=["tow"],
                project_id=project_id,
                vendor=vendor,
                principal=principal,
            )
            c.check(f"register {agent_id} ({vendor})", reg.get("success") is True)

        print("== Find ==")
        offer = await client.request_next_task("finder-a")
        task = offer.get("task") or {}
        find_id = task.get("id", "")
        c.check(
            "finder-a offered the find task",
            bool(find_id) and "Locate" in task.get("name", ""),
            f"got: {task.get('name')!r}",
        )

        no_evidence = await client.report_task_progress(
            agent_id="finder-a",
            task_id=find_id,
            status="completed",
            progress=100,
            message="done (no evidence attached)",
        )
        c.check(
            "completion WITHOUT evidence refused",
            no_evidence.get("success") is False
            and no_evidence.get("error") == "typed_evidence_missing",
            f"got: {json.dumps(no_evidence)[:200]}",
        )
        missing = set(no_evidence.get("missing_fields") or [])
        c.check(
            "rejection names the four missing fields",
            missing == {"headline", "publisher", "date", "url"},
            f"got: {sorted(missing)}",
        )

        accepted = await client.report_task_progress(
            agent_id="finder-a",
            task_id=find_id,
            status="completed",
            progress=100,
            message="claim found",
            evidence=FIND_EVIDENCE,
        )
        c.check(
            "completion WITH evidence accepted",
            accepted.get("success") is True,
            f"got: {json.dumps(accepted)[:200]}",
        )

        print("== Eligibility at the verify task ==")
        author_offer = await client.request_next_task("finder-a")
        c.check(
            "author gets plain no-task (silent refusal)",
            not author_offer.get("task"),
            f"got: {json.dumps(author_offer)[:200]}",
        )
        same_vendor_offer = await client.request_next_task("verifier-a")
        c.check(
            "same-vendor verifier gets plain no-task",
            not same_vendor_offer.get("task"),
            f"got: {json.dumps(same_vendor_offer)[:200]}",
        )
        offer_b = await client.request_next_task("verifier-b")
        verify_task = offer_b.get("task") or {}
        verify_id = verify_task.get("id", "")
        c.check(
            "different-vendor verifier offered the verify task",
            bool(verify_id) and "Cross-check" in verify_task.get("name", ""),
            f"got: {verify_task.get('name')!r}",
        )
        c.check(
            "assignment carries task_type and the handed-off inputs",
            verify_task.get("task_type") == "verify"
            and (verify_task.get("inputs") or {}).get("url") == FIND_EVIDENCE["url"],
            f"got task_type={verify_task.get('task_type')!r} "
            f"inputs keys={sorted((verify_task.get('inputs') or {}).keys())}",
        )

        context = await _call_tool(client, "get_task_context", {"task_id": verify_id})
        context_text = json.dumps(context)
        inner = context.get("context") or {}
        c.check(
            "verify context carries the claim fields",
            (inner.get("inputs") or {}).get("url") == FIND_EVIDENCE["url"],
            f"got: {context_text[:200]}",
        )
        c.check(
            "verify context contains no raw anywhere",
            "raw" not in context_text
            and "must never reach the verifier" not in context_text,
            "raw leaked into the verifier's view",
        )

        verified = await client.report_task_progress(
            agent_id="verifier-b",
            task_id=verify_id,
            status="completed",
            progress=100,
            message="verdict: verified",
            evidence=VERIFY_EVIDENCE,
        )
        c.check(
            "verify completion with verdict accepted",
            verified.get("success") is True,
            f"got: {json.dumps(verified)[:200]}",
        )

        print("== Approval ==")
        agent_approve_offer = await client.request_next_task("verifier-b")
        c.check(
            "agent is NOT offered the approve task",
            not agent_approve_offer.get("task"),
            f"got: {json.dumps(agent_approve_offer)[:200]}",
        )

        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli.approvals",
                "--url",
                url,
                "--project",
                project_id,
                "--reviewer",
                "reviewer",
                "--decide",
                "approved",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        c.check(
            "approvals CLI approved as a human principal",
            cli.returncode == 0 and "Recorded: approved" in cli.stdout,
            f"rc={cli.returncode} out={cli.stdout[-200:]} err={cli.stderr[-200:]}",
        )

        print("== Synthesize ==")
        synth_offer = await client.request_next_task("verifier-b")
        synth_task = synth_offer.get("task") or {}
        synth_id = synth_task.get("id", "")
        c.check(
            "synthesize offered only after the approval closed",
            bool(synth_id) and "Render" in synth_task.get("name", ""),
            f"got: {synth_task.get('name')!r}",
        )
        synth_done = await client.report_task_progress(
            agent_id="verifier-b",
            task_id=synth_id,
            status="completed",
            progress=100,
            message="case answer rendered",
            evidence=SYNTH_EVIDENCE,
        )
        c.check(
            "synthesize completion accepted",
            synth_done.get("success") is True,
            f"got: {json.dumps(synth_done)[:200]}",
        )

    print("== Durable record ==")
    board = SQLiteKanban(
        {
            "db_path": db_path,
            "project_name": "SOR Rehearsal",
            "attachments_dir": str(Path(db_path).parent / "attachments"),
        }
    )
    await board.connect()
    tasks = {t.task_type: t for t in await board.get_all_tasks()}
    await board.disconnect()

    c.check(
        "all four tasks DONE on the board",
        all(
            tasks[k].status == TaskStatus.DONE
            for k in ("find", "verify", "approve", "synthesize")
        ),
        f"got: {dict((k, t.status.value) for k, t in tasks.items())}",
    )
    find_record = (tasks["find"].source_context or {}).get("evidence") or {}
    c.check(
        "find evidence kept on the record WITH raw (for the audit bundle)",
        find_record.get("raw") == FIND_EVIDENCE["raw"],
        f"got: {json.dumps(find_record)[:150]}",
    )
    verify_inputs = tasks["verify"].inputs or {}
    c.check(
        "verify inputs hold the claim minus raw (the handoff)",
        verify_inputs.get("url") == FIND_EVIDENCE["url"] and "raw" not in verify_inputs,
        f"got: {json.dumps(verify_inputs)[:150]}",
    )
    approve_record = (tasks["approve"].source_context or {}).get("evidence") or {}
    c.check(
        "approval decision recorded with the reviewer's name",
        approve_record.get("decision") == "approved"
        and approve_record.get("approved_by") == "reviewer",
        f"got: {json.dumps(approve_record)[:150]}",
    )

    if c.failures:
        print(f"\n{len(c.failures)} CHECK(S) FAILED: {', '.join(c.failures)}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


def main() -> None:
    """Parse arguments and run the chosen subcommand."""
    parser = argparse.ArgumentParser(prog="rehearse_system_of_record")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_p = sub.add_parser("seed", help="Seed the four-task case")
    seed_p.add_argument("--db", required=True, help="SQLite board file")
    seed_p.add_argument("--project", default="sor-rehearsal")

    drive_p = sub.add_parser("drive", help="Drive the case end to end")
    drive_p.add_argument("--url", default="http://localhost:4777/mcp")
    drive_p.add_argument("--db", required=True, help="SQLite board file")
    drive_p.add_argument("--project", default="sor-rehearsal")

    args = parser.parse_args()
    if args.command == "seed":
        asyncio.run(seed(args.db, args.project))
        return
    sys.exit(asyncio.run(drive(args.url, args.db, args.project)))


if __name__ == "__main__":
    main()
