"""Pre-resolution IR: language-agnostic defs/calls/imports/typings each extractor parses from one file."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skillspec.models.codegraph import DefKind  # IR reuses the model enum; models never import codegraph


class CallKind(StrEnum):
    """How resolve.py looks up a call's callee — the call's receiver shape."""

    NAME = "name"              # foo()
    SELF = "self"              # self/cls.foo()
    ATTR = "attr"              # obj.foo()
    SELF_FIELD = "self_field"  # self.f.foo()


@dataclass
class Definition:
    """A function/method definition; qualname is dotted over enclosing classes+funcs (e.g. 'K.m')."""

    qualname: str
    kind: DefKind  # FUNCTION | METHOD
    start_line: int
    end_line: int
    snippet: str  # source text incl. decorators, spanning [start_line, end_line]
    code_lines: int = 0  # pure code lines: physical lines minus comments and blanks


@dataclass
class ClassDef:
    """A class definition; bases are superclass type-names as written (last component)."""

    qualname: str
    bases: list[str] = field(default_factory=list)


@dataclass
class GlobalVar:
    """A module-top binding; not a call target, so never a CodeNode. First binding per name wins."""

    name: str
    type_name: str | None
    start_line: int
    end_line: int
    snippet: str


@dataclass
class Call:
    """One call site; `kind` selects how resolve.py looks up the callee."""

    kind: CallKind
    name: str  # callee simple name (the final attribute)
    recv_var: str | None  # "attr" receiver var (obj.foo -> obj); else None
    recv_field: str | None  # "self_field" field name (self.f.foo -> f); else None
    caller: str | None  # enclosing function qualname; None at module level
    cls: str | None  # enclosing class qualname (for "self"/"self_field" resolution); else None
    line: int


@dataclass
class TypeBinding:
    """A receiver-typing fact: `name` (a var/param or field) holds an instance of class `type_name`."""

    scope: str | None  # enclosing function qualname (var_types) or class qualname (field_types)
    name: str
    type_name: str  # as written; resolve.py maps it to an internal class


@dataclass
class FileFacts:
    """Pre-resolution facts for one source file. imports: local_name -> (module_path, attr|None)."""

    module_path: str
    defs: list[Definition] = field(default_factory=list)
    classes: list[ClassDef] = field(default_factory=list)
    globals: list[GlobalVar] = field(default_factory=list)  # module-top bindings
    calls: list[Call] = field(default_factory=list)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    var_types: list[TypeBinding] = field(default_factory=list)  # function-scoped: var/param -> type
    field_types: list[TypeBinding] = field(default_factory=list)  # class-scoped: field -> type
    has_error: bool = False
