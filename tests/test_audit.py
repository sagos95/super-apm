"""Verify audit isolation never excludes arbitrary unrecorded deployment files."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import super_apm_audit as audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="super-apm-audit-test-")).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        (self.root / "apm.yml").write_text('name: example\ntargets: [claude, codex]\ndependencies: {apm: []}\n')
        (self.root / "apm.lock.yaml").write_text('dependencies: []\n')
        (self.root / ".gitignore").write_text('.claude/worktrees/\n')
        self.git("add", "apm.yml", ".gitignore")
        self.git("commit", "-qm", "Fixture")
        self.nested = self.root / ".claude/worktrees/registered"
        self.git("worktree", "add", "-q", "--detach", str(self.nested))
        (self.nested / "node_modules").mkdir()
        (self.nested / "node_modules/dependency.js").write_text("worktree dependency")
        self.unrecorded = self.root / ".claude/commands/node_modules/unrecorded.md"
        self.unrecorded.parent.mkdir(parents=True)
        self.unrecorded.write_text("unexpected hidden \u202e content")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def test_snapshot_excludes_only_registered_checkouts(self):
        unregistered = self.root / ".claude/worktrees/unregistered/file.md"
        unregistered.parent.mkdir(parents=True); unregistered.write_text("still audited")
        omitted = audit.nested_worktrees(self.root)
        self.assertEqual(omitted, [self.nested])
        dest = audit.snapshot(self.root, omitted)
        self.assertFalse((dest / self.nested.relative_to(self.root)).exists())
        self.assertEqual((dest / self.unrecorded.relative_to(self.root)).read_bytes(), self.unrecorded.read_bytes())
        self.assertEqual((dest / unregistered.relative_to(self.root)).read_text(), "still audited")
        tracked = subprocess.run(["git", "-C", str(dest), "ls-files", "-z"], capture_output=True, check=True).stdout
        self.assertEqual(tracked, self.git("ls-files", "-z"))
        remote = subprocess.run(["git", "-C", str(dest), "remote", "get-url", "origin"], capture_output=True, check=True).stdout
        self.assertEqual(remote, self.git("remote", "get-url", "origin"))

    def test_deployment_overlap_fails_closed(self):
        (self.root / "apm.lock.yaml").write_text('dependencies: []\ndeployments:\n- value: .claude/worktrees/registered/claimed.md\n')
        with self.assertRaises(audit.AuditError): audit.snapshot(self.root, [self.nested])

    def test_local_path_and_unknown_targets_fail_closed(self):
        (self.root / "apm.lock.yaml").write_text('dependencies: [{local_path: ../example}]\n')
        with self.assertRaises(audit.AuditError): audit.snapshot(self.root, [self.nested])
        (self.root / "apm.lock.yaml").write_text('dependencies: []\n')
        (self.root / "apm.yml").write_text('targets: [other]\n')
        with self.assertRaises(audit.AuditError): audit.snapshot(self.root, [self.nested])

    def test_audit_invokes_native_on_snapshot_and_preserves_failure(self):
        calls = []
        def native(command, cwd, stdout, stderr):
            calls.append(Path(cwd))
            self.assertFalse((Path(cwd) / self.nested.relative_to(self.root)).exists())
            self.assertTrue((Path(cwd) / self.unrecorded.relative_to(self.root)).exists())
            class Result: returncode = 9
            return Result()
        original = subprocess.run
        def dispatch(command, **kwargs):
            return native(command, **kwargs) if command[0] == "fake-apm" else original(command, **kwargs)
        with patch.object(subprocess, "run", side_effect=dispatch), patch("super_apm.trash", side_effect=lambda p: shutil.move(str(p), str(p) + "-preserved")):
            self.assertEqual(audit.run(self.root, "fake-apm", ["audit", "--ci"]), 9)
        self.assertNotEqual(calls[0], self.root)
        self.assertFalse(calls[0].exists())
        self.assertTrue(self.unrecorded.exists())

    def test_strip_never_edits_a_snapshot(self):
        with self.assertRaises(audit.AuditError): audit.run(self.root, "fake-apm", ["audit", "--strip"])
