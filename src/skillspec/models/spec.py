"""spec_gen product: a unit's Hoare-style behavioral specification."""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from .errors import RetryableError
from .utils import json_object


def _predicates(obj: dict, field: str) -> list[str]:
    """Coerce a spec field to a predicate list; a bare string wraps, other non-lists are a parse miss."""
    raw = obj.get(field)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise RetryableError(f"spec field {field!r} must be an array of predicates")
    return [str(p).strip() for p in raw if str(p).strip()]


class UnitSpec(BaseModel):
    """One unit's Hoare-style spec: a short {name} plus pre/post/effects predicate lists."""

    unit_id: str        # stamped by caller
    name: str = ""      # from LLM
    pre: list[str] = Field(default_factory=list)
    post: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "UnitSpec":
        """Parse {unit, pre, post, effects}; RetryableError on missing/empty JSON. Caller stamps unit_id."""
        obj = json_object(text, "spec")
        try:
            spec = cls(
                unit_id="",
                name=str(obj.get("unit", "")).strip(),
                pre=_predicates(obj, "pre"),
                post=_predicates(obj, "post"),
                effects=_predicates(obj, "effects"),
            )
        except (TypeError, ValidationError) as err:
            raise RetryableError(f"invalid spec JSON: {err}") from err
        if not (spec.name or spec.pre or spec.post or spec.effects):
            raise RetryableError("spec JSON is empty (no unit/pre/post/effects)")
        return spec

    @staticmethod
    def correction(error: str) -> str:
        return (
            f"That reply did not contain a usable spec:\n{error}\n\n"
            f"Return a single JSON object with keys 'unit', 'pre', 'post', 'effects'; "
            f"'pre'/'post' are arrays of predicates and may be empty only when the unit "
            f"genuinely has no such conditions."
        )

    @classmethod
    def tool(cls) -> dict | None:
        predicates = {"type": "array", "items": {"type": "string"}}
        return {
            "type": "function",
            "function": {
                "name": "emit_spec",
                "description": "Return the unit's Hoare-style spec as specified in the system prompt.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "unit": {"type": "string"},
                        "pre": predicates,
                        "post": predicates,
                        "effects": predicates,
                    },
                    "required": ["unit", "pre", "post", "effects"],
                },
            },
        }
