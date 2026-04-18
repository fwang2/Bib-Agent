from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


AGENT_PREFIX = "% BIB_AGENT "


@dataclass
class AgentChunk:
    metadata: dict
    raw_entry: str


@dataclass
class BibEntry:
    entry_type: str
    key: str
    raw_entry: str
    fields: dict[str, str]


def ensure_file_with_managed_block(path: Path, start_marker: str, end_marker: str) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = ""
    updated = inject_managed_block_if_missing(content, start_marker, end_marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def inject_managed_block_if_missing(content: str, start_marker: str, end_marker: str) -> str:
    if start_marker in content and end_marker in content:
        return content
    managed_block = (
        f"{start_marker}\n"
        + "% Agent-managed entries live only inside this block.\n"
        + f"{end_marker}\n"
    )
    suffix = content.lstrip("\n")
    if suffix:
        return managed_block + "\n" + suffix
    return managed_block


def _managed_bounds(content: str, start_marker: str, end_marker: str) -> tuple[int, int]:
    start = content.index(start_marker) + len(start_marker)
    end = content.index(end_marker)
    return start, end


def extract_managed_chunks(content: str, start_marker: str, end_marker: str) -> list[AgentChunk]:
    if start_marker not in content or end_marker not in content:
        return []
    start, end = _managed_bounds(content, start_marker, end_marker)
    block = content[start:end]
    lines = block.splitlines()
    chunks: list[AgentChunk] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith(AGENT_PREFIX):
            i += 1
            continue
        metadata = json.loads(line[len(AGENT_PREFIX) :])
        i += 1
        while i < len(lines) and not lines[i].lstrip().startswith("@"):
            i += 1
        if i >= len(lines):
            break
        entry_lines = [lines[i]]
        brace_depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines):
            entry_lines.append(lines[i])
            brace_depth += lines[i].count("{") - lines[i].count("}")
            i += 1
            if brace_depth <= 0:
                break
        chunks.append(AgentChunk(metadata=metadata, raw_entry="\n".join(entry_lines).strip()))
    return chunks


def replace_managed_block(path: Path, start_marker: str, end_marker: str, rendered_chunks: list[str]) -> None:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = build_updated_content(content, start_marker, end_marker, rendered_chunks)
    path.write_text(updated, encoding="utf-8")


def build_updated_content(content: str, start_marker: str, end_marker: str, rendered_chunks: list[str]) -> str:
    content = inject_managed_block_if_missing(content, start_marker, end_marker)
    start, end = _managed_bounds(content, start_marker, end_marker)
    managed_body = "\n"
    if rendered_chunks:
        managed_body += "% Agent-managed entries live only inside this block.\n\n"
        managed_body += "\n\n".join(rendered_chunks)
        managed_body += "\n"
    else:
        managed_body += "% Agent-managed entries live only inside this block.\n"
    return content[:start] + managed_body + content[end:]


def validate_rendered_chunks(rendered_chunks: list[str]) -> None:
    keys = set()
    for chunk in rendered_chunks:
        match = re.search(r"@\w+\{([^,]+),", chunk)
        if not match:
            raise ValueError("Generated entry is missing a BibTeX key.")
        key = match.group(1)
        if key in keys:
            raise ValueError(f"Duplicate generated BibTeX key: {key}")
        keys.add(key)
        depth = 0
        for character in chunk:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            if depth < 0:
                raise ValueError(f"Unbalanced braces in entry {key}")
        if depth != 0:
            raise ValueError(f"Unbalanced braces in entry {key}")


def strip_managed_block(content: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in content or end_marker not in content:
        return content
    start = content.index(start_marker)
    end = content.index(end_marker) + len(end_marker)
    before = content[:start].rstrip()
    after = content[end:].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    return before or after


def remove_bib_entry(content: str, raw_entry: str) -> str:
    if raw_entry not in content:
        return content
    updated = content.replace(raw_entry, "", 1)
    updated = re.sub(r"\n{3,}", "\n\n", updated)
    return updated.strip() + "\n" if updated.strip() else ""


def extract_bib_entries(content: str) -> list[BibEntry]:
    entries: list[BibEntry] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if not stripped.startswith("@"):
            i += 1
            continue
        entry_lines = [lines[i]]
        brace_depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines):
            entry_lines.append(lines[i])
            brace_depth += lines[i].count("{") - lines[i].count("}")
            i += 1
            if brace_depth <= 0:
                break
        raw_entry = "\n".join(entry_lines).strip()
        parsed = _parse_entry(raw_entry)
        if parsed:
            entries.append(parsed)
    return entries


def _parse_entry(raw_entry: str) -> BibEntry | None:
    header = re.match(r"@(\w+)\{([^,]+),", raw_entry, flags=re.S)
    if not header:
        return None
    entry_type = header.group(1).lower()
    key = header.group(2).strip()
    fields: dict[str, str] = {}
    for field_name in ["title", "author", "year", "doi", "url", "eprint", "journal", "booktitle", "archiveprefix"]:
        value = _extract_field(raw_entry, field_name)
        if value is not None:
            fields[field_name.lower()] = value
    return BibEntry(entry_type=entry_type, key=key, raw_entry=raw_entry, fields=fields)


def _extract_field(raw_entry: str, field_name: str) -> str | None:
    pattern = re.compile(rf"(?im)^\s*{re.escape(field_name)}\s*=\s*")
    match = pattern.search(raw_entry)
    if not match:
        return None
    index = match.end()
    while index < len(raw_entry) and raw_entry[index].isspace():
        index += 1
    if index >= len(raw_entry):
        return None

    opener = raw_entry[index]
    if opener not in "{\"\n":
        start = index
        while index < len(raw_entry) and raw_entry[index] not in ",\n":
            index += 1
        return raw_entry[start:index].strip()

    if opener == "{":
        index += 1
        depth = 1
        start = index
        while index < len(raw_entry):
            char = raw_entry[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return raw_entry[start:index].strip()
            index += 1
        return raw_entry[start:].strip()

    if opener == "\"":
        index += 1
        start = index
        while index < len(raw_entry):
            char = raw_entry[index]
            if char == "\"" and raw_entry[index - 1] != "\\":
                return raw_entry[start:index].strip()
            index += 1
        return raw_entry[start:].strip()

    return None
