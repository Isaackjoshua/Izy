"""Bringing an existing config.toml up to date without losing your edits.

`config.load()` writes the commented defaults only when the file does not
exist, so every section added after the file was first written never reached
disk. The values still default correctly — but the commented file *is* the
documentation, and knobs you cannot see are knobs you cannot tune. On a machine
that had been running since Phase 1, five of the ten sections were missing,
including the classify rules that are the main lever on LLM spend.

The merge is deliberately additive and text-level:

  * an existing value is **never** rewritten, reordered or reformatted — if the
    key is there, it is left exactly as you typed it, comment and all;
  * a missing section is appended whole, with its explanatory comments;
  * a missing key inside a section you already have is inserted at the end of
    that section, with its comment;
  * a backup is written first, because being wrong here means editing a file
    the user owns.

Nothing calls this automatically. Silently rewriting someone's config on
startup is exactly the kind of thing this tool should not do; `izy config
--upgrade` is an explicit request, and `izy doctor` only points out that it is
available.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_CONFIG_TOML

_SECTION_RE = re.compile(r"^\[([A-Za-z0-9_.]+)\]\s*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=")


@dataclass
class Section:
    name: str
    #: comments preceding the header, plus the header line itself
    header: list[str] = field(default_factory=list)
    #: key -> its comment lines plus the assignment line
    keys: dict[str, list[str]] = field(default_factory=dict)

    def text(self) -> str:
        out = list(self.header)
        for block in self.keys.values():
            out.extend(block)
        return "".join(out)


def parse(text: str) -> dict[str, Section]:
    """Split a TOML file into sections and keys, keeping every comment attached
    to whatever it introduces. Not a TOML parser — a layout-preserving one."""
    sections: dict[str, Section] = {}
    current: Section | None = None
    pending: list[str] = []

    for raw in text.splitlines(keepends=True):
        stripped = raw.strip()
        if match := _SECTION_RE.match(stripped):
            current = Section(match.group(1), header=[*pending, raw])
            sections[current.name] = current
            pending = []
            continue
        if current is not None and (match := _KEY_RE.match(stripped)):
            current.keys[match.group(1)] = [*pending, raw]
            pending = []
            continue
        pending.append(raw)

    return sections


@dataclass
class Plan:
    added_sections: list[str] = field(default_factory=list)
    added_keys: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.added_sections and not self.added_keys

    def describe(self) -> str:
        if self.empty:
            return "config is up to date"
        bits = []
        if self.added_sections:
            bits.append(f"{len(self.added_sections)} section(s): "
                        + ", ".join(self.added_sections))
        if self.added_keys:
            bits.append(f"{len(self.added_keys)} key(s): " + ", ".join(self.added_keys))
        return "missing " + "; ".join(bits)


def plan_for(text: str) -> Plan:
    """What the shipped defaults have that this file does not."""
    theirs = parse(text)
    ours = parse(DEFAULT_CONFIG_TOML)
    plan = Plan()
    for name, section in ours.items():
        if name not in theirs:
            plan.added_sections.append(name)
            continue
        for key in section.keys:
            if key not in theirs[name].keys:
                plan.added_keys.append(f"{name}.{key}")
    return plan


def apply(text: str) -> tuple[str, Plan]:
    """Return the upgraded text and what changed. Pure — does no I/O."""
    plan = plan_for(text)
    if plan.empty:
        return text, plan

    theirs = parse(text)
    ours = parse(DEFAULT_CONFIG_TOML)
    lines = text.splitlines(keepends=True)

    # Insert missing keys into sections that already exist, working from the
    # bottom up so earlier insertions do not shift later line numbers.
    insertions: list[tuple[int, list[str]]] = []
    for name, section in ours.items():
        if name not in theirs:
            continue
        missing = [k for k in section.keys if k not in theirs[name].keys]
        if not missing:
            continue
        end = _section_end(lines, name)
        block: list[str] = []
        for key in missing:
            block.extend(section.keys[key])
        insertions.append((end, block))

    for index, block in sorted(insertions, reverse=True):
        lines[index:index] = block

    out = "".join(lines)
    if plan.added_sections:
        if not out.endswith("\n"):
            out += "\n"
        for name in plan.added_sections:
            out += "\n" + ours[name].text().lstrip("\n")
    return out, plan


def _section_end(lines: list[str], name: str) -> int:
    """Index just past the last non-blank line of a section."""
    start = None
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line.strip()):
            if line.strip() == f"[{name}]":
                start = i
            elif start is not None:
                # Back up over the blank lines and comments that belong to the
                # *next* section, so the insert lands inside this one.
                end = i
                while end > start + 1 and lines[end - 1].strip() == "":
                    end -= 1
                while end > start + 1 and lines[end - 1].lstrip().startswith("#"):
                    end -= 1
                return end
    if start is None:
        return len(lines)
    end = len(lines)
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    return end


def upgrade_file(path: Path, *, backup: bool = True) -> Plan:
    """Rewrite `path` in place, keeping a .bak alongside it."""
    text = path.read_text()
    new_text, plan = apply(text)
    if plan.empty:
        return plan
    if backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(new_text)
    return plan
