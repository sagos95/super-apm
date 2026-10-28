"""MCP projection uses synthetic values only and never launches account services."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tomlkit
import super_apm_mcp as mcp


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="super-apm-mcp-test-")).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.local = self.root / ".apm-local"
        self.local.mkdir()
        self.retired = []

    def retire(self, path):
        target = self.root / f"retired-{len(self.retired)}"
        path.rename(target)
        self.retired.append(target)

    def write(self, relative, data, local=True):
        path = (self.local if local else self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(data) if path.suffix == ".toml" else json.dumps(data))
        return path

    def sync(self):
        return mcp.apply(self.root, mcp.prepare(self.root), self.retire)

    def test_stdio_launcher_keeps_native_environment_out_of_root_json(self):
        self.write(".mcp.json", {"mcpServers": {"example": {"command": "python3", "args": ["server.py"], "timeout": 60000, "env": {"SAMPLE_INPUT": "opaque-example"}}}})
        self.assertEqual(self.sync(), 1)
        entry = json.loads((self.root / ".mcp.json").read_text())["mcpServers"]["example"]
        self.assertEqual(entry["command"], sys.executable)
        self.assertIn("mcp-run", entry["args"])
        self.assertEqual(entry["timeout"], 60000)
        self.assertNotIn("opaque-example", (self.root / ".mcp.json").read_text())
        self.assertNotIn("opaque-example", (self.local / mcp.STATE).read_text())
        self.assertEqual((self.root / ".mcp.json").stat().st_mode & 0o777, 0o600)

    def test_codex_preserves_comments_and_non_mcp_settings(self):
        target = self.root / ".codex/config.toml"
        target.parent.mkdir()
        target.write_text('# keep this comment\nmodel = "example-model"\n[mcp_servers.shared]\nurl = "https://example.invalid/shared"\n')
        self.write(".codex/config.toml", {"mcp_servers": {"example": {"command": "python3", "args": ["server.py"]}}})
        self.sync()
        result = tomlkit.parse(target.read_text())
        self.assertIn("# keep this comment", target.read_text())
        self.assertEqual(result["model"], "example-model")
        self.assertEqual(result["mcp_servers"]["example"]["cwd"], str(self.local))
        self.assertIn("shared", result["mcp_servers"])

    def test_http_projection_preserves_transport_fields(self):
        entry = {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Example": "opaque-example"}}
        self.write(".mcp.json", {"mcpServers": {"example": entry}})
        self.sync()
        self.assertEqual(json.loads((self.root / ".mcp.json").read_text())["mcpServers"]["example"], entry)

    def test_tracked_config_rejected_before_any_client_write(self):
        self.write(".mcp.json", {"mcpServers": {"example": {"command": "python3"}}})
        self.write(".codex/config.toml", {"mcp_servers": {"example": {"command": "python3"}}})
        self.write(".codex/config.toml", {"model": "example-model"}, local=False)
        subprocess.run(["git", "add", ".codex/config.toml"], cwd=self.root, check=True)
        with self.assertRaises(mcp.MCPError): self.sync()
        self.assertFalse((self.root / ".mcp.json").exists())

    def test_foreign_name_is_not_overwritten(self):
        self.write(".mcp.json", {"mcpServers": {"example": {"command": "python3"}}})
        target = self.write(".mcp.json", {"mcpServers": {"example": {"command": "other"}}}, local=False)
        before = target.read_bytes()
        with self.assertRaises(mcp.MCPError): self.sync()
        self.assertEqual(target.read_bytes(), before)

    def test_external_edit_of_owned_entry_is_preserved(self):
        self.write(".mcp.json", {"mcpServers": {"example": {"url": "https://example.invalid/v1"}}})
        self.sync()
        target = self.write(".mcp.json", {"mcpServers": {"example": {"url": "https://example.invalid/manual"}}}, local=False)
        with self.assertRaises(mcp.MCPError): self.sync()
        self.assertIn("manual", target.read_text())

    def test_update_and_removal_preserve_foreign_entries(self):
        source = self.write(".mcp.json", {"mcpServers": {"example": {"url": "https://example.invalid/v1"}}})
        self.write(".mcp.json", {"mcpServers": {"shared": {"command": "other"}}, "other": True}, local=False)
        self.sync()
        source.write_text('{"mcpServers":{"example":{"url":"https://example.invalid/v2"}}}')
        self.sync()
        self.assertIn("v2", (self.root / ".mcp.json").read_text())
        source.write_text('{"mcpServers":{}}')
        self.sync()
        value = json.loads((self.root / ".mcp.json").read_text())
        self.assertEqual(set(value["mcpServers"]), {"shared"})
        self.assertTrue(value["other"])

    def test_owned_empty_file_goes_to_trash(self):
        source = self.write(".mcp.json", {"mcpServers": {"example": {"command": "python3"}}})
        self.sync(); source.write_text('{"mcpServers":{}}'); self.sync()
        self.assertFalse((self.root / ".mcp.json").exists())
        self.assertEqual(len(self.retired), 1)

    def test_detach_then_restore_keeps_native_shared_audit_clean(self):
        self.write(".mcp.json", {"mcpServers": {"example": {"command": "python3"}}})
        target = self.write(".mcp.json", {"mcpServers": {"shared": {"command": "other"}}}, local=False)
        self.sync()
        mcp.apply(self.root, mcp.prepare(self.root, expose=False), self.retire)
        self.assertEqual(set(json.loads(target.read_text())["mcpServers"]), {"shared"})
        self.sync()
        self.assertEqual(set(json.loads(target.read_text())["mcpServers"]), {"shared", "example"})

    def test_invalid_config_error_does_not_echo_contents(self):
        (self.local / ".mcp.json").write_text("opaque-private-fixture-marker")
        with self.assertRaises(mcp.MCPError) as error: self.sync()
        self.assertNotIn("opaque-private-fixture-marker", str(error.exception))

    def test_real_stdio_runner_uses_local_cwd_and_env(self):
        (self.local / "server.py").write_text('import os,json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"tools":[{"name":"example-tool"}],"cwd":os.getcwd(),"env":os.environ.get("SAMPLE_INPUT")}}),flush=True)\n')
        self.write(".mcp.json", {"mcpServers": {"example": {"command": sys.executable, "args": ["server.py"], "env": {"SAMPLE_INPUT": "opaque-example"}}}})
        result = subprocess.run([sys.executable, "-m", "super_apm", "mcp-run", "--project", str(self.root), "--server", "example"], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["result"]
        self.assertEqual(data["cwd"], str(self.local))
        self.assertEqual(data["env"], "opaque-example")
        self.assertEqual(data["tools"][0]["name"], "example-tool")

    def test_stdio_substitution_uses_inherited_values_and_defaults(self):
        (self.local / "server.py").write_text('import os,json,sys\nprint(json.dumps([os.environ["SAMPLE_INPUT"],sys.argv[1:]]))\n')
        self.write(".mcp.json", {"mcpServers": {"example": {"command": "${EXAMPLE_PYTHON}", "args": ["server.py", "${EXAMPLE_UNSET:-fallback}", "${EXAMPLE_UNSET}"], "env": {"SAMPLE_INPUT": "${EXAMPLE_VALUE}"}}}})
        result = subprocess.run([sys.executable, "-m", "super_apm", "mcp-run", "--project", str(self.root), "--server", "example"], env={**os.environ, "EXAMPLE_PYTHON": sys.executable, "EXAMPLE_VALUE": "opaque-example"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["opaque-example", ["fallback", "${EXAMPLE_UNSET}"]])


if __name__ == "__main__": unittest.main()
