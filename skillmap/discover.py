"""Discover installed Claude skills and parse their SKILL.md frontmatter.

A "skill" is any directory containing a SKILL.md. We follow symlinks (the
orchestration skill is a symlink into ~/.agents/skills). Frontmatter is minimal
YAML (name + description); we parse it without a yaml dependency so skillmap
stays stdlib-only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Default GLOBAL roots to scan for skills. ~/.claude/skills is where Claude Code
# loads user skills from; ~/.agents/skills is the Orca canonical location that
# the .claude symlinks point into. Project-level skills live under
# <project>/.claude/skills (see find_project_root / project_skills_root).
DEFAULT_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".agents" / "skills",
]


def find_project_root(start: Path | None = None) -> Path | None:
    """Nearest ancestor of `start` (default: cwd) containing .git, else None."""
    cur = (start or Path.cwd()).resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return cand
    return None


def project_skills_root(project_root: Path) -> Path:
    """Where project-level skills live: <project>/.claude/skills."""
    return Path(project_root) / ".claude" / "skills"


@dataclass
class Skill:
    """One installed skill parsed from its SKILL.md."""

    name: str
    description: str
    path: Path  # absolute path to the SKILL.md
    body: str  # SKILL.md content with frontmatter stripped
    triggers: list[str] = field(default_factory=list)  # slash-commands, e.g. /graphify
    references: list[str] = field(default_factory=list)  # references/*.md filenames
    headings: list[str] = field(default_factory=list)  # section headings from the body

    @property
    def slug(self) -> str:
        """Filesystem-safe stable id derived from the skill name."""
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (fields, body). Handles `key: value`, folded (>-), and quoted scalars.

    Minimal YAML: enough for name/description which is all skill frontmatter uses.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block, body = m.group(1), text[m.end():]
    fields: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal key, buf
        if key is not None:
            val = " ".join(s.strip() for s in buf if s.strip())
            val = val.strip().strip('"').strip("'")
            fields[key] = val
        key, buf = None, []

    for line in block.splitlines():
        # A new top-level key starts at column 0 with `key:` and is not a list item.
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km and not line.startswith(" ") and not line.startswith("\t"):
            flush()
            key = km.group(1)
            rest = km.group(2).strip()
            # Folded/literal block scalars (>- , >, |, |-) -> collect following lines.
            if rest in (">", ">-", "|", "|-", ">+", "|+"):
                buf = []
            elif rest:
                buf = [rest]
            else:
                buf = []
        elif key is not None:
            buf.append(line)
    flush()
    return fields, body


# Slash-command triggers, e.g. "Trigger: `/graphify`" or "types `/foo`".
_TRIGGER_RE = re.compile(r"[`\s\"'(]/([a-z][a-z0-9-]{1,40})\b")
_HEADING_RE = re.compile(r"^#{1,4}\s+(.+?)\s*#*$", re.MULTILINE)


# Slash-tokens that look like triggers but are paths/output-dirs/flags/prose.
_TRIGGER_BLOCKLIST = {
    "tmp", "usr", "bin", "etc", "dev", "var", "opt", "home", "abs", "rel",
    "graphify-out", "path", "dir", "out", "src", "lib", "node-modules",
}


def _extract_triggers(name: str, body: str) -> list[str]:
    found: set[str] = set()
    # A skill named X almost always answers to /X — that's the canonical trigger.
    slugcmd = "/" + re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    found.add(slugcmd)
    for m in _TRIGGER_RE.finditer(body):
        tok = m.group(1)
        if tok in _TRIGGER_BLOCKLIST or tok.endswith("-out"):
            continue
        # Only trust a slash-token if it's the skill's own name or appears with
        # trigger-ish framing ("Trigger:", "types /x", "invoked ... /x").
        if tok == slugcmd.lstrip("/"):
            found.add("/" + tok)
    return sorted(found)


def parse_skill(skill_md: Path) -> Skill | None:
    """Parse a single SKILL.md into a Skill, or None if it lacks a name."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, body = _parse_frontmatter(text)
    name = fields.get("name") or skill_md.parent.name
    description = fields.get("description", "").strip()
    if not name:
        return None

    refs: list[str] = []
    refdir = skill_md.parent / "references"
    if refdir.is_dir():
        refs = sorted(p.name for p in refdir.glob("*.md"))

    headings = [h.strip() for h in _HEADING_RE.findall(body)]

    return Skill(
        name=name,
        description=description,
        path=skill_md.resolve(),
        body=body,
        triggers=_extract_triggers(name, body),
        references=refs,
        headings=headings,
    )


def discover(roots: list[Path] | None = None) -> list[Skill]:
    """Find and parse every SKILL.md under the given roots (dedup by resolved path)."""
    roots = roots or DEFAULT_ROOTS
    seen_paths: set[Path] = set()
    seen_names: set[str] = set()
    skills: list[Skill] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        # followlinks so the orchestration symlink resolves; dedup by real path.
        for dirpath, _dirs, files in os.walk(root, followlinks=True):
            if "SKILL.md" not in files:
                continue
            skill_md = Path(dirpath) / "SKILL.md"
            real = skill_md.resolve()
            if real in seen_paths:
                continue
            seen_paths.add(real)
            skill = parse_skill(skill_md)
            if skill is None or skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            skills.append(skill)
    return sorted(skills, key=lambda s: s.name)
