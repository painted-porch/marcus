"""Typed numeric claims and the deterministic checker that resolves them.

Every number a report states is recorded as a claim: an identifier, a
value, a label, and enough addressing to find the truth. The checker
resolves each claim against the staged evaluation files and returns
one verdict per claim. It never calls a model.

Three claim kinds exist, because three different things can go wrong.
A ``lookup`` reads one value from one file, and fails when the report
states something the data does not say. A ``derived`` figure is
recomputed from its inputs, and fails when a ratio or a share was
asserted rather than calculated. A ``consistency`` claim asserts a
relation between figures, and fails when two tables disagree even
though each matches its own source.

Tolerance is declared on the claim, never decided by the checker. A
checker that decides for itself what "approximately" means is the
same discretion this design removes from the writing agent.
"""

from __future__ import annotations

import ast
import csv
import json
import math
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERDICT_VERIFIED = "verified"
VERDICT_FAILED = "failed"
VERDICT_UNRESOLVABLE = "unresolvable"
VERDICT_UNCOVERED = "uncovered"

_EXPONENT = r"(?:[eE][+-]?\d+)?"
_NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?" + _EXPONENT + r"|"
    r"-?\d+(?:\.\d+)?" + _EXPONENT + r"|"
    r"-?\.\d+" + _EXPONENT + r")"
    r"[xX]?(?![\w])"
)
_CLAIM_REF_RE = re.compile(r"\[(c\d+)\]")

_SAFE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_SAFE_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


@dataclass
class Claim:
    """One number a report states, with everything needed to check it.

    Parameters
    ----------
    id : str
        Identifier referenced from the report text and slide spec.
    value : Any
        The value as the report states it.
    label : str
        What the number is, used to catch a right value under a wrong
        heading.
    kind : str
        ``lookup``, ``derived`` or ``consistency``.
    source : Optional[str]
        For a lookup, the staged file holding the truth.
    key : Optional[str]
        For a lookup, the address inside that file.
    expr : Optional[str]
        For a derived or consistency claim, the arithmetic to apply.
    inputs : Dict[str, str]
        For a derived or consistency claim, the addresses its
        variables resolve to.
    tolerance : Optional[Dict[str, Any]]
        ``{"type": "abs"|"rel", "value": float}``. Absent means exact.
    """

    id: str
    value: Any
    label: str
    kind: str
    source: Optional[str] = None
    key: Optional[str] = None
    expr: Optional[str] = None
    inputs: Dict[str, str] = field(default_factory=dict)
    tolerance: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Claim":
        """Build a claim from its JSON form.

        Parameters
        ----------
        raw : Dict[str, Any]
            Decoded claim object.

        Returns
        -------
        Claim
            The parsed claim.
        """
        return cls(
            id=str(raw.get("id", "")),
            value=raw.get("value"),
            label=str(raw.get("label", "")),
            kind=str(raw.get("kind", "lookup")),
            source=raw.get("source"),
            key=raw.get("key"),
            expr=raw.get("expr"),
            inputs=dict(raw.get("inputs") or {}),
            tolerance=raw.get("tolerance"),
        )


@dataclass
class CheckResult:
    """The verdict on one claim.

    Parameters
    ----------
    claim_id : str
        Identifier of the claim, or a synthetic id for a coverage gap.
    verdict : str
        ``verified``, ``failed``, ``unresolvable`` or ``uncovered``.
    value : Any
        The value the report stated.
    expected : Any
        The value the staged data supports, when one could be resolved.
    detail : str
        Human-readable reason, carried back to the writer on a retry.
    label : str
        The claim's label, used by the slide mapping check.
    reason : str
        What kind of failure this is, when ``verdict`` is
        ``"failed"``: ``"value_mismatch"`` (the stated figure itself
        is wrong) or ``"label_mismatch"`` (the figure is correct but
        filed under the wrong name or unit). Empty for every other
        verdict. A false decline is only a correct figure wrongly
        rejected on its value; a label mismatch is the checker
        working as intended and must not be scored as one.
    """

    claim_id: str
    verdict: str
    value: Any = None
    expected: Any = None
    detail: str = ""
    label: str = ""
    reason: str = ""


