"""LLM-loop control-flow exceptions raised by output-model parsers."""

from __future__ import annotations


class RetryableError(Exception):
    """Parse miss — signals the LLM closed loop to feed correction() and retry."""


class WorkflowParseError(RetryableError):
    """Workflow XML malformed, missing required attributes, or violates graph invariants."""
