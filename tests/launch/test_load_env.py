"""launch/load_env.bat 的规则测试：真环境变量优先、注释/空行跳过、值原样、缺文件静默。

跑法（Windows）：
  python -m unittest discover -s tests/launch -p 'test_load_env.py' -v
  python -m pytest tests/launch/test_load_env.py -q
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOAD_ENV = ROOT / "launch" / "load_env.bat"

SAMPLE = (
    "# 注释行不算变量\n"
    "\n"
    "ALBION_TEST_ONE=one\n"
    "ALBION_TEST_TWO=two words\n"
    "ALBION_TEST_EQ=a=b\n"
    "ALBION_TEST_KEEP=from-file\n"
    "#ALBION_TEST_COMMENTED=no\n"
    "ALBION_TEST_BANG=wow!bang\n"
    "TTS_TEST_PORT=9881\n"
)


@unittest.skipUnless(os.name == "nt", "Windows cmd required")
class LoadEnv(unittest.TestCase):
    """在临时目录里造一个假仓库：launch/load_env.bat + config/local.env。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="albion loadenv ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "launch").mkdir()
        (self.root / "config").mkdir()
        shutil.copyfile(LOAD_ENV, self.root / "launch" / "load_env.bat")

    def write_env(self, text, newline="\r\n"):
        (self.root / "config" / "local.env").write_bytes(text.replace("\n", newline).encode("utf-8"))

    def load(self, extra_env=None):
        """call load_env 之后用 `set` 打印 ALBION_TEST*/TTS_TEST*，返回 dict。"""
        probe = self.root / "probe.bat"
        probe.write_bytes(
            ("@echo off\r\n"
             'call "%~dp0launch\\load_env.bat"\r\n'
             "set ALBION_TEST\r\n"
             "set TTS_TEST\r\n"
             "exit /b 0\r\n").encode("gbk"))
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("ALBION_TEST") or k.startswith("TTS_TEST"))}
        env.update(extra_env or {})
        result = subprocess.run([env.get("ComSpec", "cmd.exe"), "/d", "/c", "call", str(probe)],
                                env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", "replace"))
        got = {}
        for line in result.stdout.decode("gbk", "replace").splitlines():
            line = line.strip()
            if line.startswith(("ALBION_TEST", "TTS_TEST")) and "=" in line:
                name, value = line.split("=", 1)
                got[name] = value
        return got

    def test_values_comments_and_blank_lines(self):
        self.write_env(SAMPLE)
        got = self.load()
        self.assertEqual(got["ALBION_TEST_ONE"], "one")
        self.assertEqual(got["ALBION_TEST_TWO"], "two words")
        self.assertEqual(got["ALBION_TEST_EQ"], "a=b")           # 只按第一个 = 切
        self.assertEqual(got["ALBION_TEST_BANG"], "wow!bang")    # ! 不被吃掉
        self.assertEqual(got["TTS_TEST_PORT"], "9881")
        self.assertNotIn("ALBION_TEST_COMMENTED", got)           # 注释行跳过

    def test_real_environment_wins(self):
        self.write_env("ALBION_TEST_KEEP=from-file\n")
        self.assertEqual(self.load()["ALBION_TEST_KEEP"], "from-file")
        self.assertEqual(self.load({"ALBION_TEST_KEEP": "from-env"})["ALBION_TEST_KEEP"], "from-env")

    def test_missing_file_is_silent(self):
        self.assertEqual(self.load(), {})                        # 没有 local.env：不报错、不设变量

    def test_lf_only_file_works(self):
        self.write_env("ALBION_TEST_ONE=one\n", newline="\n")
        self.assertEqual(self.load()["ALBION_TEST_ONE"], "one")


if __name__ == "__main__":
    unittest.main()
