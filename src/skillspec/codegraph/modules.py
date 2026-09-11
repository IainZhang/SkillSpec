"""Repo-relative path -> dotted module identity (+ JS/TS relative-specifier resolution)."""

from __future__ import annotations

import posixpath

SUPPORTED_EXTS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh", ".bash"})
_MODULE_EXTS = SUPPORTED_EXTS | {".pyi"}  # also strip .pyi stubs (never parsed)


def module_path_of(rel_path: str) -> str:
    """Repo-relative path -> dotted module ('scripts/util.py' -> 'scripts.util'); strips index/__init__."""
    p = rel_path.replace("\\", "/")
    for ext in _MODULE_EXTS:
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    for tail in ("/__init__", "/index"):
        if p.endswith(tail):
            p = p[: -len(tail)]
    return p.strip("/").replace("/", ".")


def resolve_relative_module(importer_rel_path: str, spec: str) -> str | None:
    """Resolve a JS/TS relative specifier to a module path; None if bare/external."""
    if not (spec.startswith(".") or spec.startswith("/")):
        return None  # bare specifier (e.g. 'react') -> external
    base_dir = posixpath.dirname(importer_rel_path.replace("\\", "/"))
    return module_path_of(posixpath.normpath(posixpath.join(base_dir, spec)))
