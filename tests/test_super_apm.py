"""Behavioral checks; fake APM never touches the developer's real installation."""
import contextlib
import importlib.util
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

SOURCE = Path(__file__).resolve().parents[1] / "super_apm.py"
spec = importlib.util.spec_from_file_location("super_apm", SOURCE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class SuperApmTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="super-apm-test-")).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.calls = self.base / "calls.jsonl"
        self.fake = self.base / "apm"
        self.fake.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
import sys
with open(os.environ["FAKE_APM_LOG"], "a") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}) + "\\n")
raise SystemExit(int(os.environ.get("FAKE_APM_EXIT", "0")))
''')
        self.fake.chmod(0o755)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.env = patch.dict(os.environ, {"SUPER_APM_EXECUTABLE": str(self.fake), "FAKE_APM_LOG": str(self.calls)})
        self.env.start()
        self.sink = io.StringIO()
        self.out = contextlib.redirect_stdout(self.sink)
        self.err = contextlib.redirect_stderr(self.sink)
        self.out.__enter__(); self.err.__enter__()

    def tearDown(self):
        self.err.__exit__(None, None, None); self.out.__exit__(None, None, None)
        self.env.stop()
        os.chdir(self.old_cwd)
        # Keep tiny fixtures for inspection; never permanently delete user files.

    def manifest(self, local=False, packages=()):
        folder = self.root / app.LOCAL if local else self.root
        folder.mkdir(exist_ok=True)
        (folder / "apm.yml").write_text(app.yaml.safe_dump({"name": "test", "dependencies": {"apm": list(packages)}}))
        return folder

    def skill(self, name="example-skill", local=True):
        base = self.manifest(local) if not (self.root / app.LOCAL / "apm.yml").exists() else self.root / app.LOCAL
        if not local:
            base = self.root
        p = base / ".agents/skills" / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "SKILL.md").write_text("---\nname: " + name + "\n---\nOriginal\n")
        return p

    def native_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_install_without_packages_runs_both_scopes(self):
        self.manifest(); self.manifest(True)
        self.assertEqual(app.main(["install"]), 0)
        calls = self.native_calls()
        self.assertEqual([Path(c["cwd"]) for c in calls], [self.root, self.root / app.LOCAL])
        self.assertEqual(calls[1]["args"][-3:], ["--only", "apm", "--no-trust-bin"])

    def test_local_add_does_not_write_shared_manifest(self):
        self.manifest(packages=("team/shared",))
        before = (self.root / "apm.yml").read_bytes()
        self.assertEqual(app.main(["install", "owner/local", "--local"]), 0)
        self.assertEqual((self.root / "apm.yml").read_bytes(), before)
        self.assertEqual(len(self.native_calls()), 1)
        self.assertEqual(Path(self.native_calls()[0]["cwd"]), self.root / app.LOCAL)

    def test_plain_add_routes_to_shared(self):
        self.manifest(); self.manifest(True)
        self.assertEqual(app.main(["install", "team/new"]), 0)
        self.assertEqual([Path(c["cwd"]) for c in self.native_calls()], [self.root])

    def test_update_routes_package_to_local(self):
        self.manifest(packages=("team/shared",)); self.manifest(True, ("owner/local",))
        self.assertEqual(app.main(["update", "owner/local", "--yes"]), 0)
        self.assertEqual(Path(self.native_calls()[0]["cwd"]), self.root / app.LOCAL)

    def test_update_can_split_named_packages(self):
        self.manifest(packages=("team/shared",)); self.manifest(True, ("owner/local",))
        self.assertEqual(app.main(["update", "team/shared", "owner/local"]), 0)
        self.assertEqual([c["args"] for c in self.native_calls()], [["update", "team/shared"], ["update", "owner/local"]])

    def test_ambiguous_scope_fails_without_native_calls(self):
        self.manifest(packages=("owner/same",)); self.manifest(True, ("owner/same",))
        self.assertEqual(app.main(["update", "owner/same"]), 1)
        self.assertEqual(self.native_calls(), [])

    def test_sync_is_idempotent_and_git_ignores_install_and_links(self):
        source = self.skill()
        app.sync(self.root)
        link = self.root / ".agents/skills/example-skill"
        self.assertEqual(link.resolve(), source.resolve())
        first = (self.root / ".gitignore").read_bytes()
        app.sync(self.root)
        self.assertEqual((self.root / ".gitignore").read_bytes(), first)
        for ignored in (".apm-local/apm.yml", str(link)):
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", ignored], cwd=self.root).returncode, 0)
        (source / "SKILL.md").write_text("Updated")
        self.assertEqual((link / "SKILL.md").read_text(), "Updated")

    def test_conflict_preflight_keeps_other_links_uncreated(self):
        self.skill("first"); self.skill("second")
        foreign = self.root / ".agents/skills/second"
        foreign.mkdir(parents=True)
        with self.assertRaises(app.Error): app.sync(self.root)
        self.assertFalse((self.root / ".agents/skills/first").exists())
        self.assertFalse((self.root / ".gitignore").exists())

    def test_tracked_skill_cannot_be_shadowed(self):
        self.skill()
        target = self.root / ".agents/skills/example-skill"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("Shared")
        subprocess.run(["git", "add", ".agents"], cwd=self.root, check=True)
        with self.assertRaises(app.Error): app.sync(self.root)
        self.assertEqual((target / "SKILL.md").read_text(), "Shared")

    def test_shared_lock_reservation_cannot_be_shadowed(self):
        self.skill()
        (self.root / "apm.lock.yaml").write_text("dependencies:\n- name: example-skill\n")
        with self.assertRaises(app.Error): app.sync(self.root)

    def test_stale_owned_link_goes_to_trash(self):
        source = self.skill(); app.sync(self.root)
        saved = self.base / "preserved-source"
        source.rename(saved)
        trashed = []
        def fake_trash(path):
            destination = self.base / "trash-link"
            path.rename(destination); trashed.append(destination)
        with patch.object(app, "trash", fake_trash): app.sync(self.root)
        self.assertEqual(len(trashed), 1)
        self.assertTrue(trashed[0].is_symlink())
        self.assertFalse((self.root / ".agents/skills/example-skill").is_symlink())

    def test_forged_receipt_cannot_touch_external_files(self):
        self.manifest(True)
        (self.root / app.LOCAL / app.RECEIPT).write_text(json.dumps({"../outside": "elsewhere"}))
        with self.assertRaises(app.Error): app.sync(self.root)

    def test_list_is_read_only(self):
        self.manifest(); self.skill()
        self.assertEqual(app.main(["list"]), 0)
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertFalse((self.root / app.LOCAL / app.RECEIPT).exists())
        self.assertEqual(self.native_calls(), [])

    def test_unified_list_shows_scope_and_declared_vs_installed(self):
        self.manifest(packages=("team/shared",)); self.manifest(True, ("owner/local",))
        (self.root / app.LOCAL / "apm.lock.yaml").write_text(
            "dependencies:\n- repo_url: owner/local\n  name: local\n  resolved_commit: abc1234567891234567891234567891234567890\n")
        self.assertEqual(app.main(["list"]), 0)
        output = self.sink.getvalue()
        self.assertIn("team/shared", output)
        self.assertIn("owner/local", output)
        self.assertIn("declared", output)
        self.assertIn("installed", output)
        self.assertEqual(self.native_calls(), [])

    def test_new_local_dry_run_does_not_create_installation(self):
        self.manifest()
        self.assertEqual(app.main(["install", "owner/new", "--local", "--dry-run"]), 0)
        self.assertFalse((self.root / app.LOCAL).exists())
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertIn("--dry-run", self.native_calls()[0]["args"])

    def test_existing_local_dry_run_does_not_sync(self):
        self.manifest(); self.skill()
        self.assertEqual(app.main(["install", "--dry-run"]), 0)
        self.assertFalse((self.root / ".gitignore").exists())

    def test_local_read_command_does_not_initialize_empty_scope(self):
        self.manifest()
        self.assertEqual(app.main(["audit", "--local"]), 1)
        self.assertFalse((self.root / app.LOCAL).exists())

    def test_native_failure_propagates_and_stops_second_scope(self):
        self.manifest(); self.manifest(True)
        with patch.dict(os.environ, {"FAKE_APM_EXIT": "7"}):
            self.assertEqual(app.main(["update"]), 7)
        self.assertEqual(len(self.native_calls()), 1)

    def test_audit_checks_second_scope_after_first_failure(self):
        self.manifest(); self.manifest(True)
        with patch.dict(os.environ, {"FAKE_APM_EXIT": "7"}):
            self.assertEqual(app.main(["audit"]), 7)
        self.assertEqual(len(self.native_calls()), 2)

    def test_custom_root_cannot_overwrite_shared_lock(self):
        self.manifest()
        self.assertEqual(app.main(["install", "--local", "--root", "."]), 1)
        self.assertEqual(self.native_calls(), [])

    def test_uninstall_keeps_original_installation_in_trash(self):
        source = self.skill(); app.sync(self.root)
        (self.root / app.LOCAL / "apm_modules").mkdir()
        (self.root / app.LOCAL / "apm_modules/package-data").write_text("Keep original")
        backup = self.base / "trashed-installation"
        with patch.object(app, "trash", lambda path: path.rename(backup)):
            self.assertEqual(app.main(["uninstall", "example-skill", "--local"]), 0)
        self.assertEqual((backup / "apm_modules/package-data").read_text(), "Keep original")
        self.assertTrue((self.root / app.LOCAL / "apm_modules/package-data").exists())
        self.assertEqual(self.native_calls()[0]["args"], ["uninstall", "example-skill"])

    def test_local_duplicate_can_be_removed_after_shared_claim(self):
        source = self.skill(); app.sync(self.root)
        (self.root / "apm.lock.yaml").write_text("dependencies:\n- name: example-skill\n")
        def uninstall(root, scope, args):
            self.assertEqual(scope, "local")
            source.rename(self.base / "removed-source")
            return 0
        with patch.object(app, "native", uninstall), patch.object(app, "trash", lambda path: path.rename(self.base / "retired-link")):
            self.assertEqual(app.main(["uninstall", "example-skill", "--local"]), 0)
        self.assertFalse((self.root / ".agents/skills/example-skill").is_symlink())

    def test_uninstall_dry_run_does_not_move_original(self):
        self.skill()
        with patch.object(app, "trash") as operation:
            self.assertEqual(app.main(["uninstall", "example-skill", "--local", "--dry-run"]), 0)
            operation.assert_not_called()
        self.assertFalse((self.root / app.LOCAL / app.RECEIPT).exists())

    def test_link_parent_cannot_escape_project(self):
        self.skill()
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / ".agents").symlink_to(outside)
        with self.assertRaises(app.Error): app.sync(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_receipt_name_cannot_inject_ignore_rules(self):
        self.manifest(True)
        name = ".agents/skills/bad\n!secrets"
        (self.root / app.LOCAL / app.RECEIPT).write_text(json.dumps({name: f"{app.LOCAL}/{name}"}))
        with self.assertRaises(app.Error): app.sync(self.root)
        self.assertFalse((self.root / ".gitignore").exists())

    def test_migration_moves_legacy_data_and_relinks(self):
        legacy = self.root / ".git/apm-personal"
        source = legacy / ".agents/skills/example-skill"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("Legacy bytes")
        (legacy / "apm.yml").write_text("name: test-personal\n")
        link = self.root / ".agents/skills/example-skill"
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
        (legacy / ".claude/skills").mkdir(parents=True)
        with patch.object(app, "trash", lambda path: path.rename(self.base / "retired-link")):
            app.migrate(self.root)
        self.assertFalse(legacy.exists())
        self.assertEqual((link / "SKILL.md").read_text(), "Legacy bytes")
        self.assertEqual(link.resolve(), (self.root / app.LOCAL / ".agents/skills/example-skill").resolve())


if __name__ == "__main__":
    unittest.main()
