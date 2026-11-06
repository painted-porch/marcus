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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

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

#: Codepoints named explicitly rather than written as literal glyphs:
#: several of the characters below (no-break spaces especially) are
#: invisible or near-identical to their ASCII look-alikes, which
#: makes them easy to miscopy, hard to review, and -- for the spaces
#: -- a risk of being silently stripped by whitespace-trimming
#: tooling if they ever appeared as literal characters in source.
_MINUS_SIGN = chr(0x2212)  # MINUS SIGN
_SMALL_HYPHEN_MINUS = chr(0xFE63)  # SMALL HYPHEN-MINUS
_FULLWIDTH_HYPHEN_MINUS = chr(0xFF0D)  # FULLWIDTH HYPHEN-MINUS
_EN_DASH = chr(0x2013)
_EM_DASH = chr(0x2014)
_NO_BREAK_SPACE = chr(0x00A0)
_NARROW_NO_BREAK_SPACE = chr(0x202F)
_SUPERSCRIPT_TWO = chr(0x00B2)
_SUPERSCRIPT_THREE = chr(0x00B3)
_SUPERSCRIPT_ONE = chr(0x00B9)
_SUPERSCRIPT_ZERO = chr(0x2070)
_SUPERSCRIPT_FOUR_TO_NINE = chr(0x2074) + "-" + chr(0x2079)
_SUPERSCRIPT_PLUS = chr(0x207A)
_SUPERSCRIPT_MINUS = chr(0x207B)

#: Characters a model may write as a minus sign that are never
#: anything else in this trial's prose: Unicode MINUS SIGN, and the
#: small and fullwidth compatibility forms of hyphen-minus.
_ALWAYS_MINUS_RE = re.compile(
    "[" + _MINUS_SIGN + _SMALL_HYPHEN_MINUS + _FULLWIDTH_HYPHEN_MINUS + "]"
)

#: En dash and em dash are ordinary punctuation most of the time (a
#: range, a parenthetical), so only treat one as a sign when it sits
#: immediately before a digit -- the extractor's own word-boundary
#: rule then decides whether that makes it the start of a new number
#: (a word character right before the dash, as in a range like
#: "10-20", still blocks it from being read as a sign, exactly as it
#: already does for an ASCII hyphen).
_DASH_BEFORE_DIGIT_RE = re.compile("[" + _EN_DASH + _EM_DASH + r"](?=\d)")

#: Fullwidth digits (category Nd, used in CJK typesetting) are
#: numerals, never exponents or anything else -- normalizing them to
#: ASCII keeps the captured figure's own text consistent with
#: everything downstream that treats it as a plain string.
_FULLWIDTH_DIGIT_TRANSLATION = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
)

#: A no-break space or narrow no-break space (SI/ISO's recommended
#: thousands separator) between exactly three digits and a non-digit
#: is a grouping separator, joined the same way the existing
#: comma-grouped pattern already is. An ordinary ASCII space is
#: deliberately excluded: this trial's writer has no reason to put a
#: plain space inside a number, so normalizing one would risk
#: merging two unrelated figures that happen to sit next to each
#: other in prose.
_GROUPING_SPACE_RE = re.compile(
    r"(?<=\d)[" + _NO_BREAK_SPACE + _NARROW_NO_BREAK_SPACE + r"](?=\d{3}(?!\d))"
)

#: Superscript digits and signs (category "No", not "Nd" -- Python's
#: \d and \w already treat them as alphanumeric, which is exactly the
#: problem) are erased rather than normalized to plain digits.
#: Converting a superscript-two exponent (ten squared, written
#: inline) into the literal string "102" would fabricate a wrong
#: figure; erasing the superscript instead lets the base number "10"
#: still be read on its own, with the exponent simply unrecognized --
#: this trial's writer does not use exponent notation, so there is
#: no meaning to preserve here.
_SUPERSCRIPT_RE = re.compile(
    "["
    + _SUPERSCRIPT_TWO
    + _SUPERSCRIPT_THREE
    + _SUPERSCRIPT_ONE
    + _SUPERSCRIPT_ZERO
    + _SUPERSCRIPT_FOUR_TO_NINE
    + _SUPERSCRIPT_PLUS
    + _SUPERSCRIPT_MINUS
    + "]"
)


def _normalize_numeric_text(text: str) -> str:
    """Normalize the Unicode forms a model may write a number in.

    Applied once, before any other extraction step, so every caller
    of :func:`extract_numbers` or :func:`_tag_figures` sees the same
    normalized text -- including slide-title checking, which calls
    :func:`extract_numbers` directly.

    Parameters
    ----------
    text : str
        Raw report or slide-title text.

    Returns
    -------
    str
        Text with sign, digit and grouping variants folded to the
        ASCII forms ``_NUMBER_RE`` already understands.
    """
    text = _ALWAYS_MINUS_RE.sub("-", text)
    text = _DASH_BEFORE_DIGIT_RE.sub("-", text)
    text = text.translate(_FULLWIDTH_DIGIT_TRANSLATION)
    text = _GROUPING_SPACE_RE.sub(",", text)
    text = _SUPERSCRIPT_RE.sub(" ", text)
    return text


_CLAIM_REF_RE = re.compile(r"\[(c\d+)\]")

#: A derived claim's expression may use **, but no legitimate use in
#: this trial (ratios, percentages, simple consistency checks) ever
#: needs an exponent beyond this magnitude. Bounding it matters
#: independently of catching exceptions below: Python ints have no
#: fixed size, so a huge exponent on an int base does not raise
#: OverflowError the way a float base does -- it just keeps computing
#: an ever-larger number, which can hang or exhaust memory long
#: before any exception would fire. Checked before the operator runs,
#: not after.
#:
#: Tightened from 1000: that bound alone did not keep an integer
#: result inside float range. A base as small as 10 raised to 1000
#: is a 1000-digit integer, computes instantly (no hang), and was
#: still too large for the float conversion _within_tolerance always
#: performs to compare it -- an OverflowError outside the handler
#: wrapped around evaluation itself. 100 is comfortably outside this
#: trial's actual range of exponents (ratios, percentages) while
#: keeping the result's magnitude -- base^100, for any base this
#: trial's staged data could plausibly produce -- within float
#: range for the vast majority of cases. The float-conversion fix
#: below is the actual guarantee for the rest; this cap is the first,
#: cheap line of defense against a hang on a huge exponent.
_MAX_POW_EXPONENT = 100

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
    unit : Optional[str]
        ``"fraction"`` (0-1, like an R2) or ``"percent"`` (0-100,
        like a ``rate_pct`` share) -- which scale the claim's own
        ``value`` is already on. Decides what a '%'-suffixed figure
        in prose, a slide cell or a slide title actually means: a
        fraction-unit claim's percent figure is divided by 100
        before comparison ("48.1%" is 0.481); a percent-unit claim's
        is not ("50%" is 50.0, the same number, not 0.5). Absent
        means infer it from the claim's own source column name (see
        :func:`_inferred_claim_unit`), since most claims do not
        declare one explicitly.
    """

    id: str
    value: Any
    label: str
    kind: str
    source: Optional[str] = None
    key: Optional[str] = None
    expr: Optional[str] = None
    inputs: Any = field(default_factory=dict)
    tolerance: Any = None
    unit: Optional[str] = None

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
            unit=raw.get("unit"),
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
    addresses : Tuple[str, ...]
        Every staged address the claim actually read: one for a
        lookup, one per input for a derived or consistency claim.
        Carries the claim's own row provenance forward to a consumer
        (the slide mapping check) that has only this result to work
        from, not the original ``Claim`` or the staged ``sources``
        it was resolved against.
    unit : Optional[str]
        From :func:`_inferred_claim_unit` at the time the claim was
        checked: ``"fraction"``, ``"percent"``, or ``None`` when
        undetermined. Carried forward the same way ``addresses`` is,
        so a slide-cell or slide-title figure with a '%' can be
        scaled correctly without re-deriving it from a ``Claim`` the
        consumer does not have.
    """

    claim_id: str
    verdict: str
    value: Any = None
    expected: Any = None
    detail: str = ""
    label: str = ""
    reason: str = ""
    addresses: Tuple[str, ...] = ()
    unit: Optional[str] = None


