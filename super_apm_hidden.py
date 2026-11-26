"""Hidden dependencies: skills a package needs that no agent harness may discover.

A package declares them in its own apm.yml under a key APM ignores:

    super-apm:
      hidden:
        destination: references/methods
        rename: {SKILL.md: METHOD.md}
        exclude: ["*.mp4", dist]          # optional: files or folders not to copy
        dependencies:
          - owner/repo/skills/example#v1.2.0
          - path: ./vendor/local-method
          - {package: owner/repo/plugins/kit/skills/plan, as: kit/plan}

Owners are the packages in a scope's lockfile and the project's own skills in .apm/skills/.
Remote entries are resolved by `apm lock` in a private workspace: APM downloads them and
writes a lockfile but deploys nothing into harness folders. Local entries are copied from
the package itself. Files are renamed and written to the Git-ignored
.apm-hidden/<package>/<destination>/<name>/. Nothing shipped by a package is executed.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import urlsplit

import yaml

HIDDEN = ".apm-hidden"
WORK = ".super-apm"
STATE = "state.json"
KEY = "super-apm"
DEFAULT_RENAME = {"SKILL.md": "METHOD.md"}
SKIP = {".git", ".DS_Store", "__pycache__", "node_modules", ".apm-pin"}
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
SCOPES = (("shared", Path(".")), ("local", Path(".apm-local")))


class HiddenError(Exception):
    pass


def read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        raise HiddenError(f"Cannot parse YAML: {path.name}") from None
    return value if isinstance(value, dict) else {}


def segments(value, what: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise HiddenError(f"Invalid {what}")
    parts = [part for part in value.strip().split("/") if part not in ("", ".")]
    if any(not SEGMENT.fullmatch(part) for part in parts):
        raise HiddenError(f"Unsafe {what}: {value}")
    return PurePosixPath(*parts)


def identity(reference: str) -> str:
    """A host-qualified, ref-free path comparable with locked_identity()."""
    reference = reference.strip().split("#", 1)[0]
    scp = re.match(r"^[^@/\s]+@([^:/\s]+):(.+)$", reference)
    if scp:
        host, path = scp.groups()
    elif "://" in reference:
        parts = urlsplit(reference)
        host, path = parts.hostname or "", parts.path
    else:
        first, _, rest = reference.partition("/")
        host, path = (first, rest) if "." in first else ("github.com", reference)
    path = "/".join(part.removesuffix(".git") for part in path.split("/") if part)
    host = host.lower()
    return (path if host in ("", "github.com") else f"{host}/{path}").lower()


def locked_identity(dependency: dict) -> str:
    host = str(dependency.get("host") or "github.com").lower()
    path = "/".join(filter(None, (dependency.get("repo_url"), dependency.get("virtual_path"))))
    return (path if host == "github.com" else f"{host}/{path}").lower()


def package_dir(modules: Path, dependency: dict) -> Path | None:
    """Where APM 0.32 materializes a locked dependency below apm_modules."""
    if dependency.get("source") == "local":
        local = dependency.get("local_path")
        if not isinstance(local, str) or dependency.get("resolved_by"):
            return None
        path = modules / "_local" / PurePosixPath(local).name
    else:
        repo = dependency.get("repo_url")
        if not isinstance(repo, str) or not repo:
            return None
        path = modules.joinpath(*repo.split("/"))
        if dependency.get("virtual_path"):
            path = path.joinpath(*str(dependency["virtual_path"]).split("/"))
    if not path.resolve().is_relative_to(modules.resolve()):
        return None
    return path


def direct(lock: dict) -> list[dict]:
    return [d for d in lock.get("dependencies") or [] if isinstance(d, dict) and d.get("depth", 1) == 1 and not d.get("resolved_by")]


def parse_entry(owner: str, item) -> dict:
    alias = None
    if isinstance(item, dict):
        item = dict(item)
        alias = item.pop("as", None)
        if alias is not None:
            alias = segments(alias, "hidden dependency name")
            if not alias.parts:
                raise HiddenError(f"{owner}: an empty 'as' name")
        if "package" in item:
            if set(item) != {"package"}:
                raise HiddenError(f"{owner}: 'package' cannot be combined with other keys")
            item = item["package"]
    if isinstance(item, str) and item.strip():
        if item.strip().startswith((".", "/", "~")):
            raise HiddenError(f"{owner}: write local hidden dependencies as 'path: {item.strip()}'")
        return {"kind": "remote", "apm": item.strip(), "identity": identity(item), "as": alias, "label": item.strip()}
    if isinstance(item, dict) and isinstance(item.get("git"), str):
        sub = item.get("path", "")
        if not isinstance(sub, str):
            raise HiddenError(f"{owner}: invalid hidden dependency path")
        key = identity(item["git"]) + ("/" + sub.strip("/").lower() if sub.strip("/") else "")
        return {"kind": "remote", "apm": item, "identity": key, "as": alias, "label": item["git"] + (f"/{sub.strip('/')}" if sub.strip("/") else "")}
    if isinstance(item, dict) and set(item) == {"path"} and isinstance(item["path"], str):
        path = item["path"].strip()
        if not path or path.startswith(("/", "~")) or "\\" in path:
            raise HiddenError(f"{owner}: a local hidden dependency needs a relative path")
        return {"kind": "local", "path": path, "as": alias, "label": path}
    raise HiddenError(f"{owner}: unsupported hidden dependency entry")


def parse(owner: str, value) -> dict:
    if not isinstance(value, dict):
        raise HiddenError(f"{owner}: 'super-apm.hidden' must be a mapping")
    unknown = set(value) - {"destination", "rename", "exclude", "dependencies"}
    if unknown:
        raise HiddenError(f"{owner}: unknown hidden keys: {', '.join(sorted(map(str, unknown)))}")
    rename = value.get("rename", DEFAULT_RENAME)
    if not isinstance(rename, dict) or not all(isinstance(k, str) and isinstance(v, str) and SEGMENT.fullmatch(k) and SEGMENT.fullmatch(v) for k, v in rename.items()):
        raise HiddenError(f"{owner}: 'rename' maps file names to file names")
    exclude = value.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(p, str) and p.strip("/") and ".." not in p for p in exclude):
        raise HiddenError(f"{owner}: 'exclude' lists relative file or folder patterns")
    entries = value.get("dependencies")
    if not isinstance(entries, list) or not entries:
        raise HiddenError(f"{owner}: hidden 'dependencies' must be a non-empty list")
    return {"destination": segments(value.get("destination", ""), "destination"), "rename": dict(rename),
            "exclude": [p.strip("/") for p in exclude],
            "entries": [parse_entry(owner, item) for item in entries]}


def discover(root: Path) -> dict[str, dict]:
    """Installed packages of both scopes that declare hidden dependencies."""
    found = {}
    for scope, relative in SCOPES:
        base = root / relative
        for dependency in read_yaml(base / "apm.lock.yaml").get("dependencies") or []:
            if not isinstance(dependency, dict):
                continue
            folder = package_dir(base / "apm_modules", dependency)
            if folder is None or not (folder / "apm.yml").is_file():
                continue
            section = read_yaml(folder / "apm.yml").get(KEY)
            if not isinstance(section, dict) or "hidden" not in section:
                continue
            name = str(dependency.get("name") or folder.name)
            if not SEGMENT.fullmatch(name):
                raise HiddenError(f"Unsafe package name for hidden dependencies: {name}")
            if name in found:
                raise HiddenError(f"Two installed packages named {name} declare hidden dependencies; keep one")
            local = dependency.get("source") == "local"
            source = (base / dependency["local_path"]).resolve() if local else None
            found[name] = {
                "scope": scope,
                "dir": folder,
                # A local owner resolves 'path:' like APM: from its own source, siblings included.
                "base": source if source and source.is_dir() else folder,
                "remote": not local,
                "spec": section["hidden"],
                "names": {name.lower(), locked_identity(dependency), str(dependency.get("local_path", "")).lower()} - {""},
            }
        # The project's own skills in .apm/skills/ are deployed by APM without a lock entry.
        own = base / ".apm" / "skills"
        for folder in sorted(own.iterdir()) if own.is_dir() and not own.is_symlink() else []:
            if folder.is_symlink() or not (folder / "apm.yml").is_file():
                continue
            section = read_yaml(folder / "apm.yml").get(KEY)
            if not isinstance(section, dict) or "hidden" not in section:
                continue
            name = folder.name
            if not SEGMENT.fullmatch(name):
                raise HiddenError(f"Unsafe package name for hidden dependencies: {name}")
            if name in found:
                raise HiddenError(f"Two installed packages named {name} declare hidden dependencies; keep one")
            found[name] = {"scope": scope, "dir": folder, "base": folder.resolve(), "remote": False,
                           "spec": section["hidden"], "names": {name.lower()}}
    return found


def private_paths(root: Path) -> list[str]:
    return [f"{HIDDEN}/"] if (root / HIDDEN).exists() else []


def work_dir(root: Path) -> Path:
    return root / HIDDEN / WORK


def read_state(root: Path) -> dict:
    path = work_dir(root) / STATE
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise HiddenError("Cannot read the hidden dependency state") from None
    if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
        raise HiddenError("Invalid hidden dependency state")
    return value


def save_state(root: Path, state: dict) -> None:
    path = work_dir(root) / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not path.exists() or path.read_text() != text:
        temporary = path.with_name(STATE + ".tmp")
        temporary.write_text(text)
        os.replace(temporary, path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(folder: Path) -> dict[str, str]:
    result = {}
    if not folder.exists():
        return result
    for current, directories, files in os.walk(folder):
        base = Path(current)
        directories.sort()
        for name in [*directories, *files]:
            path = base / name
            if name == ".DS_Store":
                continue
            if path.is_symlink():
                result[path.relative_to(folder).as_posix()] = "symlink"
            elif path.is_file():
                result[path.relative_to(folder).as_posix()] = digest(path)
    return result


def run_apm(owner: str, workspace: Path, args: list[str]) -> None:
    executable = os.environ.get("SUPER_APM_EXECUTABLE") or shutil.which("apm")
    if not executable:
        raise HiddenError("APM is not installed or not on PATH")
    print(f"[super-apm] hidden {owner}: apm {' '.join(args)}", flush=True)
    output = subprocess.DEVNULL if os.environ.get("SUPER_APM_QUIET") == "1" else None
    if subprocess.run([executable, *args], cwd=workspace, stdin=subprocess.DEVNULL, stdout=output, stderr=output).returncode:
        raise HiddenError(f"apm {args[0]} failed for the hidden dependencies of {owner}")


def resolve_remote(owner: str, entries: list[dict], workspace: Path, update: bool) -> list[tuple[dict, Path, dict]]:
    """Lock and download remote entries; return (entry, downloaded folder, lock record)."""
    manifest = yaml.safe_dump({"name": f"{owner}-hidden", "version": "1.0.0", "dependencies": {"apm": [e["apm"] for e in entries]}},
                              sort_keys=False, allow_unicode=True)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "apm.yml"
    changed = not path.exists() or path.read_text() != manifest
    if changed:
        path.write_text(manifest)
    modules = workspace / "apm_modules"

    def pairs() -> list[tuple[dict, Path, dict]] | None:
        locked = direct(read_yaml(workspace / "apm.lock.yaml"))
        unmatched = list(locked)
        result = []
        for entry in entries:
            match = next((d for d in unmatched if locked_identity(d) == entry["identity"]), None)
            if match is None:
                result.append([entry, None])
                continue
            unmatched.remove(match)
            result.append([entry, match])
        missing = [pair for pair in result if pair[1] is None]
        # Unusual hosts may spell an identity differently; a single leftover is unambiguous.
        if len(missing) == 1 and len(unmatched) == 1:
            missing[0][1] = unmatched[0]
        output = []
        for entry, dependency in result:
            folder = package_dir(modules, dependency) if dependency else None
            if folder is None or not folder.is_dir():
                return None
            output.append((entry, folder, dependency))
        return output

    ready = None if changed or update else pairs()
    if ready is not None:
        return ready
    run_apm(owner, workspace, ["lock", "--update"] if update else ["lock"])
    resolved = pairs()
    if resolved is None:
        raise HiddenError(f"{owner}: cannot match the hidden dependencies to what APM resolved; use 'as' with a GitHub-style reference")
    return resolved


def collect(source: Path) -> tuple[dict[str, Path], int]:
    files, skipped = {}, 0
    for current, directories, names in os.walk(source):
        base = Path(current)
        kept = []
        for name in sorted(directories):
            if name in SKIP:
                continue
            if (base / name).is_symlink():
                skipped += 1
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = base / name
            if name in SKIP:
                continue
            if path.is_symlink():
                skipped += 1
                continue
            files[path.relative_to(source).as_posix()] = path
    return files, skipped


def excluded(relative: str, patterns: list[str]) -> bool:
    """A pattern names a file or folder relative to the dependency; shell wildcards allowed."""
    parts = relative.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(fnmatch.fnmatchcase(prefix, pattern) or fnmatch.fnmatchcase(parts[i - 1], pattern)
               for pattern in patterns for i, prefix in enumerate(prefixes, 1))


def plan(owner: str, data: dict, root: Path, update: bool) -> tuple[dict[str, Path], list[str], list[dict]]:
    """Desired output files (relative to .apm-hidden/<owner>), unit folders and sources."""
    spec = parse(owner, data["spec"])
    remote = [e for e in spec["entries"] if e["kind"] == "remote"]
    units = []
    for entry, folder, dependency in resolve_remote(owner, remote, work_dir(root) / owner, update) if remote else []:
        name = entry["as"] or segments(str(dependency.get("name") or folder.name), "package name")
        units.append((name, folder, {"ref": entry["label"], "commit": dependency.get("resolved_commit", "")}))
    for entry in spec["entries"]:
        if entry["kind"] != "local":
            continue
        folder = (data["base"] / entry["path"]).resolve()
        if data["remote"] and not folder.is_relative_to(data["base"].resolve()):
            raise HiddenError(f"{owner}: a remote package may only reference folders inside itself")
        if not folder.is_dir() or folder.is_relative_to((root / HIDDEN).resolve()):
            raise HiddenError(f"{owner}: hidden dependency folder not found: {entry['path']}")
        units.append((entry["as"] or segments(folder.name, "folder name"), folder, {"ref": entry["path"], "commit": "local"}))
    desired, prefixes, sources = {}, [], []
    for name, folder, source in units:
        prefix = (spec["destination"] / name).as_posix()
        for other in prefixes:
            if prefix == other or prefix.startswith(other + "/") or other.startswith(prefix + "/"):
                raise HiddenError(f"{owner}: two hidden dependencies share the folder {prefix}; give one a different 'as' name")
        prefixes.append(prefix)
        sources.append({"name": name.as_posix(), **source})
        files, skipped = collect(folder)
        if skipped:
            print(f"[super-apm] hidden {owner}: skipped {skipped} symlink(s) in {name}", flush=True)
        for relative, path in files.items():
            if excluded(relative, spec["exclude"]):
                continue
            parts = relative.split("/")
            parts[-1] = spec["rename"].get(parts[-1], parts[-1])
            if parts[-1].lower() == "skill.md":
                raise HiddenError(f"{owner}: {name}/{'/'.join(parts)} would stay visible as a skill; add a rename rule for it")
            target = f"{prefix}/{'/'.join(parts)}"
            if target in desired:
                raise HiddenError(f"{owner}: renaming makes two files collide at {target}")
            desired[target] = path
    return desired, prefixes, sources


def apply(root: Path, owner: str, desired: dict[str, Path], units: list[str], record: dict, state: dict, trash) -> bool:
    folder = root / HIDDEN / owner
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise HiddenError(f"{HIDDEN}/{owner} must be a real folder")
    target = {relative: digest(path) for relative, path in desired.items()}
    current = inventory(folder)
    known, pending = record.get("files", {}), record.get("pending", {})
    edited = [r for r, d in current.items() if d not in (known.get(r), pending.get(r))]
    if edited:
        raise HiddenError(f"{HIDDEN}/{owner}/{edited[0]} was changed by hand; move your edits out of {HIDDEN}/{owner}, then run super-apm install")
    if current == target:
        return False
    # Record the target first: an interrupted run resumes instead of reporting hand edits.
    record["pending"] = target
    save_state(root, state)
    stale = set(current) - set(target)
    for unit in sorted(set(record.get("units", [])) - set(units)):
        inside = {r for r in current if r == unit or r.startswith(unit + "/")}
        if inside and inside <= stale and (folder / unit).is_dir():
            trash(folder / unit)
            stale -= inside
    for relative in sorted(stale, reverse=True):
        if (folder / relative).is_file() or (folder / relative).is_symlink():
            trash(folder / relative)
    for relative, path in desired.items():
        if current.get(relative) == target[relative]:
            continue
        destination = folder / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.is_dir():
            raise HiddenError(f"{HIDDEN}/{owner}/{relative} is in the way")
        shutil.copy2(path, destination)
    return True


def retire(root: Path, owner: str, record: dict, trash) -> None:
    folder = root / HIDDEN / owner
    current = inventory(folder)
    edited = [r for r, d in current.items() if d not in (record.get("files", {}).get(r), record.get("pending", {}).get(r))]
    if edited:
        raise HiddenError(f"{HIDDEN}/{owner}/{edited[0]} was changed by hand; move it out, then run super-apm install")
    if folder.exists() or folder.is_symlink():
        trash(folder)
    if (work_dir(root) / owner).exists():
        trash(work_dir(root) / owner)


def selected(update, data: dict) -> bool:
    if isinstance(update, bool):
        return update
    return bool({value.split("#")[0].lower() for value in update} & data["names"])


def relevant(root: Path, scope: str) -> bool:
    if any(record.get("scope") == scope for record in read_state(root).values()):
        return True
    return any(data["scope"] == scope for data in discover(root).values())


def reconcile(root: Path, scope: str, trash, update: bool | set[str] = False) -> int:
    """Materialize hidden dependencies of one scope's packages; return the dependency count.

    update: False keeps locked versions, True refreshes every package of the scope,
    a set refreshes only the named packages.
    """
    found = discover(root)
    state = read_state(root)
    for name, data in found.items():
        if state.get(name, {}).get("scope") not in (None, data["scope"]):
            raise HiddenError(f"{name} declares hidden dependencies in both scopes; select one scope")
    failures, count = [], 0
    for name, record in sorted(state.items()):
        if record.get("scope") == scope and name not in found:
            try:
                retire(root, name, record, trash)
                del state[name]
                print(f"[super-apm] hidden {name}: removed with its package", flush=True)
            except (HiddenError, OSError) as error:
                failures.append((name, error))
    for name, data in sorted(found.items()):
        if data["scope"] != scope:
            continue
        record = state.setdefault(name, {"scope": scope})
        try:
            desired, units, sources = plan(name, data, root, selected(update, data))
            apply(root, name, desired, units, record, state, trash)
            state[name] = {"scope": scope, "files": {r: digest(p) for r, p in desired.items()}, "units": units, "sources": sources}
            count += len(units)
        except (HiddenError, OSError) as error:
            failures.append((name, error))
    save_state(root, state)
    for name, error in failures:
        detail = str(error) if isinstance(error, HiddenError) else type(error).__name__
        print(f"[super-apm] hidden {name}: stopped: {detail}", flush=True)
    if failures:
        raise HiddenError(f"Hidden dependencies of {len(failures)} package(s) were not reconciled")
    print(f"[super-apm] hidden ({scope}): {count} dependencies reconciled", flush=True)
    return count


def health(root: Path) -> list[tuple[str, int, bool]]:
    """(package, dependency count, files match the last reconcile) for each owner."""
    return [(name, len(record.get("units", [])), inventory(root / HIDDEN / name) == record.get("files") and not record.get("pending"))
            for name, record in sorted(read_state(root).items())]


def rows(root: Path, scopes: list[str]) -> list[list[str]]:
    result = []
    for name, record in sorted(read_state(root).items()):
        if record.get("scope") in scopes:
            for source in record.get("sources", []):
                result.append([f"hidden:{name}", f"{source['name']} <- {source['ref']}", "installed", str(source.get("commit") or "—")[:12]])
    return result
