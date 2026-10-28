"""Dependency-free, vendorable hook gate; compatible with Python 3.8+."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

RELEASE = "v0.3.0"
INSTALL = ("The user approved installing super-apm and Microsoft APM. "
           "Install any missing Microsoft APM using its official platform instructions. "
           "If uv is missing, install it using https://docs.astral.sh/uv/getting-started/installation/. "
           "Run: uv tool install --python 3.11 git+https://github.com/sagos95/super-apm.git@" + RELEASE + ". "
           "Make the uv tool bin directory available on PATH, run super-apm install in this project, "
           "then ask to restart the agent session. Do not add local dependencies to the shared manifest.")
MISSING = "super-apm or Microsoft APM is missing. Reply 'ставь' (install) to set up the missing tools, or 'пропусти' (skip) to continue without package refresh for this session."


def emit_context(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, ensure_ascii=False))


def prompt(root, wrapper=True):
    if wrapper and shutil.which("apm"):
        return 0
    try:
        payload = json.loads(sys.stdin.read(65536) or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session = payload.get("session_id")
    # Missing session IDs must not share a permanent skip across unrelated sessions.
    marker = root / ".apm-local" / (".skip-" + hashlib.sha256(str(session).encode()).hexdigest()[:20]) if session else None
    if marker and marker.is_file():
        return 0
    value = str(payload.get("prompt", "")).strip().strip('.!«»"').strip().casefold()
    if value in {"пропусти", "skip"}:
        if marker:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("skip\n")
            os.chmod(marker, 0o600)
        emit_context("UserPromptSubmit", "The user chose to skip package refresh for this session. Continue their work without repeated setup reminders.")
    elif value in {"ставь", "install", "install apm", "install super-apm"}:
        emit_context("UserPromptSubmit", INSTALL)
    else:
        print(json.dumps({"decision": "block", "reason": MISSING}, ensure_ascii=False))
    return 0


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    if not args or args[0] not in {"session-start", "prompt-submit"}:
        return 2
    wrapper = shutil.which("super-apm")
    if wrapper:
        return subprocess.run([wrapper, "hook", *args]).returncode
    root = Path.cwd()
    if not (root / "apm.yml").is_file():
        return 0
    if args[0] == "session-start":
        emit_context("SessionStart", MISSING + " Do not install anything without the user's explicit install choice.")
        return 0
    return prompt(root, wrapper=False)


if __name__ == "__main__":
    sys.exit(main())
