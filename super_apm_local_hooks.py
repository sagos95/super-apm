"""Project-local hook projections without modifying tracked or foreign hook entries.

APM installs a local package's hooks inside .apm-local as if it were a separate project. The
handlers it records there point at .claude/hooks and .codex/hooks of that inner project, so they
are rewritten to the .apm-local copies and merged into the clients' Git-ignored project settings:
Claude Code reads .claude/settings.local.json, Codex reads inline [hooks] tables in
.codex/config.toml.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

import tomlkit
from tomlkit.items import Table

import super_apm_mcp as mcp

STATE = "super-apm-hooks.json"
# Target in the project -> APM's record of the hooks it deployed inside .apm-local.
SOURCES = {".claude/settings.local.json": ".claude/apm-hooks.json", ".codex/config.toml": ".codex/apm-hooks.json"}
GIT_ROOT = "$(git rev-parse --show-toplevel)"
CLAUDE_PATH = re.compile(r"(\$\{CLAUDE_PROJECT_DIR\}|\$CLAUDE_PROJECT_DIR)/\.claude/hooks/")
# Codex runs commands from the session cwd, which may be a subdirectory: anchor at the Git root.
CODEX_PATH = re.compile(r"""(["']?)(?<![\w./$}-])\.codex/hooks/([^"'\s]+)\1""")


class HookError(Exception):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(mcp.plain(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plain_deep(value):
    value = mcp.plain(value)
    if isinstance(value, dict):
        return {key: plain_deep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain_deep(item) for item in value]
    return value


def state(root: Path) -> dict:
    path = root / ".apm-local" / STATE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if set(value) - set(SOURCES):
            raise ValueError()
        for entry in value.values():
            events = entry.get("entries", {})
            if not isinstance(events, dict):
                raise ValueError()
            for checksums in events.values():
                if not isinstance(checksums, list) or not all(isinstance(c, str) and len(c) == 64 for c in checksums):
                    raise ValueError()
        return value
    except (OSError, ValueError, AttributeError, TypeError):
        raise HookError("Invalid hook ownership receipt") from None


def rewrite(target: str, command: str) -> str:
    if target == ".claude/settings.local.json":
        return CLAUDE_PATH.sub(lambda m: f"{m.group(1)}/.apm-local/.claude/hooks/", command)
    return CODEX_PATH.sub(lambda m: f'"{GIT_ROOT}/.apm-local/.codex/hooks/{m.group(2)}"', command)


def desired(root: Path, target: str) -> dict[str, list[dict]]:
    """Hook groups APM deployed for one client, re-pointed at the .apm-local copies."""
    source = root / ".apm-local" / SOURCES[target]
    if not source.exists():
        return {}
    try:
        recorded = json.loads(source.read_text())
    except (OSError, ValueError):
        raise HookError(f"Cannot parse local hook record: {SOURCES[target]}") from None
    if not isinstance(recorded, dict):
        raise HookError(f"Invalid local hook record: {SOURCES[target]}")
    result = {}
    for event, groups in recorded.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise HookError(f"Invalid local hook record: {SOURCES[target]}")
        projected = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise HookError(f"Invalid local hook group: {SOURCES[target]}")
            group = {key: copy.deepcopy(value) for key, value in group.items() if not key.startswith("_apm")}
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise HookError(f"Invalid local hook handler: {SOURCES[target]}")
                if isinstance(handler.get("command"), str):
                    handler["command"] = rewrite(target, handler["command"])
            projected.append(group)
        if projected:
            result[event] = projected
    return result


def private_paths(root: Path) -> list[str]:
    previous = state(root)
    return [target for target, source in SOURCES.items() if target in previous or (root / ".apm-local" / source).exists()]


def to_toml(group: dict):
    table = tomlkit.table()
    for key, value in group.items():
        if key == "hooks":
            handlers = tomlkit.aot()
            for handler in value:
                handlers.append(tomlkit.item(handler))
            table.add(key, handlers)
        else:
            table.add(key, value)
    return table


def read(path: Path):
    if path.suffix == ".toml":
        return mcp.read_config(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HookError(f"Cannot parse hook configuration: {path.name}") from None
    if not isinstance(value, dict):
        raise HookError(f"Cannot parse hook configuration: {path.name}")
    return value


def prepare(root: Path, expose: bool = True):
    """Preflight both clients before returning any writes; never print hook commands."""
    previous = state(root)
    changes, new_state, ignored = [], {}, []
    for target_name in SOURCES:
        wanted = desired(root, target_name) if expose else {}
        old = previous.get(target_name, {})
        owned = old.get("entries", {})
        if not wanted and not owned and not old:
            continue
        target = root / target_name
        if not target.parent.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise HookError(f"Unsafe hook configuration destination: {target_name}")
        if mcp.tracked(root, target):
            raise HookError(f"Local hooks need a Git-ignored config; tracked file: {target_name}")
        current = read(target)
        updated = copy.deepcopy(current)
        events = updated.get("hooks", {})
        if not hasattr(events, "items"):
            raise HookError(f"Invalid hook map: {target_name}")
        remaining = {}
        for event, groups in events.items():
            pending = list(owned.get(event, []))
            kept = []
            for group in groups:
                checksum = digest(plain_deep(group))
                if checksum in pending:
                    pending.remove(checksum)  # Ours from the previous sync: replaced below.
                else:
                    kept.append(group)
            remaining[event] = kept
        hashes = {}
        for event, groups in wanted.items():
            remaining.setdefault(event, [])
            for group in groups:
                remaining[event].append(to_toml(group) if target.suffix == ".toml" else group)
                hashes.setdefault(event, []).append(digest(group))
        remaining = {event: groups for event, groups in remaining.items() if groups}
        if target.suffix == ".toml":
            table = tomlkit.table()
            for event, groups in remaining.items():
                aot = tomlkit.aot()
                for group in groups:
                    aot.append(group if isinstance(group, Table) else to_toml(plain_deep(group)))
                table.add(event, aot)
            if remaining:
                updated["hooks"] = table
            elif "hooks" in updated:
                del updated["hooks"]
        elif remaining:
            updated["hooks"] = remaining
        else:
            updated.pop("hooks", None)
        created = bool(old.get("created", not target.exists()))
        serialized = tomlkit.dumps(updated) if target.suffix == ".toml" else json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        old_text = target.read_text() if target.exists() else None
        if created and not plain_deep(updated):
            # Nothing but our own entries ever lived in a file we created.
            if target.exists():
                changes.append((target, None))
        elif serialized != old_text:
            changes.append((target, serialized))
        if hashes:
            new_state[target_name] = {"created": created, "entries": hashes}
            ignored.append(target_name)
        elif owned:
            ignored.append(target_name)
    return changes, new_state, ignored


def apply(root: Path, plan, trash) -> int:
    changes, new_state, _ = plan
    for path, content in changes:
        if content is None:
            if path.exists():
                trash(path)
        else:
            mcp.write_private(path, content)
    receipt = root / ".apm-local" / STATE
    if new_state or receipt.exists():
        text = json.dumps(new_state, sort_keys=True, indent=2) + "\n"
        if not receipt.exists() or receipt.read_text() != text:
            mcp.write_private(receipt, text)
    return sum(len(checksums) for entry in new_state.values() for checksums in entry["entries"].values())
