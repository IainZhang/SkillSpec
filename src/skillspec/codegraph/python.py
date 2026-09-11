"""PyExtractor: defs, classes, calls, imports, type bindings."""

from __future__ import annotations

import tree_sitter_python as tspy
from tree_sitter import Language, Node, Parser

from .grammar import NodeSpec, Param
from .walk import BaseExtractor

PY_PARSER = Parser(Language(tspy.language()))

_PY_SPEC = NodeSpec(
    name_node="identifier",
    member_access_node="attribute",
    receiver_field="object",
    selector_field="attribute",
    call_nodes=frozenset({"call"}),
    self_names=frozenset({"self", "cls"}),
    build_node=None,  # Python constructors are bare calls; resolve.py maps class name -> __init__
    transparent_nodes=frozenset({"parenthesized_expression", "await"}),
    block_nodes=frozenset({"block"}),
)


class PyExtractor(BaseExtractor):
    spec = _PY_SPEC

    # --- definitions ---
    def on_function_definition(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            return
        parent = node.parent  # widen snippet/span to include decorators
        span = parent if (parent is not None and parent.type == "decorated_definition") else node
        self.emit_func(
            self.text(name), self.parse_params(node.child_by_field_name("parameters")),
            self.parse_return(node), is_method=self.cur_class() is not None,
            body_node=node.child_by_field_name("body"), identifier=name, def_node=span,
        )

    def on_class_definition(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            return
        self.emit_class(self.text(name), self.parse_bases(node), node.child_by_field_name("body"))

    def on_decorated_definition(self, node):
        d = node.child_by_field_name("definition")  # walk only the def — skip decorator-expression calls
        if d is not None:
            self.walk(d)
        else:
            for c in node.children:
                self.walk(c)

    # --- params / bases / annotations ---
    def parse_params(self, parameters) -> list:
        out = []
        if parameters is None:
            return out
        for c in parameters.named_children:
            t = c.type
            if t == "identifier":
                out.append(Param(self.text(c), None))
            elif t == "typed_parameter":
                ident = next((cc for cc in c.children if cc.type == "identifier"), None)
                if ident is not None:
                    out.append(Param(self.text(ident), self._annotation_type(c.child_by_field_name("type"))))
            elif t == "default_parameter":
                nm = c.child_by_field_name("name")
                if nm is not None and nm.type == "identifier":
                    out.append(Param(self.text(nm), None))
            elif t == "typed_default_parameter":
                nm = c.child_by_field_name("name")
                if nm is not None and nm.type == "identifier":
                    out.append(Param(self.text(nm), self._annotation_type(c.child_by_field_name("type"))))
        return out

    def parse_bases(self, class_node) -> list:
        sup = class_node.child_by_field_name("superclasses")
        if sup is None:
            return []
        bases = []
        for c in sup.named_children:
            if c.type == "identifier":
                bases.append(self.text(c))
            elif c.type == "attribute":
                prop = c.child_by_field_name("attribute")
                if prop is not None:
                    bases.append(self.text(prop))
        return bases

    def parse_return(self, fn_node) -> str | None:
        return self._annotation_type(fn_node.child_by_field_name("return_type"))

    def _annotation_type(self, type_node) -> str | None:
        """Unwrap Python `type` node, then take the simple class name."""
        if type_node is None:
            return None
        inner = type_node
        if type_node.type == "type":
            nc = type_node.named_children
            if not nc:
                return None
            inner = nc[0]
        return self._class_name(inner)

    def _class_name(self, n) -> str | None:
        t = n.type
        if t in ("identifier", "type_identifier"):
            return self.text(n)
        if t == "dotted_name":
            ids = [c for c in n.children if c.type == "identifier"]
            return self.text(ids[-1]) if ids else None
        if t == "attribute":
            prop = n.child_by_field_name("attribute")
            return self.text(prop) if prop is not None else None
        if t == "generic_type":
            for c in n.children:
                if c.type in ("identifier", "type_identifier"):
                    return self.text(c)
            return None
        if t == "string":  # forward-ref annotation "Foo"
            frag = next((c for c in n.children if c.type == "string_content"), None)
            return self.text(frag) if frag is not None else self.text(n).strip("\"'")
        if t == "subscript":
            v = n.child_by_field_name("value")
            return self._class_name(v) if v is not None else None
        return None

    # --- assignments / statements ---
    def on_assignment(self, node):
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        type_node = node.child_by_field_name("type")
        if left is None:
            return
        ann = self._annotation_type(type_node) if type_node is not None else None
        if left.type == "identifier":
            var = self.text(left)
            if ann:
                self.emit_var_type(var, ann)
            if right is not None:
                self.emit_var_type(var, self.parse_ctor(right))
                self.walk_calls(right)
            self.emit_global_var(var, ann, node)
        elif left.type == "attribute":
            obj = left.child_by_field_name("object")
            prop = left.child_by_field_name("attribute")
            if obj is not None and prop is not None and self._is_self(obj) and self.cur_class():
                field = self.text(prop)
                if ann:
                    self.emit_field_type(field, ann)
                if right is not None:
                    self.emit_field_type(field, self.parse_ctor(right))
            if right is not None:
                self.walk_calls(right)
        elif right is not None:  # tuple/list/multi-target — bail on binding, still find calls
            self.walk_calls(right)

    # --- imports ---
    def on_import_statement(self, node):
        imports = self.facts.imports
        for nm in node.children_by_field_name("name"):
            if nm.type == "aliased_import":
                target = self.text(nm.child_by_field_name("name"))
                alias = self.text(nm.child_by_field_name("alias"))
                imports[alias] = (target, None)
            elif nm.type == "dotted_name":
                local = self.text(nm).split(".")[0]  # `import a.b` binds `a`
                imports[local] = (local, None)

    def on_import_from_statement(self, node):
        imports = self.facts.imports
        base, attrs = self._from_module_base(node.child_by_field_name("module_name"))
        for nm in node.children_by_field_name("name"):
            if nm.type == "aliased_import":
                imported = self.text(nm.child_by_field_name("name"))
                alias = self.text(nm.child_by_field_name("alias"))
                imports[alias] = (base, imported) if attrs else (f"{base}.{imported}".strip("."), None)
            elif nm.type == "dotted_name":
                imported = self.text(nm)
                local = imported.split(".")[0]
                imports[local] = (base, imported) if attrs else (f"{base}.{imported}".strip("."), None)

    def _from_module_base(self, mod_node: Node | None) -> tuple[str, bool]:
        """(base_module, names_are_attrs) for `from ... import`; resolves relative imports."""
        if mod_node is None:
            return "", False
        if mod_node.type == "dotted_name":
            return self.text(mod_node), True
        if mod_node.type == "relative_import":
            dots, named = 0, None
            for ch in mod_node.children:
                if ch.type == "import_prefix":
                    dots = self.text(ch).count(".")
                elif ch.type == "dotted_name":
                    named = self.text(ch)
            pkg = self._package_of(self.facts.module_path, dots or 1)
            return (f"{pkg}.{named}".strip("."), True) if named else (pkg, False)
        return "", False

    @staticmethod
    def _package_of(module_path: str, dots: int) -> str:
        """Package a relative import resolves against: 1 dot = own package, each extra strips a level."""
        parts = module_path.split(".")[:-1]
        extra = dots - 1
        if extra > 0:
            parts = parts[:-extra] if extra <= len(parts) else []
        return ".".join(parts)
