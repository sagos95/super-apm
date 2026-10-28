"""Audit a deployment snapshot without traversing registered nested worktrees."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile


class AuditError(Exception):
    pass


def nested_worktrees(root):
    root = root.resolve()
    result = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"], capture_output=True)
    if result.returncode:
        raise AuditError("Cannot enumerate Git worktrees for audit")
    paths = []
    boundary = root / ".claude/worktrees"
    for field in result.stdout.decode().split("\0"):
        if not field.startswith("worktree "):
            continue
        path = Path(field[9:]).resolve()
        if path != boundary and path.is_relative_to(boundary) and (path / ".git").is_file():
            paths.append(path)
    return paths


def snapshot(root, omitted):
    root = root.resolve()
    import super_apm as app
    manifest = app.read_yaml(root / "apm.yml")
    targets = manifest.get("targets")
    # Other target roots and local-source references require native semantics.
    if not isinstance(targets, list) or not targets or not set(targets) <= {"claude", "codex"}:
        raise AuditError("Nested-worktree audit isolation requires explicit Claude/Codex targets; use native apm audit for other targets")
    lock = app.read_yaml(root / "apm.lock.yaml")
    for dep in lock.get("dependencies", []):
        if dep.get("source") == "local" or dep.get("local_path"):
            raise AuditError("Nested-worktree audit isolation does not support local-path dependencies; use native apm audit")
        for relative in dep.get("deployed_files", []):
            if any((root / relative).is_relative_to(path) for path in omitted):
                raise AuditError("A deployment claim overlaps a nested worktree; refusing to exclude it")
    for dep in lock.get("deployments", []):
        relative = dep.get("value", "")
        if relative and any((root / relative).is_relative_to(path) for path in omitted):
            raise AuditError("A deployment claim overlaps a nested worktree; refusing to exclude it")
    policy = manifest.get("policy", {})
    if isinstance(policy, dict) and any(isinstance(v, str) and (v.startswith(('.', '/')) or (root / v).exists()) for v in policy.values()):
        raise AuditError("Nested-worktree audit isolation does not support file-based policy overrides; use native apm audit")
    dest = Path(tempfile.mkdtemp(prefix="super-apm-audit-"))
    os.chmod(dest, 0o700)
    try:
        omitted = set(omitted)
        def ignore(directory, names):
            return [name for name in names if Path(directory) / name in omitted]
        # Full governed trees are retained, including unrecorded files. Never
        # exclude arbitrary node_modules, ignored files, or unregistered folders.
        for name in (".claude", ".codex", ".agents", ".apm", "apm_modules"):
            source = root / name
            if source.is_symlink():
                (dest / name).symlink_to(os.readlink(source), target_is_directory=True)
            elif source.is_dir():
                shutil.copytree(source, dest / name, symlinks=True, ignore=ignore)
        for name in ("apm.yml", "apm.lock.yaml", "apm.lock", ".gitignore", ".mcp.json", "AGENTS.md", "CLAUDE.md"):
            source = root / name
            if source.is_symlink():
                (dest / name).symlink_to(os.readlink(source))
            elif source.is_file():
                shutil.copy2(source, dest / name)
        # Keep tracked-file and organization-policy discovery equivalent without
        # sharing the project's actual index, config, hooks, or object store.
        subprocess.run(["git", "init", "-q", str(dest)], check=True, capture_output=True)
        index = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "-z"], check=True, capture_output=True).stdout
        subprocess.run(["git", "-C", str(dest), "update-index", "--info-only", "-z", "--index-info"], input=index, check=True, capture_output=True)
        remote = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True)
        if remote.returncode == 0:
            subprocess.run(["git", "-C", str(dest), "remote", "add", "origin", remote.stdout.strip()], check=True, capture_output=True)
        return dest
    except Exception:
        app.trash(dest)
        raise


def run(root, executable, args, output=None):
    root = root.resolve()
    import super_apm as app
    omitted = nested_worktrees(root)
    if not omitted or "--file" in args or any(a.startswith("--file=") for a in args):
        return subprocess.run([executable, *args], cwd=root, stdout=output, stderr=output).returncode
    if "--strip" in args:
        raise AuditError("Use native apm audit --strip for intentional source edits; the isolated audit is read-only")
    args = list(args)
    for index, arg in enumerate(args):
        if arg == "--policy" and index + 1 < len(args) and not args[index + 1].startswith("https://"):
            raise AuditError("Use native apm audit for file-based policy overrides")
        if arg.startswith("--policy=") and not arg.split("=", 1)[1].startswith("https://"):
            raise AuditError("Use native apm audit for file-based policy overrides")
        if arg in {"--output", "-o"} and index + 1 < len(args):
            args[index + 1] = str((root / args[index + 1]).resolve())
        elif arg.startswith("--output="):
            args[index] = "--output=" + str((root / arg.split("=", 1)[1]).resolve())
    dest = snapshot(root, omitted)
    try:
        print(f"[super-apm] audit deployment snapshot; {len(omitted)} registered nested worktrees excluded (audit each checkout separately)", flush=True)
        return subprocess.run([executable, *args], cwd=dest, stdout=output, stderr=output).returncode
    finally:
        app.trash(dest)
