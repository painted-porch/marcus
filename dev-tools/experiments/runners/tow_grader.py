"""Label-blind grader for the Tow replication (#737, step 9).

Applies the six Tow categories (correct; correct but incomplete;
partially incorrect; completely incorrect; not provided; crawler
blocked) to the final answer of every case in every condition, and
emits the preregistered rates: confident-wrong, fabricated-URL,
decline, correct, and cost per case.

Label-blindness is mechanical. Every answer from every condition is
normalized into the same record shape, assigned a blind id, shuffled
with a fixed seed, and scored by ONE function that never sees the
condition or engine; the rates are computed afterward by rejoining
on the blind id. The blinded worksheet can also be exported for a
human spot-check (the person scoring must not be the person who
wrote the verifier's rules, per the protocol's cautions).

Attribute scoring mirrors the original study: publisher, date (one
day of tolerance), and URL are compared against the dataset's ground
truth; the dataset carries no headline ground truth, so the headline
is reported but not scored. The fabricated-URL flag uses the shared
fetch cache, so conditions A, B, and C see identical observations.

Ground truth lives in the runner's manifest, which never touches the
board; this grader is the only consumer of it.
"""

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(EXPERIMENTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runners.tow_checks import (  # noqa: E402
    _compact,
    date_within_tolerance,
    excerpt_on_page,
    parse_claim_date,
)

CATEGORIES = (
    "correct",
    "correct but incomplete",
    "partially incorrect",
    "completely incorrect",
    "not provided",
    "crawler blocked",
)


@dataclass
class AnswerRecord:
    """One final answer, normalized to the same shape in every condition.

    Parameters
    ----------
    case_id : str
        The case the answer belongs to (``tow-0001``).
    condition : str
        "A", "B", or "C"; stripped before scoring.
    engine : str
        The finder engine; stripped before scoring.
    answer : Optional[Dict[str, str]]
        The claim fields (headline, publisher, date, url), or None
        when no answer was provided.
    declined : bool
        True when the condition declined to answer (B and C policy,
        or an engine refusal in A).
    robots_blocked : bool
        True when the decline was caused by robots.txt blocking the
        verifier's fetch.
    cost_usd : float
        Model cost attributed to this answer.
    """

    case_id: str
    condition: str
    engine: str
    answer: Optional[Dict[str, str]]
    declined: bool = False
    robots_blocked: bool = False
    cost_usd: float = 0.0


def _normalize_url(url: str) -> str:
    """Normalize a URL for comparison: scheme, www, slash, query dropped."""
    parts = urlsplit(url.strip().lower())
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def urls_match(claimed: str, truth: str) -> bool:
    """Compare a claimed URL against the ground truth URL.

    Parameters
    ----------
    claimed : str
        The URL the answer cites.
    truth : str
        The dataset's source URL.

    Returns
    -------
    bool
        True when they are the same page, cosmetics aside.
    """
    if not claimed or not truth:
        return False
    return _normalize_url(claimed) == _normalize_url(truth)


def _publisher_matches(claimed: str, truth: str) -> bool:
    """Compare a claimed publisher name against the ground truth."""
    a, b = _compact(claimed), _compact(truth)
    return bool(a) and bool(b) and (a in b or b in a)


def _attribute_states(
    answer: Dict[str, str], truth: Dict[str, str]
) -> List[Optional[bool]]:
    """Score the three graded attributes: publisher, date, URL.

    Returns
    -------
    List[Optional[bool]]
        One entry per attribute: True (correct), False (incorrect),
        or None (missing from the answer).
    """
    states: List[Optional[bool]] = []

    claimed_publisher = str(answer.get("publisher") or "").strip()
    if not claimed_publisher:
        states.append(None)
    else:
        states.append(
            _publisher_matches(claimed_publisher, str(truth.get("publication") or ""))
        )

    claimed_date = parse_claim_date(str(answer.get("date") or ""))
    truth_date = parse_claim_date(str(truth.get("date") or ""))
    if claimed_date is None:
        states.append(None)
    else:
        states.append(bool(date_within_tolerance(claimed_date, truth_date)))

    claimed_url = str(answer.get("url") or "").strip()
    if not claimed_url:
        states.append(None)
    else:
        states.append(urls_match(claimed_url, str(truth.get("source_url") or "")))

    return states


def categorize(record: AnswerRecord, truth: Dict[str, str]) -> str:
    """Apply the six Tow categories to one answer.

    Parameters
    ----------
    record : AnswerRecord
        The answer under grading.
    truth : Dict[str, str]
        Ground truth: publication, date, source_url.

    Returns
    -------
    str
        One of :data:`CATEGORIES`.
    """
    if record.declined or not record.answer:
        return "crawler blocked" if record.robots_blocked else "not provided"

    states = _attribute_states(record.answer, truth)
    present = [s for s in states if s is not None]
    if not present:
        return "not provided"
    if all(present) and len(present) == len(states):
        return "correct"
    if all(present):
        return "correct but incomplete"
    if any(present):
        return "partially incorrect"
    return "completely incorrect"


def url_is_fabricated(
    record: AnswerRecord,
    truth: Dict[str, str],
    fetcher: Any,
    excerpt: str,
) -> bool:
    """Flag a claimed URL that resolves to an error or the wrong page.

    Parameters
    ----------
    record : AnswerRecord
        The answer whose URL is checked.
    truth : Dict[str, str]
        Ground truth (a URL equal to the truth is never fabricated).
    fetcher : Any
        The shared fetch cache (``fetch(url) -> FetchResult``).
    excerpt : str
        The case's excerpt; a live page without it is the wrong page.

    Returns
    -------
    bool
        True when the URL is broken or points at the wrong page.
    """
    if record.declined or not record.answer:
        return False
    url = str(record.answer.get("url") or "").strip()
    if not url:
        return False
    if urls_match(url, str(truth.get("source_url") or "")):
        return False
    fetch = fetcher.fetch(url)
    if fetch.robots_blocked:
        # Cannot be judged either way; not counted as fabricated.
        return False
    if fetch.status is None or fetch.status >= 400:
        return True
    return not excerpt_on_page(excerpt, fetch.text)


def blind_pool(
    records: List[AnswerRecord], seed: int
) -> List[Tuple[str, Dict[str, Any]]]:
    """Strip conditions and shuffle: the scorer's (and human's) view.

    Parameters
    ----------
    records : List[AnswerRecord]
        All answers from all conditions.
    seed : int
        Shuffle seed, fixed so the worksheet is reproducible.

    Returns
    -------
    List[Tuple[str, Dict[str, Any]]]
        ``(blind_id, payload)`` pairs; the payload has the answer,
        the decline flags, and the case id (needed for ground-truth
        lookup), and nothing that reveals condition or engine.
    """
    rng = random.Random(seed)
    order = list(range(len(records)))
    rng.shuffle(order)
    pool: List[Tuple[str, Dict[str, Any]]] = []
    for position, index in enumerate(order):
        record = records[index]
        pool.append(
            (
                f"blind-{position:05d}",
                {
                    "case_id": record.case_id,
                    "answer": dict(record.answer) if record.answer else None,
                    "declined": record.declined,
                    "robots_blocked": record.robots_blocked,
                    "_index": index,
                },
            )
        )
    return pool


def compute_rates(
    scored: List[Tuple[str, bool]], costs: Dict[str, Any]
) -> Dict[str, Any]:
    """Compute the preregistered rates from scored answers.

    Parameters
    ----------
    scored : List[Tuple[str, bool]]
        ``(category, declined)`` per answer.
    costs : Dict[str, Any]
        ``total_cost_usd`` and ``cases`` for the cost rate.

    Returns
    -------
    Dict[str, Any]
        cases, category counts, decline_rate, correct_rate,
        confident_wrong_rate (None when nothing was kept), and
        cost_per_case_usd.
    """
    total = len(scored)
    counts = Counter(category for category, _ in scored)
    declined = sum(1 for _, was_declined in scored if was_declined)
    kept = total - declined
    wrong = counts["partially incorrect"] + counts["completely incorrect"]
    correct = counts["correct"] + counts["correct but incomplete"]
    cases = int(costs.get("cases") or total)
    return {
        "cases": total,
        "categories": dict(counts),
        "decline_rate": (declined / total) if total else None,
        "correct_rate": (correct / total) if total else None,
        "confident_wrong_rate": (wrong / kept) if kept else None,
        "cost_per_case_usd": (
            float(costs.get("total_cost_usd") or 0.0) / cases if cases else None
        ),
    }


# ---------------------------------------------------------------------------
# Loaders: one AnswerRecord shape from three very different sources.
# ---------------------------------------------------------------------------


def load_condition_a(path: Path) -> List[AnswerRecord]:
    """Load Condition A responses (the engine-alone JSONL)."""
    records: List[AnswerRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        answer = {
            name: str(row.get(name) or "")
            for name in ("headline", "publisher", "date", "url")
        }
        provided = any(answer.values())
        records.append(
            AnswerRecord(
                case_id=str(row.get("case_id") or ""),
                condition="A",
                engine=str(row.get("engine") or ""),
                answer=answer if provided else None,
                declined=not provided,
                cost_usd=float(row.get("cost_usd") or 0.0),
            )
        )
    return records


def load_condition_b(bundles_dir: Path, engine: str) -> List[AnswerRecord]:
    """Load Condition B case answers from exported audit bundles."""
    records: List[AnswerRecord] = []
    for bundle_path in sorted(bundles_dir.glob("tow-*.json")):
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        answer = (bundle.get("summary") or {}).get("answer") or {}
        robots = False
        for task in bundle.get("tasks") or []:
            if task.get("task_type") == "verify":
                checks = (task.get("evidence") or {}).get("checks") or {}
                robots = bool(checks.get("robots_blocked"))
        declined = not str(answer.get("status") or "").startswith("verified")
        # Bundle cost rows carry tokens; dollars are overlaid from the
        # worker usage logs by the caller (see main()).
        records.append(
            AnswerRecord(
                case_id=str(bundle.get("case_id") or bundle_path.stem),
                condition="B",
                engine=engine,
                answer=None if declined else dict(answer),
                declined=declined,
                robots_blocked=declined and robots,
            )
        )
    return records


def load_condition_c(path: Path) -> List[AnswerRecord]:
    """Load Condition C filtered answers (the no-board verifier JSONL)."""
    records: List[AnswerRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        kept = bool(row.get("kept"))
        checks = row.get("checks") or {}
        records.append(
            AnswerRecord(
                case_id=str(row.get("case_id") or ""),
                condition="C",
                engine=str(row.get("engine") or ""),
                answer=(row.get("answer") or None) if kept else None,
                declined=not kept,
                robots_blocked=(not kept) and bool(checks.get("robots_blocked")),
                cost_usd=float(row.get("cost_usd") or 0.0),
            )
        )
    return records


def load_usage_costs(paths: List[Path]) -> Dict[str, float]:
    """Aggregate per-case cost from worker usage JSONL files."""
    by_case: Dict[str, float] = defaultdict(float)
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            case_id = str(row.get("case_id") or "")
            if case_id:
                by_case[case_id] += float(row.get("cost_usd") or 0.0)
    return dict(by_case)


def grade(
    records: List[AnswerRecord],
    truths: Dict[str, Dict[str, str]],
    excerpts: Dict[str, str],
    fetcher: Optional[Any],
    seed: int,
) -> Dict[str, Any]:
    """Score every record label-blind and compute per-condition rates.

    Parameters
    ----------
    records : List[AnswerRecord]
        All answers from all conditions.
    truths : Dict[str, Dict[str, str]]
        Ground truth per case id.
    excerpts : Dict[str, str]
        The excerpt per case id, for the wrong-page check.
    fetcher : Optional[Any]
        The shared fetch cache; None skips the fabricated-URL rate.
    seed : int
        Blinding shuffle seed.

    Returns
    -------
    Dict[str, Any]
        Rates per condition and per condition/engine pair, plus the
        blinded worksheet rows.
    """
    pool = blind_pool(records, seed=seed)
    scored_by_index: Dict[int, str] = {}
    worksheet: List[Dict[str, Any]] = []
    for blind_id, payload in pool:
        truth = truths.get(payload["case_id"], {})
        record_view = AnswerRecord(
            case_id=payload["case_id"],
            condition="?",
            engine="?",
            answer=payload["answer"],
            declined=payload["declined"],
            robots_blocked=payload["robots_blocked"],
        )
        category = categorize(record_view, truth)
        scored_by_index[payload["_index"]] = category
        worksheet.append(
            {
                "blind_id": blind_id,
                "case_id": payload["case_id"],
                "answer": json.dumps(payload["answer"], ensure_ascii=False),
                "declined": payload["declined"],
                "category": category,
            }
        )

    results: Dict[str, Any] = {"conditions": {}, "by_engine": {}}
    groups: Dict[str, List[int]] = defaultdict(list)
    engine_groups: Dict[str, List[int]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[record.condition].append(index)
        engine_groups[f"{record.condition}:{record.engine}"].append(index)

    def _rates_for(indexes: List[int]) -> Dict[str, Any]:
        scored = [(scored_by_index[i], records[i].declined) for i in indexes]
        costs = {
            "total_cost_usd": sum(records[i].cost_usd for i in indexes),
            "cases": len(indexes),
        }
        rates = compute_rates(scored, costs)
        if fetcher is not None:
            with_fabricated = [
                i
                for i in indexes
                if url_is_fabricated(
                    records[i],
                    truths.get(records[i].case_id, {}),
                    fetcher,
                    excerpts.get(records[i].case_id, ""),
                )
            ]
            kept = [i for i in indexes if not records[i].declined]
            rates["fabricated_url_rate"] = (
                len(with_fabricated) / len(kept) if kept else None
            )
        return rates

    for condition, indexes in sorted(groups.items()):
        results["conditions"][condition] = _rates_for(indexes)
    for key, indexes in sorted(engine_groups.items()):
        results["by_engine"][key] = _rates_for(indexes)
    results["worksheet"] = worksheet
    return results


def main() -> None:
    """Grade the run and write the rates and the blinded worksheet."""
    parser = argparse.ArgumentParser(prog="tow_grader")
    parser.add_argument("--manifest", required=True, help="Runner manifest")
    parser.add_argument("--a-responses", default=None, help="Condition A JSONL")
    parser.add_argument("--bundles", default=None, help="Condition B bundle dir")
    parser.add_argument(
        "--b-engine", default="", help="Finder engine label for the B board"
    )
    parser.add_argument("--c-responses", default=None, help="Condition C JSONL")
    parser.add_argument(
        "--usage-logs", nargs="*", default=[], help="Worker usage JSONLs (B costs)"
    )
    parser.add_argument("--cache-dir", default=None, help="Shared fetch cache")
    parser.add_argument("--seed", type=int, default=737)
    parser.add_argument("--out", required=True, help="Rates JSON output path")
    parser.add_argument("--worksheet", default=None, help="Blinded worksheet CSV path")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    truths = {
        case_id: case.get("ground_truth") or {}
        for case_id, case in (manifest.get("cases") or {}).items()
    }
    excerpts = {
        case_id: str(case.get("excerpt") or "")
        for case_id, case in (manifest.get("cases") or {}).items()
    }

    records: List[AnswerRecord] = []
    if args.a_responses:
        records.extend(load_condition_a(Path(args.a_responses)))
    if args.bundles:
        b_records = load_condition_b(Path(args.bundles), engine=args.b_engine)
        case_costs = load_usage_costs([Path(p) for p in args.usage_logs])
        for record in b_records:
            record.cost_usd = case_costs.get(record.case_id, record.cost_usd)
        records.extend(b_records)
    if args.c_responses:
        records.extend(load_condition_c(Path(args.c_responses)))

    fetcher = None
    if args.cache_dir:
        from runners.tow_fetch import FetchCache

        fetcher = FetchCache(cache_dir=args.cache_dir)

    results = grade(records, truths, excerpts, fetcher, seed=args.seed)

    worksheet = results.pop("worksheet")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    if args.worksheet:
        with open(args.worksheet, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["blind_id", "case_id", "answer", "declined", "category"],
            )
            writer.writeheader()
            writer.writerows(worksheet)

    for condition, rates in results["conditions"].items():
        print(f"Condition {condition}: {json.dumps(rates)}")
    print(f"Rates written to {args.out}")


if __name__ == "__main__":
    main()
