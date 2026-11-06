"""
Marcus config-path resolution: ``MARCUS_CONFIG`` always wins when set.

Issue #891: ``MARCUS_CONFIG`` was honored inconsistently across four
independent config-loading code paths. Two checked it first
(``MarcusConfig.get_config()``, the legacy ``ConfigLoader``), one
checked it LAST -- after its other candidate locations, so an
explicit override could lose to an ambient file even when both
existed (``WorkspaceManager._load_default_config``) -- and one never
checked it at all (``KanbanClient._load_config``). An isolated
experiment or live check that sets ``MARCUS_CONFIG`` expecting Marcus
to honor it everywhere could not actually rely on that.

This module centralizes the one invariant every one of those loaders
actually shares -- the env override, when set, is always the first
candidate -- the same way :mod:`src.core.paths` centralizes
``MARCUS_DATA_DIR`` resolution for #724/#792. A future config-loading
path that builds its own candidate list from scratch cannot
reintroduce the drift by getting the order wrong, because the order
is no longer its own to get wrong.

What stays separate, on purpose: each caller still builds its own
fallback list (cwd, repo root, XDG, a single literal default -- these
differ genuinely by component) and decides what "nothing found" means
(raise, log and fall through, or a provider's own tolerant default).
Unifying that part too would risk changing behavior these callers
rely on for reasons unrelated to this issue.
"""

import os
from pathlib import Path
from typing import List, Union

#: The environment variable every config-loading path must check first.
CONFIG_PATH_ENV = "MARCUS_CONFIG"


def resolve_config_candidates(*fallbacks: Union[str, Path]) -> List[Path]:
    """Build a config-path candidate list with the env override first.

    Parameters
    ----------
    *fallbacks : Union[str, Path]
        This caller's own fallback locations, in priority order, used
        only when ``MARCUS_CONFIG`` is unset.

    Returns
    -------
    List[Path]
        The ``MARCUS_CONFIG`` override (if set) followed by
        ``fallbacks``, in order.
    """
    candidates: List[Path] = []
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        candidates.append(Path(override))
    candidates.extend(Path(f) for f in fallbacks)
    return candidates