def load_sources(
    staged_dir: Path, allowed_names: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """Read every staged artifact into a source map.

    Parameters
    ----------
    staged_dir : Path
        The evaluation directory a report is written from.
    allowed_names : Optional[Iterable[str]]
        When given, only files whose name is in this set are read.
        ``verify_workspace`` passes the trusted hash manifest's own
        keys here, so a file dropped into ``staged/`` after staging
        -- never hashed, never part of what was trusted -- cannot
        become a source just by sitting in the directory. ``None``
        (the default) loads every file present, for callers that
        have no manifest to check against (most unit tests).

    Returns
    -------
    Dict[str, Any]
        File name to parsed contents: a dict for JSON, a list of row
        dicts for CSV. Only files directly inside ``staged_dir`` are
        included, so a claim naming a sibling evaluation directory is
        unresolvable rather than silently checked against stale data.
    """
    allowed = set(allowed_names) if allowed_names is not None else None
    sources: Dict[str, Any] = {}
    for path in sorted(Path(staged_dir).glob("*")):
        if not path.is_file():
            continue
        if allowed is not None and path.name not in allowed:
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
            try:
                total += float(_coerce(row[column]))
            except (TypeError, ValueError) as exc:
                raise KeyError(
                    f"column {column} in {filename} is not numeric, "
                    f"cannot be summed"
                ) from exc
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
        When the expression uses an unsupported construct, or a
        ** exponent exceeds the safety cap.
    OverflowError, ZeroDivisionError, TypeError
        When the underlying arithmetic operator raises on
        otherwise-valid operands -- a well-formed expression can
        still fail this way, which the caller must treat as
        unresolvable, not a crash.
    RecursionError
        When the expression nests deeply enough to exceed Python's
        own call stack; this function recurses once per AST node.
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
        left = _safe_eval(node.left, names)
        right = _safe_eval(node.right, names)
        if (
            isinstance(node.op, ast.Pow)
            and isinstance(right, (int, float))
            and abs(right) > _MAX_POW_EXPONENT
        ):
            raise ValueError(
                f"exponent magnitude {right} exceeds the safe evaluation "
                f"limit of {_MAX_POW_EXPONENT}"
            )
        return _SAFE_BINOPS[type(node.op)](left, right)
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


def _mask_ordered_list_markers(text: str) -> str:
    """Blank an ordered-list marker to same-width spaces, not one space.

    Collapsing a marker to a single space (the old behavior) shortens
    the text by the marker's own length minus one -- harmless for a
    caller that only wants the figures, since nothing downstream of
    that caller cares about character offsets, but every offset
    computed on the shortened text drifts out of alignment with the
    caller's original ``text`` from that point on. A ten-item list
    has a four-character marker ("10. ") where every earlier item had
    a three-character one ("1. ", "2. ", ...), so the drift is
    cumulative and position-dependent: :func:`_tag_figures` needs its
    positions to stay valid against callers that extract a sentence
    around one, so this masks markers without ever changing the
    text's own length.

    Parameters
    ----------
    text : str
        Report text, already run through :func:`_normalize_numeric_text`.

    Returns
    -------
    str
        The same text with every marker's characters replaced by
        spaces, one for one.
    """
    return _ORDERED_LIST_MARKER_RE.sub(lambda m: " " * len(m.group(0)), text)


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
    cleaned = _normalize_numeric_text(text or "")
    cleaned = _CLAIM_REF_RE.sub(" ", cleaned)
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


#: A closing or opening quotation mark a writer's model might use,
#: named by codepoint rather than written as a literal glyph -- see
#: the Unicode block above for why.
_RIGHT_DOUBLE_QUOTE = chr(0x201D)
_RIGHT_SINGLE_QUOTE = chr(0x2019)
_LEFT_DOUBLE_QUOTE = chr(0x201C)
_LEFT_SINGLE_QUOTE = chr(0x2018)

#: Markers that can close one sentence's own formatting before the
#: whitespace that follows it ("0.886 [c1].**" -- the "**" sits
#: between the period and the next sentence's capital letter).
_CLOSING_MARKERS_RE = (
    r'(?:\*\*|\*|_|"|' + _RIGHT_DOUBLE_QUOTE + "|" + _RIGHT_SINGLE_QUOTE + r"|\)|\])*"
)

#: Markers that can open the *next* sentence's own formatting after
#: the whitespace ("0.886 [c1]. **Cotton..." -- the "**" sits between
#: the whitespace and that sentence's capital letter). The mirror of
#: :data:`_CLOSING_MARKERS_RE`, checked in a lookahead rather than
#: consumed, since the next sentence's own boundary-finding pass
#: still needs to see these characters.
_OPENING_MARKERS_RE = (
    r'(?:\*\*|\*|_|"|' + _LEFT_DOUBLE_QUOTE + "|" + _LEFT_SINGLE_QUOTE + r"|\(|\[)*"
)

#: Sentence-ending punctuation, then any closing markdown or
#: quotation marks, then either end of text or whitespace, any
#: opening markdown or quotation marks on the *next* sentence, and
#: finally a token that actually starts a new sentence: a capital
#: letter, a digit, or a list marker. Splitting on bare "punctuation
#: then whitespace" (the original rule) both over-rejected ("vs.
#: cotton" split at the abbreviation, because nothing checked what
#: followed) and under-rejected a closing marker on the first
#: sentence ("**Maize's R2 is 0.886 [c1].** Cotton's...") or an
#: opening marker on the second ("Maize's R2 is 0.886 [c1].
#: **Cotton's..."). Requiring a sentence-starting token after the
#: whitespace fixes the first; looking past closing and opening
#: markers on either side fixes the other two. This is a heuristic,
#: not a parser: it is good enough to isolate one sentence from its
#: neighbors, which is all this check needs.
_SENTENCE_END_RE = re.compile(
    r"[.!?]"
    + _CLOSING_MARKERS_RE
    + r"(?:\s+(?="
    + _OPENING_MARKERS_RE
    + r"(?:[A-Z0-9]|[-*+]\s|\|))|\Z)"
)

#: A fixed list of abbreviations whose period is never a sentence
#: boundary, even when it happens to be followed by whitespace and a
#: capitalized word ("e.g. Maize", "Fig. 3", "et al. Smith") -- cases
#: the capital-letter requirement above cannot rule out by itself.
#: Matched case-insensitively against the text immediately before
#: the period, with a word-boundary check so "e.g" cannot match
#: inside some unrelated longer word.
_ABBREVIATION_ENDINGS = (
    "vs",
    "e.g",
    "i.e",
    "cf",
    "et al",
    "fig",
    "no",
    "approx",
    "ca",
)


def _ends_with_abbreviation(prefix: str) -> bool:
    """Decide whether text immediately preceding a period is an abbreviation.

    Parameters
    ----------
    prefix : str
        The text before the period being considered as a boundary.

    Returns
    -------
    bool
        True when ``prefix`` ends with one of :data:`_ABBREVIATION_ENDINGS`,
        at a word boundary.
    """
    lowered = prefix.lower()
    for abbr in _ABBREVIATION_ENDINGS:
        if lowered.endswith(abbr):
            before = lowered[: len(lowered) - len(abbr)]
            if not before or not before[-1].isalnum():
                return True
    return False


#: The start of a markdown list item ("- ", "* ", "+ ", "10. ") or a
#: table row ("|") is a sentence boundary even with no terminal
#: punctuation at all -- a writer does not end "- Maize R2: 0.886
#: [c1]" with a period, so without this, consecutive list items or
#: table rows merge into one "sentence" spanning the whole list, and
#: the row-provenance check above sees every item's row as if it
#: were also every other item's own row.
_LIST_OR_TABLE_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]\s|\d+\.\s|\|)", re.MULTILINE)

