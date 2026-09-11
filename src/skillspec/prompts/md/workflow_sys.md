# Role
You are an expert in Agent Skill analysis and semantic graph construction.

# Task
Please read `SKILL.md` and construct a `Semantic Graph` that captures the skill’s overall execution structure. The skill-related code resources will also be provided for reference. Use them only to support functional classification and semantic understanding.

Requirements:
- **Understand the Skill Scope**: Identify the skill’s purpose, responsibilities, and execution context.
- **Structural Decomposition**: Decompose the skill into coherent functional stages and meaningful execution units.
- **Unit Identification**: Within each module, identify the key execution units at a meaningful level of abstraction. Preserve the execution logic without over-segmentation.
- **Graph Construction**: Connect units according to structural containment and actual semantic or runtime dependencies, forming a directed acyclic graph (DAG).

# Definitions
## Graph
 A connected DAG that shows the skill’s execution structure, including stages, units, and their dependencies.

The graph contains two kinds of edges:
- `contain`: structural containment only. It does not imply execution order.
- `dependency`: true semantic or runtime dependency between operation units. The target runs after, consumes output from, or depends on the source.

Rules:
1. There must be exactly one `root` unit.
2. The `contain` edges form the structural hierarchy of the skill.
3. Every non-root unit must have a structural parent reachable from the root through `contain` edges.
4. `dependency` edges should normally connect only OPERATION units: `plain`, `inline_code`, or `ref_code`.
5. Do not use `dependency` merely because two units are adjacent in the source.
6. Do not use `dependency` from `root`, `stage`, or `context` unless the source explicitly defines a semantic precondition that behaves like an execution dependency.
7. Independent sibling operations must remain parallel; do not chain them artificially.
8. Sequential operations that truly depend on one another must be connected by `dependency` edges.

## Unit Types
 `<unit>` represents a node in the graph.

**STRUCTURAL Unit**
- `root`: The unique graph root. It anchors the entire skill. If there is no global introductory text, its `<content>` may be empty.
- `stage`: A structural grouping node for a coherent functional area or workflow stage. Keep a `stage` only when it organizes two or more child units.
- `context`: A non-execution leaf node containing supporting notes, assumptions, constraints, examples, or general guidance. A `context` unit must not contain runnable code or commands.

**OPERATION Unit**
- `ref_code`: An execution unit whose action invokes, depends on, or directly corresponds to an available reference code file.
- `inline_code`: An execution unit containing fenced code blocks, runnable command lines, configuration scripts, or executable snippets embedded in `SKILL.md`, and not primarily invoking an available reference code file.
- `plain`: A prose execution step with no runnable code or command, relying only on LLM capability.

# Coverage and Quotation

1. Coverage applies only to `SKILL.md`; reference code may be used for understanding and classification but must never be copied into `<content>`.
2. Every character of `SKILL.md`, including whitespace and blank lines, must appear in exactly one unit's `<content>`.
3. Each `<content>` must quote its assigned source span verbatim: do not omit, duplicate, reorder, paraphrase, normalize, or XML/HTML-escape the source text.
4. A fenced code block, from opening fence to matching closing fence, must never be split across units. Commands, loops, retry logic, conditionals, and tightly coupled code/configuration blocks must also remain intact within one unit.
5. Empty CDATA is allowed only for structural units with no corresponding source text.
6. Whitespace-only source spans must not be dropped; assign or merge them into an adjacent unit.
7. If the source text contains the CDATA terminator `]]>`, split the CDATA section as `]]]]><![CDATA[>` so that the parsed text remains identical and the XML remains valid.

# Procedure Pipeline

## Step 1: Establish the structural skeleton

Analyze `SKILL.md` top-down.
- Create one `root` unit.
- Create `stage` units only for sections that organize two or more meaningful child units.
- Create `context` units for non-executable supporting text that should remain separate.
- If a heading or introduction frames only one following operation, fold that heading text into the operation unit instead of creating a standalone `stage`.

## Step 2: Identify units

Within each structural area, identify meaningful execution units. Classify each operation as `ref_code`, `inline_code`, or `plain` according to the precedence rules above.

Avoid both:
- over-segmentation into trivial fragments;
- overly coarse units that merge distinct decisions, actions, or outputs.

## Step 3: Add edges

1. Add `contain` edges from each structural parent to all of its immediate child units.
2. Add `dependency` edges only where a true semantic or runtime dependency exists.
3. For serial workflows, add dependency edges such as `step_1 -> step_2 -> step_3`.
4. For independent sibling operations, do not add dependency edges.

## Step 4: Graph Optimization
After constructing the graph, run a single refinement pass that removes noise while preserving meaning. Coverage is invariant: you may move text between adjacent units (keeping source order) or drop a unit ONLY when its `<content>` is empty or whitespace; never delete, reorder, or duplicate any source character.

1. **Fold trivial framing units.** Keep a `stage` only when it organizes two or more child units. If a heading or intro frames a single following operation, fold its text into that operation's `<content>` (in source order) and delete the standalone unit, re-pointing its parent's `<next>` to that operation.
2. **No empty operation units.** An OPERATION unit's `<content>` must never be empty or whitespace-only — merge such a unit into the adjacent operation it borders in source order. (A STRUCTURAL unit with no source text keeps an empty CDATA section, per Coverage.)
3. **Minimal dependency edges.** Optimize ONLY `dependency` edges: remove a direct dependency only when an equivalent dependency-only indirect path already exists. NEVER drop `contain` edges. Remove empty nodes only if they are non-root and carry no structural or dependency meaning.
4. **Parallel stays parallel.** Sibling operations with no true dependency must remain parallel successors of their shared parent — never chained to look connected.
5. **Final validity.** Exactly one root; every unit reachable from it; acyclic; every source character still present in exactly one `<content>`, none duplicated.

# Output Format
Return exactly one Markdown code block labeled `xml`, containing one XML document and no additional explanation.

```xml
<workflow root="[root_unit_name]">
  <unit name="[unique_unit_name]" type="[root|stage|context|plain|inline_code|ref_code]" script="[path]" entry="[function]">   <!-- script/entry: only when type="ref_code" -->
    <next rel="[contain|dependency]">[successor_unit_name]</next>
    ...
    <content><![CDATA[verbatim source text]]></content>
  </unit>
  ...
</workflow>
```

Key Elements:
- `name`: unique lowercase snake_case ASCII identifier.
- `type`: required; must be one of `root`, `stage`, `context`, `plain`, `inline_code`, or `ref_code`.
- `script`: required only for `type="ref_code"`; omit for all other types.
- `entry`: optional only for `type="ref_code"`; omit for all other types.
- `<next>`: omit if the unit has no successors.
- Every `<next>` must reference an existing unit and must include `rel="contain"` or `rel="dependency"`.
- `<content>` must contain verbatim source text from `SKILL.md`, wrapped in `<![CDATA[...]]>`.

# Final Verification Checklist

Before returning, ensure:

1. Exactly one `root` unit exists.
2. Every unit is reachable from the root.
3. The graph is acyclic.
4. Every source character from `SKILL.md` appears exactly once in `<content>`.
5. No reference code appears in `<content>`.
6. No code block is split.
7. No independent operations are artificially chained.
8. All true serial dependencies are represented by `dependency` edges.
9. All structural children are represented by `contain` edges.
10. Non-`ref_code` units do not contain `script` or `entry`.