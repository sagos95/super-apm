"""Hook policy, background scheduling and unrelated-settings preservation."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import super_apm as app
import super_apm_hooks as hooks


class HookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="super-apm-hook-test-")).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.local = self.root / ".apm-local"; self.local.mkdir()
        (self.root / "apm.yml").write_text('name: example\ndependencies: {apm: []}\n')
        (self.local / "apm.yml").write_text('name: local\ndependencies: {apm: []}\n')
        self.quiet = contextlib.redirect_stdout(io.StringIO()); self.quiet.__enter__()

    def tearDown(self): self.quiet.__exit__(None, None, None)

    def test_install_preserves_foreign_handlers_and_settings(self):
        file = self.root / ".claude/settings.json"; file.parent.mkdir()
        foreign = {"type": "command", "command": "example-guard", "timeout": 7}
        file.write_text(json.dumps({"permissions": {"allow": ["example"]}, "hooks": {"SessionStart": [{"hooks": [foreign, {"command": "# apm-hooks v3\napm install"}]}], "Stop": [{"hooks": [foreign]}]}}))
        hooks.install(self.root, "pinned", False)
        result = json.loads(file.read_text())
        self.assertEqual(result["permissions"], {"allow": ["example"]})
        self.assertEqual(result["hooks"]["Stop"], [{"hooks": [foreign]}])
        self.assertEqual(result["hooks"]["SessionStart"][0]["hooks"], [foreign])
        self.assertIn("--shared-policy pinned", result["hooks"]["SessionStart"][1]["hooks"][0]["command"])
        codex = json.loads((self.root / ".codex/hooks.json").read_text())
        handler = codex["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(handler["command"], handler["commandWindows"])

    def test_reinstall_is_idempotent(self):
        hooks.install(self.root, "daily", True)
        before = (self.root / ".claude/settings.json").read_bytes()
        self.assertEqual(hooks.install(self.root, "daily", True), 0)
        self.assertEqual((self.root / ".claude/settings.json").read_bytes(), before)

    def test_project_pre_step_cannot_escape_root(self):
        with self.assertRaises(hooks.HookError): hooks.install(self.root, "pinned", False, "../../outside.py")
        self.assertFalse((self.root / ".claude/settings.json").exists())

    def test_pinned_shared_and_first_local_update(self):
        calls = []
        with patch.object(app, "native", lambda root, scope, args: calls.append((scope, args)) or 0), patch.object(app, "sync"):
            self.assertEqual(hooks.worker(["--project", str(self.root), "--shared-policy", "pinned"]), 0)
        self.assertEqual(calls, [("shared", ["install"]), ("local", ["update", "--yes"])])
        state = json.loads((self.local / hooks.STATE).read_text())
        self.assertEqual(state["status"], "ok")
        self.assertIn("local_updated_at", state)
        self.assertNotIn("shared_updated_at", state)

    def test_fresh_daily_scopes_install_without_ref_refresh(self):
        import time
        (self.local / hooks.STATE).write_text(json.dumps({"shared_updated_at": time.time(), "local_updated_at": time.time()}))
        calls = []
        with patch.object(app, "native", lambda root, scope, args: calls.append((scope, args)) or 0), patch.object(app, "sync"):
            hooks.worker(["--project", str(self.root)])
        self.assertEqual(calls, [("shared", ["install"]), ("local", ["install"])])

    def test_shared_failure_does_not_prevent_independent_local_update(self):
        calls = []
        def native(root, scope, args): calls.append(scope); return 9 if scope == "shared" else 0
        with patch.object(app, "native", native), patch.object(app, "sync"):
            self.assertEqual(hooks.worker(["--project", str(self.root)]), 1)
        self.assertEqual(calls, ["shared", "local"])
        state = json.loads((self.local / hooks.STATE).read_text())
        self.assertEqual(state["results"], {"shared": 9, "local": 0})
        self.assertNotIn("shared_updated_at", state)
        self.assertIn("local_updated_at", state)

    def test_shared_exception_does_not_prevent_local_update(self):
        calls = []
        def native(root, scope, args):
            calls.append(scope)
            if scope == "shared": raise app.Error("opaque-private-error")
            return 0
        with patch.object(app, "native", native), patch.object(app, "sync"):
            self.assertEqual(hooks.worker(["--project", str(self.root)]), 1)
        self.assertEqual(calls, ["shared", "local"])
        state = (self.local / hooks.STATE).read_text()
        self.assertNotIn("opaque-private-error", state)
        self.assertIn("local_updated_at", state)

    def test_background_failure_is_reported_once(self):
        (self.local / hooks.STATE).write_text(json.dumps({"status": "failed", "finished_at": 1, "started_at": time.time()}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hooks.schedule(self.root, [])
            hooks.schedule(self.root, [])
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("previous background package refresh failed", json.loads(output.getvalue())["systemMessage"])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_real_detached_worker_and_pre_step(self):
        fake = self.root / "fake-apm"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PWD/native-calls"\n')
        fake.chmod(0o700)
        (self.root / "prepare.py").write_text('from pathlib import Path\nPath("prepared").write_text("ok")\n')
        env = {"SUPER_APM_EXECUTABLE": str(fake), "PYTHONPATH": str(Path(app.__file__).parent)}
        with patch.dict(os.environ, env):
            started = time.monotonic()
            hooks.schedule(self.root, ["--shared-policy", "pinned", "--pre-shared", "prepare.py"])
            self.assertLess(time.monotonic() - started, 3)
            for _ in range(100):
                state = json.loads((self.local / hooks.STATE).read_text())
                if state["status"] != "running": break
                time.sleep(.05)
        self.assertEqual(state["status"], "ok", state)
        self.assertEqual((self.root / "prepared").read_text(), "ok")
        self.assertEqual((self.root / "native-calls").read_text().strip(), "install")
        self.assertEqual((self.local / "native-calls").read_text().strip(), "update --yes")

    def test_duplicate_session_does_not_launch_second_worker(self):
        import time
        (self.local / hooks.STATE).write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
        with patch.object(subprocess, "Popen") as spawn:
            self.assertEqual(hooks.schedule(self.root, []), 0)
            spawn.assert_not_called()

    def test_prompt_requires_exact_install_choice(self):
        output = io.StringIO()
        with patch.object(hooks.shutil, "which", return_value=None), patch("sys.stdin", io.StringIO('{"prompt":"представь пример","session_id":"example"}')), contextlib.redirect_stdout(output):
            hooks.prompt(self.root)
        self.assertEqual(json.loads(output.getvalue())["decision"], "block")

    def test_prompt_skip_is_scoped_to_session_and_ignores_path_input(self):
        payload = '{"prompt":"пропусти","session_id":"../../example"}'
        with patch.object(hooks.shutil, "which", return_value=None), patch("sys.stdin", io.StringIO(payload)):
            hooks.prompt(self.root)
        self.assertEqual(len(list(self.local.glob('.skip-*'))), 1)
        self.assertFalse((self.root.parent / "example").exists())

    def test_project_lock_does_not_unignore_mcp_files(self):
        (self.local / ".mcp.json").write_text('{"mcpServers":{}}')
        with hooks.operation_lock(self.root):
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", ".mcp.json"], cwd=self.root).returncode, 0)

    @unittest.skipIf(os.name == "nt", "POSIX lock timing fixture")
    def test_interactive_sync_waits_for_existing_worker_lock(self):
        import sys
        env = {**os.environ, "PYTHONPATH": str(Path(app.__file__).parent)}
        with hooks.operation_lock(self.root):
            process = subprocess.Popen([sys.executable, "-m", "super_apm", "sync"], cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(.3)
            self.assertIsNone(process.poll(), "sync raced an active refresh")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__": unittest.main()