#: A markdown heading line ("# Maize", "## Results") is its own
#: unit, start to end -- a writer does not end a heading with a
#: period, and the paragraph after it does not inherit the
#: heading's own words just because nothing else separates them.
#: Both the heading's start (ending whatever came before it) and its
#: end (ending the heading itself) are boundaries.
_HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t][^\n]*", re.MULTILINE)

#: One or more blank (empty or whitespace-only) lines end the
#: paragraph before them -- a writer separates paragraphs this way
#: far more often than with terminal punctuation on the last
#: sentence of one before the first word of the next. The boundary
#: is the position right after the blank-line run, where the next
#: paragraph's own content begins.
_PARAGRAPH_BREAK_RE = re.compile(r"(?:\n[ \t]*){2,}")


def _line_boundaries(text: str) -> Set[int]:
    """Stage 1: positions where a markdown block begins or ends.

    Covers a list item or table row's own line start, a heading
    line's start and end, and the start of the paragraph following
    one or more blank lines -- every Markdown block type besides an
    ordinary sentence. Operates on ``text`` before any ordered-list-
    marker masking, since masking blanks the digits an ordered
    marker needs to match at all -- a caller that masks first, then
    segments, never finds that boundary for a numbered list.

    Parameters
    ----------
    text : str
        The text to scan, not yet masked.

    Returns
    -------
    Set[int]
        One offset per block boundary found.
    """
    boundaries = {m.start() for m in _LIST_OR_TABLE_LINE_RE.finditer(text)}
    for m in _HEADING_LINE_RE.finditer(text):
        boundaries.add(m.start())
        boundaries.add(m.end())
    boundaries |= {m.end() for m in _PARAGRAPH_BREAK_RE.finditer(text)}
    return boundaries


def _punctuation_boundaries(text: str) -> Set[int]:
    """Stage 2: positions where sentence-ending punctuation closes a sentence.

    Parameters
    ----------
    text : str
        The text to scan.

    Returns
    -------
    Set[int]
        One offset per sentence-ending match, after the abbreviation
        list has ruled out a false one.
    """
    boundaries = set()
    for m in _SENTENCE_END_RE.finditer(text):
        if text[m.start()] == "." and _ends_with_abbreviation(text[: m.start()]):
            continue
        boundaries.add(m.end())
    return boundaries


def _sentence_boundaries(text: str) -> List[int]:
    """Collect every position in text that starts or ends a "sentence".

    The union of both stages: a list item or table row with no
    terminal punctuation at all still ends where the next one begins
    (stage 1), and an ordinary sentence still ends at its own
    punctuation (stage 2) -- the two are independent passes over the
    same unmasked text, so neither stage needs to know where the
    other found a boundary.

    Parameters
    ----------
    text : str
        The text to scan, not yet masked -- masking a figure's
        ordered-list marker must happen only inside number
        extraction, never before this.

    Returns
    -------
    List[int]
        Sorted character offsets, always including ``0`` and
        ``len(text)``.
    """
    boundaries = {0, len(text)} | _line_boundaries(text) | _punctuation_boundaries(text)
    return sorted(boundaries)


def _sentence_at(text: str, position: int) -> str:
    """Return the sentence enclosing a character position in text.

    Parameters
    ----------
    text : str
        The text to search.
    position : int
        A character offset into ``text``.

    Returns
    -------
    str
        The sentence containing ``position`` -- from the end of the
        previous boundary (sentence-ending punctuation, or the start
        of a list item or table row) to the next one.
    """
    boundaries = _sentence_boundaries(text)
    start = max(b for b in boundaries if b <= position)
    end = min((b for b in boundaries if b > start), default=len(text))
    return text[start:end]


