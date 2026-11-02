"""Shared snapshots that only their owner refreshes from a private upstream.

A snapshot is a tracked folder in the project, for example .apm/skills/<name>, which every
collaborator gets through Git. Where it comes from lives only in the Git-ignored .apm-local:
upstreams.yml names the source (a local Git checkout or a Git URL), the folder inside it and
the ref; upstreams-state.json remembers what was copied last. Only the committed state of the
source is copied, so work in progress never reaches the project. Collaborators without the
ignored files keep the snapshot as committed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

import yaml

from super_apm_mcp import write_private

CONFIG = "upstreams.yml"
STATE = "upstreams-state.json"
CACHE = "upstream-cache"
IGNORED = {".git", ".DS_Store", "__pycache__", "node_modules"}


class UpstreamError(Exception):
    pass


def local_dir(root: Path) -> Path:
    return root / ".apm-local"


def load(root: Path) -> list[dict]:
    path = local_dir(root) / CONFIG
    if not path.exists():
        return []
    try:
        value = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        raise UpstreamError("Cannot parse the upstream configuration") from None
    entries = value.get("upstreams", []) if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise UpstreamError("Invalid upstream configuration")
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("path", "source", "subpath")):
            raise UpstreamError("Each upstream needs path, source and subpath")
        if not isinstance(entry.get("ref", "HEAD"), str):
            raise UpstreamError("Invalid upstream ref")
        destination(root, entry["path"])
    return entries


def save_config(root: Path, entries: list[dict]) -> None:
    write_private(local_dir(root) / CONFIG, yaml.safe_dump({"upstreams": entries}, sort_keys=False, allow_unicode=True))


def read_state(root: Path) -> dict:
    path = local_dir(root) / STATE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise UpstreamError("Invalid upstream state") from None


def save_state(root: Path, state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = local_dir(root) / STATE
    if not path.exists() or path.read_text() != text:
        write_private(path, text)


def destination(root: Path, relative: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", relative) or ".." in PurePosixPath(relative).parts:
        raise UpstreamError(f"Unsafe snapshot path: {relative}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.resolve().is_relative_to(local_dir(root).resolve()):
        raise UpstreamError(f"A snapshot must live in the project, outside .apm-local: {relative}")
    if path.is_symlink():
        raise UpstreamError(f"A snapshot root must not be a symlink: {relative}")
    return path


def git(repo: Path, *args: str) -> bytes:
    call = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if call.returncode:
        # Never echo Git output: it may contain the private source location.
        raise UpstreamError("Git could not read an upstream")
    return call.stdout


def repository(root: Path, entry: dict) -> tuple[Path, str]:
    """A readable Git directory and the commit to copy from."""
    source, ref = entry["source"], entry.get("ref", "HEAD")
    candidate = Path(os.path.expanduser(source))
    if candidate.is_dir():
        return candidate, ref
    # A URL: keep a private bare mirror and refresh it on every sync.
    mirror = local_dir(root) / CACHE / (hashlib.sha256(source.encode()).hexdigest()[:16] + ".git")
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        call = subprocess.run(["git", "clone", "--bare", "--quiet", source, str(mirror)], capture_output=True)
        if call.returncode:
            raise UpstreamError("Cannot clone an upstream")
    git(mirror, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*")
    return mirror, ref


def committed(repo: Path, ref: str, subpath: str) -> dict[str, tuple[str, str]]:
    """relative path -> (sha256, blob id) of the folder as committed at ref."""
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    prefix = subpath.strip("/") + "/"
    result = {}
    for record in git(repo, "ls-tree", "-r", "-z", commit, "--", prefix).split(b"\0"):
        if not record:
            continue
        meta, path = record.decode().split("\t", 1)
        mode, kind, blob = meta.split()
        relative = path[len(prefix):]
        if any(part in IGNORED or part.startswith(".env") for part in relative.split("/")):
            continue
        if mode == "120000" or kind != "blob":
            raise UpstreamError("Symlinks and submodules are not supported inside a snapshot")
        result[relative] = (hashlib.sha256(git(repo, "cat-file", "blob", blob)).hexdigest(), blob)
    return dict(sorted(result.items()))


def inventory(folder: Path) -> dict[str, str]:
    result = {}
    if not folder.exists():
        return result
    for current, directories, files in os.walk(folder, followlinks=False):
        base = Path(current)
        directories[:] = sorted(d for d in directories if d not in IGNORED and not d.startswith(".env"))
        for name in directories + files:
            if name in IGNORED or name.startswith(".env"):
                continue
            path = base / name
            if path.is_symlink():
                raise UpstreamError("Symlinks are not supported inside a snapshot")
            if path.is_file():
                result[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def ours(current: dict, *known) -> bool:
    """Every file matches a version written by super-apm: the last sync or an interrupted one."""
    return all(any(k and k.get(path) == checksum for k in known) for path, checksum in current.items())


def add(root: Path, path: str, source: str, subpath: str, ref: str = "HEAD") -> None:
    entries = [e for e in load(root) if e["path"] != path]
    folder = destination(root, path)
    entry = {"path": path, "source": source, "subpath": subpath.strip("/")}
    if ref != "HEAD":
        entry["ref"] = ref
    repo, commit_ref = repository(root, entry)
    if not committed(repo, commit_ref, entry["subpath"]):
        raise UpstreamError("The upstream folder is empty at this ref")
    save_config(root, [*entries, entry])
    state = read_state(root)
    # An existing snapshot is adopted as the last synced version, so the next sync may replace it.
    state[path] = {"baseline": inventory(folder)} if folder.exists() else {}
    save_state(root, state)


def remove(root: Path, path: str) -> None:
    entries = load(root)
    if not any(e["path"] == path for e in entries):
        raise UpstreamError(f"No upstream for {path}")
    save_config(root, [e for e in entries if e["path"] != path])
    state = read_state(root)
    state.pop(path, None)
    save_state(root, state)


def sync(root: Path, trash, dry_run: bool = False) -> list[str]:
    """Copy each upstream's committed folder into its snapshot; keep local edits untouched."""
    entries = load(root)
    if not entries:
        return []
    state = read_state(root)
    changed, failures = [], 0
    for entry in entries:
        path = entry["path"]
        record = state.setdefault(path, {})
        try:
            folder = destination(root, path)
            repo, ref = repository(root, entry)
            source = committed(repo, ref, entry["subpath"])
            if not source:
                raise UpstreamError("The upstream folder is empty at this ref")
            upstream = {relative: checksum for relative, (checksum, _) in source.items()}
            current = inventory(folder)
            baseline, pending = record.get("baseline"), record.get("pending")
            if current == upstream:
                if not dry_run and (baseline != upstream or pending):
                    state[path] = {"baseline": upstream}
                continue
            if baseline is None:
                edited = bool(current) and not ours(current, pending)
            else:
                edited = current != baseline and not (pending and ours(current, baseline, pending))
            if edited:
                print(f"[super-apm] upstream {path}: local edits preserved; merge them into the upstream", flush=True)
                continue
            print(f"[super-apm] upstream {path}: {'would update' if dry_run else 'updating'}", flush=True)
            changed.append(path)
            if dry_run:
                continue
            # Record the target first: an interrupted copy is resumed, not mistaken for edits.
            record["pending"] = upstream
            save_state(root, state)
            for relative in sorted(set(current) - set(upstream)):
                trash(folder / relative)
            for relative, (checksum, blob) in source.items():
                if current.get(relative) == checksum:
                    continue
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(git(repo, "cat-file", "blob", blob))
            state[path] = {"baseline": upstream}
        except (OSError, UpstreamError) as error:
            failures += 1
            # Report the snapshot, never the source location or Git output.
            print(f"[super-apm] upstream {path}: sync stopped ({type(error).__name__}); inspect the snapshot before retry", flush=True)
    if not dry_run:
        save_state(root, state)
    if failures:
        raise UpstreamError(f"{failures} upstream snapshot(s) could not be refreshed")
    return changed


def describe(root: Path) -> list[str]:
    state = read_state(root)
    rows = []
    for entry in load(root):
        synced = "synced" if state.get(entry["path"], {}).get("baseline") else "not synced"
        rows.append(f"{entry['path']}  <- {entry['source']}:{entry['subpath']}@{entry.get('ref', 'HEAD')}  ({synced})")
    return rows
