"""Synthetic deployment selection; never consult or modify the real installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from access_fixtures import RecycleFixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wechat_qq_access.deployment import default_private_root, resolve_config_path


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RecycleFixture(prefix="deployment-")
        self.root = Path(self.fixture.name)
        self.env = {"USERPROFILE": str(self.root / "profile"), "LOCALAPPDATA": str(self.root / "legacy")}
        self.fresh = default_private_root(self.env) / "deployment.json"
        self.legacy = Path(self.env["LOCALAPPDATA"]) / "MessagingAccess" / "deployment.json"

    def tearDown(self):
        self.fixture.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def test_new_default_and_existing_legacy_compatibility(self):
        self.assertEqual(resolve_config_path(environ=self.env), self.fresh)
        self.write(self.legacy, "{}")
        self.assertEqual(resolve_config_path(environ=self.env), self.legacy)
        self.write(self.fresh, "{}")
        self.assertEqual(resolve_config_path(environ=self.env), self.fresh)

    def test_explicit_then_environment_override_even_if_missing(self):
        env_path, explicit = self.root / "env.json", self.root / "explicit.json"
        self.write(self.fresh, "{}")
        env = {**self.env, "MESSAGING_ACCESS_CONFIG": str(env_path)}
        self.assertEqual(resolve_config_path(environ=env), env_path)
        self.assertEqual(resolve_config_path(explicit, env), explicit)

    def test_bad_selected_new_file_never_falls_back_to_valid_legacy(self):
        self.write(self.fresh, "{invalid}")
        self.write(self.legacy, json.dumps({"schemaVersion": 1, "privateRoot": str(self.root / "private")}))
        env = {**os.environ, **self.env}
        env.pop("MESSAGING_ACCESS_CONFIG", None)
        run = subprocess.run([sys.executable, str(ROOT / "scripts/access.py")], input='{"action":"policy.get"}',
                             text=True, capture_output=True, env=env)
        self.assertEqual(json.loads(run.stdout)["error"]["code"], "INVALID_JSON")
        explicit = self.root / "custom.json"
        self.write(explicit, json.dumps({"schemaVersion": 1, "privateRoot": str(self.root / "private")}))
        env["MESSAGING_ACCESS_CONFIG"] = str(self.fresh)
        run = subprocess.run([sys.executable, str(ROOT / "scripts/access.py"), "--config", str(explicit)],
                             input='{"action":"policy.get"}', text=True, capture_output=True, env=env)
        self.assertTrue(json.loads(run.stdout)["ok"])

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is unavailable")
    def test_powershell_selection_matches_python_and_does_not_parse_or_fallback(self):
        helper = str(ROOT / "scripts/deployment-path.ps1").replace("'", "''")
        env = {**os.environ, **self.env}
        env.pop("MESSAGING_ACCESS_CONFIG", None)
        command = ". '" + helper + "'; Resolve-AccessConfigPath"
        def selected(extra="", environment=None):
            run = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command + extra],
                                 text=True, capture_output=True, env=environment or env)
            self.assertEqual(run.returncode, 0, run.stderr)
            return Path(run.stdout.strip())
        self.assertEqual(selected(), self.fresh)
        self.write(self.legacy, "{}")
        self.assertEqual(selected(), self.legacy)
        self.write(self.fresh, "invalid")
        self.assertEqual(selected(), self.fresh)
        alternate = self.root / "environment.json"
        self.assertEqual(selected(environment={**env, "MESSAGING_ACCESS_CONFIG": str(alternate)}), alternate)
        explicit = self.root / "explicit.json"
        quoted = str(explicit).replace("'", "''")
        self.assertEqual(selected(" -ConfigPath '" + quoted + "' -Explicit",
                                  {**env, "MESSAGING_ACCESS_CONFIG": str(alternate)}), explicit)


if __name__ == "__main__":
    unittest.main()
