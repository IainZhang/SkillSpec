"""Parse scripts into AST via Tree-sitter, then build a call graph."""

from __future__ import annotations

import logging

from skillspec.models.codegraph import CodeGraph

from .facts import FileFacts
from .javascript import JS_PARSER, JsExtractor
from .modules import SUPPORTED_EXTS, module_path_of
from .python import PY_PARSER, PyExtractor
from .resolve import build_graph
from .shell import SH_PARSER, ShExtractor
from .typescript import TS_PARSER, TSX_PARSER, TsExtractor

logger = logging.getLogger(__name__)

__all__ = [
    "build_codegraph",
    "lang_parse",
    "SUPPORTED_EXTS",
]


def _parse(rel_path: str, content: str, parser, extractor, *extra) -> FileFacts:
    src = content.encode("utf-8")
    root_ast = parser.parse(src).root_node

    facts = FileFacts(module_path=module_path_of(rel_path))
    facts.has_error = root_ast.has_error
    extractor(facts, src, *extra).walk(root_ast)
    return facts


def lang_parse(path: str, content: str) -> tuple[str, FileFacts] | None:
    """(language, facts) for a supported file, dispatched by extension; None when unsupported."""
    dot = path.rfind(".")
    if dot == -1 or path[dot:] not in SUPPORTED_EXTS:
        return None
    match path[dot:]:
        case ".py":
            return "python", _parse(path, content, PY_PARSER, PyExtractor)
        case ".ts" | ".tsx":
            parser = TSX_PARSER if path.endswith(".tsx") else TS_PARSER
            return "typescript", _parse(path, content, parser, TsExtractor, path)
        case ".js" | ".jsx" | ".mjs" | ".cjs":
            return "javascript", _parse(path, content, JS_PARSER, JsExtractor, path)
        case ".sh" | ".bash":
            return "shell", _parse(path, content, SH_PARSER, ShExtractor)
        case _:
            return None


def build_codegraph(resources: dict[str, str]) -> CodeGraph:
    """Traverse all code files to facts, then resolve into a CodeGraph."""
    facts_by_path: dict[str, FileFacts] = {}
    unparsed: list[str] = []
    languages: set[str] = set()
    for path, content in resources.items():
        try:
            parsed = lang_parse(path, content)
        except Exception:  # one malformed file never breaks the pipeline
            logger.warning("Callgraph: failed to extract %s — recorded as unparsed.", path, exc_info=True)
            unparsed.append(path)
            continue
        if parsed is None:
            continue
        language, facts = parsed
        if facts.has_error:  # tree-sitter recovered locally; facts may be partial
            logger.warning("Callgraph: %s has syntax errors — extracted best-effort, marked unparsed.", path)
            unparsed.append(path)
        facts_by_path[path] = facts
        languages.add(language)

    graph = build_graph(facts_by_path, unparsed)
    if languages:
        graph.language = ",".join(sorted(languages))
    return graph
