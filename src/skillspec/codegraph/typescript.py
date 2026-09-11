"""TsExtractor: JavaScript structure + TS type nodes."""

from __future__ import annotations

import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

from .grammar import Param
from .javascript import JsExtractor

TS_PARSER = Parser(Language(tsts.language_typescript()))
TSX_PARSER = Parser(Language(tsts.language_tsx()))


class TsExtractor(JsExtractor):
    """TypeScript = JavaScript + type nodes. Inherits structure from JsExtractor."""

    def _param_extra(self, node, out):
        if node.type in ("required_parameter", "optional_parameter"):
            pat = node.child_by_field_name("pattern")
            if pat is None or pat.type != "identifier":
                return
            out.append(Param(self.text(pat), self._annotation_type(node.child_by_field_name("type"))))

    def parse_return(self, fn_node) -> str | None:
        return self._annotation_type(fn_node.child_by_field_name("return_type"))

    def _declarator_annotation(self, node) -> str | None:
        ta = next((c for c in node.children if c.type == "type_annotation"), None)
        return self._annotation_type(ta)

    def on_public_field_definition(self, node):  # TS class field: name in `name`
        cls = self.cur_class()
        if cls is None:
            return
        name = node.child_by_field_name("name")
        if name is None:
            return
        self._bind_value(name, node.child_by_field_name("value"),
                        self._annotation_type(node.child_by_field_name("type")),
                        is_method=True, emit_type=self.emit_field_type)

    def _annotation_type(self, type_annotation) -> str | None:
        """Unwrap TS `type_annotation`, then reduce to class name via inherited `_class_name`."""
        if type_annotation is None:
            return None
        for c in type_annotation.named_children:
            return self._class_name(c)
        return None
