"""Input rendering: turn stage data into the XML-tagged user messages the prompts expect."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from xml.sax.saxutils import escape, quoteattr

from skillspec.codegraph import SUPPORTED_EXTS
from skillspec.models import CodeNode, CodeContext, DefectGroup, Layer, Metadata, NodeContext, Policy, Stage, Step, StepContext, UnitSpec, VerifyKind, View, policy
from skillspec.prompts.prompt import Prompt, read_md
from skillspec.sandbox import layout

_MAX_FILE_LINES = 200  # per-file raw line cap
_MAX_FILE_CHARS = 24_000  # per-file raw char cap (~8K tokens)
_SHORT_FILE_LINES = 50  # shorter files count as reference content
REFERENCE_EXTS = frozenset({".md", ".txt"})  # prose docs shown as reference, not code


def _text(s: str) -> str:
    """Escape text for the XML blocks."""
    return escape(s)


def _attrs(**kv: str) -> str:
    """Escaped XML attributes: `_attrs(path=p)` -> ` path="p"`."""
    return "".join(f" {k}={quoteattr(v)}" for k, v in kv.items())


def _number_lines(content: str, start: int = 1) -> str:
    """`<n>: <line>` lines, numbered from `start` (1 for a whole file, a def's own line for a snippet)."""
    return "\n".join(f"{i + start}: {line}" for i, line in enumerate(content.splitlines()))


def manifest_xml(name: str, description: str, content: str) -> str:
    """Skill manifest as a <name>/<description>/<body> block (body line-numbered)."""
    return (
        f"<name> {_text(name)} </name>\n"
        f"<description> {_text(description)} </description>\n"
        f"<body>\n{_text(_number_lines(content))}\n</body>"
    )


def resources_xml(resources: dict[str, str]) -> str:
    """Resources as XML; oversized code files stay as per-path stubs, oversized others are path-only."""
    code: list[str] = []
    reference: list[str] = []
    other: list[str] = []
    for path in sorted(resources):
        suffix = Path(path).suffix
        body = resources[path]
        line_count = len(body.splitlines())
        char_count = len(body)
        oversized = line_count > _MAX_FILE_LINES or char_count > _MAX_FILE_CHARS
        if suffix in SUPPORTED_EXTS:
            if oversized:
                code.append(f"<file{_attrs(path=path)}>\n(file too long: {line_count} lines, not inlined)\n</file>")
            else:
                code.append(f"<file{_attrs(path=path)}>\n{_text(_number_lines(body))}\n</file>")
        elif oversized:
            other.append(path)  # oversized non-code: path-only
        elif suffix in REFERENCE_EXTS or line_count < _SHORT_FILE_LINES:
            reference.append(f"<file{_attrs(path=path)}>\n{_text(body)}\n</file>")
        else:
            other.append(path)
    sections = ["\n".join(code)] if code else []
    if reference:
        sections.append("<reference_files>\n" + "\n".join(reference) + "\n</reference_files>")
    if other:
        listed = "\n".join(f"- {_text(p)}" for p in other)
        sections.append(f"<other_files>\n{listed}\n</other_files>")
    return "\n".join(sections)


def skill_parts(metadata: Metadata) -> tuple[str, str]:
    """Render the complete manifest and resources used to derive the workflow."""
    m = metadata.manifest
    return manifest_xml(m.name, m.description, m.content), resources_xml(metadata.resources)


def node_skill_block(ctx: NodeContext, resources: dict[str, str]) -> str:
    """Render <source> from the manifest and resources relevant to the current node."""
    manifest = manifest_xml(ctx.skill_name, ctx.overview, ctx.manifest_body)
    scoped = {k: resources[k] for k in ctx.relevant_resources if k in resources}
    return f"<source>\n{manifest}\n{resources_xml(scoped)}\n</source>"


# Shared node indices with named view references.


_TARGET = "TargetNode"  # the unit under analysis: an index entry promoted out of the index


def _node_block(tag: str, attrs: str, body: str, extra: str) -> str:
    """One node entry; `extra` carries the target's linked refs and is empty for an index entry."""
    inner = f"{body}\n{extra}" if extra else body
    return f"<{tag}{attrs}>\n{inner}\n</{tag}>"


def _skill_node_xml(step: Step, *, tag: str = "node", extra: str = "") -> str:
    """One SkillNodes entry: name + kind + line-numbered instruction."""
    body = _text(_number_lines(step.instruction.strip() or "(no instruction)"))
    return _node_block(tag, _attrs(name=step.name, kind=step.kind), body, extra)


def _code_node_xml(node: CodeNode, language: str, *, reveal_source: bool = True, tag: str = "node", extra: str = "") -> str:
    """One CodeNodes entry, optionally hiding the source."""
    content = node.snippet.strip() if reveal_source else "(implementation not visible in this context)"
    body = _text(_number_lines(content or "(no source)"))
    attrs = _attrs(id=node.id, name=node.name, path=node.file, kind=node.kind, lang=language)
    return _node_block(tag, attrs, body, extra)


def _node_index(tag: str, bodies: list[str]) -> str:
    """Index block of node entries; empty -> omitted."""
    return f"<{tag}>\n" + "\n".join(bodies) + f"\n</{tag}>" if bodies else ""


def _target_block(sides: "_Sides", linked_refs: list[str]) -> str:
    """Render the target and linked references according to the stage policy."""
    link = f"  <{sides.link_tag}>{_text(', '.join(linked_refs))}</{sides.link_tag}>" if linked_refs else ""
    return sides.self_xml(link)


def _names_field(tag: str, names: list[str]) -> str:
    """<Lineage>/<Neighbors> name refs; empty -> omitted."""
    return f"  <{tag}>{_text(', '.join(names))}</{tag}>" if names else ""


def _resolvable(paths: list[list[str]], index: dict) -> list[list[str]]:
    """Keep chains whose references are all indexed."""
    # all-or-nothing per chain: dropping one ref mid-chain would splice two nodes that never call
    return [p for p in paths if all(ref in index for ref in p)]


def _chain(refs: list[str]) -> str:
    """One ordered ref chain; escapes the refs, not the arrow (_text would emit "-&gt;")."""
    return " -> ".join(_text(ref) for ref in refs)


def _lineage_block(names: list[str], wf_paths: list[list[str]], call_paths: list[list[str]]) -> str:
    """<Lineage> name refs; a code ExpectSpec adds the ordered workflow/call chains it sits on."""
    if not (wf_paths or call_paths):
        return _names_field("Lineage", names)
    inner = [f"    <workflow_path>{_chain(p)}</workflow_path>" for p in wf_paths]
    inner += [f"    <call_path>{_chain(p)}</call_path>" for p in call_paths]
    if names:  # transitive callers the enumerated chains left out; unordered
        inner.append(f"    <callers>{_text(', '.join(names))}</callers>")
    return "  <Lineage>\n" + "\n".join(inner) + "\n  </Lineage>"


def _holistic_block(ctx: NodeContext, names: list[str]) -> str:
    """Skill name + description + root/context node refs."""
    inner: list[str] = []
    if ctx.skill_name.strip():
        inner.append(f"  <skill>{_text(ctx.skill_name.strip())}</skill>")
    if ctx.overview.strip():
        inner.append(f"  <overview>{_text(ctx.overview.strip())}</overview>")
    if names:
        inner.append(f"  <nodes>{_text(', '.join(names))}</nodes>")
    return "<Holistic>\n" + "\n".join(inner) + "\n</Holistic>" if inner else ""


class _Rendered(NamedTuple):
    skill: str
    indices: str  # SkillNodes + CodeNodes, evidence only
    target: str   # <TargetNode>…</TargetNode>
    view: str     # <View>…</View>; empty when every maskable layer is


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


class _Sides(NamedTuple):
    """Rendering choices for workflow and code contexts."""

    kind: str                        # which space this unit lives in: "step" | "code"
    link_tag: str                    # tag wrapping the linked counterpart's refs
    reg_self: Callable[[Any], str]   # register into this unit's own space
    reg_link: Callable[[Any], str]   # register into the counterpart's space
    self_xml: Callable[[str], str]   # render the unit as <TargetNode>, wrapping the given inner refs
    show_link: bool                  # this stage reveals the counterpart's identity
    # (cross-space steps, workflow chains, call chains); None when the kind carries no chains
    chains: tuple[list[Step], list[list[str]], list[list[str]]] | None


def _sides(
    ctx: NodeContext, pol: Policy, reg_step: Callable[[Step], str], reg_code: Callable[[CodeNode], str]
) -> _Sides:
    """Resolve a context's kind into the renderer's per-kind choices."""
    if isinstance(ctx, StepContext):
        step_xml = lambda extra: _skill_node_xml(ctx.entity, tag=_TARGET, extra=extra)  # noqa: E731
        return _Sides("step", "invoked_code", reg_step, reg_code, step_xml, pol.step_shows_code, None)
    if isinstance(ctx, CodeContext):
        chains = (ctx.step_lineage, ctx.workflow_paths, ctx.call_paths)
        code_xml = lambda extra: _code_node_xml(  # noqa: E731
            ctx.entity, ctx.language, reveal_source=pol.code_body, tag=_TARGET, extra=extra
        )
        return _Sides("code", "linked_steps", reg_code, reg_step, code_xml, pol.code_shows_steps, chains)
    raise ValueError(f"NodeContext {ctx.unit_id} has neither step nor code")


def _render_context(
    ctx: NodeContext,
    view: View,
    stage: Stage,
    *,
    resources: dict[str, str] | None = None,
) -> _Rendered:
    """Masked context as a <TargetNode>, shared evidence indices, and a View of name-refs.

    Two gates, both owned by `models.mask`: `view.layers` decides which neighbourhood fields are
    filled, the stage's `Policy` decides what each may show. Shared nodes are registered once and
    referenced by name; registration order fixes the index order, so it is load-bearing.
    """
    pol = policy(stage)
    layers = view.layers
    skill_nodes: dict[str, Step] = {}
    code: dict[str, CodeNode] = {}

    def reg_step(s: Step) -> str:
        skill_nodes.setdefault(s.name, s)
        return s.name

    def reg_code(n: CodeNode) -> str:
        code.setdefault(n.id, n)
        return n.id

    sides = _sides(ctx, pol, reg_step, reg_code)

    self_ref = sides.reg_self(ctx.entity)
    linked = [sides.reg_link(x) for x in ctx.linked] if sides.show_link else []
    show_global = Layer.HOLISTIC in layers
    global_names = [reg_step(s) for s in ctx.holistic_ctx] if show_global else []  # holistic_ctx are always Steps
    upstream_names = [sides.reg_self(x) for x in ctx.lineage] if Layer.LINEAGE in layers else []
    neighbor_names = [sides.reg_self(x) for x in ctx.neighbors] if Layer.NEIGHBORS in layers else []
    downstream_names = (
        [reg_step(s) for s in ctx.downstream]
        if pol.show_downstream and isinstance(ctx, StepContext)
        else []
    )

    wf_paths: list[list[str]] = []
    call_paths: list[list[str]] = []
    if pol.show_paths and Layer.LINEAGE in layers and sides.chains is not None:
        cross_steps, ctx_wf_paths, ctx_call_paths = sides.chains
        for s in cross_steps:
            reg_step(s)
        wf_paths = _resolvable(ctx_wf_paths, skill_nodes)
        call_paths = _resolvable(ctx_call_paths, code)

    skill = ""
    has_skill_context = any(
        (ctx.skill_name.strip(), ctx.overview.strip(), ctx.manifest_body.strip(), ctx.relevant_resources)
    )
    if pol.show_skill and has_skill_context:
        skill = node_skill_block(ctx, resources or {})
    # the target is promoted into <TargetNode>; it stays *registered* so the refs that name it — a
    # call_path terminating at it, or Holistic when it is itself a ROOT/CONTEXT step — still resolve
    self_step = self_ref if sides.kind == "step" else ""
    self_code = self_ref if sides.kind == "code" else ""
    indices = _join(
        _node_index("SkillNodes", [_skill_node_xml(s) for s in skill_nodes.values() if s.name != self_step]),
        _node_index(
            "CodeNodes",
            [
                _code_node_xml(n, ctx.language, reveal_source=pol.code_body)
                for n in code.values()
                if n.id != self_code
            ],
        ),
    )
    layers = _join(
        _holistic_block(ctx, global_names) if show_global else "",
        _lineage_block(upstream_names, wf_paths, call_paths),
        _names_field("Neighbors", neighbor_names),
        _names_field("Downstream", downstream_names),
    )
    view_block = f"<View>\n{layers}\n</View>" if layers else ""
    return _Rendered(skill=skill, indices=indices, target=_target_block(sides, linked), view=view_block)


def expect_context(ctx: NodeContext) -> str:
    """Render expectation context using the EXPECT visibility policy."""
    r = _render_context(ctx, View.FULL, Stage.EXPECT)
    return _join(r.skill, r.indices, r.target, r.view)


def fact_context(ctx: NodeContext, view: View) -> str:
    """FactSpec input: implementation visible under one masked view."""
    r = _render_context(ctx, view, Stage.FACT)
    return _join(r.skill, r.indices, r.target, r.view)


def reason_context(ctx: NodeContext, resources: dict[str, str]) -> str:
    """Adjudication input: never masked — Full View, implementation, and the scoped raw source."""
    r = _render_context(ctx, View.FULL, Stage.REASON, resources=resources)
    return _join(r.skill, r.indices, r.target, r.view)


def _section(tag: str, item_tag: str, lines: list[str]) -> str:
    if not lines:
        return ""
    inner = "\n".join(f"  <{item_tag}>{_text(line)}</{item_tag}>" for line in lines)
    return f"<{tag}>\n{inner}\n</{tag}>"


def spec_block(spec: UnitSpec, tag: str, *, views: Sequence[View] = ()) -> str:
    """UnitSpec as <tag>…</tag>; FactSpecs carry their source view label(s)."""
    parts = [f"<{tag}{_attrs(view=', '.join(v.value for v in views)) if views else ''}>"]
    if spec.name:
        parts.append(f"  <Unit>{_text(spec.name)}</Unit>")
    parts.append(_section("PreCondition", "Predicate", spec.pre))
    parts.append(_section("PostCondition", "Predicate", spec.post))
    parts.append(_section("Effects", "Effect", spec.effects))
    parts.append(f"</{tag}>")
    return "\n".join(p for p in parts if p)


def candidate_bug(group: DefectGroup) -> str:
    """One unit's defect group as JSON (inlined into the verify task), each finding a claim."""
    return group.model_dump_json(exclude={"verdicts": {"__all__": {"decision"}}}, indent=2)


def unit_block(ctx: NodeContext | None, unit_id: str) -> str:
    """The defect owner as <target_unit>: what its unit_id denotes — identity, location, own body.

    Strictly the unit itself; no neighbours, callers, or linked counterparts. One unit is always
    rendered whole: the per-file caps above bound a stage's fan-out, which a single unit has none of.
    """
    if ctx is None:  # verify can run from defects.jsonl alone, without a graph to resolve against
        return f"<target_unit{_attrs(id=unit_id)}>\n(unit source unavailable)\n</target_unit>"
    entity = ctx.entity
    if isinstance(ctx, StepContext):
        attrs = {"kind": "workflow", "name": entity.name, "node_kind": entity.kind.value}
        if entity.script_path:  # the step's own REF_CODE fields
            attrs["script"] = entity.script_path
        if entity.entry_point:
            attrs["entry"] = entity.entry_point
        body = _number_lines(entity.instruction.strip() or "(no instruction)")
    else:
        attrs = {
            "kind": "code", "id": entity.id, "name": entity.name, "path": entity.file,
            "lines": f"{entity.start_line}-{entity.end_line}", "def_kind": entity.kind.value,
            "lang": ctx.language,
        }
        # numbered at true file lines: reports quote the repository's line numbers, not the snippet's
        body = _number_lines(entity.snippet.strip() or "(no source)", start=entity.start_line)
    return f"<target_unit{_attrs(**attrs)}>\n{_text(body)}\n</target_unit>"


# verify template stem per defect kind: code runs probes, workflow emits a trigger
_VERIFY_TEMPLATE: dict[VerifyKind, str] = {"code": "sandbox_code", "workflow": "sandbox_markdown"}
_LATE_TOKENS = ("CandidateDefect", "TargetUnit")  # inlined after the guard: their content may itself contain '{{'


def render_verify_instruction(skill_name: str, tag: str, kind: VerifyKind, *, report: str, unit: str) -> str:
    """Verify task for one defect run: fill container paths, then inline the report and its target unit."""
    stem = _VERIFY_TEMPLATE[kind]
    out = Prompt.render(read_md(stem), **layout.prompt_placeholders(skill_name, tag))
    skeleton = out
    for key in _LATE_TOKENS:
        token = f"{{{{{key}}}}}"
        if token not in out:
            raise ValueError(f"verify prompt {stem}.md is missing its {token} placeholder")
        skeleton = skeleton.replace(token, "")
    if "{{" in skeleton:  # guard the template, not the content that is about to fill it
        raise ValueError(f"verify prompt {stem}.md has an unfilled/unknown placeholder ('{{{{…}}}}')")
    return Prompt.render(out, CandidateDefect=report, TargetUnit=unit)  # single pass: values are never rescanned
