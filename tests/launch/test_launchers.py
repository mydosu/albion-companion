"""L3 Windows 隔离接线测试：只跑夹具，替换端口检查/服务启动/自检，不停启生产。

覆盖 launch/ 收口后的四个根入口 + 服务 bat 的接线：
  一键启动.bat（构建版，隐藏）、一键启动-dev.bat（dev，可带 show）、launch/start_all.bat（唯一实现）
跑法（Windows）：
  python -m unittest discover -s tests/launch -p 'test_launchers.py' -v
  python -m pytest tests/launch/test_launchers.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 根入口 + 会被 start_all 调到的服务 bat（都要保持原编码与 CRLF）
ENTRY_BATS = [
    "一键启动.bat",
    "一键启动-dev.bat",
    "一键关闭.bat",
    "一键安装.bat",
    "launch/start_all.bat",
    "launch/load_env.bat",
    "tts/irodori/start_backend.bat",
    "tts/irodori/start_bridge.bat",
    "tts/stream/start_stream.bat",
    "pet/start_ear.bat",
]

SERVICE_BATS = [
    "tts/irodori/start_backend.bat",
    "tts/irodori/start_bridge.bat",
    "tts/stream/start_stream.bat",
    "pet/start_ear.bat",
    "pet/tools/eyes/start_eyes.bat",
]

EYES_TOKEN = "/".join(["pet", "tools", "eyes", "token.txt"])
EYES_DIR = "/".join(["pet", "tools", "eyes"])


@unittest.skipUnless(os.name == "nt", "Windows cmd/PowerShell required")
class Launchers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="albion launcher ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = self.root / "records.jsonl"
        self.env = dict(os.environ, L3_RECORDS=str(self.records))
        self.env.pop("ALBION_SHOW_CONSOLES", None)
        for name in ENTRY_BATS + ["launch/hidden.vbs"]:
            dst = self.root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, dst)
        # 唯一实现里只保留分支：端口一律空闲、外部动作打桩（不探真端口、不起真服务）
        p = self.root / "launch/start_all.bat"
        text = p.read_bytes().decode("gbk")
        text = text[:text.index(":check\r\n")].rstrip() + "\r\n:check\r\nset PORTFREE=1\r\nexit /b\r\n"
        text = text.replace("ping -n 11 127.0.0.1 >nul", "rem skipped fixture wait")
        text = text.replace('"%WIFE%\\tts\\GPT-sovits\\runtime\\python.exe" "%WIFE%\\launch\\check_all.py" 90',
                            "echo FIXTURE_SELF_CHECK")
        text = text.replace('start "" "%WIFE%\\shell\\apps\\stage-tamagotchi\\dist\\win-unpacked\\airi.exe" '
                            '--remote-debugging-port=9335', "echo FIXTURE_EXE")
        p.write_bytes(text.encode("gbk"))
        # 外部依赖全部打桩
        (self.root / "launch/silent_run.ps1").write_text(
            'param([Parameter(Mandatory=$false)][string]$Cmd,[string]$Bat,[string]$BatArgs,'
            '[string]$Log,[switch]$Visible)\n'
            '@{Cmd=$Cmd;Bat=$Bat;BatArgs=$BatArgs;Log=$Log;Visible=[bool]$Visible}'
            ' | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:L3_RECORDS\n',
            encoding="utf-8")
        (self.root / "launch/start_brain.ps1").write_text("exit 0\n", encoding="utf-8")
        widget = self.root / "pet/widget/AlbionStatus.vbs"
        widget.parent.mkdir(parents=True, exist_ok=True)
        widget.write_text("WScript.Quit 0\n", encoding="ascii")
        token = self.root / EYES_TOKEN
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("fixture-only", encoding="ascii")

    # ---- helpers ---------------------------------------------------------
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
        eye = next(r for r in rows if "capture_agent.py" in (r["Cmd"] or ""))
        self.assertIn('"' + str(self.root / EYES_TOKEN) + '"', eye["Cmd"])
        self.assertNotIn("fixture-only", json.dumps(rows))

    # ---- 入口 -------------------------------------------------------------
    def test_dev_hidden_from_other_cwd(self):
        out, rows = self.run_entry("一键启动-dev.bat")
        self.assert_services(rows, False)
        self.assertIn("electron-vite dev", rows[-1]["Cmd"])
        self.assertIn("FIXTURE_SELF_CHECK", out)

    def test_dev_show_turns_consoles_on(self):
        out, rows = self.run_entry("一键启动-dev.bat", "show")
        self.assert_services(rows, True)

    def test_build_entry_forwards_hidden_exe(self):
        out, rows = self.run_entry("一键启动.bat")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Bat"], str(self.root / "launch/start_all.bat"))
        self.assertEqual(rows[0]["BatArgs"], "exe __hidden")
        self.assertEqual(rows[0]["Log"], str(self.root / "logs/start_hidden.log"))

    def test_prod_selects_exe(self):
        exe = self.root / "shell/apps/stage-tamagotchi/dist/win-unpacked/airi.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        out, rows = self.run_entry("launch/start_all.bat", "exe")
        self.assert_services(rows, False)
        self.assertIn("FIXTURE_EXE", out)
        self.assertFalse(any("electron-vite" in r["Cmd"] for r in rows))

    def test_missing_exe_falls_back_to_dev(self):
        out, rows = self.run_entry("launch/start_all.bat", "exe")
        self.assert_services(rows, False)
        self.assertIn("electron-vite dev", rows[-1]["Cmd"])

    def test_local_env_reaches_start_all(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "local.env").write_text("ALBION_SHOW_CONSOLES=1\n", encoding="utf-8")
        out, rows = self.run_entry("一键启动-dev.bat")           # 没带 show，靠 local.env
        self.assert_services(rows, True)

    # ---- 静态约束 ---------------------------------------------------------
    def test_batch_encoding_and_crlf(self):
        for name in ENTRY_BATS + ["launch/hidden.vbs"]:
            raw = (ROOT / name).read_bytes()
            raw.decode("gbk")                                    # GBK 超集能读纯 ASCII
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), name)

    def test_service_bats_load_local_env(self):
        for name in SERVICE_BATS:
            text = (ROOT / name).read_bytes().decode("gbk")
            self.assertIn("load_env.bat", text, name)

    def test_only_start_all_starts_things(self):
        start_all = (ROOT / "launch/start_all.bat").read_bytes()
        self.assertIn(b"netstat", start_all)
        for name in ENTRY_BATS + ["pet/tools/eyes/start_eyes.bat"]:
            if name == "launch/start_all.bat":
                continue
            raw = (ROOT / name).read_bytes()
            self.assertNotIn(b"netstat", raw, name)
        for name in ["一键启动.bat", "一键启动-dev.bat", "一键关闭.bat", "一键安装.bat"]:
            self.assertNotIn(b"capture_agent.py", (ROOT / name).read_bytes(), name)

    def test_old_forwarders_are_gone(self):
        for name in ["_silent_run.ps1", "_启动脑Pi.ps1", "_收控制台.ps1", "_静默启动.vbs",
                     "一键启动-构建版.bat", "一键启动-构建版-隐藏.vbs",
                     "pet/start_all.bat", "pet/start_all_prod.bat", "pet/check_all.py",
                     "tools/albion_brain_watch.ps1",
                     "tts/irodori/启动Irodori后端.bat", "tts/irodori/启动TTS桥.bat"]:
            self.assertFalse((ROOT / name).exists(), name)

    # ---- 真跑一遍小东西 ---------------------------------------------------
    def test_real_silent_helper_handles_spaces_and_cmd_form(self):
        helper = ROOT / "launch/silent_run.ps1"
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
        subprocess.run(["powershell", "-NoProfile", "-File", str(helper), "-Cmd", command],
                       check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertEqual(marker.read_text().strip(), "alias")

    def test_eye_command_executes_with_spaces_and_preserves_arguments(self):
        out, rows = self.run_entry("一键启动-dev.bat")
        command = next(r["Cmd"] for r in rows if "capture_agent.py" in r["Cmd"])
        script = self.root / EYES_DIR / "capture_agent.py"
        marker = self.root / "arguments.json"
        script.write_text("import sys,json\nfrom pathlib import Path\nPath(" + repr(str(marker)) +
                          ").write_text(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        command = command.replace(str(self.root / "tts/GPT-sovits/runtime/python.exe"), sys.executable)
        subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "launch/silent_run.ps1"),
                        "-Cmd", command], check=True, capture_output=True, timeout=10)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertEqual(json.loads(marker.read_text()),
                         ["--port", "8790", "--token-file", str(self.root / EYES_TOKEN),
                          "--max-width", "0", "--bind", "127.0.0.1"])

    def test_hidden_vbs_runs_dragged_batch(self):
        vbs = ROOT / "launch/hidden.vbs"
        batch = self.root / "dragged batch.bat"
        marker = self.root / "dragged.txt"
        batch.write_bytes(('@echo off\r\necho ran>"' + str(marker) + '"\r\n').encode("gbk"))
        subprocess.run(["cscript", "//nologo", str(vbs), str(batch)], env=self.env,
                       check=True, capture_output=True, timeout=15)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.02)
        self.assertTrue(marker.exists(), "hidden.vbs 没有把拖进来的 bat 跑起来")


if __name__ == "__main__":
    unittest.main()

