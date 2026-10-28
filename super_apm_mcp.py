"""Project-local MCP projections without modifying tracked or foreign entries."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import tomlkit

STATE = "super-apm-mcp.json"
SOURCES = {".mcp.json": "mcpServers", ".codex/config.toml": "mcp_servers"}


class MCPError(Exception):
    pass


def plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_config(path: Path):
    if not path.exists():
        return tomlkit.document() if path.suffix == ".toml" else {}
    try:
        text = path.read_text(encoding="utf-8")
        value = tomlkit.parse(text) if path.suffix == ".toml" else json.loads(text)
        if not hasattr(value, "get"):
            raise ValueError()
        return value
    except (OSError, ValueError, tomlkit.exceptions.TOMLKitError):
        raise MCPError(f"Cannot parse MCP configuration: {path.name}") from None


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".super-apm-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(content)
        os.chmod(name, 0o600)
        os.replace(name, path)
    except OSError:
        # Retain the private temporary file for recovery instead of deleting it.
        raise MCPError(f"Cannot write MCP configuration: {path.name}") from None


def tracked(root: Path, path: Path) -> bool:
    call = subprocess.run(["git", "-C", str(root), "ls-files", "--", str(path.relative_to(root))], capture_output=True, text=True)
    if call.returncode:
        raise MCPError("Cannot inspect MCP configuration ownership")
    return bool(call.stdout.strip())


def state(root: Path) -> dict:
    path = root / ".apm-local" / STATE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if set(value) - set(SOURCES):
            raise ValueError()
        for entry in value.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("entries", {}), dict):
                raise ValueError()
            for checksum in entry.get("entries", {}).values():
                if not isinstance(checksum, str) or len(checksum) != 64:
                    raise ValueError()
        return value
    except (OSError, ValueError, AttributeError, TypeError):
        raise MCPError("Invalid MCP ownership receipt") from None


def projection(root: Path, relative: str, name: str, value: dict) -> dict:
    entry = copy.deepcopy(plain(value))
    local = root / ".apm-local"
    if relative == ".mcp.json" and entry.get("command") and not entry.get("url"):
        # Claude has no stdio cwd field. The launcher reads the ignored source.
        entry = {key: value for key, value in entry.items() if key not in {"command", "args", "env", "cwd"}}
        entry.update({"type": "stdio", "command": sys.executable, "args": ["-m", "super_apm", "mcp-run", "--project", str(root), "--server", name]})
    elif relative.endswith(".toml") and entry.get("command") and not entry.get("url"):
        cwd = entry.get("cwd", str(local))
        entry["cwd"] = str(local / cwd) if not Path(cwd).is_absolute() else cwd
    return entry


def private_paths(root: Path) -> list[str]:
    previous = state(root)
    return [name for name in SOURCES if name in previous or (root / ".apm-local" / name).exists()]


def prepare(root: Path, expose: bool = True):
    """Preflight all clients before returning any writes; never print config values."""
    local = root / ".apm-local"
    previous = state(root)
    changes = []
    new_state = {}
    ignored = []
    for relative, key in SOURCES.items():
        source = read_config(local / relative)
        desired_source = source.get(key, {}) if expose else {}
        if not isinstance(desired_source, dict) and not hasattr(desired_source, "items"):
            raise MCPError(f"Invalid MCP server map: {relative}")
        old = previous.get(relative, {})
        owned = old.get("entries", {})
        if not desired_source and not owned and not old:
            continue
        target = root / relative
        if not target.parent.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise MCPError(f"Unsafe MCP configuration destination: {relative}")
        if tracked(root, target):
            raise MCPError(f"Local MCP needs a Git-ignored config; tracked file: {relative}")
        current = read_config(target)
        entries = current.get(key, {})
        if not isinstance(entries, dict) and not hasattr(entries, "items"):
            raise MCPError(f"Invalid MCP server map: {relative}")
        for name, checksum in owned.items():
            if name in entries and digest(entries[name]) != checksum:
                raise MCPError(f"An owned MCP entry was edited outside super-apm: {relative}")
        for name, value in desired_source.items():
            if not isinstance(name, str) or not isinstance(plain(value), dict):
                raise MCPError(f"Invalid MCP server entry: {relative}")
            if name in entries and name not in owned:
                raise MCPError(f"A foreign/shared MCP server has the same name: {relative}")
        updated = copy.deepcopy(current)
        if key not in updated:
            updated[key] = {}
        output = updated[key]
        for name in owned:
            output.pop(name, None)
        hashes = {}
        for name, value in desired_source.items():
            entry = projection(root, relative, name, value)
            output[name] = entry
            hashes[name] = digest(entry)
        created = bool(old.get("created", not target.exists()))
        serialized = tomlkit.dumps(updated) if target.suffix == ".toml" else json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        old_text = target.read_text() if target.exists() else None
        if serialized != old_text:
            changes.append((target, serialized))
        if not expose:
            new_state[relative] = {"created": created, "entries": {}}
        elif hashes:
            new_state[relative] = {"created": created, "entries": hashes}
            ignored.append(relative)
        elif old:
            # Keep an existing config's other tables and user-authored settings.
            if expose and created and not output and not (set(updated) - {key}):
                changes = [(path, content) for path, content in changes if path != target]
                if target.exists():
                    changes.append((target, None))
        if owned:
            ignored.append(relative)
    return changes, new_state, ignored


def apply(root: Path, plan, trash) -> int:
    changes, new_state, ignored = plan
    for path, content in changes:
        if content is None:
            if path.exists():
                trash(path)
        else:
            write_private(path, content)
    receipt = root / ".apm-local" / STATE
    if new_state or receipt.exists():
        text = json.dumps(new_state, sort_keys=True, indent=2) + "\n"
        if not receipt.exists() or receipt.read_text() != text:
            write_private(receipt, text)
    return sum(len(x["entries"]) for x in new_state.values())


def run_stdio(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="super-apm mcp-run")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--server", required=True)
    options = parser.parse_args(argv)
    root = options.project.resolve()
    local = root / ".apm-local"
    try:
        source = read_config(local / ".mcp.json")
        entry = source.get("mcpServers", {}).get(options.server)
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            raise MCPError("Local stdio MCP definition is missing")
        arguments = entry.get("args", [])
        environment = entry.get("env", {})
        if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
            raise MCPError("Invalid local stdio arguments")
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise MCPError("Invalid local stdio environment")
        # Match Claude's documented .mcp.json substitution for stdio fields.
        def expand(value):
            def replace(match):
                name, default = match.group(1), match.group(2)
                return os.environ.get(name, default if default is not None else match.group(0))
            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replace, value)
        cwd = Path(expand(entry.get("cwd", str(local))))
        if not cwd.is_absolute():
            cwd = local / cwd
        command = [expand(entry["command"]), *(expand(value) for value in arguments)]
        environment = {**os.environ, **{key: expand(value) for key, value in environment.items()}}
        if os.name != "nt":
            os.chdir(cwd)
            os.execvpe(command[0], command, environment)
        return subprocess.run(command, cwd=cwd, env=environment).returncode
    except (MCPError, OSError):
        print("super-apm: cannot start local stdio MCP; inspect its ignored configuration", file=sys.stderr)
        return 1
