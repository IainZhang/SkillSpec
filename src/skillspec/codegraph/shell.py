"""ShExtractor: `foo() {...}` defines, `foo args` calls; resolve.py drops commands naming no defined function."""

from __future__ import annotations

import tree_sitter_bash as tsbash
from tree_sitter import Language, Node, Parser

from .grammar import NodeSpec
from .walk import BaseExtractor

SH_PARSER = Parser(Language(tsbash.language()))

_SH_SPEC = NodeSpec(
    name_node="command_name",
    call_nodes=frozenset({"command"}),
    callee_field="name",
    block_nodes=frozenset({"compound_statement", "subshell"}),
)  # no member_access_node: shell has no member access


class ShExtractor(BaseExtractor):
    spec = _SH_SPEC

    def on_function_definition(self, node: Node) -> None:
        name = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "word"), None)
        if name is None:
            return
        body = node.child_by_field_name("body") or next(
            (c for c in node.children if c.type in self.spec.block_nodes), None)
        self.emit_func(self.text(name), [], None, is_method=False, body_node=body,
                             identifier=name, def_node=node)

    def on_variable_assignment(self, node: Node) -> None:
        name = node.child_by_field_name("name")
        if name is not None:
            self.emit_global_var(self.text(name), None, node)
        for c in node.children:
            self.walk(c)