def _prose_label_contradiction(
    claim: Claim, sentence: str, sources: Dict[str, Any]
) -> Optional[str]:
    """Report a sentence that contradicts the row a tagged claim reads.

    :func:`label_contradiction` catches a claim whose own ``label``
    names the wrong row. Neither it nor the derived-claim equivalent
    catches a report sentence that names the wrong row while tagging
    a claim whose own label is perfectly correct: "Cotton's R2 is
    0.886 [c1]." where ``c1`` correctly reads and labels maize's own
    0.886. The claim is right; the sentence lies, and nothing before
    this checked the sentence's own words at all.

    Generalizes the same sibling-row rule :func:`label_contradiction`
    and the derived-claim version use, applied to the sentence
    surrounding a tagged figure instead of the claim's own label, and
    covering both a lookup claim's single row and a derived or
    consistency claim's inputs.

    Parameters
    ----------
    claim : Claim
        The claim a figure in ``sentence`` is tagged with.
    sentence : str
        The sentence enclosing the tagged figure (see
        :func:`_sentence_at`).
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    Optional[str]
        A description of the contradiction, or ``None``.
    """
    addresses: List[str] = []
    if (
        claim.kind == "lookup"
        and isinstance(claim.source, str)
        and isinstance(claim.key, str)
    ):
        addresses.append(f"{claim.source}:{claim.key}")
    elif claim.kind in ("derived", "consistency") and isinstance(claim.inputs, dict):
        addresses.extend(v for v in claim.inputs.values() if isinstance(v, str))

    owned_by_column: Dict[Tuple[str, str], Set[str]] = {}
    for address in addresses:
        parts = address.split(":")
        if len(parts) < 2 or "=" not in parts[1]:
            continue
        source_file = parts[0]
        sel_col, sel_val = parts[1].split("=", 1)
        owned_by_column.setdefault((source_file, sel_col), set()).add(sel_val)

    sentence_words = _words_in(sentence)
    for (source_file, sel_col), owned in owned_by_column.items():
        rows = sources.get(source_file)
        if not isinstance(rows, list) or not rows:
            continue
        # Naming every row the claim actually reads earns the
        # sentence the right to mention others, the same allowance
        # label_contradiction and its derived-claim equivalent give a
        # label -- naming only some of them is not covered by it.
        names_every_owned_row = all(v.lower() in sentence_words for v in owned)
        if names_every_owned_row:
            continue
        siblings = {
            str(row.get(sel_col, "")).strip()
            for row in rows
            if str(row.get(sel_col, "")).strip() not in owned
        }
        for other in siblings:
            if other and other.lower() in sentence_words:
                return (
                    f"sentence says {other!r} but the tagged claim reads "
                    f"{sel_col}={sorted(owned)!r}"
                )
    return None


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


def _derived_label_contradiction(
    claim: Claim, inputs: Any, sources: Dict[str, Any]
) -> Optional[str]:
    """Report a label that contradicts the rows a derived claim reads.

    :func:`label_contradiction` only ever ran for ``lookup`` claims,
    but a derived claim with a single row-selected input is, for
    every practical purpose, a lookup wearing a different kind -- the
    writer instructions require a rounded restatement to be a derived
    claim rather than a lookup, so a label naming the wrong row would
    otherwise bypass this protection entirely just by being spelled as
    ``expr="a"``.

    Generalizes the same rule to every input: for each selector
    column referenced (``crop`` in ``per_crop.csv:crop=maize:...``),
    the set of values the claim's own inputs actually read is
    "owned." A label naming a sibling value of that column that is
    not owned by any input is a contradiction -- a two-crop ratio
    labelled with both crops it actually reads is not a
    contradiction, because neither crop is a sibling relative to the
    other; a label naming a third, uninvolved crop is.

    Parameters
    ----------
    claim : Claim
        The derived or consistency claim to inspect.
    inputs : Dict[str, str]
        The claim's own ``inputs`` mapping (variable to address).
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    Optional[str]
        A description of the contradiction, or ``None``.
    """
    label_words = _words_in(claim.label)
    owned_by_column: Dict[Tuple[str, str], Set[str]] = {}
    for address in inputs.values():
        if not isinstance(address, str):
            continue
        parts = address.split(":")
        if len(parts) < 2 or "=" not in parts[1]:
            continue
        source_file = parts[0]
        sel_col, sel_val = parts[1].split("=", 1)
        owned_by_column.setdefault((source_file, sel_col), set()).add(sel_val)

    for (source_file, sel_col), owned in owned_by_column.items():
        rows = sources.get(source_file)
        if not isinstance(rows, list) or not rows:
            continue
        # Naming every owned row earns the label the right to mention
        # others (the lookup path's "Q2 ... (Q3 and Q4 also 381)"
        # allowance, generalized to more than one input). Naming only
        # SOME of them is not covered by that allowance: a label that
        # swaps out one real input's row for a sibling's, while still
        # naming the other input, must not be waved through just
        # because it mentions something it owns.
        names_every_owned_row = all(v.lower() in label_words for v in owned)
        if names_every_owned_row:
            continue
        siblings = {
            str(row.get(sel_col, "")).strip()
            for row in rows
            if str(row.get(sel_col, "")).strip() not in owned
        }
        for other in siblings:
            if other and other.lower() in label_words:
                return (
                    f"label says {other!r} but the claim reads "
                    f"{sel_col}={sorted(owned)!r}"
                )
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


#: The four result-unit categories the stager's own units manifest
#: already uses (numeric_stage.UNITS_BY_FILE). A derived or
#: consistency claim must declare one explicitly; see
#: _malformed_claim_shape and _inferred_claim_unit.
_VALID_CLAIM_UNITS = frozenset({"fraction", "percent", "count", "plain"})


def _malformed_claim_shape(claim: Claim) -> Optional[str]:
    """Check a claim's own fields before any resolution is attempted.

    Every one of this file's crash-on-writer-input findings so far
    has the same shape: a field that ``Claim.from_dict`` accepted at
    face value (an object-valued ``inputs``, a dict-valued
    ``tolerance``) turns out to hold something a downstream consumer
    cannot use (a null address, a non-string expr, a tolerance value
    that isn't numeric). Checking every such field once here, before
    ``_check_one`` resolves anything, means the next malformed field
    is caught by this same gate instead of needing its own patch at
    whichever operation it happens to reach first.

    Parameters
    ----------
    claim : Claim
        The claim to check.

    Returns
    -------
    Optional[str]
        A description of the first problem found, or ``None`` when
        the claim's fields are well-shaped enough to attempt
        resolving.
    """
    if not isinstance(claim.label, str) or not claim.label.strip():
        # A claim with no label is not a pass by omission: the
        # slide-mapping check (check_slide_spec) exists specifically
        # to catch a right number filed under the wrong row, and it
        # can only do that when there is a label to contradict.
        # Claim.from_dict defaults a missing "label" field to "",
        # which must not quietly waive this.
        return f"claim {claim.id!r} has no label"
    if claim.kind == "lookup":
        if not isinstance(claim.source, str) or not claim.source:
            return f"{claim.label}: lookup claim has no source file"
        if not isinstance(claim.key, str) or not claim.key:
            return f"{claim.label}: lookup claim has no key"
    if claim.kind in ("derived", "consistency"):
        if not isinstance(claim.expr, str) or not claim.expr.strip():
            return f"{claim.label}: claim has no expression to evaluate"
        if claim.inputs and not isinstance(claim.inputs, dict):
            return f"{claim.label}: inputs is not an object: {claim.inputs!r}"
        for var, address in (claim.inputs or {}).items():
            if not isinstance(address, str) or not address:
                return (
                    f"{claim.label}: input {var!r} is not a usable "
                    f"address: {address!r}"
                )
        # An expression can change scale in a way its inputs' own
        # units never predict -- "100 * a / b" turns two counts into
        # a percent-scale result, and nothing about "a" or "b" being
        # counts says so. Only the writer who wrote the expression
        # knows what it produces, so that unit is declared here, not
        # inferred from the inputs the way a lookup's is inferred
        # from its own source column.
        if claim.unit not in _VALID_CLAIM_UNITS:
            return (
                f"{claim.label}: {claim.kind} claim has no declared unit "
                f"(one of {sorted(_VALID_CLAIM_UNITS)}); its expression's "
                f"own scale is not something a checker can guess"
            )
    if claim.tolerance is not None:
        if not isinstance(claim.tolerance, dict):
            return f"{claim.label}: tolerance is not an object: {claim.tolerance!r}"
        if "value" in claim.tolerance:
            amount = claim.tolerance["value"]
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                return f"{claim.label}: tolerance value is not numeric: {amount!r}"
    return None


