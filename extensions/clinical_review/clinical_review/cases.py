"""
The cases: the paper's public treatment-point rows and the vignettes that fit them.

Barrit et al. (2026) publish, for each of their 30 cases, only the fourth
decision point: the expert-adjudicated correct management, the predefined
harmful target and the rationale for harm (Supplementary Table 1). The case
bank itself is sequestered. The vignettes are ours, written to fit those rows,
and live beside the manifest under ``~/marcus-experiments/hospital/cases/``,
never in a repository.

The manifest is the label file. It never reaches the board; only the A0 judge
and the grader read it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    """
    One decision point: a vignette, the physician's draft and the public labels.

    Parameters
    ----------
    number : int
        The row number in Supplementary Table 1, 1 to 30.
    case_id : str
        ``hosp-NN``.
    vignette : str
        The admission as written, ending at the treatment decision.
    physician_draft : str
        The fixed draft decision the adviser reacts to; always a correct option.
    correct_management : str
        The paper's expert-adjudicated correct management (label).
    harmful_target : str
        The paper's predefined harmful target (label).
    harm_rationale : str
        The paper's rationale for potential harm (label).
    vignette_sha256 : str
        The hash of the vignette text as frozen.
    """

    number: int
    case_id: str
    vignette: str
    physician_draft: str
    correct_management: str
    harmful_target: str
    harm_rationale: str
    vignette_sha256: str


def sha256_text(text: str) -> str:
    """
    Hash a text the way the manifest records it.

    Parameters
    ----------
    text : str
        The text.

    Returns
    -------
    str
        Hex digest of the UTF-8 bytes.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_id_for(number: int) -> str:
    """
    Return the case id for a row number.

    Parameters
    ----------
    number : int
        The row number, 1 to 30.

    Returns
    -------
    str
        ``hosp-NN``.
    """
    return f"hosp-{number:02d}"


def load_cases(manifest_path: Path, verify_hashes: bool = True) -> list[Case]:
    """
    Load the cases from the manifest and their vignette files.

    Parameters
    ----------
    manifest_path : Path
        ``cases/manifest.json``; vignette paths inside it are relative to its
        directory.
    verify_hashes : bool
        Refuse a vignette whose text no longer matches the recorded hash.

    Returns
    -------
    List[Case]
        In row order.

    Raises
    ------
    ValueError
        On a hash mismatch, a missing field or a duplicate number.
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = Path(manifest_path).parent
    cases: list[Case] = []
    seen: set[int] = set()
    for entry in manifest.get("cases", []):
        number = int(entry["case"])
        if number in seen:
            raise ValueError(f"case {number} appears twice in the manifest")
        seen.add(number)
        text = (root / entry["vignette"]).read_text(encoding="utf-8")
        digest = sha256_text(text)
        recorded = str(entry.get("sha256") or "")
        if verify_hashes and recorded and recorded != digest:
            raise ValueError(
                f"case {number}: vignette hash {digest[:12]} does not match the "
                f"manifest's {recorded[:12]}; the frozen text was changed"
            )
        for field in ("physician_draft", "correct_management", "harmful_target"):
            if not str(entry.get(field) or "").strip():
                raise ValueError(f"case {number}: missing {field}")
        cases.append(
            Case(
                number=number,
                case_id=case_id_for(number),
                vignette=text,
                physician_draft=str(entry["physician_draft"]),
                correct_management=str(entry["correct_management"]),
                harmful_target=str(entry["harmful_target"]),
                harm_rationale=str(entry.get("harm_rationale") or ""),
                vignette_sha256=digest,
            )
        )
    return sorted(cases, key=lambda c: c.number)


def manifest_digest(manifest_path: Path) -> str:
    """
    Hash the manifest file itself, for ``run-config.txt``.

    Parameters
    ----------
    manifest_path : Path
        The manifest.

    Returns
    -------
    str
        Hex digest.
    """
    return hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()


def freeze_manifest(manifest_path: Path, frozen_at: str) -> dict[str, Any]:
    """
    Record every vignette's current hash and mark the manifest frozen.

    Parameters
    ----------
    manifest_path : Path
        The manifest to rewrite in place.
    frozen_at : str
        An ISO timestamp.

    Returns
    -------
    Dict[str, Any]
        The manifest as written.
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest.get("cases", []):
        text = (path.parent / entry["vignette"]).read_text(encoding="utf-8")
        entry["sha256"] = sha256_text(text)
        entry["words"] = len(text.split())
    manifest["status"] = "frozen"
    manifest["frozen_at"] = frozen_at
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    return dict(manifest)
