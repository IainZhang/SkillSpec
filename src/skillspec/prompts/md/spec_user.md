Write a specification only for the Unit in `<TargetNode>` below. All other visible nodes and fields are contextual evidence.

The context uses shared node indices plus a reference-based `<View>`:

- `<SkillNodes>`: workflow nodes keyed by `name` with instructions.
- `<CodeNodes>`: code nodes keyed by `id`, with public metadata and source — or `(implementation not visible in this context)` when the source is withheld.
- `<TargetNode>`: the Unit to specify, in the same form as an indexed node and under the same source-visibility rule. References in `<invoked_code>` or `<linked_steps>` are supporting evidence for it.
- `<View>`: the visible `<Holistic>`, `<Lineage>`, and `<Neighbors>` context; omitted when no layer is visible.

Every name or ID in `<View>` resolves to an indexed node or to `<TargetNode>`.

```xml
{{Context}}
```

{{Stance}}

Return the spec as the single JSON object defined in the system prompt.
