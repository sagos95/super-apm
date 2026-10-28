"""Run the vendored gate without the installed package or third-party imports."""
import ast
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import super_apm_bootstrap as bootstrap
import super_apm_hooks as hooks


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="super-apm-bootstrap-"))
        (self.root / "apm.yml").write_text("name: example\n")

    def run_gate(self, prompt, session="test", wrapper=False):
        output = io.StringIO()
        with patch.object(bootstrap.shutil, "which", return_value=None), patch("sys.stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": session}))), contextlib.redirect_stdout(output):
            bootstrap.prompt(self.root, wrapper=wrapper)
        return json.loads(output.getvalue()) if output.getvalue() else None

    def test_gate_without_wrapper_even_when_native_apm_exists(self):
        with patch.object(bootstrap.shutil, "which", return_value="/example/apm"):
            self.assertEqual(self.run_gate("do work")["decision"], "block")

    def test_install_is_exact_and_includes_pinned_public_wrapper(self):
        self.assertEqual(self.run_gate("представь пример")["decision"], "block")
        context = self.run_gate("ставь")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Microsoft APM", context)
        self.assertIn("super-apm.git@v0.3.0", context)

    def test_skip_is_scoped_and_safe(self):
        self.run_gate("skip", "../../unsafe")
        self.assertIsNone(self.run_gate("work", "../../unsafe"))
        self.assertEqual(self.run_gate("work", "new")["decision"], "block")
        self.assertEqual(len(list((self.root / ".apm-local").iterdir())), 1)

    def test_missing_session_id_does_not_create_shared_skip(self):
        self.run_gate("skip", None)
        self.assertEqual(self.run_gate("work", None)["decision"], "block")

    def test_vendored_source_uses_python38_syntax_and_stdlib_only(self):
        source = Path(bootstrap.__file__).read_text()
        ast.parse(source, feature_version=(3, 8))
        script = self.root / "bootstrap.py"; script.write_text(source)
        result = subprocess.run([sys.executable, "-I", str(script), "prompt-submit"], cwd=self.root, input='{"prompt":"install","session_id":"isolated"}', text=True, capture_output=True, env={**os.environ, "PATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("super-apm.git@v0.3.0", result.stdout)

    def test_installed_wrapper_receives_flags_and_stdin(self):
        with patch.object(bootstrap.shutil, "which", return_value="wrapper"), patch.object(bootstrap.subprocess, "run") as call:
            call.return_value.returncode = 7
            self.assertEqual(bootstrap.main(["session-start", "--shared-policy", "pinned"]), 7)
            call.assert_called_once_with(["wrapper", "hook", "session-start", "--shared-policy", "pinned"])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_shell_and_windows_delegate_with_policy_from_subdirectory(self):
        fake = self.root / "bin"; fake.mkdir()
        log = self.root / "called"
        cli = fake / "super-apm"
        cli.write_text('#!/bin/sh\nprintf "%s" "$*" > "' + str(log) + '"\n'); cli.chmod(0o700)
        nested = self.root / "nested"; nested.mkdir()
        shell, windows, polyglot = hooks.bootstrap_commands("session-start", " --shared-policy pinned")
        env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(self.root)}
        for command in (shell, polyglot):
            result = subprocess.run(["/bin/sh", "-c", command], cwd=nested, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(), "hook session-start --shared-policy pinned")
        if shutil.which("pwsh"):
            ps = base64.b64decode(windows.split()[-1]).decode("utf-16-le")
            for code in (ps, polyglot):
                result = subprocess.run(["pwsh", "-NoProfile", "-Command", code], cwd=nested, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(log.read_text(), "hook session-start --shared-policy pinned")