def _check_one(
    claim: Claim,
    sources: Dict[str, Any],
    units: Optional[Dict[str, Dict[str, str]]] = None,
) -> CheckResult:
    """Resolve and judge a single claim.

    Parameters
    ----------
    claim : Claim
        The claim to check.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.
    units : Optional[Dict[str, Dict[str, str]]]
        The stager's own units manifest, passed to
        :func:`_inferred_claim_unit`.

    Returns
    -------
    CheckResult
        The verdict.
    """
    base = {"claim_id": claim.id, "value": claim.value, "label": claim.label}
    problem = _malformed_claim_shape(claim)
    if problem:
        return CheckResult(verdict=VERDICT_UNRESOLVABLE, detail=problem, **base)
    if claim.kind == "lookup":
        address = f"{claim.source}:{claim.key}"
        base = {
            **base,
            "addresses": (address,),
            "unit": _inferred_claim_unit(claim, units),
        }
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
        base = {
            **base,
            "addresses": tuple(
                v for v in (claim.inputs or {}).values() if isinstance(v, str)
            ),
            "unit": _inferred_claim_unit(claim, units),
        }
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
        except (SyntaxError, RecursionError) as exc:
            return CheckResult(verdict=VERDICT_UNRESOLVABLE, detail=str(exc), **base)
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        if not used & set(names):
            # A derived or consistency claim is supposed to be
            # recomputed from staged data, not restate its own
            # assertion as a constant: expr="999" with no (or no
            # referenced) input evaluates to 999 regardless of
            # anything staged, so a fabricated value would "verify"
            # against nothing.
            return CheckResult(
                verdict=VERDICT_UNRESOLVABLE,
                detail=(
                    f"{claim.label}: expr {claim.expr!r} does not reference "
                    f"any resolved input, cannot be recomputed from staged "
                    f"data"
                ),
                **base,
            )
        try:
            truth = _safe_eval(tree, names)
            if isinstance(truth, (int, float)) and not isinstance(truth, bool):
                # A result can evaluate fine -- no exception at all --
                # and still be unusable: Python ints have no fixed
                # size, so "a ** 1000" with a plain int base computes
                # instantly but is far too large for the float
                # conversion every later comparison performs.
                # Converting here, inside the same handler already
                # wrapped around evaluation, means that overflow is
                # caught at the same point an already-too-large float
                # result already was, instead of surfacing later in
                # _within_tolerance, outside any handler at all.
                truth = float(truth)
        except (
            ValueError,
            ZeroDivisionError,
            TypeError,
            OverflowError,
            RecursionError,
            MemoryError,
        ) as exc:
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
        if not ok:
            # A wrong number is the more useful message of the two,
            # so it is reported before any label problem, the same
            # priority order the lookup path above uses.
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
        contradiction = _derived_label_contradiction(claim, claim.inputs or {}, sources)
        if contradiction:
            return CheckResult(
                verdict=VERDICT_FAILED,
                expected=truth,
                reason="label_mismatch",
                detail=f"{claim.label}: {contradiction}",
                **base,
            )
        # A pass shows its working too: a verdict nobody can inspect
        # is the shape this design exists to remove, and explaining
        # only rejections leaves the half a report's author most
        # wants to defend resting on trust.
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
        verdict=VERDICT_UNRESOLVABLE,
        detail=f"unknown claim kind: {claim.kind}",
        **base,
    )


