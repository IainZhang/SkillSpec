"""SKILL.md manifest schema and deterministic parse (no LLM)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

NAME_MAX_LEN = 64
DESCRIPTION_MAX_LEN = 1024
COMPATIBILITY_MAX_LEN = 500
NAME_RE = re.compile(r"^[a-z0-9-]+$")  # kebab-case
# tags only: "<tag>", "</tag>", "<br/>", "<!-- -->" — a bare "<"/">" (e.g. ">5 tool calls")
# is a comparison operator, not markup
TAG_RE = re.compile(r"<[/!?]?[A-Za-z][^>]*>")
ALLOWED_PROPERTIES = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}


class Metadata(BaseModel):
    """Deterministic parse result; empty if no SKILL.md."""

    manifest: SkillManifest | None = None
    resources: dict[str, str] = Field(default_factory=dict)  # rel path -> file content

    def id(self) -> str:
        """Content-addressed id: manifest name + last 4 hex of SKILL.md MD5."""
        if not self.manifest:
            return ""
        digest = hashlib.md5(self.manifest.raw_text.encode("utf-8")).hexdigest()
        return f"{self.manifest.name}_{digest[-4:]}"

    @classmethod
    def parse(cls, src: Path) -> tuple["Metadata", str]:
        """Parse root SKILL.md into (metadata, message); failure signaled by manifest is None."""
        skill_md = src / "SKILL.md"
        if not skill_md.exists():
            return cls(), "SKILL.md not found"

        raw = skill_md.read_text(encoding="utf-8")
        manifest, message = SkillManifest.from_raw(raw)
        if manifest is None:
            return cls(), message

        resources: dict[str, str] = {}
        for p in sorted(src.rglob("*")):
            if not p.is_file() or p == skill_md:
                continue
            rel = p.relative_to(src)
            if any(part.startswith(".") for part in rel.parts):  # skip hidden dirs
                continue
            try:
                resources[str(rel)] = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable — skip, don't abort the whole skill

        return cls(manifest=manifest, resources=resources), message


class SkillManifest(BaseModel):
    """Parsed SKILL.md: YAML frontmatter + markdown body."""

    name: str
    description: str = ""
    content: str = ""    # markdown body verbatim
    raw_text: str = ""  # original SKILL.md content verbatim

    @classmethod
    def from_raw(cls, raw: str) -> tuple["SkillManifest | None", str]:
        """Parse and validate SKILL.md into (manifest, message); failure signaled by manifest is None."""
        fm, content, err = cls.split(raw)
        if fm is None:
            return None, err or "invalid frontmatter"
        ok, msg = cls.check_compliance(fm)
        if not ok:
            return None, msg
        manifest = cls(
            name=fm["name"].strip(),
            description=fm.get("description", ""),
            content=content,
            raw_text=raw,
        )
        return manifest, msg

    @staticmethod
    def split(raw: str) -> tuple[dict | None, str, str | None]:
        """Partition SKILL.md into (frontmatter dict, content, error)."""
        if not raw.startswith("---"):
            return None, raw, "no YAML frontmatter block"
        # the closing fence is a lone "---" line; a bare find("\n---") would match "----"
        # or "---text" inside a frontmatter value
        m = re.search(r"\n---[ \t]*(?=\r?\n|$)", raw[3:])
        if m is None:
            return None, raw, "unterminated frontmatter block"
        end = 3 + m.start()
        block = raw[3:end]
        # content is everything after the closing-fence line's terminating newline.
        split_at = raw.find("\n", end + 1)
        split_at = len(raw) if split_at == -1 else split_at + 1
        content = raw[split_at:]
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as e:
            return None, raw, f"frontmatter is not valid YAML: {e}"
        if not isinstance(data, dict):
            return None, raw, "frontmatter is not a mapping"
        return data, content, None

    @staticmethod
    def check_compliance(frontmatter: dict) -> tuple[bool, str]:
        """Validate frontmatter; unexpected keys are tolerated as a warning (reported, not fatal)."""
        warning = ""
        unexpected = set(frontmatter.keys()) - ALLOWED_PROPERTIES
        if unexpected:
            warning = f"unexpected key(s): {', '.join(sorted(unexpected))}"

        if "name" not in frontmatter:
            return False, "missing 'name'"
        if "description" not in frontmatter:
            return False, "missing 'description'"

        name = frontmatter.get("name", "")
        if not isinstance(name, str):
            return False, f"name must be a string, got {type(name).__name__}"
        name = name.strip()
        if not name:
            return False, "name must not be empty"
        if not NAME_RE.match(name) or name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"name '{name}' must be kebab-case (no leading/trailing/double hyphens)"
        if len(name) > NAME_MAX_LEN:
            return False, f"name too long ({len(name)} > {NAME_MAX_LEN})"

        description = frontmatter.get("description", "")
        if not isinstance(description, str):
            return False, f"description must be a string, got {type(description).__name__}"
        description = description.strip()
        if description:
            if TAG_RE.search(description):
                return False, "description cannot contain HTML/XML tags"
            if len(description) > DESCRIPTION_MAX_LEN:
                return False, f"description too long ({len(description)} > {DESCRIPTION_MAX_LEN})"

        compatibility = frontmatter.get("compatibility", "")
        if compatibility:
            if not isinstance(compatibility, str):
                return False, f"compatibility must be a string, got {type(compatibility).__name__}"
            if len(compatibility) > COMPATIBILITY_MAX_LEN:
                return False, f"compatibility too long ({len(compatibility)} > {COMPATIBILITY_MAX_LEN})"

        return True, warning
