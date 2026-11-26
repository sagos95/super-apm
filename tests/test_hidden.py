"""Hidden dependencies: declared by a package, fetched by APM, invisible to every harness."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import super_apm as app
import super_apm_hidden as hidden
import super_apm_hooks as hooks

# Stands in for `apm lock`: copies fixture folders into apm_modules and writes direct lock
# entries, so tests never reach the network or the developer's real APM installation.
FAKE_APM = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
import yaml
with open(os.environ["FAKE_APM_LOG"], "a") as log:
    log.write(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}) + "\n")
if sys.argv[1:2] == ["lock"]:
    sources = Path(os.environ["FAKE_APM_SOURCES"])
    entries = []
    for item in yaml.safe_load(Path("apm.yml").read_text())["dependencies"]["apm"]:
        reference = item if isinstance(item, str) else item["git"] + "/" + item["path"]
        path = reference.split("#")[0].removeprefix("https://github.com/")
        owner, repo, *virtual = path.split("/")
        target = Path("apm_modules", owner, repo, *virtual)
        shutil.copytree(sources / path, target, dirs_exist_ok=True)
        entry = {"repo_url": f"{owner}/{repo}", "name": (virtual or [repo])[-1], "host": "github.com",
                 "resolved_commit": os.environ.get("FAKE_COMMIT", "c1")}
        if virtual:
            entry.update(virtual_path="/".join(virtual), is_virtual=True)
        entries.append(entry)
    Path("apm.lock.yaml").write_text(yaml.safe_dump({"lockfile_version": "1", "dependencies": entries}))
raise SystemExit(int(os.environ.get("FAKE_APM_EXIT", "0")))
'''

DECLARATION = {
    "destination": "references/methods",
    "rename": {"SKILL.md": "METHOD.md"},
    "dependencies": [
        "acme/skills/skills/animate#v1",
        {"package": "acme/kit/plugins/strategy/skills/plan", "as": "strategy/plan"},
        {"path": "./vendor/notes", "as": "notes"},
        {"path": "../sibling"},
    ],
}


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="super-apm-hidden-test-")).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.calls = self.base / "calls.jsonl"
        self.sources = self.base / "sources"
        self.trashed = []
        fake = self.base / "apm"
        fake.write_text(FAKE_APM.replace("#!/usr/bin/env python3", f"#!{shutil.which('python3')}"))
        fake.chmod(0o755)
        self.remote("acme/skills/skills/animate", RECIPES="recipes")
        self.remote("acme/kit/plugins/strategy/skills/plan")
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.env = patch.dict(os.environ, {"SUPER_APM_EXECUTABLE": str(fake), "FAKE_APM_LOG": str(self.calls),
                                           "FAKE_APM_SOURCES": str(self.sources)})
        self.env.start()
        self.patches = [patch.object(app, "trash", self.fake_trash)]
        for item in self.patches:
            item.start()
        self.sink = io.StringIO()
        self.out = contextlib.redirect_stdout(self.sink); self.out.__enter__()
        self.err = contextlib.redirect_stderr(self.sink); self.err.__enter__()

    def tearDown(self):
        self.err.__exit__(None, None, None); self.out.__exit__(None, None, None)
        for item in self.patches:
            item.stop()
        self.env.stop()
        os.chdir(self.old_cwd)

    def fake_trash(self, path: Path):
        destination = self.base / f"trash-{len(self.trashed)}-{path.name}"
        shutil.move(str(path), str(destination))
        self.trashed.append(destination)

    def remote(self, path: str, **extra):
        folder = self.sources / path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {folder.name}\ndescription: test\n---\n")
        for name, text in extra.items():
            (folder / f"{name}.md").write_text(text)

    def owner(self, declaration=DECLARATION, scope_base=None, name="router"):
        """Install a local-path owner package the way APM 0.32 records and materializes it."""
        base = scope_base or self.root
        source = self.root / "skills" / name
        (source / "vendor/notes").mkdir(parents=True, exist_ok=True)
        (source / "vendor/notes/SKILL.md").write_text("---\nname: notes\n---\n")
        (source / "vendor/notes/scripts").mkdir(exist_ok=True)
        (source / "vendor/notes/scripts/tool.py").write_text("print('never executed')\n")
        sibling = self.root / "skills/sibling"
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "SKILL.md").write_text("---\nname: sibling\n---\n")
        (source / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        (source / "apm.yml").write_text(yaml.safe_dump({"name": name, "version": "1.0.0", "super-apm": {"hidden": declaration}}))
        installed = base / "apm_modules/_local" / name
        shutil.copytree(source, installed, dirs_exist_ok=True)
        local_path = os.path.relpath(source, base)
        (base / "apm.yml").write_text(yaml.safe_dump({"name": "scope", "dependencies": {"apm": [{"path": local_path}]}}))
        (base / "apm.lock.yaml").write_text(yaml.safe_dump({"dependencies": [
            {"repo_url": f"_local/{name}", "name": name, "source": "local", "local_path": local_path}]}))
        return installed

    def uninstall_owner(self, base=None):
        (base or self.root / "apm.lock.yaml").write_text("dependencies: []\n")

    def lock_calls(self):
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [c["args"] for c in calls if c["args"][:1] == ["lock"]]

    def output(self, name="router"):
        folder = self.root / hidden.HIDDEN / name
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())

    def test_install_materializes_renamed_tree_outside_harness_folders(self):
        self.owner()
        self.assertEqual(app.main(["install"]), 0, self.sink.getvalue())
        self.assertEqual(self.output(), [
            "references/methods/animate/METHOD.md",
            "references/methods/animate/RECIPES.md",
            "references/methods/notes/METHOD.md",
            "references/methods/notes/scripts/tool.py",
            "references/methods/sibling/METHOD.md",
            "references/methods/strategy/plan/METHOD.md",
        ])
        # Original SKILL.md files remain only in APM's private download cache.
        visible = [p for p in (self.root / hidden.HIDDEN).rglob("SKILL.md")
                   if not p.is_relative_to(self.root / hidden.HIDDEN / hidden.WORK)]
        self.assertEqual(visible, [])
        self.assertIn("/.apm-hidden/", (self.root / ".gitignore").read_text())
        self.assertEqual(self.lock_calls(), [["lock"]])
        manifest = yaml.safe_load((self.root / hidden.HIDDEN / hidden.WORK / "router/apm.yml").read_text())
        self.assertEqual(manifest["dependencies"]["apm"], ["acme/skills/skills/animate#v1", "acme/kit/plugins/strategy/skills/plan"])
        self.assertFalse((self.root / ".claude").exists())

    def test_global_install_materializes_under_global_apm(self):
        home = self.base / "home/.apm"
        home.mkdir(parents=True)
        self.owner(scope_base=home)
        with patch.object(app, "global_root", lambda: home):
            self.assertEqual(app.main(["install", "-g"]), 0, self.sink.getvalue())
            folder = home / hidden.HIDDEN / "router"
            self.assertTrue((folder / "references/methods/animate/METHOD.md").is_file())
            self.assertTrue((folder / "references/methods/sibling/METHOD.md").is_file())
            self.assertFalse((home / ".gitignore").exists())
            self.assertFalse((self.root / hidden.HIDDEN).exists())
            (folder / "references/methods/animate/METHOD.md").unlink()
            self.assertEqual(app.main(["hidden", "-g"]), 0, self.sink.getvalue())
            self.assertTrue((folder / "references/methods/animate/METHOD.md").is_file())
            with patch.dict(os.environ, {"FAKE_COMMIT": "c2"}):
                self.assertEqual(app.main(["update", "-g", "--yes"]), 0, self.sink.getvalue())
        self.assertEqual(self.lock_calls(), [["lock"], ["lock", "--update"]])

    def test_exclude_skips_files_and_folders(self):
        self.remote("acme/skills/skills/animate", RECIPES="recipes")
        (self.sources / "acme/skills/skills/animate/demo.mp4").write_text("video")
        (self.sources / "acme/skills/skills/animate/dist").mkdir()
        (self.sources / "acme/skills/skills/animate/dist/SKILL.md").write_text("nested")
        self.owner({**DECLARATION, "exclude": ["*.mp4", "dist/"]})
        self.assertEqual(app.main(["install"]), 0, self.sink.getvalue())
        files = self.output()
        self.assertIn("references/methods/animate/RECIPES.md", files)
        self.assertFalse([f for f in files if f.endswith(".mp4") or "/dist/" in f])
        self.assertEqual(hidden.excluded("a/b/c.txt", ["b"]), True)
        self.assertEqual(hidden.excluded("ab/c.txt", ["b"]), False)
        with self.assertRaises(hidden.HiddenError):
            hidden.parse("x", {**DECLARATION, "exclude": ["../secret"]})

    def test_project_own_skill_declares_hidden_dependencies(self):
        own = self.root / ".apm/skills/router"
        (own / "vendor/notes").mkdir(parents=True)
        (own / "vendor/notes/SKILL.md").write_text("---\nname: notes\n---\n")
        (own / "SKILL.md").write_text("---\nname: router\n---\n")
        declaration = {"destination": "methods", "dependencies": ["acme/skills/skills/animate#v1", {"path": "./vendor/notes", "as": "notes"}]}
        (own / "apm.yml").write_text(yaml.safe_dump({"name": "router", "version": "1.0.0", "super-apm": {"hidden": declaration}}))
        (self.root / "apm.yml").write_text(yaml.safe_dump({"name": "project", "dependencies": {"apm": []}}))
        self.assertEqual(app.main(["install"]), 0, self.sink.getvalue())
        self.assertEqual(self.output(), ["methods/animate/METHOD.md", "methods/animate/RECIPES.md", "methods/notes/METHOD.md"])

    def test_failed_global_apm_skips_hidden(self):
        home = self.base / "home/.apm"
        home.mkdir(parents=True)
        self.owner(scope_base=home)
        with patch.object(app, "global_root", lambda: home), patch.dict(os.environ, {"FAKE_APM_EXIT": "3"}):
            self.assertEqual(app.main(["install", "-g"]), 3)
        self.assertFalse((home / hidden.HIDDEN).exists())

    def test_second_install_reuses_lock_and_update_refreshes_it(self):
        self.owner()
        app.main(["install"])
        app.main(["install"])
        self.assertEqual(self.lock_calls(), [["lock"]])
        (self.sources / "acme/skills/skills/animate/SKILL.md").write_text("---\nname: animate\n---\nv2\n")
        with patch.dict(os.environ, {"FAKE_COMMIT": "c2"}):
            self.assertEqual(app.main(["update", "--yes"]), 0, self.sink.getvalue())
        self.assertEqual(self.lock_calls(), [["lock"], ["lock", "--update"]])
        self.assertIn("v2", (self.root / hidden.HIDDEN / "router/references/methods/animate/METHOD.md").read_text())
        self.assertIn("c2", self.sink.getvalue() + json.dumps(hidden.rows(self.root, ["shared"])))

    def test_named_update_refreshes_only_that_owner(self):
        self.owner()
        app.main(["install"])
        with patch.object(app, "identities", lambda base: {"other/pkg"}):
            self.assertEqual(app.main(["update", "other/pkg", "--yes"]), 0, self.sink.getvalue())
        self.assertEqual(self.lock_calls(), [["lock"]])
        with patch.object(app, "identities", lambda base: {"router"}):
            app.main(["update", "router", "--yes"])
        self.assertEqual(self.lock_calls(), [["lock"], ["lock", "--update"]])

    def test_removing_owner_moves_output_and_workspace_to_trash(self):
        self.owner()
        app.main(["install"])
        self.uninstall_owner()
        self.assertEqual(app.reconcile_hidden(self.root, "shared"), 0)
        self.assertEqual([p.name.split("-", 2)[2] for p in self.trashed], ["router", "router"])
        self.assertFalse((self.root / hidden.HIDDEN / "router").exists())
        self.assertEqual(hidden.read_state(self.root), {})

    def test_dropped_dependency_goes_to_trash_as_one_folder(self):
        installed = self.owner()
        app.main(["install"])
        declaration = {**DECLARATION, "dependencies": DECLARATION["dependencies"][1:]}
        self.owner(declaration)
        self.assertEqual(app.reconcile_hidden(self.root, "shared"), 3)
        self.assertEqual([p.name.split("-", 2)[2] for p in self.trashed], ["animate"])
        self.assertTrue((self.trashed[0] / "RECIPES.md").is_file())
        self.assertNotIn("references/methods/animate/METHOD.md", self.output())
        self.assertTrue(installed.is_dir())

    def test_hand_edits_are_never_overwritten(self):
        self.owner()
        app.main(["install"])
        edited = self.root / hidden.HIDDEN / "router/references/methods/sibling/METHOD.md"
        edited.write_text("my notes")
        (self.root / "skills/sibling/SKILL.md").write_text("---\nname: sibling\n---\nnew\n")
        self.assertEqual(app.main(["install"]), 1)
        self.assertEqual(edited.read_text(), "my notes")
        self.assertIn("changed by hand", self.sink.getvalue())
        self.uninstall_owner()
        with self.assertRaises(hidden.HiddenError):
            app.reconcile_hidden(self.root, "shared")
        self.assertEqual(edited.read_text(), "my notes")
        self.assertEqual(self.trashed, [])

    def test_interrupted_copy_resumes(self):
        self.owner()
        app.main(["install"])
        (self.root / "skills/sibling/SKILL.md").write_text("---\nname: sibling\n---\nnew\n")
        real_copy = shutil.copy2
        def interrupted(source, destination):
            real_copy(source, destination)
            raise OSError("disk full")
        with patch.object(hidden.shutil, "copy2", interrupted):
            with self.assertRaises(hidden.HiddenError):
                app.reconcile_hidden(self.root, "shared")
        self.assertEqual(app.reconcile_hidden(self.root, "shared"), 4)
        self.assertIn("new", (self.root / hidden.HIDDEN / "router/references/methods/sibling/METHOD.md").read_text())

    def test_status_and_list_report_hidden_dependencies(self):
        self.owner()
        app.main(["install"])
        self.sink.truncate(0)
        self.assertEqual(app.main(["status"]), 0)
        self.assertIn("hidden router: 4 dependencies, healthy", self.sink.getvalue())
        app.main(["list"])
        self.assertIn("strategy/plan <- acme/kit/plugins/strategy/skills/plan", self.sink.getvalue())
        (self.root / hidden.HIDDEN / "router/references/methods/notes/METHOD.md").write_text("x")
        self.assertEqual(app.main(["status"]), 1)

    def test_local_scope_owner_and_cross_scope_conflict(self):
        local = self.root / app.LOCAL
        local.mkdir()
        self.owner(scope_base=local)
        self.assertEqual(app.main(["install", "--local"]), 0, self.sink.getvalue())
        self.assertIn("references/methods/animate/METHOD.md", self.output())
        self.assertEqual(hidden.read_state(self.root)["router"]["scope"], "local")
        self.owner()
        with self.assertRaises(hidden.HiddenError):
            app.reconcile_hidden(self.root, "shared")

    def test_dry_run_and_projects_without_declarations_touch_nothing(self):
        (self.root / "apm.yml").write_text("name: plain\ndependencies: {apm: []}\n")
        self.assertEqual(app.main(["install"]), 0)
        self.assertFalse((self.root / hidden.HIDDEN).exists())
        self.owner()
        self.assertEqual(app.main(["install", "--dry-run"]), 0)
        self.assertFalse((self.root / hidden.HIDDEN).exists())

    def test_tracked_hidden_folder_is_refused(self):
        self.owner()
        (self.root / hidden.HIDDEN).mkdir()
        (self.root / hidden.HIDDEN / "keep").write_text("tracked")
        subprocess.run(["git", "-C", str(self.root), "add", hidden.HIDDEN], check=True)
        self.assertEqual(app.main(["install"]), 1)
        self.assertIn("untracked", self.sink.getvalue())

    def test_unsafe_declarations_are_rejected(self):
        cases = [
            {**DECLARATION, "rename": {}},
            {**DECLARATION, "destination": "../escape"},
            {**DECLARATION, "dependencies": [{"path": "/etc"}]},
            {**DECLARATION, "dependencies": [{"package": "acme/skills/skills/animate", "as": "../x"}]},
            {**DECLARATION, "dependencies": ["./vendor/notes"]},
            {**DECLARATION, "dependencies": []},
            {**DECLARATION, "postinstall": "rm -rf ~"},
            {**DECLARATION, "dependencies": [{"path": "./vendor/notes", "as": "a"}, {"path": "../sibling", "as": "a/b"}]},
        ]
        for declaration in cases:
            with self.subTest(declaration=declaration):
                self.owner(declaration)
                with self.assertRaises(hidden.HiddenError):
                    app.reconcile_hidden(self.root, "shared")
                self.assertEqual(self.output() if (self.root / hidden.HIDDEN / "router").exists() else [], [])

    def test_remote_owner_cannot_reach_outside_itself(self):
        (self.root / "apm.yml").write_text("name: p\ndependencies: {apm: [acme/router-pkg]}\n")
        installed = self.root / "apm_modules/acme/router-pkg"
        installed.mkdir(parents=True)
        (installed / "apm.yml").write_text(yaml.safe_dump({"name": "router-pkg", "super-apm": {"hidden": {
            "dependencies": [{"path": "../../../skills"}]}}}))
        (self.root / "skills").mkdir()
        (self.root / "apm.lock.yaml").write_text(yaml.safe_dump({"dependencies": [
            {"repo_url": "acme/router-pkg", "name": "router-pkg", "host": "github.com", "resolved_commit": "c1"}]}))
        with self.assertRaises(hidden.HiddenError) as caught:
            app.reconcile_hidden(self.root, "shared")
        self.assertIn("inside itself", self.sink.getvalue() + str(caught.exception))

    def test_background_worker_refreshes_hidden_dependencies(self):
        self.owner()
        app.main(["install"])
        (self.root / app.LOCAL).mkdir(exist_ok=True)
        with patch.object(app, "native", lambda root, scope, args: 0):
            self.assertEqual(hooks.worker(["--project", str(self.root)]), 0)
        self.assertEqual(self.lock_calls(), [["lock"], ["lock", "--update"]])

    def test_identity_matches_apm_lock_spellings(self):
        locked = {"repo_url": "acme/kit", "virtual_path": "skills/plan", "host": "github.com"}
        for reference in ("acme/kit/skills/plan#v1", "github.com/acme/kit/skills/plan",
                          "https://github.com/Acme/kit.git/skills/plan"):
            self.assertEqual(hidden.identity(reference), hidden.locked_identity(locked))
        gitlab = {"repo_url": "group/repo", "host": "gitlab.example.com"}
        for reference in ("gitlab.example.com/group/repo", "git@gitlab.example.com:group/repo.git#main",
                          "ssh://git@gitlab.example.com/group/repo.git"):
            self.assertEqual(hidden.identity(reference), hidden.locked_identity(gitlab))


if __name__ == "__main__":
    unittest.main()
