"""JsExtractor: defs, classes, calls, imports, bindings."""

from __future__ import annotations

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser

from .facts import FileFacts
from .grammar import NodeSpec, Param
from .modules import resolve_relative_module
from .walk import BaseExtractor

JS_PARSER = Parser(Language(tsjs.language()))

_JS_WRAPPERS = frozenset({
    "parenthesized_expression", "await_expression", "as_expression",
    "non_null_expression", "type_assertion", "satisfies_expression",
})

_JS_SPEC = NodeSpec(
    name_node="identifier",
    member_access_node="member_expression",
    receiver_field="object",
    selector_field="property",
    call_nodes=frozenset({"call_expression"}),
    self_nodes=frozenset({"this"}),
    build_node="new_expression",
    constructed_type_field="constructor",
    transparent_nodes=_JS_WRAPPERS,
    block_nodes=frozenset({"statement_block"}),
)

_FN_VALUE_NODES = ("arrow_function", "function_expression",
                   "generator_function", "generator_function_declaration")


class JsExtractor(BaseExtractor):
    spec = _JS_SPEC

    def __init__(self, facts: FileFacts, source: bytes, rel_path: str):
        super().__init__(facts, source)
        self.rel_path = rel_path  # for resolving relative import specifiers

    # --- definitions ---
    def on_function_declaration(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            return
        self.emit_func(self.text(name), self._func_params(node), self.parse_return(node),
                             is_method=False, body_node=node.child_by_field_name("body"),
                             identifier=name, def_node=node)

    on_generator_function_declaration = on_function_declaration

    def on_method_definition(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            return
        self.emit_func(self.text(name), self._func_params(node), self.parse_return(node),
                             is_method=True, body_node=node.child_by_field_name("body"),
                             identifier=name, def_node=node)

    def on_class_declaration(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            for c in node.children:
                self.walk(c)
            return
        self.emit_class(self.text(name), self.parse_bases(node), node.child_by_field_name("body"))

    on_abstract_class_declaration = on_class_declaration

    def on_class(self, node):  # class expression
        name = node.child_by_field_name("name")
        if name is not None:
            self.emit_class(self.text(name), self.parse_bases(node), node.child_by_field_name("body"))
        else:
            for c in node.children:
                self.walk(c)

    # --- params / bases ---
    def _func_params(self, fn_node) -> list:
        p = fn_node.child_by_field_name("parameters")
        if p is not None:
            return self.parse_params(p)
        single = fn_node.child_by_field_name("parameter")  # arrow single-param `x =>`
        if single is not None and single.type == "identifier":
            return [Param(self.text(single), None)]
        return []

    def parse_params(self, formal) -> list:
        out = []
        for c in formal.named_children:
            if c.type == "identifier":
                out.append(Param(self.text(c), None))
            else:
                self._param_extra(c, out)
        return out

    def _param_extra(self, node, out):
        pass

    def parse_bases(self, class_node) -> list:
        bases = []
        for c in class_node.children:
            if c.type != "class_heritage":
                continue
            for cc in c.children:
                if cc.type == "identifier":                  # JS: extends Ident
                    bases.append(self.text(cc))
                elif cc.type == "member_expression":         # JS: extends a.B
                    nm = self._class_name(cc)
                    if nm:
                        bases.append(nm)
                elif cc.type == "extends_clause":            # TS
                    for v in cc.children_by_field_name("value"):
                        nm = self._class_name(v)
                        if nm:
                            bases.append(nm)
        return bases

    def _class_name(self, node) -> str | None:
        """Reduce a type/expr node to its simple class name."""
        t = node.type
        if t in ("identifier", "type_identifier"):
            return self.text(node)
        if t == "member_expression":
            prop = node.child_by_field_name("property")
            return self.text(prop) if prop is not None else None
        if t == "nested_type_identifier":
            ids = [c for c in node.children if c.type == "type_identifier"]
            return self.text(ids[-1]) if ids else None
        if t == "generic_type":
            nm = node.child_by_field_name("name")
            return self._class_name(nm) if nm is not None else None
        if t == "union_type":
            for c in node.named_children:
                nm = self._class_name(c)
                if nm:
                    return nm
        return None

    # --- variables / assigns ---
    def _bind_value(self, name_node, value, ann, *, is_method, emit_type):
        """Function-valued binding becomes a def; otherwise record its type and walk calls."""
        fname = self.text(name_node)
        if value is not None and value.type in _FN_VALUE_NODES:
            self.emit_func(fname, self._func_params(value), self.parse_return(value),
                                 is_method=is_method, body_node=value.child_by_field_name("body"),
                                 identifier=name_node, def_node=value)
            return
        if ann:
            emit_type(fname, ann)
        if value is not None:
            emit_type(fname, self.parse_ctor(value))
            self.walk_calls(value)

    def on_lexical_declaration(self, node):
        for c in node.children:
            if c.type == "variable_declarator":
                self._declarator(c)

    on_variable_declaration = on_lexical_declaration

    def _declarator(self, node):
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or name.type != "identifier":
            if value is not None:
                self.walk(value)  # destructuring: still find nested calls/defs
            return
        ann = self._declarator_annotation(node)
        self._bind_value(name, value, ann, is_method=False, emit_type=self.emit_var_type)
        if value is None or value.type not in _FN_VALUE_NODES:
            self.emit_global_var(self.text(name), ann, node)

    def _declarator_annotation(self, node) -> str | None:
        return None  # JS has none; TS overrides

    def on_assignment_expression(self, node):
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            for c in node.children:
                self.walk(c)
            return
        if left.type == "identifier":
            is_fn = right.type in _FN_VALUE_NODES  # right is non-None (guarded above)
            self._bind_value(left, right, None, is_method=False, emit_type=self.emit_var_type)
            if not is_fn:
                self.emit_global_var(self.text(left), None, node)
        elif left.type == "member_expression":
            obj = left.child_by_field_name("object")
            prop = left.child_by_field_name("property")
            if obj is not None and prop is not None and self._is_self(obj) and self.cur_class():
                self.emit_field_type(self.text(prop), self.parse_ctor(right))
            self.walk_calls(right)
        else:
            self.walk_calls(right)

    def on_field_definition(self, node):  # JS class field: name lives in `property`
        cls = self.cur_class()
        if cls is None:
            return
        prop = node.child_by_field_name("property")
        if prop is None:
            return
        self._bind_value(prop, node.child_by_field_name("value"), None,
                        is_method=True, emit_type=self.emit_field_type)

    # --- imports ---
    def on_import_statement(self, node):
        source = node.child_by_field_name("source")
        if source is None:
            return
        mod = resolve_relative_module(self.rel_path, self.text(source).strip("\"'`"))
        if mod is None:
            return
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            return
        for c in clause.children:
            if c.type == "identifier":  # default import
                self.facts.imports[self.text(c)] = (mod, "default")
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type != "import_specifier":
                        continue
                    nm = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    if nm is None:
                        continue
                    local = self.text(alias) if alias is not None else self.text(nm)
                    self.facts.imports[local] = (mod, self.text(nm))
            elif c.type == "namespace_import":  # import * as U
                ident = next((cc for cc in c.children if cc.type == "identifier"), None)
                if ident is not None:
                    self.facts.imports[self.text(ident)] = (mod, None)
