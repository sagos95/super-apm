"""Portable hook installation and quiet, per-scope background refresh."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from super_apm_mcp import write_private

HOOK_MARKER = "super-apm hook"
BOOTSTRAP_MARKER = "super-apm bootstrap"
STATE = "super-apm-hook.json"


class HookError(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def operation_lock(root: Path):
    import super_apm as app
    app.update_ignore(root, list(app.receipt(root)))
    lock = root / ".super-apm-operation.lock"
    if lock.is_symlink() or app.tracked(root, lock):
        raise HookError("Unsafe operation-lock path")
    with file_lock(lock):
        yield


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise HookError(f"Cannot read hook metadata: {path.name}") from None


def is_managed(handler: dict) -> bool:
    return any(isinstance(value := handler.get(field), str) and ("apm-hooks" in value or HOOK_MARKER in value or BOOTSTRAP_MARKER in value) for field in ("command", "commandWindows"))


def bootstrap_commands(verb: str, arguments: str):
    # Resolve the project at execution time, including when the client starts in a subdirectory.
    shell = ("# super-apm bootstrap\n"
             'R="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"\n'
             'cd "$R" || exit 1\n'
             'export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"\n'
             'if command -v super-apm >/dev/null 2>&1; then exec super-apm hook ' + verb + arguments + '; fi\n'
             'for p in python3 python; do\n'
             '  if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys;sys.exit(sys.version_info < (3,8))" 2>/dev/null; then exec "$p" .super-apm/bootstrap.py ' + verb + arguments + '; fi\n'
             'done\n'
             "printf '%s\\n' '" + json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart" if verb == "session-start" else "UserPromptSubmit", "additionalContext": "Package refresh is unavailable: super-apm and Python 3.8+ are missing. Ask the user to choose install or skip; if they explicitly choose install, set up uv, super-apm and Microsoft APM using official instructions, then run super-apm install. Otherwise continue without refresh. Do not install tools without consent."}}) + "'\n")
    ps = ("# super-apm bootstrap\n"
          "$r = $env:CLAUDE_PROJECT_DIR; if (-not $r) { $r = git rev-parse --show-toplevel 2>$null }; if ($r) { Set-Location $r }\n"
          "$env:PATH = $env:PATH + [IO.Path]::PathSeparator + (Join-Path $HOME '.local/bin')\n"
          "if (Get-Command super-apm -ErrorAction SilentlyContinue) { & super-apm hook " + verb + arguments + "; exit $LASTEXITCODE }\n"
          "foreach ($p in @('python3','python','py')) { if (Get-Command $p -ErrorAction SilentlyContinue) { & $p -c 'import sys;sys.exit(sys.version_info < (3,8))' 2>$null; if ($LASTEXITCODE -eq 0) { & $p .super-apm/bootstrap.py " + verb + arguments + "; exit $LASTEXITCODE } } }\n"
          "'" + json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart" if verb == "session-start" else "UserPromptSubmit", "additionalContext": "Package refresh is unavailable: super-apm and Python 3.8+ are missing. Ask for install or skip. Only after explicit install consent, install uv, super-apm and Microsoft APM using official instructions, then run super-apm install. Otherwise continue without refresh."}}) + "'\n")
    import base64
    windows = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + base64.b64encode(ps.encode("utf-16-le")).decode()
    polyglot = "echo `# <#` >/dev/null\n" + shell + "exit 0\n#> > $null\n" + ps
    return shell, windows, polyglot


def install(root: Path, policy: str, global_updates: bool, pre_shared: str | None = None):
    import super_apm as app
    if policy not in {"daily", "pinned"}:
        raise HookError("Unknown shared update policy")
    if pre_shared and (not re.fullmatch(r"[a-zA-Z0-9_./-]+", pre_shared) or not (root / pre_shared).resolve().is_relative_to(root.resolve()) or not (root / pre_shared).is_file()):
        raise HookError("The shared pre-step must be a project-relative existing script")
    arguments = f" --shared-policy {policy}" + (" --global-updates" if global_updates else "")
    if pre_shared:
        arguments += f" --pre-shared {pre_shared}"
    changes = []
    for relative in (".claude/settings.json", ".codex/hooks.json"):
        path = root / relative
        document = read_json(path)
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise HookError("Invalid hook event map")
        for event, verb in (("SessionStart", "session-start"), ("UserPromptSubmit", "prompt-submit")):
            kept = []
            for group in hooks.get(event, []):
                handlers = [h for h in group.get("hooks", []) if not is_managed(h)]
                if handlers:
                    kept.append({**group, "hooks": handlers})
            shell, windows, polyglot = bootstrap_commands(verb, arguments if verb == "session-start" else "")
            handler = {"type": "command", "command": shell if relative.startswith(".codex/") else polyglot, "timeout": 20}
            if relative.startswith(".codex/"):
                handler["commandWindows"] = windows
            kept.append({"hooks": [handler]})
            hooks[event] = kept
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if not path.exists() or path.read_text() != content:
            changes.append((path, content))
    source = Path(__file__).with_name("super_apm_bootstrap.py").read_text()
    bootstrap = root / ".super-apm/bootstrap.py"
    if bootstrap.is_symlink() or bootstrap.parent.is_symlink():
        raise HookError("Unsafe bootstrap path")
    if bootstrap.exists() and not bootstrap.read_text().startswith('"""Dependency-free, vendorable hook gate;'):
        raise HookError("The bootstrap destination contains a foreign file")
    if not bootstrap.exists() or bootstrap.read_text() != source:
        changes.append((bootstrap, source))
    app.update_ignore(root, list(app.receipt(root)))
    backup = root / app.LOCAL / "hook-backups" / str(time.time_ns())
    for path, content in changes:
        if path.exists():
            target = backup / path.relative_to(root)
            write_private(target, path.read_text())
        write_private(path, content)
    print(f"[super-apm] updated {len(changes)} hook configs; shared={policy}, local=daily", flush=True)
    return len(changes)


def alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_policy(args: list[str], worker=False):
    parser = argparse.ArgumentParser(prog="super-apm hook")
    parser.add_argument("--shared-policy", choices=("daily", "pinned"), default="daily")
    parser.add_argument("--global-updates", action="store_true")
    parser.add_argument("--pre-shared")
    if worker:
        parser.add_argument("--project", type=Path, required=True)
    return parser.parse_args(args)


def schedule(root: Path, args: list[str]) -> int:
    import super_apm as app
    parse_policy(args)
    app.update_ignore(root, list(app.receipt(root)))
    local = root / app.LOCAL
    with file_lock(local / ".hook-launch.lock"):
        state = read_json(local / STATE)
        if state.get("status") == "failed" and state.get("notified_at") != state.get("finished_at"):
            print(json.dumps({"systemMessage": "super-apm: the previous background package refresh failed. Run super-apm status and retry super-apm install in this project."}))
            state["notified_at"] = state.get("finished_at")
            write_private(local / STATE, json.dumps(state, indent=2) + "\n")
        if alive(state.get("pid")) and time.time() - state.get("started_at", 0) < 3600:
            return 0
        if time.time() - state.get("started_at", 0) < 3:
            return 0
        logfile = local / "super-apm-hook.log"
        logfile.parent.mkdir(exist_ok=True)
        with logfile.open("ab") as output:
            os.chmod(logfile, 0o600)
            process = subprocess.Popen([sys.executable, "-m", "super_apm", "hook-worker", "--project", str(root), *args], cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True, env={**os.environ, "SUPER_APM_QUIET": "1"})
        state.update({"pid": process.pid, "started_at": time.time(), "status": "running"})
        write_private(local / STATE, json.dumps(state, indent=2) + "\n")
    return 0


def worker(args: list[str]) -> int:
    import super_apm as app
    options = parse_policy(args, worker=True)
    root = options.project.resolve()
    local = root / app.LOCAL
    with file_lock(local / ".hook-launch.lock"):
        state = read_json(local / STATE)
        state.update({"pid": os.getpid(), "status": "running"})
        write_private(local / STATE, json.dumps(state, indent=2) + "\n")
    result = {}
    try:
        with operation_lock(root):
            now = time.time()
            for scope, directory in (("shared", root), ("local", local)):
                if not (directory / "apm.yml").is_file():
                    continue
                last = state.get(f"{scope}_updated_at", 0)
                command = "install" if scope == "shared" and options.shared_policy == "pinned" else ("update" if now - last >= 86400 else "install")
                code = 0
                try:
                    if scope == "shared" and options.pre_shared:
                        script = (root / options.pre_shared).resolve()
                        if not script.is_relative_to(root) or not script.is_file():
                            raise HookError("Invalid shared pre-step")
                        code = subprocess.run([sys.executable, str(script)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
                    if not code:
                        call = [command] + (["--yes"] if command == "update" else [])
                        code = app.native(root, scope, call)
                    if not code and (local / "apm.yml").exists():
                        app.sync(root)
                except Exception as error:
                    code = 1
                    state[f"{scope}_error_type"] = type(error).__name__
                result[scope] = code
                if not code and command == "update":
                    state[f"{scope}_updated_at"] = time.time()
            if options.global_updates and (Path.home() / ".apm/apm.yml").is_file():
                global_root = Path.home() / ".apm"
                with file_lock(global_root / ".super-apm-update.lock"):
                    stamp = global_root / ".last-update"
                    if not stamp.exists() or time.time() - stamp.stat().st_mtime >= 86400:
                        executable = os.environ.get("SUPER_APM_EXECUTABLE") or shutil.which("apm")
                        code = subprocess.run([executable, "update", "-g", "--yes"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode if executable else 1
                        result["global"] = code
                        if not code:
                            stamp.touch()
    except Exception as error:
        result["worker"] = 1
        # Exception text may contain runtime configuration. Record its type only.
        state["error_type"] = type(error).__name__
    state.update({"pid": None, "finished_at": time.time(), "status": "ok" if not any(result.values()) else "failed", "results": result})
    write_private(local / STATE, json.dumps(state, indent=2) + "\n")
    print(f"[super-apm] hook finished: {state['status']}", flush=True)
    return 1 if any(result.values()) else 0


def prompt(root: Path) -> int:
    import super_apm_bootstrap as bootstrap
    return bootstrap.prompt(root)


def entry(root: Path, args: list[str]) -> int:
    if not args:
        raise HookError("A hook event is required")
    if args[0] == "session-start":
        if not shutil.which("apm"):
            import super_apm_bootstrap as bootstrap
            bootstrap.emit_context("SessionStart", bootstrap.MISSING)
            return 0
        return schedule(root, args[1:])
    if args[0] == "prompt-submit":
        return prompt(root)
    raise HookError("Unknown hook event")
