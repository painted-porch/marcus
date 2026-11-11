"""
Run guards: prove a run executed the code its record names.

* :func:`tree_hash` hashes the files that execute, the construction the
  Minecraft image guard uses (sorted path, length, bytes); a missing file is
  a refusal, never a quietly shorter hash.
* :func:`committed_hash` hashes the same files as committed in a git
  repository, so uncommitted edits cannot satisfy the guard.
* :func:`git_version` is the ``marcus-version.txt`` record: checkout,
  branch, commit, dirty file count.

Lifted from the Minecraft plumbing's ``plumbing_hash.py``.
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404 - git on a path we own, fixed argv
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence


def tree_hash(root: Path, files: Sequence[str]) -> str:
    """
    Hash the named files under ``root``.

    Parameters
    ----------
    root : Path
        The directory the run executes from.
    files : Sequence[str]
        The files that execute, relative to ``root``.

    Returns
    -------
    str
        A sha256 hex digest over each file's relative path, byte length and
        bytes, in sorted order.

    Raises
    ------
    FileNotFoundError
        If ``root`` is not a directory or any named file is missing.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    digest = hashlib.sha256()
    for name in sorted(files):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"missing from {root}: {name}")
        content = path.read_bytes()
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(content)).encode())
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def committed_hash(
    repo: Path, files: Sequence[str], subdir: str = "", ref: str = "HEAD"
) -> str:
    """
    Hash the same files as they are committed in ``repo`` at ``ref``.

    Parameters
    ----------
    repo : Path
        The repository root.
    files : Sequence[str]
        The files, relative to ``subdir``.
    subdir : str
        Where they live inside the repository (``""`` for the root).
    ref : str
        The commit to read.

    Returns
    -------
    str
        The same construction as :func:`tree_hash`, over the committed bytes.

    Raises
    ------
    FileNotFoundError
        If git cannot be run, the ref does not exist, or a file is not in that commit.
    """
    digest = hashlib.sha256()
    prefix = f"{subdir.rstrip('/')}/" if subdir else ""
    for name in sorted(files):
        try:
            content = subprocess.run(  # nosec B603 B607
                ["git", "-C", str(repo), "show", f"{ref}:{prefix}{name}"],
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise FileNotFoundError(
                f"not in {repo} at {ref}: {prefix}{name} ({error})"
            ) from error
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(content)).encode())
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def uncommitted(repo: Path, files: Sequence[str], subdir: str = "") -> List[str]:
    """
    Which of the files have uncommitted changes in ``repo``.

    Parameters
    ----------
    repo : Path
        The repository root.
    files : Sequence[str]
        The files, relative to ``subdir``.
    subdir : str
        Where they live inside the repository.

    Returns
    -------
    List[str]
        Paths git reports as changed; empty when git cannot answer.
    """
    prefix = f"{subdir.rstrip('/')}/" if subdir else ""
    try:
        out = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain",
                "--",
                *(f"{prefix}{n}" for n in files),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def git_version(checkout: Path) -> Dict[str, str]:
    """
    Read the version record of a checkout: what ``marcus-version.txt`` holds.

    Parameters
    ----------
    checkout : Path
        A git working directory.

    Returns
    -------
    Dict[str, str]
        ``checkout``, ``branch``, ``commit`` and ``dirty`` (a count of changed
        files, as text).
    """

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(checkout), *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()  # nosec B603 B607
        except (OSError, subprocess.CalledProcessError):
            return ""

    dirty = git("status", "--porcelain")
    return {
        "checkout": str(checkout),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": str(len([line for line in dirty.splitlines() if line.strip()])),
    }


def format_version(version: Dict[str, str]) -> str:
    """Render :func:`git_version` the way the Minecraft run records print it."""
    return (
        f"checkout: {version['checkout']}\nbranch:   {version['branch']}\n"
        f"commit:   {version['commit']}\ndirty:    {version['dirty']} files\n"
    )


def contains_commit(checkout: Path, sha: str) -> bool:
    """
    Whether ``checkout``'s HEAD descends from ``sha``.

    Parameters
    ----------
    checkout : Path
        A git working directory.
    sha : str
        A commit, full or abbreviated.

    Returns
    -------
    bool
    """
    try:
        subprocess.run(
            ["git", "-C", str(checkout), "merge-base", "--is-ancestor", sha, "HEAD"],
            capture_output=True,
            check=True,
        )  # nosec B603 B607
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    """Print the hash of a directory or of a repository's committed tree."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] != "--committed":
        try:
            print(tree_hash(Path(args[0]), args[1].split(",")))
        except FileNotFoundError as error:
            print(f"integrity: {error}", file=sys.stderr)
            return 2
        return 0
    if len(args) >= 3 and args[0] == "--committed":
        try:
            print(
                committed_hash(
                    Path(args[1]),
                    args[2].split(","),
                    subdir=args[3] if len(args) > 3 else "",
                )
            )
        except FileNotFoundError as error:
            print(f"integrity: {error}", file=sys.stderr)
            return 2
        return 0
    print(
        "usage: marcus-runner-hash <dir> <file,file,...>"
        " | --committed <repo> <file,file,...> [subdir]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
