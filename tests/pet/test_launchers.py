"""L3 Windows 隔离接线测试：仅运行夹具，替换端口检查/服务启动/自检，不停启生产。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
BAT_FILES = ["start_all.bat", "一键启动.bat", "一键启动-dev.bat", "一键启动-构建版.bat",
             "pet/start_all.bat", "pet/start_all_prod.bat"]


@unittest.skipUnless(os.name == "nt", "Windows cmd/PowerShell required")
class Launchers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="albion launcher ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = self.root / "records.jsonl"
        self.env = dict(os.environ, L3_RECORDS=str(self.records))
        self.env.pop("ALBION_SHOW_CONSOLES", None)
        for name in BAT_FILES:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, p)
        # Execute the real dispatch branches, replacing only external operations.
        p = self.root / "start_all.bat"
        text = p.read_bytes().decode("gbk")
        text = text[:text.index(":check\r\n")].rstrip() + "\r\n:check\r\nset PORTFREE=1\r\nexit /b\r\n"
        text = text.replace("ping -n 11 127.0.0.1 >nul", "rem skipped fixture wait")
        text = text.replace('"%WIFE%\\tts\\GPT-sovits\\runtime\\python.exe" "%WIFE%\\pet\\check_all.py" 90', "echo FIXTURE_SELF_CHECK")
        text = text.replace('start "" "%WIFE%\\shell\\apps\\stage-tamagotchi\\dist\\win-unpacked\\airi.exe" --remote-debugging-port=9335', "echo FIXTURE_EXE")
        p.write_bytes(text.encode("gbk"))
        (self.root / "_silent_run.ps1").write_text('''param([string]$Cmd,[string]$Bat,[string]$BatArgs,[string]$Log,[switch]$Visible)
@{Cmd=$Cmd;Bat=$Bat;BatArgs=$BatArgs;Log=$Log;Visible=[bool]$Visible} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:L3_RECORDS
''', encoding="utf-8")
        (self.root / "_启动脑Pi.ps1").write_text("exit 0", encoding="utf-8")
        token = self.root / "pet/tools/eyes/token.txt"
        token.parent.mkdir(parents=True)
        token.write_text("fixture-only", encoding="ascii")

    def run_entry(self, name, *args):
        result = subprocess.run([os.environ["ComSpec"], "/d", "/c", "call", str(self.root / name), *args],
                                cwd=self.root.parent, env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", "replace"))
        rows = [json.loads(s) for s in self.records.read_text(encoding="utf-8-sig").splitlines()]
        return result.stdout.decode("gbk", "replace"), rows

    def assert_services(self, rows, visible):
        bats = [Path(r["Bat"]).name for r in rows if r["Bat"]]
        self.assertEqual(bats, ["start_backend.bat", "start_bridge.bat", "start_stream.bat", "start_ear.bat"])
        self.assertTrue(all(r["Visible"] == visible for r in rows))
        eye = next(r for r in rows if "capture_agent.py" in r["Cmd"])
        self.assertIn('"' + str(self.root / "pet/tools/eyes/token.txt") + '"', eye["Cmd"])
        self.assertNotIn("fixture-only", json.dumps(rows))

    def test_dev_hidden_from_other_cwd(self):
        out, rows = self.run_entry("一键启动-dev.bat")
        self.assert_services(rows, False)
        self.assertIn("electron-vite dev", rows[-1]["Cmd"])
        self.assertIn("FIXTURE_SELF_CHECK", out)

    def test_original_visible_and_pet_forwarder(self):
        out, rows = self.run_entry("pet/start_all.bat")
        self.assert_services(rows, True)
        self.assertIn("electron-vite dev", rows[-1]["Cmd"])

    def test_prod_selects_exe(self):
        exe = self.root / "shell/apps/stage-tamagotchi/dist/win-unpacked/airi.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        out, rows = self.run_entry("pet/start_all_prod.bat")
        self.assert_services(rows, False)
        self.assertIn("FIXTURE_EXE", out)
        self.assertFalse(any("electron-vite" in r["Cmd"] for r in rows))

    def test_missing_exe_falls_back_to_dev(self):
        out, rows = self.run_entry("一键启动-构建版.bat", "show")
        self.assert_services(rows, False)
        self.assertIn("electron-vite dev", rows[-1]["Cmd"])

    def test_build_double_click_uses_hidden_batch_forwarding(self):
        out, rows = self.run_entry("一键启动-构建版.bat")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Bat"], str(self.root / "start_all.bat"))
        self.assertEqual(rows[0]["BatArgs"], "exe __hidden")

    def test_batch_encoding_and_single_implementation(self):
        for name in BAT_FILES:
            raw = (ROOT / name).read_bytes()
            raw.decode("gbk")
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), name)
            if name != "start_all.bat":
                self.assertNotIn(b"netstat", raw)
                self.assertNotIn(b"capture_agent.py", raw)

    def test_real_silent_helper_handles_spaces_and_cmd_form(self):
        helper = ROOT / "_silent_run.ps1"
        target = self.root / "tiny batch.bat"
        marker = self.root / "marker.txt"
        target.write_bytes(('@echo off\r\necho %1>"' + str(marker) + '"\r\n').encode("gbk"))
        subprocess.run(["powershell", "-NoProfile", "-File", str(helper), "-Bat", str(target),
                        "-BatArgs", "success", "-Log", str(self.root / "new logs/run.log")],
                       check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertEqual(marker.read_text().strip(), "success")
        marker.unlink()
        command = 'call "' + str(target) + '" alias'
        subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "_silent_run.ps1"),
                        "-Cmd", command], check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertEqual(marker.read_text().strip(), "alias")

    def test_eye_command_executes_with_spaces_and_preserves_arguments(self):
        out, rows = self.run_entry("一键启动-dev.bat")
        command = next(r["Cmd"] for r in rows if "capture_agent.py" in r["Cmd"])
        script = self.root / "pet/tools/eyes/capture_agent.py"
        marker = self.root / "arguments.json"
        script.write_text("import sys,json\nfrom pathlib import Path\nPath(" + repr(str(marker)) +
                          ").write_text(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        command = command.replace(str(self.root / "tts/GPT-sovits/runtime/python.exe"), sys.executable)
        subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "_silent_run.ps1"),
                        "-Cmd", command], check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertEqual(json.loads(marker.read_text()), ["--port", "8790", "--token-file",
                         str(self.root / "pet/tools/eyes/token.txt"), "--max-width", "1920",
                         "--bind", "127.0.0.1"])

    def test_hidden_vbs_forwards_dragged_batch_without_starting_services(self):
        vbs = self.root / "hidden.vbs"
        shutil.copyfile(ROOT / "一键启动-构建版-隐藏.vbs", vbs)
        batch = self.root / "dragged batch.bat"
        batch.write_bytes(b"@echo off\r\nexit /b\r\n")
        subprocess.run(["cscript", "//nologo", str(vbs), str(batch)], env=self.env,
                       check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if self.records.exists():
                break
            time.sleep(.02)
        row = json.loads(self.records.read_text(encoding="utf-8-sig"))
        self.assertEqual(row["Bat"], str(batch))
        self.assertEqual(row["Log"], str(self.root / "logs/start_hidden.log"))


if __name__ == "__main__":
    unittest.main()
