"""Language-agnostic AST walk owning scope, call collection, and receiver classification."""

from __future__ import annotations

from tree_sitter import Node

from skillspec.models.codegraph import DefKind

from .facts import Call, CallKind, ClassDef, Definition, FileFacts, GlobalVar, TypeBinding
from .grammar import NodeSpec


class BaseExtractor:
    """Walks one parsed file into its FileFacts; subclasses set a grammar `NodeSpec` and add node handlers."""

    spec: NodeSpec  # set by each language subclass

    def __init__(self, facts: FileFacts, source: bytes):
        self.facts = facts
        self.src = source
        self.scope: list[str] = []        # simple names of enclosing defs/classes
        self.func_stack: list[str] = []   # qualnames of enclosing functions, e.g. "K.m"
        self.class_stack: list[str] = []  # qualnames of enclosing classes, e.g. "K"
        self._global_names_seen: set[str] = set()  # module-top names already emitted

    # --- context ---
    def text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return self.src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def cur_func(self) -> str | None:
        return self.func_stack[-1] if self.func_stack else None

    def cur_class(self) -> str | None:
        return self.class_stack[-1] if self.class_stack else None

    def qualname(self, simple: str) -> str:
        return ".".join(self.scope + [simple])

    # --- walk ---
    def walk(self, node: Node) -> None:
        # Dispatch to on_<node_type>; node types are defined by the tree-sitter grammar.
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node)
            return
        # No handler: emit a call if this is a call node, then recurse into children.
        if node.type in self.spec.call_nodes:
            self.emit_call(node)
        for c in node.children:
            self.walk(c)

    def walk_calls(self, node) -> None:
        """Walk an expression subtree, emitting every call (used by handlers that don't recurse via walk)."""
        if node is None:
            return
        if node.type in self.spec.call_nodes:
            self.emit_call(node)
        for c in node.children:
            self.walk_calls(c)

    def _walk_body(self, body) -> None:
        if body is None:
            return
        # block -> recursion
        if body.type in self.spec.block_nodes:
            for c in body.children:
                self.walk(c)
        # expression-bodied (arrow `x => g(x)`) — still find calls
        else:
            self.walk_calls(body)

    # --- definitions ---
    def emit_class(self, simple, bases, body_node) -> None:
        qualname = self.qualname(simple)
        self.facts.classes.append(ClassDef(qualname=qualname, bases=bases))
        self.class_stack.append(qualname)
        self.scope.append(simple)
        if body_node is not None:
            for c in body_node.children:
                self.walk(c)
        self.scope.pop()
        self.class_stack.pop()

    def emit_func(self, simple, params, return_ann, is_method, body_node,
                        identifier=None, def_node=None) -> None:
        qualname = self.qualname(simple)

        # prefer def_node (full function node) over body_node for accurate line spans
        src_node = def_node or body_node
        start_line = (src_node.start_point.row + 1) if src_node is not None else (
            identifier.start_point.row + 1 if identifier is not None else 1)
        end_line = (src_node.end_point.row + 1) if src_node is not None else start_line
        self.facts.defs.append(Definition(
            qualname=qualname,
            kind=DefKind.METHOD if is_method else DefKind.FUNCTION,
            start_line=start_line,
            end_line=end_line,
            snippet=self.text(src_node) if src_node is not None else "",
            code_lines=self._code_lines(src_node) if src_node is not None else 0,
        ))

        # annotated params seed receiver typing
        for p in params:
            if p.ann:
                self.facts.var_types.append(TypeBinding(scope=qualname, name=p.name, type_name=p.ann))
        self.func_stack.append(qualname)
        self.scope.append(simple)
        self._walk_body(body_node)
        self.scope.pop()
        self.func_stack.pop()

    def _code_lines(self, node: Node) -> int:
        """Pure code lines (non-blank, non-comment) within a definition node, reusing the parse."""
        base = node.start_byte
        buf = bytearray(self.src[base:node.end_byte])
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "comment":
                for i in range(n.start_byte - base, n.end_byte - base):
                    if buf[i] != 0x0A:  # blank comment bytes, keep newlines so physical lines don't merge
                        buf[i] = 0x20
            else:
                stack.extend(n.children)
        return sum(1 for line in bytes(buf).decode("utf-8", errors="replace").splitlines() if line.strip())

    def emit_global_var(self, name: str, type_name: str | None, def_node) -> None:
        """Record the first module-top binding of `name`; no-op inside any function/class scope."""
        if self.cur_func() is not None or self.cur_class() is not None:
            return
        if name in self._global_names_seen:
            return
        self._global_names_seen.add(name)
        self.facts.globals.append(GlobalVar(
            name=name,
            type_name=type_name,
            start_line=def_node.start_point.row + 1,
            end_line=def_node.end_point.row + 1,
            snippet=self.text(def_node),
        ))

    # --- calls ---
    def _is_self(self, node) -> bool:
        if node.type in self.spec.self_nodes:
            return True
        return node.type == self.spec.name_node and self.text(node) in self.spec.self_names

    def emit_call(self, node) -> None:
        call = self._classify_call(node)
        if call is not None:
            self.facts.calls.append(call)

    def _classify_call(self, call_node) -> Call | None:
        fn = call_node.child_by_field_name(self.spec.callee_field)
        if fn is None:
            return None

        # unwrap cast/await wrappers so the real callee node is classified below
        while fn.type in self.spec.transparent_nodes:  # drill through (x as T).m(), (await f).m()
            nc = fn.named_children
            if not nc:
                break
            fn = nc[0]
        line = call_node.start_point.row + 1

        # bare function call: foo()
        if fn.type == self.spec.name_node:
            return Call(kind=CallKind.NAME, name=self.text(fn), recv_var=None, recv_field=None,
                        caller=self.cur_func(), cls=self.cur_class(), line=line)
        if self.spec.member_access_node is not None and fn.type == self.spec.member_access_node:
            obj = fn.child_by_field_name(self.spec.receiver_field)
            prop = fn.child_by_field_name(self.spec.selector_field)
            if obj is None or prop is None:
                return None
            name = self.text(prop)

            # self.method()
            if self._is_self(obj):
                return Call(kind=CallKind.SELF, name=name, recv_var=None, recv_field=None,
                            caller=self.cur_func(), cls=self.cur_class(), line=line)

            if obj.type == self.spec.member_access_node:  # self.field.method()?
                io = obj.child_by_field_name(self.spec.receiver_field)
                ip = obj.child_by_field_name(self.spec.selector_field)

                # self.field.method()
                if io is not None and ip is not None and self._is_self(io):
                    return Call(kind=CallKind.SELF_FIELD, name=name, recv_var=None, recv_field=self.text(ip),
                                caller=self.cur_func(), cls=self.cur_class(), line=line)
                return None

            # var.method() — receiver is a plain identifier
            if obj.type == self.spec.name_node:
                return Call(kind=CallKind.ATTR, name=name, recv_var=self.text(obj), recv_field=None,
                            caller=self.cur_func(), cls=self.cur_class(), line=line)
        return None

    # --- typing helpers---
    def emit_var_type(self, var: str, type_name: str | None) -> None:
        if type_name:
            self.facts.var_types.append(TypeBinding(scope=self.cur_func(), name=var, type_name=type_name))

    def emit_field_type(self, field: str, type_name: str | None, cls: str | None = None) -> None:
        cls = cls or self.cur_class()
        if cls and type_name:
            self.facts.field_types.append(TypeBinding(scope=cls, name=field, type_name=type_name))

    def parse_ctor(self, node) -> str | None:
        """Class name if `node` constructs one: `new X()` (js/ts) or a bare call `X()` (py); else None.

        A bare call may also be a plain function — resolve.py keeps the binding only if the name is a class.
        """
        if node is None:
            return None
        # js/ts: `new X(...)` has an explicit construct node
        if self.spec.build_node and node.type == self.spec.build_node:
            cn = node.child_by_field_name(self.spec.constructed_type_field)
            return self.text(cn) if (cn is not None and cn.type == self.spec.name_node) else None
        # py: `X(...)` looks identical to a function call; caller disambiguates via class names
        if node.type in self.spec.call_nodes:
            fn = node.child_by_field_name(self.spec.callee_field)
            if fn is not None and fn.type == self.spec.name_node:
                return self.text(fn)
        return None

    # --- hooks (override) ---
    def parse_params(self, params_node) -> list:
        return []

    def parse_bases(self, class_node) -> list:
        return []

    def parse_return(self, fn_node) -> str | None:
        return None
