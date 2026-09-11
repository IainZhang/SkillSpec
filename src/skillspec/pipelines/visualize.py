"""Workflow DAG and code callgraph rendering — presentation only, kept out of the graph schemas."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

try:
    import graphviz
except ImportError:  # presentation-only; rendering degrades to a no-op when graphviz is absent
    graphviz = None

from skillspec.models import CodeGraph, EdgeKind, NodeKind, WorkflowGraph

logger = logging.getLogger(__name__)

# Shared graphviz attributes — only rankdir and label vary between graphs.
_GRAPH_ATTR = {
    "bgcolor": "white", "fontname": "Helvetica", "labelloc": "b",
    "fontsize": "10", "fontcolor": "#6b7280",
    "nodesep": "0.35", "ranksep": "0.55", "pad": "0.3",
}
_NODE_ATTR = {"fontname": "Helvetica", "fontsize": "11", "penwidth": "1.2"}
_EDGE_ATTR = {"arrowsize": "0.8"}

# Shared palette: (fill, border, font).
_BLUE = ("#dbeafe", "#60a5fa", "#1e3a8a")   # light card — plain op / real function
_GREY = ("#e5e7eb", "#9ca3af", "#374151")   # grey box — structural node / module-level caller
_EDGE = "#334155"                            # solid dependency / call edge


def _new_digraph(rankdir: str, label: str):
    return graphviz.Digraph(
        format="png",
        graph_attr={**_GRAPH_ATTR, "rankdir": rankdir, "label": label},
        node_attr=_NODE_ATTR,
        edge_attr=_EDGE_ATTR,
    )


def _colors(style: tuple[str, str, str]) -> dict[str, str]:
    fill, border, font = style
    return {"fillcolor": fill, "color": border, "fontcolor": font}


def _render(dot, path: Path, who: str) -> None:
    try:
        # cleanup=False: the extensionless dot source ("workflow"/"codegraph") is itself a
        # retained artifact — it must survive even when the PNG render succeeds.
        dot.render(str(path.with_suffix("")), cleanup=False)
    except Exception as exc:
        logger.warning("%s: PNG export failed — %s", who, exc)


def _ensure_renderable(nodes: dict, who: str) -> bool:
    """True if there's something to draw and graphviz is available; warns (then False) when it's missing."""
    if not nodes:
        return False
    if graphviz is None:
        logger.warning("%s: graphviz not installed — skipping PNG.", who)
        return False
    return True


def draw_workflow(wf: WorkflowGraph, path: Path) -> None:
    """Render the workflow DAG to a PNG."""
    if not _ensure_renderable(wf.nodes, "draw_workflow"):
        return

    dot = _new_digraph(
        "LR",
        "folder = structural   ·   card = operation (darker = more code)"
        "   ·   solid → dependency   ·   dashed → containment",
    )

    # Operation kinds share one hue/shape; fill intensity encodes the sub-kind
    # (ref_code darkest → inline_code → plain lightest). Structural kinds are uniform folders.
    op_style = {
        NodeKind.PLAIN:       _BLUE,
        NodeKind.INLINE_CODE: ("#93c5fd", "#3b82f6", "#1e3a8a"),
        NodeKind.REF_CODE:    ("#3b82f6", "#1d4ed8", "#ffffff"),
    }

    for name, step in wf.nodes.items():
        label = f"{name}\n{len(step.instruction.split())} words"
        attrs = {"label": label, "shape": "box"}
        if step.is_structural:
            attrs |= {"style": "filled", "fontname": "Helvetica-Bold", **_colors(_GREY)}
        else:
            attrs |= {"style": "rounded,filled", **_colors(op_style.get(step.kind, _BLUE))}
        if name == wf.entry:  # mark the start; keep the group fill so the real kind still shows
            attrs.update(color="#f59e0b", penwidth="2.5")
        dot.node(name, **attrs)

    for name, step in wf.nodes.items():
        for nxt in step.next:
            if nxt not in wf.nodes:
                continue
            if step.edge_kind(nxt) is EdgeKind.DEPENDENCY:
                dot.edge(name, nxt, color=_EDGE, penwidth="1.6")
            else:
                dot.edge(name, nxt, color="#9ca3af", style="dashed",
                         arrowhead="empty", penwidth="1.0")

    _render(dot, path, "draw_workflow")


def draw_codegraph(cg: CodeGraph, path: Path) -> None:
    """Render the code callgraph to a PNG."""
    if not _ensure_renderable(cg.nodes, "draw_codegraph"):
        return

    legend = ("box = function/method (bigger = more lines)   ·   grey = module-level caller"
              "   ·   solid → calls   ·   cluster = file")
    if cg.unparsed:
        legend += f"   ·   {len(cg.unparsed)} file(s) unparsed"

    dot = _new_digraph("TB", legend)

    by_file: dict[str, list] = defaultdict(list)
    for node in cg.nodes.values():
        by_file[node.file].append(node)
    # CodeNode ids contain "::" (graphviz reads ":" as node:port); map to safe ids for node/edge refs
    gid = {nid: f"n{i}" for i, nid in enumerate(sorted(cg.nodes))}

    for i, file in enumerate(sorted(by_file)):
        with dot.subgraph(name=f"cluster_{i}") as sub:
            sub.attr(label=file, color="#cbd5e1", style="rounded",
                     fontsize="10", fontcolor="#6b7280")
            for node in sorted(by_file[file], key=lambda n: n.start_line):
                if node.is_real:
                    lines = node.end_line - node.start_line + 1
                    # box grows with line count; clamp keeps small funcs legible and giants bounded
                    w = min(0.6 + 0.05 * lines, 4.0)
                    h = min(0.4 + 0.03 * lines, 2.5)
                    sub.node(gid[node.id], label=f"{node.name}\n{lines} lines",
                             shape="box", style="rounded,filled",
                             width=f"{w:.2f}", height=f"{h:.2f}", **_colors(_BLUE))
                else:  # synthetic module node — no line span; nohtml keeps "<module>" a literal label
                    sub.node(gid[node.id], label=graphviz.nohtml(node.name),
                             shape="box", style="filled",
                             fontname="Helvetica-Bold", **_colors(_GREY))

    for e in cg.edges:
        if e.caller in cg.nodes and e.callee in cg.nodes:
            dot.edge(gid[e.caller], gid[e.callee], color=_EDGE, penwidth="1.4")

    _render(dot, path, "draw_codegraph")
