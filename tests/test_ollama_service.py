"""Regression checks for Windows service ownership; no sockets or apps opened."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


POWERSHELL = shutil.which("powershell.exe")
HELPER = Path(__file__).resolve().parents[1] / "ollama_service.ps1"


def row(owner, remote_port=0, address="127.0.0.1", state=0, remote=None):
    return {"LocalAddress": address, "LocalPort": 11435,
            "RemoteAddress": remote or ("127.0.0.1" if remote_port else "0.0.0.0"),
            "RemotePort": remote_port, "OwningProcess": owner, "State": state}


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required for launcher checks")
class LocalServiceTests(unittest.TestCase):
    def test_source_startup_overrides_request_logging_and_extra_origins(self):
        helper = str(HELPER).replace("'", "''")
        script = f". '{helper}'\nGet-PrivateOllamaEnvironment -ModelsDirectory 'D:\\Models' | ConvertTo-Json -Compress"
        environment = dict(os.environ, OLLAMA_DEBUG="2", OLLAMA_DEBUG_LOG_REQUESTS="true", OLLAMA_ORIGINS="*")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment, text=True, capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values["OLLAMA_DEBUG"], "0")
        self.assertEqual(values["OLLAMA_DEBUG_LOG_REQUESTS"], "0")
        self.assertEqual(values["OLLAMA_ORIGINS"], "")
        self.assertEqual(values["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(values["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(values["OLLAMA_MODELS"], r"D:\Models")
        # Compatibility helpers no longer launch services. Source execution uses
        # the installed app's authenticated lifecycle and managed shutdown.
        start = (HELPER.parent / "Start Translator.ps1").read_text(encoding="utf-8-sig")
        setup = (HELPER.parent / "Setup Ollama.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("installer_main.py", start)
        self.assertIn("--configure", start)
        self.assertIn("'Start Translator.ps1') -Configure", setup)
        for source in (start, setup):
            self.assertNotIn("Get-PrivateOllamaEnvironment -ModelsDirectory", source)
            self.assertNotIn("'ollama_service.ps1'", source)
            self.assertNotIn("-ArgumentList 'serve'", source)
            self.assertNotIn("Invoke-RestMethod", source)
            self.assertNotIn("/api/pull", source)

    def test_source_launchers_check_only_is_headless_and_read_only(self):
        for name in ("Start Translator.ps1", "Setup Ollama.ps1"):
            with self.subTest(name=name):
                result = subprocess.run(
                    [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(HELPER.parent / name), "-CheckOnly"],
                    cwd=HELPER.parent, text=True, capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["windows_opened"])
                self.assertFalse(report["services_started"])

    def select(self, rows):
        helper = str(HELPER).replace("'", "''")
        script = f"""$ErrorActionPreference = 'Stop'
. '{helper}'
$rows = [Console]::In.ReadToEnd() | ConvertFrom-Json
try {{
    $selected = Find-LocalOllamaListener -Connections $rows -Port 11435 -ProbePort 50001
    [pscustomobject]@{{ Owner=$selected.OwningProcess; Address=$selected.LocalAddress }} | ConvertTo-Json -Compress
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            input=json.dumps(rows), text=True, capture_output=True, timeout=15,
        )

    def test_stale_ownerless_listen_state_cannot_replace_the_real_owner(self):
        result = self.select([row(0, 63287, state=2), row(123), row(123, 50001)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Owner": 123, "Address": "127.0.0.1"})

    def test_normal_state_values_work_too(self):
        result = self.select([row(123, state=2), row(123, 50001, state=5)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["Owner"], 123)

    def test_bound_socket_alone_does_not_prove_service_ownership(self):
        result = self.select([row(123, state=2)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verify the owner", result.stderr)

    def test_stale_connection_with_a_different_remote_port_is_ignored(self):
        result = self.select([row(123), row(123, 50002)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verify the owner", result.stderr)

    def test_active_connection_and_listener_must_have_the_same_owner(self):
        result = self.select([row(123), row(456, 50001)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exclusively local", result.stderr)

    def test_wildcard_listener_is_not_accepted_as_loopback_only(self):
        result = self.select([row(123, address="0.0.0.0"), row(123, 50001)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exclusively local", result.stderr)

    def test_additional_ipv6_wildcard_listener_is_not_ignored(self):
        result = self.select([row(123), row(123, address="::", remote="::"), row(123, 50001)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exclusively local", result.stderr)

    def test_conflicting_connection_owners_are_rejected(self):
        result = self.select([row(123), row(123, 50001), row(456, 50001)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verify the owner", result.stderr)


if __name__ == "__main__":
    unittest.main()