def load_sources(staged_dir: Path) -> Dict[str, Any]:
    """Read every staged artifact into a source map.

    Parameters
    ----------
    staged_dir : Path
        The evaluation directory a report is written from.

    Returns
    -------
    Dict[str, Any]
        File name to parsed contents: a dict for JSON, a list of row
        dicts for CSV. Only files directly inside ``staged_dir`` are
        included, so a claim naming a sibling evaluation directory is
        unresolvable rather than silently checked against stale data.
    """
    sources: Dict[str, Any] = {}
    for path in sorted(Path(staged_dir).glob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".json":
            try:
                sources[path.name] = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
        elif path.suffix == ".csv":
            with path.open(newline="") as fh:
                sources[path.name] = list(csv.DictReader(fh))
    return sources


def _coerce(text: Any) -> Any:
    """Convert a cell to a number when it looks like one.

    Parameters
    ----------
    text : Any
        Raw cell value.

    Returns
    -------
    Any
        A float when parseable, otherwise the input unchanged.
    """
    if isinstance(text, (int, float, bool)):
        return text
    if not isinstance(text, str):
        return text
    stripped = text.replace(",", "").replace("%", "").strip()
    try:
        return float(stripped)
    except ValueError:
        return text


def resolve_address(address: str, sources: Dict[str, Any]) -> Any:
    """Resolve one address against the source map.

    Addresses take three forms. ``metrics.json:r2_overall`` reads a
    JSON key. ``per_crop.csv:crop=maize:r2`` selects the row whose
    ``crop`` column is ``maize`` and reads its ``r2`` column.
    ``tiers.csv:sum:n_records`` sums a column across every row.

    Parameters
    ----------
    address : str
        The address to resolve.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    Any
        The resolved value.

    Raises
    ------
    KeyError
        When the file, row or column does not exist.
    """
    parts = address.split(":")
    if len(parts) < 2:
        raise KeyError(f"malformed address: {address}")
    filename, rest = parts[0], parts[1:]
    if filename not in sources:
        raise KeyError(f"no such staged file: {filename}")
    data = sources[filename]

    if isinstance(data, dict):
        key = rest[0]
        if key not in data:
            raise KeyError(f"no such key: {key} in {filename}")
        return _coerce(data[key])

    # CSV: either an aggregate or a row selector plus a column.
    if rest[0] == "sum":
        if len(rest) < 2:
            raise KeyError(f"malformed sum address, no column given: {address}")
        column = rest[1]
        total = 0.0
        for row in data:
            if column not in row:
                raise KeyError(f"no such column: {column} in {filename}")
            total += float(_coerce(row[column]))
        return total
    if rest[0] == "count":
        return float(len(data))
    if len(rest) < 2 or "=" not in rest[0]:
        raise KeyError(f"malformed csv address: {address}")
    sel_col, sel_val = rest[0].split("=", 1)
    column = rest[1]
    for row in data:
        if str(row.get(sel_col, "")).strip() == sel_val:
            if column not in row:
                raise KeyError(f"no such column: {column} in {filename}")
            return _coerce(row[column])
    raise KeyError(f"no row where {sel_col}={sel_val} in {filename}")


def _safe_eval(node: ast.AST, names: Dict[str, Any]) -> Any:
    """Evaluate an arithmetic or comparison expression safely.

    Only literals, named inputs, arithmetic, comparison and unary
    minus are permitted. Anything else raises, so a claim cannot
    smuggle code through its expression.

    Parameters
    ----------
    node : ast.AST
        Parsed expression node.
    names : Dict[str, Any]
        Resolved input values by variable name.

    Returns
    -------
    Any
        The evaluated result.

    Raises
    ------
    ValueError
        When the expression uses an unsupported construct.
    """
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise ValueError(f"unknown input: {node.id}")
        return names[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand, names)
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINOPS:
        return _SAFE_BINOPS[type(node.op)](
            _safe_eval(node.left, names), _safe_eval(node.right, names)
        )
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        op = type(node.ops[0])
        if op not in _SAFE_CMPOPS:
            raise ValueError("unsupported comparison")
        return _SAFE_CMPOPS[op](
            _safe_eval(node.left, names),
            _safe_eval(node.comparators[0], names),
        )
    raise ValueError("unsupported expression")


def _within_tolerance(
    stated: Any, truth: Any, tolerance: Optional[Dict[str, Any]]
) -> bool:
    """Compare a stated value with the truth under a declared tolerance.

    Parameters
    ----------
    stated : Any
        Value as the report states it.
    truth : Any
        Value the staged data supports.
    tolerance : Optional[Dict[str, Any]]
        Declared tolerance, or ``None`` for exact comparison.

    Returns
    -------
    bool
        True when the stated value is acceptable.
    """
    if isinstance(truth, bool) and isinstance(stated, bool):
        return stated == truth
    if isinstance(truth, bool) or isinstance(stated, bool):
        # One side is a genuine boolean and the other is a real
        # number; these are different kinds of claim and must never
        # be treated as equal just because the number is truthy.
        return False
    try:
        s = float(_coerce(stated))
        t = float(_coerce(truth))
    except (TypeError, ValueError):
        return str(stated).strip() == str(truth).strip()
    if tolerance is None:
        return math.isclose(s, t, rel_tol=0.0, abs_tol=1e-9)
    kind = str(tolerance.get("type", "abs"))
    amount = float(tolerance.get("value", 0.0))
    if kind == "rel":
        return abs(s - t) <= abs(t) * amount
    return abs(s - t) <= amount


#: A markdown ordered-list marker at the start of a line: one or more
#: digits, a dot, then whitespace, e.g. "1. Adding confidence
#: intervals." The digit here numbers a list item, not a measurement,
#: and a writer using an ordered list (any model, not model-specific)
#: would otherwise show up as a run of uncovered figures 1, 2, 3, ...
_ORDERED_LIST_MARKER_RE = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


def extract_numbers(text: str) -> List[str]:
    """Pull every figure out of report prose.

    Claim references of the form ``[c12]`` are stripped first, so an
    identifier is never mistaken for a figure. A markdown ordered-list
    marker ("1. ", "2. ", ...) at the start of a line is stripped the
    same way: it numbers a list item, not a measurement, and any
    figure that genuinely follows it on the same line (a real value
    after the list text begins) is still extracted normally.

    Parameters
    ----------
    text : str
        Report text.

    Returns
    -------
    List[str]
        Figures as they appear, in order of appearance.
    """
    cleaned = _CLAIM_REF_RE.sub(" ", text or "")
    cleaned = _ORDERED_LIST_MARKER_RE.sub(" ", cleaned)
    return [m.group(1) for m in _NUMBER_RE.finditer(cleaned)]


def _words_in(text: str) -> Set[str]:
    """Lowercase word tokens of a label, for contradiction checks.

    Parameters
    ----------
    text : str
        The claim's label.

    Returns
    -------
    set
        Lowercased alphanumeric tokens.
    """
    return set(re.findall(r"[a-z0-9/]+", (text or "").lower()))


def label_contradiction(claim: Claim, sources: Dict[str, Any]) -> Optional[str]:
    """Report a label that contradicts the row the claim reads.

    The label is the writer's own words, so nothing in the numbers
    stops it naming a different crop or a different unit from the row
    the claim addresses. A correct value under the wrong heading is
    still wrong, and it would pass every later check, because the deck
    is checked against the claim's label rather than against the
    source.

    The rule detects contradiction, not absence. A label naming a
    sibling row of the same table is a contradiction. A label naming
    nothing the table recognizes is left alone, because report prose
    uses synonyms the data does not: a report says corn where the data
    says maize, and failing that would punish ordinary writing.

    Parameters
    ----------
    claim : Claim
        The claim to inspect.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    Optional[str]
        A description of the contradiction, or ``None``.
    """
    if claim.kind != "lookup" or not claim.source or not claim.key:
        return None
    rows = sources.get(claim.source)
    if not isinstance(rows, list) or not rows:
        return None
    parts = claim.key.split(":")
    if len(parts) < 2 or "=" not in parts[0]:
        return None
    sel_col, sel_val = parts[0].split("=", 1)
    label_words = _words_in(claim.label)

    # Naming its own row earns a label the right to mention others.
    # A rehearsal produced "Q2 area quartile record count (Q3 and Q4
    # also 381)", where the value was Q2's and correct and the
    # parenthetical was useful context. Only a label that names a
    # sibling *instead of* its own row is a wrong-row error.
    names_own_row = sel_val.lower() in label_words

    # A sibling row's selector value appearing in the label.
    siblings = {
        str(row.get(sel_col, "")).strip()
        for row in rows
        if str(row.get(sel_col, "")).strip() != sel_val
    }
    for other in siblings if not names_own_row else ():
        if other and other.lower() in label_words:
            return f"label says {other!r} but the claim reads " f"{sel_col}={sel_val!r}"

    # A different unit appearing in the label. Units are compared as
    # token sets so spacing around the slash does not matter.
    unit_cols = [c for c in rows[0] if c.lower() in ("unit", "units")]
    if not unit_cols:
        return None
    column = unit_cols[0]
    own = ""
    for row in rows:
        if str(row.get(sel_col, "")).strip() == sel_val:
            own = str(row.get(column, "")).strip()
            break
    if not own:
        return None
    own_words = _words_in(own)
    # The same allowance: a label stating its own unit may mention
    # another for contrast.
    if own_words and own_words <= label_words:
        return None
    others = {
        str(row.get(column, "")).strip()
        for row in rows
        if str(row.get(column, "")).strip()
    }
    for other in others:
        other_words = _words_in(other)
        if other_words == own_words:
            continue
        if other_words and other_words <= label_words:
            return f"label states unit {other!r} but the row is reported " f"in {own!r}"
    return None


def _tolerance_note(tolerance: Optional[Dict[str, Any]]) -> str:
    """Render the tolerance a pass relied on, for the verdict message.

    Parameters
    ----------
    tolerance : Optional[Dict[str, Any]]
        The claim's declared tolerance, if any.

    Returns
    -------
    str
        A trailing clause, or an empty string for an exact match.
    """
    if not tolerance:
        return ""
    kind = str(tolerance.get("type", "abs"))
    amount = tolerance.get("value", 0)
    unit = "relative" if kind == "rel" else "absolute"
    return f", within a declared {unit} tolerance of {amount}"


def _check_one(claim: Claim, sources: Dict[str, Any]) -> CheckResult:
    """Resolve and judge a single claim.

    Parameters
    ----------
    claim : Claim
        The claim to check.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    CheckResult
        The verdict.
    """
    base = {"claim_id": claim.id, "value": claim.value, "label": claim.label}
    if claim.kind == "lookup":
        address = f"{claim.source}:{claim.key}"
        try:
            truth = resolve_address(address, sources)
        except KeyError as exc:
            return CheckResult(verdict=VERDICT_UNRESOLVABLE, detail=str(exc), **base)
        try:
            ok = _within_tolerance(claim.value, truth, claim.tolerance)
        except (TypeError, ValueError) as exc:
            return CheckResult(
                verdict=VERDICT_UNRESOLVABLE,
                detail=f"malformed tolerance: {exc}",
                **base,
            )
        if not ok:
            # A wrong number is the more useful message of the two,
            # so it is reported before any label problem.
            return CheckResult(
                verdict=VERDICT_FAILED,
                expected=truth,
                reason="value_mismatch",
                detail=(
                    f"{claim.label}: stated {claim.value}, " f"source says {truth}"
                ),
                **base,
            )
        contradiction = label_contradiction(claim, sources)
        if contradiction:
            return CheckResult(
                verdict=VERDICT_FAILED,
                expected=truth,
                reason="label_mismatch",
                detail=f"{claim.label}: {contradiction}",
                **base,
            )
        return CheckResult(
            verdict=VERDICT_VERIFIED,
            expected=truth,
            detail=(
                f"{claim.label}: {claim.value} matches {address}"
                f"{_tolerance_note(claim.tolerance)}"
            ),
            **base,
        )

    if claim.kind in ("derived", "consistency"):
        names: Dict[str, Any] = {}
        for var, address in (claim.inputs or {}).items():
            try:
                names[var] = resolve_address(address, sources)
            except KeyError as exc:
                return CheckResult(
                    verdict=VERDICT_UNRESOLVABLE, detail=str(exc), **base
                )
        try:
            tree = ast.parse(claim.expr or "", mode="eval")
            truth = _safe_eval(tree, names)
        except (SyntaxError, ValueError, ZeroDivisionError, TypeError) as exc:
            return CheckResult(verdict=VERDICT_UNRESOLVABLE, detail=str(exc), **base)
        try:
            ok = _within_tolerance(claim.value, truth, claim.tolerance)
        except (TypeError, ValueError) as exc:
            return CheckResult(
                verdict=VERDICT_UNRESOLVABLE,
                detail=f"malformed tolerance: {exc}",
                **base,
            )
        inputs_detail = ", ".join(f"{k}={v}" for k, v in names.items())
        if ok:
            # A pass shows its working too: a verdict nobody can
            # inspect is the shape this design exists to remove, and
            # explaining only rejections leaves the half a report's
            # author most wants to defend resting on trust.
            return CheckResult(
                verdict=VERDICT_VERIFIED,
                expected=truth,
                detail=(
                    f"{claim.label}: {claim.value} recomputed as {truth} "
                    f"from {inputs_detail}"
                    f"{_tolerance_note(claim.tolerance)}"
                ),
                **base,
            )
        return CheckResult(
            verdict=VERDICT_FAILED,
            expected=truth,
            reason="value_mismatch",
            detail=(
                f"{claim.label}: stated {claim.value}, recomputed {truth} "
                f"from {inputs_detail}"
            ),
            **base,
        )

    return CheckResult(
        verdict=VERDICT_UNRESOLVABLE,
        detail=f"unknown claim kind: {claim.kind}",
        **base,
    )


def _tag_figures(text: str) -> List[Tuple[str, Optional[str]]]:
    """Pair each figure in report prose with the claim id tagged after it.

    A number is bound to the next ``[cN]`` tag that appears before any
    other number, which is how a writer tags a figure immediately
    after stating it ("0.481 [c1]"). A number with no tag before the
    next number, or before the text ends, is untagged.

    Parameters
    ----------
    text : str
        Report prose.

    Returns
    -------
    List[Tuple[str, Optional[str]]]
        ``(figure, claim_id)`` pairs in order of appearance;
        ``claim_id`` is ``None`` when the figure carries no tag.
    """
    cleaned = _ORDERED_LIST_MARKER_RE.sub(" ", text or "")
    tokens: List[Tuple[int, str, str]] = []
    for m in _NUMBER_RE.finditer(cleaned):
        tokens.append((m.start(), "num", m.group(1)))
    for m in _CLAIM_REF_RE.finditer(cleaned):
        tokens.append((m.start(), "ref", m.group(1)))
    tokens.sort(key=lambda t: t[0])

    pairs: List[Tuple[str, Optional[str]]] = []
    pending_value: Optional[str] = None
    for _, kind, val in tokens:
        if kind == "num":
            if pending_value is not None:
                pairs.append((pending_value, None))
            pending_value = val
        elif pending_value is not None:
            pairs.append((pending_value, val))
            pending_value = None
        # else: a tag with no preceding unbound number -- ignore it.
    if pending_value is not None:
        pairs.append((pending_value, None))
    return pairs


def _figure_matches_claim_value(figure: str, value: Any) -> bool:
    """Check a report figure against one claim's value, rounding-tolerant.

    Parameters
    ----------
    figure : str
        The figure exactly as it appears in the report text.
    value : Any
        The claim's declared value.

    Returns
    -------
    bool
        True when the figure is the same number as the claim's value,
        allowing for prose restating it at coarser rounding (0.481
        written as "0.48" later is still the same figure).
    """
    candidates = {str(value)}
    coerced = _coerce(value)
    if isinstance(coerced, float):
        candidates.add(f"{coerced:g}")
        if coerced.is_integer():
            candidates.add(str(int(coerced)))
            candidates.add(f"{int(coerced):,}")
        else:
            for places in range(4):
                candidates.add(f"{round(coerced, places):g}")
    normalized = figure.replace(",", "")
    if figure in candidates or normalized in candidates:
        return True
    try:
        as_float = float(normalized)
    except ValueError:
        return False
    return f"{as_float:g}" in candidates


def check_claims(
    claims: Sequence[Claim],
    sources: Dict[str, Any],
    report_text: Optional[str] = None,
    coverage_claims: Optional[Sequence[Claim]] = None,
) -> List[CheckResult]:
    """Check every claim, and every number in the report text.

    Parameters
    ----------
    claims : Sequence[Claim]
        Every claim to resolve and return a verdict for.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.
    report_text : Optional[str]
        When given, every figure in the text must carry a ``[cN]`` tag
        naming the claim that backs it, and that claim's value must
        match; uncovered and mismatched figures are returned as their
        own results.
    coverage_claims : Optional[Sequence[Claim]]
        Which claims a figure's tag may reference. Defaults to
        ``claims`` itself. :func:`check_report` passes only the
        writer's own declared claims here, never the harness-injected
        required checks -- a required check is never something a
        writer could tag, since the writer never sees its id.

    Returns
    -------
    List[CheckResult]
        One result per claim, followed by one per uncovered or
        wrongly-tagged figure.
    """
    results = [_check_one(c, sources) for c in claims]
    if report_text is None:
        return results

    coverage_lookup = {
        c.id: c for c in (coverage_claims if coverage_claims is not None else claims)
    }
    for figure, ref_id in _tag_figures(report_text):
        if ref_id is None:
            results.append(
                CheckResult(
                    claim_id=f"uncovered:{figure}",
                    verdict=VERDICT_UNCOVERED,
                    value=figure,
                    detail=f"figure {figure} appears in the report with no claim tag",
                )
            )
            continue
        claim = coverage_lookup.get(ref_id)
        if claim is None:
            results.append(
                CheckResult(
                    claim_id=f"uncovered:{figure}",
                    verdict=VERDICT_UNCOVERED,
                    value=figure,
                    detail=(
                        f"figure {figure} is tagged [{ref_id}], which is not "
                        f"a declared claim"
                    ),
                )
            )
            continue
        if not _figure_matches_claim_value(figure, claim.value):
            # A figure whose own tagged claim does not support it has
            # no VALID claim behind it, same as no tag at all -- so
            # this is a coverage gap (VERDICT_UNCOVERED), not a
            # distinct failure. Coded as VERDICT_FAILED here instead,
            # numeric_verify.verify_workspace's coverage/claims_required
            # math and numeric_grader.score_case's coverage_gaps filter
            # (both keyed on VERDICT_UNCOVERED) would silently miss it
            # and misreport the experiment's own coverage metric.
            results.append(
                CheckResult(
                    claim_id=f"uncovered:{figure}",
                    verdict=VERDICT_UNCOVERED,
                    value=figure,
                    expected=claim.value,
                    detail=(
                        f"figure {figure} is tagged [{ref_id}], but that "
                        f"claim's value is {claim.value}"
                    ),
                )
            )
    return results


#: Column holding a per-row record count in a breakdown table. Every
#: such table must account for the same records the summary counts.
COUNT_COLUMN = "n_records"

#: Column holding a per-row share of the whole, in percent.
RATE_COLUMN = "rate_pct"

#: Where the summary record count lives.
SUMMARY_SOURCE = "metrics.json"
SUMMARY_COUNT_KEY = "n_records"

#: A rate column is rounded per row, so its sum drifts from 100 by at
#: most half a unit in the last place per row. The allowance scales
#: with the row count rather than being a flat guess.
RATE_ROUNDING_SLACK = 0.05


def required_consistency_claims(sources: Dict[str, Any]) -> List[Claim]:
    """Derive the cross-checks a report must satisfy, whatever it says.

    Consistency claims are the ones nobody thinks to ask for, which
    is exactly why a writer cannot be trusted to offer them. A report
    that declares only correct look-ups and never mentions that its
    bucket table is short of the record count would otherwise pass
    with the contradiction sitting in the staged files.

    So the harness reads the staged schema and writes these claims
    itself. Two relations are derived. Every breakdown table carrying
    a record count must account for the same records the summary
    counts. Every column of shares must total one hundred, within the
    drift its own rounding allows.

    Parameters
    ----------
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    List[Claim]
        Claims the writer did not author and cannot remove.
    """
    derived: List[Claim] = []
    summary = sources.get(SUMMARY_SOURCE)
    has_total = isinstance(summary, dict) and SUMMARY_COUNT_KEY in summary

    for name, data in sorted(sources.items()):
        if not isinstance(data, list) or not data:
            continue
        columns = set(data[0])
        stem = name.rsplit(".", 1)[0]

        if COUNT_COLUMN in columns and has_total:
            derived.append(
                Claim(
                    id=f"required:{stem}-total",
                    value=True,
                    label=f"{stem} accounts for every evaluated record",
                    kind="consistency",
                    expr="a == b",
                    inputs={
                        "a": f"{name}:sum:{COUNT_COLUMN}",
                        "b": f"{SUMMARY_SOURCE}:{SUMMARY_COUNT_KEY}",
                    },
                )
            )

        if RATE_COLUMN in columns:
            derived.append(
                Claim(
                    id=f"required:{stem}-pct",
                    value=100,
                    label=f"{stem} shares total one hundred percent",
                    kind="consistency",
                    expr="a",
                    inputs={"a": f"{name}:sum:{RATE_COLUMN}"},
                    tolerance={
                        "type": "abs",
                        "value": max(
                            RATE_ROUNDING_SLACK,
                            len(data) * RATE_ROUNDING_SLACK,
                        ),
                    },
                )
            )

    return derived


def check_report(
    claims: Sequence[Claim],
    sources: Dict[str, Any],
    report_text: Optional[str] = None,
) -> List[CheckResult]:
    """Check a report: the harness's required claims, then the writer's.

    This is the entry point a verifier uses. :func:`check_claims` is
    the primitive underneath it and checks exactly what it is handed;
    this adds the claims the writer is not allowed to omit.

    Parameters
    ----------
    claims : Sequence[Claim]
        The claims the writer declared.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.
    report_text : Optional[str]
        The finished report, so every figure in it can be required to
        carry a claim.

    Returns
    -------
    List[CheckResult]
        Required claims first, then the writer's, then any figure in
        the text with no claim behind it.
    """
    required = required_consistency_claims(sources)
    return check_claims(
        list(required) + list(claims),
        sources,
        report_text=report_text,
        coverage_claims=claims,
    )


def check_slide_spec(
    spec: Dict[str, Any],
    accepted: Dict[str, CheckResult],
    labels: Dict[str, str],
) -> List[str]:
    """Check that every slide figure maps to an accepted claim.

    This replaces re-auditing the deck. A number on a slide must carry
    the identifier of a claim that already passed, must equal that
    claim's value, and must sit under that claim's label. A deck built
    this way is provably a subset of the verified report.

    Parameters
    ----------
    spec : Dict[str, Any]
        Slide specification with ``slides`` each holding ``cells``.
    accepted : Dict[str, CheckResult]
        Results by claim id; only ``verified`` entries are usable.
    labels : Dict[str, str]
        Claim id to the label the claim was verified under.

    Returns
    -------
    List[str]
        One message per violation; empty when the deck is clean.
    """
    problems: List[str] = []
    for index, slide in enumerate(spec.get("slides", []), start=1):
        backed_values = [
            result.value
            for cell in slide.get("cells", [])
            for result in [accepted.get(cell.get("claim", ""))]
            if result is not None and result.verdict == VERDICT_VERIFIED
        ]
        for figure in extract_numbers(str(slide.get("title") or "")):
            if not any(
                _within_tolerance(figure, value, None) for value in backed_values
            ):
                problems.append(
                    f"slide {index}: title states {figure}, which no "
                    f"accepted cell on this slide backs"
                )
        for cell in slide.get("cells", []):
            value = cell.get("value")
            claim_id = cell.get("claim")
            where = f"slide {index}"
            if not claim_id:
                problems.append(f"{where}: figure {value} carries no claim id")
                continue
            result = accepted.get(claim_id)
            if result is None:
                problems.append(f"{where}: claim {claim_id} is not in the accepted set")
                continue
            if result.verdict != VERDICT_VERIFIED:
                problems.append(
                    f"{where}: claim {claim_id} did not pass " f"({result.verdict})"
                )
                continue
            if not _within_tolerance(value, result.value, None):
                problems.append(
                    f"{where}: shows {value} but claim {claim_id} "
                    f"was verified as {result.value}"
                )
                continue
            expected_label = labels.get(claim_id)
            cell_label = cell.get("label")
            # A missing or empty label is not a pass by omission: the
            # whole point of this check is that a figure stays under
            # its accepted label, and a cell that states no label at
            # all makes that unverifiable, not satisfied.
            if expected_label and not cell_label:
                problems.append(
                    f"{where}: claim {claim_id} has no label on the "
                    f"slide cell; expected '{expected_label}'"
                )
            elif expected_label and cell_label != expected_label:
                problems.append(
                    f"{where}: claim {claim_id} is '{expected_label}' "
                    f"but appears under '{cell_label}'"
                )
    return problems


def load_claims(path: Path) -> List[Claim]:
    """Read a claims file written by a report writer.

    Parameters
    ----------
    path : Path
        JSON file holding a ``claims`` list.

    Returns
    -------
    List[Claim]
        The parsed claims.
    """
    raw = json.loads(Path(path).read_text())
    items: Iterable[Dict[str, Any]] = (
        raw.get("claims", []) if isinstance(raw, dict) else raw
    )
    return [Claim.from_dict(item) for item in items]