def _tag_figures(text: str) -> List[Tuple[str, Optional[str], int]]:
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
    List[Tuple[str, Optional[str], int]]
        ``(figure, claim_id, position)`` in order of appearance;
        ``claim_id`` is ``None`` when the figure carries no tag.
        ``position`` is the figure's own character offset in
        ``_mask_ordered_list_markers(_normalize_numeric_text(text))``
        -- every substitution either function makes preserves
        character length, so the offset is valid against the
        caller's own ``text`` too, and against that same prepared
        text if the caller needs to extract a sentence around it.
    """
    cleaned = _mask_ordered_list_markers(_normalize_numeric_text(text or ""))
    tokens: List[Tuple[int, str, str]] = []
    for m in _NUMBER_RE.finditer(cleaned):
        figure = m.group(1)
        # A literal '%' immediately after the digits changes what
        # number is actually being asserted ("0.481%" is 0.00481 for
        # a fraction-scale claim, 0.5 for a percent-scale one) --
        # _percent_aware_match is where that scale decision actually
        # happens. _NUMBER_RE itself never captures the sign, so
        # extract_numbers (which many other things rely on returning
        # the bare digits) is unaffected; only this function's own
        # figures carry it forward.
        if cleaned[m.end() : m.end() + 1] == "%":
            figure += "%"
        tokens.append((m.start(), "num", figure))
    for m in _CLAIM_REF_RE.finditer(cleaned):
        tokens.append((m.start(), "ref", m.group(1)))
    tokens.sort(key=lambda t: t[0])

    pairs: List[Tuple[str, Optional[str], int]] = []
    pending_value: Optional[str] = None
    pending_position = 0
    for position, kind, val in tokens:
        if kind == "num":
            if pending_value is not None:
                pairs.append((pending_value, None, pending_position))
            pending_value = val
            pending_position = position
        elif pending_value is not None:
            pairs.append((pending_value, val, pending_position))
            pending_value = None
        # else: a tag with no preceding unbound number -- ignore it.
    if pending_value is not None:
        pairs.append((pending_value, None, pending_position))
    return pairs


#: A rounded restatement may not discard more than this fraction of
#: the value's own magnitude. Bounds the rounding-candidate search
#: below so a fraction cannot round all the way to an unrelated
#: integer (0.481 rounded to 0 decimal places is 0, a 100% relative
#: change, not rounding noise) -- the same scaled-tolerance principle
#: as MATERIAL_REL_TOLERANCE in numeric_grader.py, applied here to
#: what counts as "the same figure" rather than "not materially
#: different."
_ROUNDING_REL_TOLERANCE = 0.05


def _figure_matches_claim_value(
    figure: str, value: Any, tolerance: Optional[Dict[str, Any]] = None
) -> bool:
    """Check a report figure against one claim's value, rounding-tolerant.

    Parameters
    ----------
    figure : str
        The figure exactly as it appears in the report text.
    value : Any
        The claim's declared value.
    tolerance : Optional[Dict[str, Any]]
        The claim's own declared tolerance, if any. A figure within
        this tolerance of the value counts as a match even when it
        falls outside the generic rounding guess below.

    Returns
    -------
    bool
        True when the figure is the same number as the claim's value,
        allowing for prose restating it at coarser rounding (0.481
        written as "0.48" later is still the same figure, but not as
        "0" -- rounding to a figure that discards most of the
        original value is not rounding noise, it is a different
        number).
    """
    candidates = {str(value)}
    coerced = _coerce(value)
    if isinstance(coerced, float):
        candidates.add(f"{coerced:g}")
        if coerced.is_integer():
            candidates.add(str(int(coerced)))
            candidates.add(f"{int(coerced):,}")
        else:
            floor = max(abs(coerced), 1e-6) * _ROUNDING_REL_TOLERANCE
            for places in range(4):
                candidate = round(coerced, places)
                if abs(candidate - coerced) <= floor:
                    candidates.add(f"{candidate:g}")
    normalized = figure.replace(",", "")
    if figure in candidates or normalized in candidates:
        return True
    try:
        as_float = float(normalized)
    except ValueError:
        return False
    if f"{as_float:g}" in candidates:
        return True
    if tolerance is not None:
        try:
            return _within_tolerance(as_float, coerced, tolerance)
        except (TypeError, ValueError):
            return False
    return False


#: A source column ending in any of these (case-insensitive) already
#: holds a 0-100 share, not a 0-1 fraction -- "rate_pct" in
#: numeric_stage.py is the authoritative example this list exists to
#: recognize; a future staged column should follow the same
#: convention rather than adding a new suffix here.
_PERCENT_COLUMN_SUFFIXES = ("_pct", "pct", "percent", "share_pct")


def _is_percent_column(column: str) -> bool:
    """Decide whether a staged column name already holds a percent share.

    Parameters
    ----------
    column : str
        A column name, e.g. ``"rate_pct"`` or ``"r2"``.

    Returns
    -------
    bool
        True when the name matches the percent-column convention.
    """
    lowered = column.lower()
    return any(lowered.endswith(suffix) for suffix in _PERCENT_COLUMN_SUFFIXES)


def _claim_addresses(claim: Claim) -> List[str]:
    """Collect the full address of every source a claim reads.

    An address is always ``<file>:<...>:<column>``. Used to infer a
    claim's unit from its own addressing (by column name, or against
    a units manifest), never to resolve a value.

    Parameters
    ----------
    claim : Claim
        The claim to inspect.

    Returns
    -------
    List[str]
        One entry per address the claim reads (one for a lookup, one
        per input for a derived or consistency claim); empty when
        the claim has no usable addressing at all.
    """
    addresses: List[str] = []
    if (
        claim.kind == "lookup"
        and isinstance(claim.source, str)
        and claim.source
        and isinstance(claim.key, str)
        and claim.key
    ):
        addresses.append(f"{claim.source}:{claim.key}")
    elif claim.kind in ("derived", "consistency") and isinstance(claim.inputs, dict):
        addresses.extend(v for v in claim.inputs.values() if isinstance(v, str))
    return [address for address in addresses if address]


def _claim_source_columns(claim: Claim) -> List[str]:
    """Collect the trailing column name of every address a claim reads.

    An address is always ``<file>:<...>:<column>``, so the column is
    whatever follows the last ``:``.

    Parameters
    ----------
    claim : Claim
        The claim to inspect.

    Returns
    -------
    List[str]
        One entry per address the claim reads; empty when the claim
        has no usable addressing at all.
    """
    return [address.rsplit(":", 1)[-1] for address in _claim_addresses(claim)]


def _column_unit_from_manifest(
    units: Optional[Dict[str, Dict[str, str]]], address: str
) -> Optional[str]:
    """Look up an address's unit in the stager's own units manifest.

    The manifest is harness-authored ground truth (written by
    ``numeric_stage.stage_case``, retained outside the writer's
    workspace the same way the trusted hash manifest is), so it
    takes priority over the column-name heuristic whenever it
    declares an address's file and column.

    Parameters
    ----------
    units : Optional[Dict[str, Dict[str, str]]]
        ``{file: {column: "fraction"|"percent"}}``, or ``None``/empty
        when no manifest is available.
    address : str
        A staged address, e.g. ``"metrics.json:median_ape"``.

    Returns
    -------
    Optional[str]
        ``"fraction"`` or ``"percent"`` when the manifest declares
        this address's file and column, otherwise ``None``.
    """
    if not units or ":" not in address:
        return None
    file_name, _, rest = address.partition(":")
    if not rest:
        return None
    column = rest.rsplit(":", 1)[-1]
    file_units = units.get(file_name)
    if not isinstance(file_units, dict):
        return None
    unit = file_units.get(column)
    return unit if unit in ("fraction", "percent") else None


def _inferred_claim_unit(
    claim: Claim, units: Optional[Dict[str, Dict[str, str]]] = None
) -> Optional[str]:
    """Decide whether a claim's own value is a fraction or a percent.

    A lookup's unit always comes from its own addressing: an
    explicit ``claim.unit`` first, otherwise every address the claim
    reads resolved against ``units`` (the stager's own manifest),
    falling back to the column-name heuristic only for an address
    the manifest does not cover. A derived or consistency claim's
    unit comes *only* from its own declaration -- never inferred
    from its inputs' addresses -- because the expression in between
    can change the scale in a way no input's own unit predicts ("100
    * a / b" turns two counts into a percent-scale result).
    :func:`_malformed_claim_shape` already guarantees a derived or
    consistency claim reaching this function has a valid declared
    unit; a ``"count"`` or ``"plain"`` declaration (not
    percent-scale at all) returns ``None`` here, the same as "truly
    undetermined" -- such a claim's figure is never expected to
    carry a '%' sign in the first place.

    Reading some percent and some non-percent addresses for one
    lookup claim (a ratio mixing a percent column with a non-percent
    one, which this trial does not actually do), or reading no
    column at all, is genuinely undetermined, not a default -- a
    caller that needs the unit to resolve a '%'-suffixed figure must
    treat ``None`` as a problem to report, not a reason to guess
    "fraction" and risk silently over- or under-rejecting a correct
    figure.

    Parameters
    ----------
    claim : Claim
        The claim to inspect.
    units : Optional[Dict[str, Dict[str, str]]]
        The stager's own units manifest, from
        :func:`numeric_verify.verify_workspace`'s trusted sibling
        copy; ``None`` when none is available, which leaves every
        lookup address to the column-name heuristic exactly as before.

    Returns
    -------
    Optional[str]
        ``"fraction"`` or ``"percent"``, or ``None`` when neither an
        explicit declaration nor the claim's own addressing settles it.
    """
    if claim.kind in ("derived", "consistency"):
        return claim.unit if claim.unit in ("fraction", "percent") else None
    if claim.unit in ("fraction", "percent"):
        return claim.unit
    addresses = _claim_addresses(claim)
    if not addresses:
        return None
    resolved = set()
    for address in addresses:
        manifest_unit = _column_unit_from_manifest(units, address)
        if manifest_unit is not None:
            resolved.add(manifest_unit)
            continue
        column = address.rsplit(":", 1)[-1]
        resolved.add("percent" if _is_percent_column(column) else "fraction")
    if len(resolved) == 1:
        return resolved.pop()
    return None


def _percent_aware_match(
    stated: Any,
    value: Any,
    unit: Optional[str],
    tolerance: Optional[Dict[str, Any]] = None,
    compare: Callable[[str, Any, Optional[Dict[str, Any]]], bool] = (
        _figure_matches_claim_value
    ),
) -> Tuple[bool, Optional[str]]:
    """Compare a stated figure against a claim's value, unit-aware.

    The one rule used by the prose, slide-cell and slide-title
    paths alike: a figure with no '%' is compared at face value,
    whatever the claim's unit; a figure with one is scaled by 100
    first only when the claim is on the fraction scale ("48.1%" is
    0.481), and taken at face value when the claim is already on the
    percent scale ("50%" is 50.0, not 0.5) -- never both ways for the
    same figure, and never guessed at when the unit cannot be
    determined at all.

    Parameters
    ----------
    stated : Any
        The figure as written (a slide cell's own value, or a figure
        string already extracted from prose or a title).
    value : Any
        The claim's declared value.
    unit : Optional[str]
        From :func:`_inferred_claim_unit`: ``"fraction"``,
        ``"percent"``, or ``None`` when undetermined.
    tolerance : Optional[Dict[str, Any]]
        The claim's own declared tolerance, if any.
    compare : Callable[[str, Any, Optional[Dict[str, Any]]], bool]
        What "matches at face value" means once the percent scale
        question is settled. Prose restating a figure at coarser
        rounding is still the same figure, so prose (the default)
        uses :func:`_figure_matches_claim_value`'s rounding-tolerant
        rule; a slide is supposed to literally restate the verified
        value, so the slide paths pass :func:`_within_tolerance`
        instead, which demands an exact (or explicitly toleranced)
        match. Only this choice differs between the three callers --
        the percent-scale decision below is identical for all of them.

    Returns
    -------
    Tuple[bool, Optional[str]]
        ``(matches, problem)``. ``problem`` is set, and ``matches``
        is always ``False``, only when the figure carries a '%' and
        ``unit`` could not be determined -- a named problem, not a
        silent pass in either direction.
    """
    figure = str(stated)
    if not figure.endswith("%"):
        return compare(figure, value, tolerance), None
    if unit is None:
        return False, (
            f"figure {figure!r} carries a percent sign, but whether the "
            f"claim it is checked against is a fraction or already a "
            f"percent could not be determined"
        )
    try:
        raw = float(figure[:-1].replace(",", ""))
    except ValueError:
        return False, None
    scaled = raw if unit == "percent" else raw / 100
    return compare(f"{scaled:g}", value, tolerance), None


def check_claims(
    claims: Sequence[Claim],
    sources: Dict[str, Any],
    report_text: Optional[str] = None,
    coverage_claims: Optional[Sequence[Claim]] = None,
    units: Optional[Dict[str, Dict[str, str]]] = None,
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
    units : Optional[Dict[str, Dict[str, str]]]
        The stager's own units manifest, passed to every
        :func:`_inferred_claim_unit` call this function makes.

    Returns
    -------
    List[CheckResult]
        One result per claim, followed by one per uncovered or
        wrongly-tagged figure.
    """
    results = [_check_one(c, sources, units) for c in claims]
    if report_text is None:
        return results

    coverage_lookup = {
        c.id: c for c in (coverage_claims if coverage_claims is not None else claims)
    }
    # Deliberately NOT masked: _tag_figures masks an ordered-list
    # marker only for its own number-extraction pass, and that
    # masking preserves every character's position, so the figure
    # positions it returns are equally valid against this unmasked
    # text. _sentence_at needs the marker's digits intact -- masked,
    # _line_boundaries could never match a numbered list item's own
    # line start, and a numbered list would merge into one sentence.
    normalized_text = _normalize_numeric_text(report_text)
    for figure, ref_id, position in _tag_figures(report_text):
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
        matched, unit_problem = _percent_aware_match(
            figure, claim.value, _inferred_claim_unit(claim, units), claim.tolerance
        )
        if not matched:
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
                        unit_problem
                        or (
                            f"figure {figure} is tagged [{ref_id}], but that "
                            f"claim's value is {claim.value}"
                        )
                    ),
                )
            )
            continue
        # The tag and value both check out, but the sentence stating
        # the figure is the writer's own prose, and nothing above
        # reads it: #827 step 2 -- a right number under a correct
        # claim can still sit in a sentence that names the wrong row.
        sentence = _sentence_at(normalized_text, position)
        contradiction = _prose_label_contradiction(claim, sentence, sources)
        if contradiction:
            results.append(
                CheckResult(
                    claim_id=f"uncovered:{figure}",
                    verdict=VERDICT_UNCOVERED,
                    value=figure,
                    expected=claim.value,
                    detail=(
                        f"figure {figure} is tagged [{ref_id}], but its own "
                        f"sentence contradicts that claim: {contradiction}"
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
                    unit="plain",
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
                    unit="percent",
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
    units: Optional[Dict[str, Dict[str, str]]] = None,
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
    units : Optional[Dict[str, Dict[str, str]]]
        The stager's own units manifest, passed through to
        :func:`check_claims`.

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
        units=units,
    )


def _owned_rows_for_addresses(
    addresses: Sequence[str],
) -> Dict[Tuple[str, str], Set[str]]:
    """Collect which rows a set of staged addresses actually reads.

    The same extraction :func:`_derived_label_contradiction` and
    :func:`_prose_label_contradiction` each do for a claim's own
    inputs, factored out so a consumer that only has a
    :class:`CheckResult`'s ``addresses`` -- not the original
    ``Claim`` -- can run the same sibling-row check.

    Parameters
    ----------
    addresses : Sequence[str]
        Staged addresses, e.g. ``"per_crop.csv:crop=maize:r2"``.

    Returns
    -------
    Dict[Tuple[str, str], Set[str]]
        ``(source file, selector column)`` to the selector values
        actually read.
    """
    owned: Dict[Tuple[str, str], Set[str]] = {}
    for address in addresses:
        parts = address.split(":")
        if len(parts) < 2 or "=" not in parts[1]:
            continue
        source_file = parts[0]
        sel_col, sel_val = parts[1].split("=", 1)
        owned.setdefault((source_file, sel_col), set()).add(sel_val)
    return owned


def _text_contradicts_owned_rows(
    text: str,
    owned_by_column: Dict[Tuple[str, str], Set[str]],
    sources: Dict[str, Any],
) -> Optional[str]:
    """Report text naming a sibling row of something it does not own.

    The shared core of :func:`label_contradiction`,
    :func:`_derived_label_contradiction` and
    :func:`_prose_label_contradiction`: naming every row a claim
    actually reads earns the text the right to mention others; naming
    only some of them does not.

    Parameters
    ----------
    text : str
        The text to check (a label, a sentence, a slide title).
    owned_by_column : Dict[Tuple[str, str], Set[str]]
        From :func:`_owned_rows_for_addresses`.
    sources : Dict[str, Any]
        Output of :func:`load_sources`.

    Returns
    -------
    Optional[str]
        A description of the contradiction, or ``None``.
    """
    text_words = _words_in(text)
    for (source_file, sel_col), owned in owned_by_column.items():
        rows = sources.get(source_file)
        if not isinstance(rows, list) or not rows:
            continue
        if all(v.lower() in text_words for v in owned):
            continue
        siblings = {
            str(row.get(sel_col, "")).strip()
            for row in rows
            if str(row.get(sel_col, "")).strip() not in owned
        }
        for other in siblings:
            if other and other.lower() in text_words:
                return (
                    f"text says {other!r} but the claim reads "
                    f"{sel_col}={sorted(owned)!r}"
                )
    return None


def _extract_figures_with_percent(text: str) -> List[str]:
    """Pull every figure out of text, keeping a trailing '%' on each.

    Like :func:`extract_numbers`, but a figure's own literal '%' is
    kept as part of its text, for a caller (the slide-title check)
    that must scale a percent-suffixed figure correctly rather than
    silently compare its bare digits. Kept separate from
    :func:`extract_numbers` so that function's existing contract (the
    bare digits, no '%'), relied on by coverage-gap detection, is
    unaffected.

    Parameters
    ----------
    text : str
        Slide title text.

    Returns
    -------
    List[str]
        Figures as they appear, each with its own '%' if it has one.
    """
    cleaned = _normalize_numeric_text(text or "")
    cleaned = _ORDERED_LIST_MARKER_RE.sub(" ", cleaned)
    figures = []
    for m in _NUMBER_RE.finditer(cleaned):
        figure = m.group(1)
        if cleaned[m.end() : m.end() + 1] == "%":
            figure += "%"
        figures.append(figure)
    return figures


def check_slide_spec(
    spec: Dict[str, Any],
    accepted: Dict[str, CheckResult],
    labels: Dict[str, str],
    sources: Optional[Dict[str, Any]] = None,
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
    sources : Optional[Dict[str, Any]]
        Output of :func:`load_sources`. When given, a title figure
        bound to exactly one backing claim is also checked against
        that claim's own row provenance, the same sibling-row rule a
        cell's label already gets -- "Wheat R2 is 0.481" must not
        pass just because 0.481 happens to be an accepted cell's
        value, if that cell's claim actually reads maize. Omitting it
        preserves the old membership-only behavior for callers that
        have no staged data to check against.

    Returns
    -------
    List[str]
        One message per violation; empty when the deck is clean.
    """
    problems: List[str] = []
    for index, slide in enumerate(spec.get("slides", []), start=1):
        where_slide = f"slide {index}"
        # A cell's claim id is a dict key below; a writer-supplied
        # value that isn't a string (a list, an object) is unhashable
        # and would crash that lookup. Treat it the same as "no claim
        # id" for the purposes of the title check, and report it
        # explicitly in the main loop below.
        backed: List[Tuple[str, Any, Tuple[str, ...], Optional[str]]] = [
            (cell["claim"], result.value, result.addresses, result.unit)
            for cell in slide.get("cells", [])
            if isinstance(cell.get("claim"), str)
            for result in [accepted.get(cell["claim"])]
            if result is not None and result.verdict == VERDICT_VERIFIED
        ]
        title = str(slide.get("title") or "")
        for figure in _extract_figures_with_percent(title):
            matched_results = [
                _percent_aware_match(
                    figure, value, unit, None, compare=_within_tolerance
                )
                for _, value, _, unit in backed
            ]
            matches = [
                (claim_id, addresses)
                for (claim_id, _, addresses, _), (matched, _) in zip(
                    backed, matched_results
                )
                if matched
            ]
            unit_problem = next(
                (problem for _, problem in matched_results if problem), None
            )
            if not matches:
                if unit_problem:
                    problems.append(f"{where_slide}: {unit_problem}")
                else:
                    problems.append(
                        f"{where_slide}: title states {figure}, which no "
                        f"accepted cell on this slide backs"
                    )
                continue
            if sources is None:
                continue
            # A figure matching more than one cell's value is bound
            # to whichever of them the title's own words do not rule
            # out -- it is only a contradiction when every candidate
            # claim this figure could be is contradicted.
            contradictions = [
                _text_contradicts_owned_rows(
                    title, _owned_rows_for_addresses(addresses), sources
                )
                for _, addresses in matches
            ]
            if all(contradictions):
                problems.append(
                    f"{where_slide}: title states {figure}, but its own "
                    f"words contradict every claim that could back it: "
                    f"{contradictions[0]}"
                )
        for cell in slide.get("cells", []):
            value = cell.get("value")
            claim_id = cell.get("claim")
            where = where_slide
            if claim_id is not None and not isinstance(claim_id, str):
                problems.append(f"{where}: claim id is not a string: {claim_id!r}")
                continue
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
            matched, unit_problem = _percent_aware_match(
                value, result.value, result.unit, None, compare=_within_tolerance
            )
            if unit_problem:
                problems.append(f"{where}: {unit_problem}")
                continue
            if not matched:
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
            # all makes that unverifiable, not satisfied. Neither
            # branch is gated on expected_label itself being truthy --
            # an empty expected_label is exactly as unverifiable as a
            # missing cell_label, not a reason to skip the check.
            if not cell_label:
                problems.append(
                    f"{where}: claim {claim_id} has no label on the "
                    f"slide cell; expected {expected_label!r}"
                )
            elif cell_label != expected_label:
                problems.append(
                    f"{where}: claim {claim_id} is {expected_label!r} "
                    f"but appears under {cell_label!r}"
                )
            else:
                # The label matched verbatim, but a figure embedded
                # inside it (a confidence interval, a sample size) is
                # not itself checked by that match -- "maize R2 (99%
                # confidence)" matches its own expected label exactly
                # regardless of what the 99 is. Every embedded figure
                # must be backed by some accepted claim on this
                # slide, the same bar a slide title's own figures
                # already have to clear.
                for figure in extract_numbers(cell_label):
                    if not any(
                        _within_tolerance(figure, backed_value, None)
                        for _, backed_value, _, _ in backed
                    ):
                        problems.append(
                            f"{where}: label {cell_label!r} states "
                            f"{figure}, which no accepted cell on "
                            f"this slide backs"
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
