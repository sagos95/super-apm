"""Delegate dependency management to APM and expose project-local skills safely."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit

import yaml

LOCAL = ".apm-local"
RECEIPT = "super-apm-links.json"
START = "# BEGIN super-apm local (managed)"
END = "# END super-apm local (managed)"
HELP = """super-apm — shared + local APM skills

  super-apm install                  Install both configured scopes
  super-apm install PACKAGE          Add a shared dependency
  super-apm install PACKAGE --local  Add a Git-ignored local dependency
  super-apm update [PACKAGE...]      Update both scopes, or route named packages
  super-apm list                     List shared and local dependencies
  super-apm audit                    Audit both installations
  super-apm sync                     Reconcile local skill links and .gitignore
  super-apm status                   Show scopes and local link health
  super-apm migrate-local            Move the legacy .git/apm-personal installation

Use --shared or --local to select one scope. Native APM options are forwarded.
Local scope supports skills for Claude and Codex; MCP/config/hook merging is not
implemented. APM keeps separate manifests and lockfiles. No Git commit or push.
"""


class Error(Exception):
    pass


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        raise Error(f"Cannot parse YAML: {path.name}") from None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Error(f"Expected a YAML mapping: {path.name}")
    return value


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise Error("Git could not inspect this project")
    return result.stdout.strip()


def project_root(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()


def tracked(root: Path, path: Path) -> bool:
    return bool(git(root, "ls-files", "--", str(path.relative_to(root))))


def trash(path: Path) -> None:
    if Path("/usr/bin/trash").is_file():
        subprocess.run(["/usr/bin/trash", str(path)], check=True)
        return
    destination = Path.home() / ".Trash"
    if not destination.is_dir():
        raise Error("macOS Trash is unavailable; nothing will be permanently deleted")
    shutil.move(str(path), str(destination / f"{path.name}-{uuid.uuid4().hex}"))


def owned_link(path: Path, target: Path) -> bool:
    return path.is_symlink() and os.readlink(path) == os.path.relpath(target, path.parent)


def update_ignore(root: Path, paths: list[str]) -> None:
    path = root / ".gitignore"
    source = path.read_text() if path.exists() else ""
    if source.count(START) != source.count(END) or source.count(START) > 1:
        raise Error("Malformed super-apm block in .gitignore")
    block = START + "\n" + "\n".join(sorted({f"/{LOCAL}/", "/.apm-local-stage-*/", *('/' + p for p in paths)})) + "\n" + END
    if START in source:
        start = source.index(START)
        finish = source.index(END, start) + len(END)
        result = source[:start] + block + source[finish:]
    else:
        result = source + ("" if not source or source.endswith("\n") else "\n") + "\n" + block + "\n"
    if result != source:
        path.write_text(result)


def shared_names(root: Path) -> set[str]:
    names = set()
    for dependency in read_yaml(root / "apm.lock.yaml").get("dependencies", []):
        if dependency.get("name"):
            names.add(dependency["name"])
        for path in dependency.get("deployed_file_hashes", {}):
            parts = Path(path).parts
            if len(parts) >= 3 and parts[:2] in ((".agents", "skills"), (".claude", "skills")):
                names.add(parts[2])
    return names


def local_sources(root: Path) -> dict[str, Path]:
    local = root / LOCAL
    manifest = read_yaml(local / "apm.yml")
    if any(manifest.get("dependencies", {}).get(kind) for kind in ("mcp", "lsp")):
        raise Error("Local scope currently supports skills; MCP/LSP configuration merging is not implemented")
    result = {}
    for client in (".agents", ".claude"):
        folder = local / client / "skills"
        for source in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not (source / "SKILL.md").is_file():
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", source.name):
                raise Error("Unsafe local skill folder name")
            if not source.resolve().is_relative_to(local.resolve()):
                raise Error("A local source escapes its installation")
            result[f"{client}/skills/{source.name}"] = source
    # Rendering these files locally would not activate them in the project.
    for client in (".agents", ".claude", ".codex"):
        folder = local / client
        for item in folder.iterdir() if folder.is_dir() else []:
            if item.name != "skills":
                raise Error(f"Unsupported local APM output: {client}/{item.name}; only skills are exposed")
    return result


def receipt(root: Path) -> dict[str, str]:
    path = root / LOCAL / RECEIPT
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise Error("Cannot read the local link receipt") from None
    if not isinstance(value, dict):
        raise Error("Invalid local link receipt")
    for name, target in value.items():
        if not isinstance(name, str) or not isinstance(target, str):
            raise Error("Invalid local link receipt")
        parts = Path(name).parts
        if len(parts) != 3 or parts[:2] not in ((".agents", "skills"), (".claude", "skills")):
            raise Error("Invalid local link path")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", parts[2]):
            raise Error("Invalid local skill name")
        if target != f"{LOCAL}/{name}":
            raise Error("Invalid local link target")
    return value


def sync(root: Path, allow_shared_conflicts: bool = False) -> int:
    local = root / LOCAL
    if not (local / "apm.yml").exists():
        return 0
    if local.is_symlink() or tracked(root, local):
        raise Error("The local installation must be an untracked real directory")
    desired = local_sources(root)
    previous = receipt(root)
    reserved = shared_names(root)
    # Check the entire plan before changing any project links.
    for name in set(desired) | set(previous):
        path = root / name
        if not path.parent.resolve().is_relative_to(root.resolve()):
            raise Error(f"Skill destination escapes the project: {name}")
        if tracked(root, path):
            raise Error(f"A tracked skill conflicts with local scope: {name}")
        if name in desired and Path(name).name in reserved and not allow_shared_conflicts:
            raise Error(f"A shared package also owns this skill: {name}; select one scope")
        if path.exists() or path.is_symlink():
            if name not in previous or not owned_link(path, root / previous[name]):
                raise Error(f"An existing file or foreign link conflicts with local scope: {name}")
    update_ignore(root, sorted(desired))
    for name, old_target in previous.items():
        path = root / name
        if name not in desired and path.is_symlink():
            trash(path)
    for name, source in desired.items():
        path = root / name
        if not path.is_symlink():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(os.path.relpath(source, path.parent), target_is_directory=True)
    state = {name: str(source.relative_to(root)) for name, source in sorted(desired.items())}
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    path = local / RECEIPT
    if not path.exists() or path.read_text() != content:
        path.write_text(content)
    print(f"[super-apm] local: {len(desired)} skill links reconciled", flush=True)
    return len(desired)


def init_local(root: Path) -> None:
    local = root / LOCAL
    if tracked(root, local) or local.is_symlink():
        raise Error("The local installation must be an untracked real directory")
    local.mkdir(exist_ok=True)
    update_ignore(root, list(receipt(root)))
    path = local / "apm.yml"
    if not path.exists():
        path.write_text(yaml.safe_dump({"name": "local", "version": "1.0.0", "targets": ["claude", "codex"], "dependencies": {"apm": []}}, sort_keys=False))


def native(root: Path, scope: str, args: list[str]) -> int:
    cwd = root if scope == "shared" else root / LOCAL
    if scope == "local":
        local_sources(root)
        if "--mcp" in args or "--legacy-skill-paths" in args:
            raise Error("Local scope exposes Claude/Codex skills only")
        for index, arg in enumerate(args):
            if arg in {"--target", "-t", "--runtime"} and index + 1 < len(args):
                if not set(args[index + 1].split(",")) <= {"claude", "codex"}:
                    raise Error("Local scope supports only Claude and Codex targets")
            if arg.startswith(("--target=", "--runtime=")):
                if not set(arg.split("=", 1)[1].split(",")) <= {"claude", "codex"}:
                    raise Error("Local scope supports only Claude and Codex targets")
        if "--dry-run" in args and not cwd.exists():
            cwd = Path(tempfile.mkdtemp(prefix="super-apm-preview-"))
            if not any(a in {"--target", "-t", "--runtime"} or a.startswith(("--target=", "--runtime=")) for a in args):
                args = [*args, "--target", "claude,codex"]
        if args[0] not in {"install", "update", "uninstall", "deps", "audit", "outdated", "view"}:
            raise Error(f"Local scope does not support '{args[0]}' in this version")
        if args[0] == "install":
            args = [*args, "--only", "apm", "--no-trust-bin"]
        if args[0] == "uninstall" and "--dry-run" not in args:
            # Preserve the actual old installation in Trash before APM removes files.
            staging = root / f".apm-local-stage-{uuid.uuid4().hex}"
            shutil.copytree(cwd, staging, symlinks=True)
            trash(cwd)
            staging.rename(cwd)
    executable = os.environ.get("SUPER_APM_EXECUTABLE") or shutil.which("apm")
    if not executable:
        raise Error("APM is not installed or not on PATH")
    print(f"[super-apm] {scope}: apm {' '.join(args)}", flush=True)
    return subprocess.run([executable, *args], cwd=cwd).returncode


def identities(root: Path) -> set[str]:
    result = set()
    manifest = read_yaml(root / "apm.yml")
    for section in ("dependencies", "devDependencies"):
        for item in manifest.get(section, {}).get("apm", []):
            if isinstance(item, str):
                result.add(item.split("#")[0].lower())
    for dep in read_yaml(root / "apm.lock.yaml").get("dependencies", []):
        result.add(str(dep.get("name", "")).lower())
        result.add("/".join(filter(None, (dep.get("repo_url"), dep.get("virtual_path")))).lower())
    return result - {""}


def package_args(args: list[str]) -> list[str]:
    values = {"--target", "-t", "--runtime", "--only", "--exclude", "--parallel-downloads", "--skill", "--audit", "--as", "--allow-insecure-host", "--registry"}
    packages = []
    skip = False
    for arg in args[1:]:
        if skip:
            skip = False
        elif arg in values:
            skip = True
        elif not arg.startswith("-"):
            packages.append(arg)
    return packages


def migrate(root: Path) -> None:
    legacy = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "apm-personal"))
    local = root / LOCAL
    if not (legacy / "apm.yml").is_file() or local.exists():
        raise Error("Migration requires .git/apm-personal and no existing .apm-local")
    links = []
    for client in (".agents", ".claude"):
        for source in (legacy / client / "skills").iterdir():
            link = root / client / "skills" / source.name
            if not link.is_symlink() or link.resolve() != source.resolve() or tracked(root, link):
                raise Error("Legacy migration found a missing, changed or tracked link")
            links.append(link)
    # The legacy directory is moved, not copied into a second installation.
    shutil.move(str(legacy), str(local))
    manifest = read_yaml(local / "apm.yml")
    manifest["name"] = str(manifest.get("name", "local")).removesuffix("-personal") + "-local"
    (local / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for link in links:
        trash(link)
    (local / RECEIPT).write_text("{}\n")
    update_ignore(root, [str(p.relative_to(root)) for p in links])
    sync(root)
    (local / "README.md").write_text(
        "# Локальные APM-пакеты проекта\n\n"
        "Установка хранится в .apm-local и исключена через .gitignore.\n"
        "Общие apm.yml и lockfile не меняются. Управление из папки проекта:\n\n"
        "```sh\nsuper-apm list\nsuper-apm install\nsuper-apm update --local --yes\n"
        "super-apm audit --local\nsuper-apm sync\n```\n\n"
        "Новые зависимости автоматически получают ссылки после успешного install/update.\n"
        "Фонового обновления нет. Обычный apm в корне управляет общим набором.\n"
    )
    exclude = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    if exclude.exists():
        source = exclude.read_text()
        marker = "# Personal APM installation; not shared with collaborators."
        if marker in source:
            owned = {"/" + str(link.relative_to(root)) for link in links}
            exclude.write_text("".join(line for line in source.splitlines(keepends=True)
                                       if line.strip() != marker and line.strip() not in owned))
    print("[super-apm] moved legacy installation to .apm-local", flush=True)


def display_ref(value: str) -> str:
    if value.startswith(("https://", "http://", "ssh://")):
        parsed = urlsplit(value)
        value = (parsed.hostname or "") + parsed.path
    return "".join(c if c.isprintable() else "?" for c in value)


def list_packages(root: Path, scopes: list[str]) -> None:
    rows = []
    for scope in scopes:
        base = root if scope == "shared" else root / LOCAL
        seen = set()
        for dep in read_yaml(base / "apm.lock.yaml").get("dependencies", []):
            ref = "/".join(filter(None, (dep.get("repo_url"), dep.get("virtual_path"))))
            seen.add(ref.lower())
            if dep.get("local_path"):
                seen.add(dep["local_path"].lower())
            pin = dep.get("resolved_commit", "local" if dep.get("source") == "local" else "—")
            rows.append([scope, display_ref(ref or dep.get("name", "unknown")), "installed", str(pin)[:12]])
        manifest = read_yaml(base / "apm.yml")
        for section in ("dependencies", "devDependencies"):
            for item in manifest.get(section, {}).get("apm", []):
                ref = item if isinstance(item, str) else item.get("path", "")
                if ref.split("#")[0].lower() not in seen:
                    rows.append([scope, display_ref(ref), "declared", "—"])
    table = [["SCOPE", "PACKAGE", "STATE", "COMMIT"], *rows]
    widths = [max(len(row[index]) for row in table) for index in range(4)]
    for row in table:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
    print(f"\n{len(rows)} packages across {', '.join(scopes)}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        print(HELP)
        return 0
    if args == ["--version"]:
        print("super-apm 0.1.0")
        return 0
    try:
        selected = [flag[2:] for flag in ("--local", "--shared") if flag in args]
        if len(selected) > 1:
            raise Error("Choose either --local or --shared")
        args = [a for a in args if a not in {"--local", "--shared"}]
        if not args:
            raise Error("A command is required")
        if "--root" in args or any(a.startswith("--root=") for a in args):
            raise Error("--root bypasses scope isolation; use native apm explicitly for custom destinations")
        if "-g" in args or "--global" in args:
            if selected:
                raise Error("Global APM cannot be combined with --local/--shared")
            executable = os.environ.get("SUPER_APM_EXECUTABLE") or shutil.which("apm")
            if not executable:
                raise Error("APM is not installed")
            return subprocess.run([executable, *args]).returncode
        root = project_root(Path.cwd())
        command = args[0]
        if command == "migrate-local":
            migrate(root)
            return 0
        if command == "sync":
            sync(root)
            return 0
        if command == "status":
            print(f"shared: {root / 'apm.yml'}")
            print(f"local: {root / LOCAL / 'apm.yml'}")
            desired = local_sources(root)
            healthy = sum(owned_link(root / name, path) and (root / name / "SKILL.md").is_file() for name, path in desired.items())
            print(f"local links: {healthy}/{len(desired)} healthy")
            return 0 if healthy == len(desired) else 1
        if command == "list":
            if len(args) != 1:
                raise Error("Use list --local/--shared, or native 'deps list' options")
            available = [scope for scope, path in (("shared", root), ("local", root / LOCAL)) if (path / "apm.yml").exists()]
            scopes = selected or available
            list_packages(root, scopes)
            return 0
        packages = package_args(args) if command in {"install", "update", "uninstall"} else []
        available = [scope for scope, path in (("shared", root), ("local", root / LOCAL)) if (path / "apm.yml").exists()]
        dry_run = "--dry-run" in args
        mutating = command in {"install", "update", "uninstall"} and not dry_run
        if command == "uninstall" and not packages:
            raise Error("Name the package to uninstall")
        if selected:
            if selected[0] == "local" and not (root / LOCAL / "apm.yml").exists():
                if command != "install":
                    raise Error("No local APM manifest; install a package with --local first")
                if not dry_run:
                    init_local(root)
            scopes = selected
        elif command == "install" and packages:
            scopes = ["shared"]
        elif command in {"update", "uninstall"} and packages:
            groups = {scope: identities(root if scope == "shared" else root / LOCAL) for scope in available}
            routed = {}
            for package in packages:
                matches = [scope for scope, names in groups.items() if package.split("#")[0].lower() in names]
                if len(matches) != 1:
                    raise Error(f"Cannot choose scope for {package}; pass --local or --shared")
                routed[package] = matches[0]
            scopes = [scope for scope in available if scope in routed.values()]
        elif command in {"install", "update", "audit", "list", "outdated"} or args[:2] in (["deps", "list"], ["deps", "tree"]):
            scopes = available
        else:
            scopes = ["shared"]
        if not scopes:
            raise Error("No APM manifest; use install PACKAGE or install PACKAGE --local")
        if mutating and (root / LOCAL / "apm.yml").exists():
            sync(root, allow_shared_conflicts=command == "uninstall")
        code = 0
        for scope in scopes:
            scoped = args
            if command in {"update", "uninstall"} and packages and not selected:
                scoped = [a for a in args if a not in packages or routed[a] == scope]
            result = native(root, scope, scoped)
            code = code or result
            if result:
                if mutating:
                    break
                continue
            if mutating and (root / LOCAL / "apm.yml").exists():
                sync(root)
        return code
    except (Error, OSError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, Error) else type(exc).__name__
        print(f"super-apm: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
