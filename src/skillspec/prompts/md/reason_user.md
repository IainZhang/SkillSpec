The input is provided as XML. `<TargetNode>` holds the unit to judge in full; `<ExpectSpec>` states its intended specification, and each view-labeled `<FactSpec>` states its implemented specification.

Judge whether `<TargetNode>` contains a genuine bug, following the system prompt.

```xml
{{Context}}

{{ExpectSpec}}

{{FactSpec}}
```
