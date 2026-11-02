"""Owner-only snapshots use synthetic Git repositories and never contact real services."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import super_apm
import super_apm_upstream as upstream

SNAPSHOT = ".apm/skills/example"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="super-apm-upstream-test-")).resolve()
        self.root, self.source = base / "project", base / "source"
        for repo in (self.root, self.source):
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test")
        (self.root / ".apm-local").mkdir()
        self.retired = []
        self.commit_source({"SKILL.md": "v1\n", "references/notes.md": "notes\n"})

    def retire(self, path):
        target = self.root.parent / f"retired-{len(self.retired)}"
        path.rename(target)
        self.retired.append(target)

    def commit_source(self, files, removed=()):
        folder = self.source / "skills/example"
        for relative, text in files.items():
            (folder / relative).parent.mkdir(parents=True, exist_ok=True)
            (folder / relative).write_text(text)
        for relative in removed:
            git(self.source, "rm", "-q", f"skills/example/{relative}")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "update")

    def snapshot(self, relative="SKILL.md"):
        return (self.root / SNAPSHOT / relative).read_text()

    def sync(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return upstream.sync(self.root, self.retire)

    def add(self, source=None):
        upstream.add(self.root, SNAPSHOT, str(source or self.source), "skills/example")

    def test_first_sync_copies_the_committed_folder(self):
        self.add()
        self.assertEqual(self.sync(), [SNAPSHOT])
        self.assertEqual(self.snapshot(), "v1\n")
        self.assertEqual(self.snapshot("references/notes.md"), "notes\n")

    def test_uncommitted_source_changes_stay_out(self):
        self.add()
        self.sync()
        (self.source / "skills/example/SKILL.md").write_text("work in progress\n")
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.snapshot(), "v1\n")

    def test_new_commit_updates_and_retires_removed_files(self):
        self.add()
        self.sync()
        self.commit_source({"SKILL.md": "v2\n"}, removed=["references/notes.md"])
        self.assertEqual(self.sync(), [SNAPSHOT])
        self.assertEqual(self.snapshot(), "v2\n")
        self.assertFalse((self.root / SNAPSHOT / "references/notes.md").exists())
        self.assertEqual(len(self.retired), 1)

    def test_local_edits_are_preserved(self):
        self.add()
        self.sync()
        (self.root / SNAPSHOT / "SKILL.md").write_text("edited in the project\n")
        self.commit_source({"SKILL.md": "v2\n"})
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.snapshot(), "edited in the project\n")

    def test_existing_snapshot_is_adopted_and_replaced(self):
        (self.root / SNAPSHOT).mkdir(parents=True)
        (self.root / SNAPSHOT / "SKILL.md").write_text("old copy\n")
        self.add()
        self.assertEqual(self.sync(), [SNAPSHOT])
        self.assertEqual(self.snapshot(), "v1\n")

    def test_url_source_goes_through_a_private_mirror(self):
        self.add(source=self.source.as_uri())
        self.sync()
        self.assertEqual(self.snapshot(), "v1\n")
        self.commit_source({"SKILL.md": "v2\n"})
        self.sync()
        self.assertEqual(self.snapshot(), "v2\n")
        self.assertTrue(any((self.root / ".apm-local" / upstream.CACHE).iterdir()))

    def test_source_is_kept_out_of_tracked_files(self):
        self.add()
        self.sync()
        config = (self.root / ".apm-local" / upstream.CONFIG).read_text()
        self.assertIn(str(self.source), config)
        for path in (self.root / ".apm").rglob("*"):
            if path.is_file():
                self.assertNotIn(str(self.source), path.read_text())
        state = json.loads((self.root / ".apm-local" / upstream.STATE).read_text())
        self.assertNotIn(str(self.source), json.dumps(state))

    def test_unsafe_destinations_rejected(self):
        for path in ("../outside", ".apm-local/inner", "/absolute"):
            with self.assertRaises(upstream.UpstreamError):
                upstream.add(self.root, path, str(self.source), "skills/example")

    def test_broken_source_reports_without_location(self):
        self.add()
        upstream.save_config(self.root, [{"path": SNAPSHOT, "source": str(self.root.parent / "missing.git"), "subpath": "skills/example"}])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(upstream.UpstreamError):
            upstream.sync(self.root, self.retire)
        self.assertNotIn("missing.git", output.getvalue())

    def test_cli_add_sync_list_remove(self):
        cwd = Path.cwd()
        try:
            import os
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(super_apm.main(["upstream", "add", SNAPSHOT, "--source", str(self.source), "--subpath", "skills/example"]), 0)
                self.assertEqual(super_apm.main(["upstream", "sync"]), 0)
                self.assertEqual(super_apm.main(["upstream", "list"]), 0)
                self.assertEqual(super_apm.main(["upstream", "remove", SNAPSHOT]), 0)
            self.assertIn("1 snapshot(s) refreshed", output.getvalue())
            self.assertEqual(self.snapshot(), "v1\n")
            self.assertIn("/.apm-local/", (self.root / ".gitignore").read_text())
            self.assertEqual(upstream.load(self.root), [])
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
