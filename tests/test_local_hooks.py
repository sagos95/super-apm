"""Local hook projection uses synthetic packages only and never runs the hooks."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import tomlkit
import super_apm
import super_apm_local_hooks as localhooks

CLAUDE_GUARD = 'python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/example/hooks/guard.py"'
CODEX_GUARD = "python3 .codex/hooks/example/hooks/guard.py"


class LocalHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="super-apm-hooks-test-")).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.local = self.root / ".apm-local"
        self.local.mkdir()
        self.retired = []

    def retire(self, path):
        target = self.root / f"retired-{len(self.retired)}"
        path.rename(target)
        self.retired.append(target)

    def record(self, client, events):
        path = self.local / client / "apm-hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events))
        return path

    def sync(self):
        return localhooks.apply(self.root, localhooks.prepare(self.root), self.retire)

    def claude_settings(self):
        return json.loads((self.root / ".claude/settings.local.json").read_text())

    def guard_record(self, command=CLAUDE_GUARD):
        return {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 10}],
                                "_apm_source": "example"}]}

    def test_claude_hook_points_at_local_copy_and_keeps_user_settings(self):
        target = self.root / ".claude/settings.local.json"
        target.parent.mkdir()
        foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
        target.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "hooks": {"PreToolUse": [foreign]}}))
        self.record(".claude", self.guard_record())
        self.assertEqual(self.sync(), 1)
        settings = self.claude_settings()
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls:*)"]})
        groups = settings["hooks"]["PreToolUse"]
        self.assertEqual(groups[0], foreign)
        self.assertEqual(groups[1]["hooks"][0]["command"],
                         'python3 "${CLAUDE_PROJECT_DIR}/.apm-local/.claude/hooks/example/hooks/guard.py"')
        self.assertNotIn("_apm_source", groups[1])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_repeated_sync_does_not_duplicate(self):
        self.record(".claude", self.guard_record())
        self.sync()
        first = (self.root / ".claude/settings.local.json").read_text()
        self.sync()
        self.assertEqual((self.root / ".claude/settings.local.json").read_text(), first)
        self.assertEqual(len(self.claude_settings()["hooks"]["PreToolUse"]), 1)

    def test_changed_package_replaces_only_its_own_entry(self):
        target = self.root / ".claude/settings.local.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        self.record(".claude", self.guard_record())
        self.sync()
        self.record(".claude", self.guard_record(CLAUDE_GUARD.replace("guard.py", "guard2.py")))
        self.sync()
        settings = self.claude_settings()
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
        self.assertIn("guard2.py", settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "echo mine")

    def test_removed_package_retires_a_file_it_created(self):
        source = self.record(".claude", self.guard_record())
        self.sync()
        source.rename(self.root / "removed-record")
        self.assertEqual(self.sync(), 0)
        self.assertFalse((self.root / ".claude/settings.local.json").exists())
        self.assertEqual(len(self.retired), 1)

    def test_removed_package_keeps_a_file_the_user_owns(self):
        target = self.root / ".claude/settings.local.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"permissions": {"allow": []}}))
        source = self.record(".claude", self.guard_record())
        self.sync()
        source.rename(self.root / "removed-record")
        self.sync()
        self.assertEqual(self.claude_settings(), {"permissions": {"allow": []}})

    def test_prompt_hook_passes_through(self):
        prompt = {"Stop": [{"hooks": [{"type": "prompt", "prompt": "Check: $ARGUMENTS", "timeout": 30}]}]}
        self.record(".claude", prompt)
        self.sync()
        self.assertEqual(self.claude_settings()["hooks"], prompt)

    def test_codex_hook_goes_to_inline_toml_anchored_at_git_root(self):
        target = self.root / ".codex/config.toml"
        target.parent.mkdir()
        target.write_text('# keep this comment\nmodel = "example-model"\n')
        self.record(".codex", self.guard_record(CODEX_GUARD))
        self.assertEqual(self.sync(), 1)
        text = target.read_text()
        self.assertIn("# keep this comment", text)
        result = tomlkit.parse(text).unwrap()
        self.assertEqual(result["model"], "example-model")
        handler = result["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(handler["command"],
                         'python3 "$(git rev-parse --show-toplevel)/.apm-local/.codex/hooks/example/hooks/guard.py"')
        self.sync()
        self.assertEqual(len(tomlkit.parse(target.read_text()).unwrap()["hooks"]["PreToolUse"]), 1)

    def test_tracked_settings_rejected_before_any_write(self):
        target = self.root / ".claude/settings.local.json"
        target.parent.mkdir()
        target.write_text("{}")
        subprocess.run(["git", "add", "-f", ".claude/settings.local.json"], cwd=self.root, check=True)
        self.record(".claude", self.guard_record())
        with self.assertRaises(localhooks.HookError):
            self.sync()
        self.assertEqual(target.read_text(), "{}")

    def test_hook_outputs_are_accepted_and_unknown_outputs_rejected(self):
        (self.local / "apm.yml").write_text("name: local\nversion: 1.0.0\n")
        for name in (".claude/hooks/example", ".codex/hooks/example"):
            (self.local / name).mkdir(parents=True)
        self.record(".claude", self.guard_record())
        self.assertEqual(super_apm.local_sources(self.root), {})
        (self.local / ".claude/agents").mkdir()
        with self.assertRaises(super_apm.Error):
            super_apm.local_sources(self.root)

    def test_sync_ignores_projected_settings(self):
        (self.local / "apm.yml").write_text("name: local\nversion: 1.0.0\n")
        self.record(".claude", self.guard_record())
        super_apm.sync(self.root)
        ignored = (self.root / ".gitignore").read_text()
        self.assertIn("/.claude/settings.local.json", ignored)
        self.assertIn("/.apm-local/", ignored)


if __name__ == "__main__":
    unittest.main()
