# -*- coding: utf-8 -*-
"""`_cc_keys()` 的退路开关：`TTS_TRANSLATE_CC_FALLBACK=0` ⇒ 不读 key#1/#2、不打印；不设 ⇒ 老行为不变。

2026-11-28 指挥官定：翻译统一走 magpie `group/ds`，Command Code 直连退路取消。
关掉时最要紧的是**不报错**：`_fallback_key()`/`_fallback_key2()` 读不到 key 会各打一行
"读失败"，而 `_hedge(enabled=lambda: bool(_cc_keys()))` 在 `ALBION_TRANSLATE_HEDGE_S=0`
下每句都会调一次 ⇒ key 读不到就是每句两行刷屏。全部离线。
"""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tts/irodori"), str(ROOT / "tts/stream")]
import bridge


class CcFallbackSwitch(unittest.TestCase):
    def setUp(self):
        bridge._FALLBACK_KEY.clear()
        bridge._FALLBACK_KEY2.clear()

    def test_switch_off_returns_no_keys_and_stays_quiet(self):
        """开关为 0：空列表、不读文件、一行都不打。"""
        with patch.dict(os.environ, {"TTS_TRANSLATE_CC_FALLBACK": "0"}, clear=False), \
                patch.object(bridge, "_fallback_key") as k1, \
                patch.object(bridge, "_fallback_key2") as k2, \
                patch("builtins.print") as printed:
            self.assertEqual(bridge._cc_keys(), [])
            k1.assert_not_called()       # 连读都不读（读失败会打日志）
            k2.assert_not_called()
            printed.assert_not_called()

    def test_switch_default_keeps_old_behaviour(self):
        """不设 / =1：照旧读两把 key（顺序 #1 → #2、去重）。"""
        for value in (None, "1", "true"):
            env = {"TTS_TRANSLATE_FALLBACK_KEY": "k1"} if value is None else \
                {"TTS_TRANSLATE_CC_FALLBACK": value, "TTS_TRANSLATE_FALLBACK_KEY": "k1"}
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(bridge, "_fallback_key", return_value="k1"), \
                    patch.object(bridge, "_fallback_key2", return_value="k2"):
                self.assertEqual(bridge._cc_keys(), ["k1", "k2"], value)

    def test_switch_off_does_not_change_first_leg(self):
        """首腿（ask_brain → magpie）不受影响：开关关掉也照样译，只是不再有退路。"""
        with patch.dict(os.environ, {"TTS_TRANSLATE_CC_FALLBACK": "0"}, clear=False), \
                patch.object(bridge, "brain_route", return_value={"provider": "magpie", "model": "group/ds"}), \
                patch.object(bridge, "_ask_magpie", return_value="おはよう。") as magpie:
            self.assertEqual(bridge.ask_brain("早啊。"), "おはよう。")
            magpie.assert_called_once()
            self.assertEqual(bridge._cc_keys(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)