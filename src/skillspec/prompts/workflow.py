"""Workflow-derivation prompt: SKILL.md -> workflow XML."""

from __future__ import annotations

from skillspec.models import Metadata, WorkflowGraph
from skillspec.prompts.prompt import Prompt, read_md
from skillspec.prompts.render import skill_parts


class WorkflowPrompt(Prompt[WorkflowGraph]):
    system = read_md("workflow_sys")
    template = read_md("workflow_user")
    schema = WorkflowGraph

    def build(self, metadata: Metadata) -> str:
        manifest, resources = skill_parts(metadata)
        return self.render(self.template, Manifest=manifest, Resources=resources)


WORKFLOW = WorkflowPrompt()
