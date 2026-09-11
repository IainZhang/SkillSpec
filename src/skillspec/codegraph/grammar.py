"""Per-language grammar seam: node-type/field names and structural markers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Param:
    name: str
    ann: str | None = None  # annotation


@dataclass(frozen=True)
class NodeSpec:
    """Per-language node-type / field names — the seam between grammars."""

    name_node: str
    call_nodes: frozenset[str]
    callee_field: str = "function"
    member_access_node: str | None = None  # None for languages without member access (shell)
    receiver_field: str = ""
    selector_field: str = ""
    self_names: frozenset[str] = frozenset()
    self_nodes: frozenset[str] = frozenset()
    build_node: str | None = None
    constructed_type_field: str = "constructor"
    transparent_nodes: frozenset[str] = frozenset()
    block_nodes: frozenset[str] = frozenset({"block", "statement_block"})
