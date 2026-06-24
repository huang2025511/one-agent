"""Path-containment security helpers.

Consolidates the ``Path.relative_to()``-based directory-traversal
checks that were previously duplicated across:

  - ``multimodal/__init__.py``  (5 sites — image/audio/file path checks)
  - ``api/__init__.py``         (4 sites — skills/documents path checks)
  - ``skills/document_search.py`` (1 site — ingest root containment)

All checks use ``Path.relative_to()`` rather than ``str.startswith()``
because ``startswith`` can be bypassed by sibling directories that share
a prefix (e.g. ``/data/skills_evil`` bypasses a ``/data/skills`` check).
``relative_to`` raises ``ValueError`` if the path is not strictly inside
the base directory, which is the correct containment semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, Path]


def is_path_within(path: PathLike, base: PathLike) -> bool:
    """Return ``True`` if ``path`` resolves to a location inside ``base``.

    Both arguments are resolved (symlinks followed) before the
    containment check, so symlink-based escapes are caught.

    Args:
        path: The path to check.
        base: The directory that must contain ``path``.

    Returns:
        ``True`` if ``path`` is inside ``base``, ``False`` otherwise.
    """
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def is_path_within_any(path: PathLike, bases: Iterable[PathLike]) -> bool:
    """Return ``True`` if ``path`` is inside any of ``bases``.

    Equivalent to ``any(is_path_within(path, b) for b in bases)`` but
    resolves ``path`` only once.
    """
    resolved = Path(path).resolve()
    for base in bases:
        try:
            resolved.relative_to(Path(base).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def validate_path_within(path: PathLike, base: PathLike) -> Path:
    """Resolve ``path`` and verify it is inside ``base``.

    Args:
        path: The path to validate.
        base: The directory that must contain ``path``.

    Returns:
        The resolved ``Path`` of ``path`` (symlinks followed).

    Raises:
        ValueError: If ``path`` is not inside ``base`` or cannot be
            resolved.
    """
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(Path(base).resolve())
    except ValueError as exc:
        raise ValueError(
            f"path '{resolved}' is outside allowed directory '{base}'"
        ) from exc
    except OSError as exc:
        raise ValueError(f"cannot resolve path '{path}': {exc}") from exc
    return resolved
