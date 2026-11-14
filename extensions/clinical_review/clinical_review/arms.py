"""
The off-board steps: generation, the A0 judge, arm A1 and arm C.

Each step reads and writes JSONL under the run record and sees no label: the
rows carry the opaque chain id, never the mode. Only the judge step records
which letter each proposition was shown under, beside the judgement, so the
grader can score it; the judge model itself never sees that mapping.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .cases import Case
from .models import Metered
from .prompts import (
    build_adviser_messages,
    build_checker_messages,
    build_in_conversation_messages,
    build_judge_messages,
    parse_judgement,
    parse_verdict,
)

ADVISER_MAX_TOKENS = 3072
CHECK_MAX_TOKENS = 600
JUDGE_MAX_TOKENS = 300


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """
    Read a JSONL file, or nothing when it does not exist.

    Parameters
    ----------
    path : Path
        The file.

    Returns
    -------
    List[Dict[str, Any]]
    """
    if not Path(path).is_file():
        return []
    return [
        json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()
    ]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append one row, flushed at once."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def generate(
    chains: Iterable[tuple[Case, str, str]],
    model: Metered,
    out: Path,
) -> list[dict[str, Any]]:
    """
    Generate one recommendation per chain, resuming past rows already written.

    Parameters
    ----------
    chains : Iterable[Tuple[Case, str, str]]
        ``(case, chain_id, mode)`` triples; the mode picks the instruction and
        is NOT written to the output.
    model : Metered
        The adviser model behind the ledger.
    out : Path
        ``generation.jsonl``.

    Returns
    -------
    List[Dict[str, Any]]
        Every row, old and new: chain_id, case_id, messages, recommendation,
        model, tokens.
    """
    rows = read_jsonl(out)
    done = {r["chain_id"] for r in rows}
    for case, chain, mode in chains:
        if chain in done:
            continue
        messages = build_adviser_messages(case, mode)
        reply = model.complete(
            messages,
            max_tokens=ADVISER_MAX_TOKENS,
            extra={"step": "generate", "chain": chain},
        )
        row = {
            "chain_id": chain,
            "case_id": case.case_id,
            "messages": messages,
            "recommendation": reply.text,
            "model": reply.model,
            "tokens": reply.tokens,
            "raw": reply.raw,
        }
        append_jsonl(out, row)
        rows.append(row)
    return rows


def judge(
    rows: list[dict[str, Any]],
    cases_by_id: dict[str, Case],
    model: Metered,
    out: Path,
    seed: int,
) -> list[dict[str, Any]]:
    """
    Arm A0: classify each recommendation's explicit advocacy, label-blind.

    The correct management and the harmful target are shown as Reference A and
    Reference B in an order drawn per chain from ``seed``. The row records the
    ratings by letter and the letter-to-role mapping; the model saw only the
    letters.

    Parameters
    ----------
    rows : List[Dict[str, Any]]
        Generation rows.
    cases_by_id : Dict[str, Case]
        The cases.
    model : Metered
        The judge model behind the ledger.
    out : Path
        ``judge.jsonl``.
    seed : int
        The RNG seed.

    Returns
    -------
    List[Dict[str, Any]]
    """
    done_rows = read_jsonl(out)
    done = {r["chain_id"] for r in done_rows}
    for row in rows:
        chain = row["chain_id"]
        if chain in done:
            continue
        case = cases_by_id[row["case_id"]]
        rng = random.Random(f"{seed}:{chain}")
        harmful_is_a = rng.random() < 0.5
        prop_a, prop_b = (
            (case.harmful_target, case.correct_management)
            if harmful_is_a
            else (case.correct_management, case.harmful_target)
        )
        messages = build_judge_messages(
            case.vignette, row["recommendation"], prop_a, prop_b
        )
        reply = model.complete(
            messages,
            max_tokens=JUDGE_MAX_TOKENS,
            extra={"step": "judge", "chain": chain},
        )
        parsed = parse_judgement(reply.text)
        if parsed is None:
            reply = model.complete(
                messages,
                max_tokens=JUDGE_MAX_TOKENS,
                extra={"step": "judge", "chain": chain, "retry": 1},
            )
            parsed = parse_judgement(reply.text) or {
                "reference_a": "unclear",
                "reference_b": "unclear",
                "intervention_class": "",
            }
        harmful_letter = "reference_a" if harmful_is_a else "reference_b"
        correct_letter = "reference_b" if harmful_is_a else "reference_a"
        out_row = {
            "chain_id": chain,
            "case_id": row["case_id"],
            "ratings": parsed,
            "harmful_letter": harmful_letter,
            "advocates_harmful": parsed[harmful_letter],
            "advocates_correct": parsed[correct_letter],
            "intervention_class": parsed["intervention_class"],
            "model": reply.model,
            "raw": reply.text,
        }
        append_jsonl(out, out_row)
        done_rows.append(out_row)
    return done_rows


def check_rows(
    rows: list[dict[str, Any]],
    cases_by_id: dict[str, Case],
    model: Metered,
    out: Path,
    arm: str,
) -> list[dict[str, Any]]:
    """
    Arm A1 or C: one checker verdict per recommendation.

    Parameters
    ----------
    rows : List[Dict[str, Any]]
        Generation rows.
    cases_by_id : Dict[str, Case]
        The cases (for the sealed prompt's vignette and draft).
    model : Metered
        The checker model behind the ledger.
    out : Path
        ``arms/<arm>/verdicts.jsonl``.
    arm : str
        ``A1`` continues the adviser's own conversation with one safety
        question; ``C`` runs the sealed prompt from a fresh script.

    Returns
    -------
    List[Dict[str, Any]]
    """
    if arm not in ("A1", "C"):
        raise ValueError(f"check_rows handles A1 and C, not {arm!r}")
    done_rows = read_jsonl(out)
    done = {r["chain_id"] for r in done_rows}
    for row in rows:
        chain = row["chain_id"]
        if chain in done:
            continue
        case = cases_by_id[row["case_id"]]
        if arm == "A1":
            messages = build_in_conversation_messages(
                row["messages"], row["recommendation"]
            )
        else:
            messages = build_checker_messages(
                case.vignette, case.physician_draft, row["recommendation"]
            )
        reply = model.complete(
            messages, max_tokens=CHECK_MAX_TOKENS, extra={"arm": arm, "chain": chain}
        )
        verdict = parse_verdict(reply.text)
        if verdict["verdict"] == "indeterminate":
            reply = model.complete(
                messages,
                max_tokens=CHECK_MAX_TOKENS,
                extra={"arm": arm, "chain": chain, "retry": 1},
            )
            verdict = parse_verdict(reply.text)
        out_row = {
            "chain_id": chain,
            "case_id": row["case_id"],
            "arm": arm,
            **verdict,
            "model": reply.model,
            "tokens": reply.tokens,
            "raw": reply.text,
        }
        append_jsonl(out, out_row)
        done_rows.append(out_row)
    return done_rows
