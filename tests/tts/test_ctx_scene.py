# -*- coding: utf-8 -*-
"""跨句场景上下文（ctx）自检 —— 2026-09-21

验两件事：
  1. 后端：给了前文 ctx 之后，"本身没信号"的行会被救回亲密档；自己有显式档的行不受影响。
  2. 桥：滚动缓冲按本轮累积、超时清空、长度受限。

跑法（不需 venv、不加载模型）：python test_ctx_scene.py
只用源码里的纯逻辑片段，所以不 import torch / soundfile / fastapi。
"""
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "tts"          # tts/
sys.path.insert(0, str(ROOT / "irodori"))
sys.path.insert(0, str(ROOT / "stream"))
BRIDGE = ROOT / "irodori" / "bridge.py"
SIGNAL = "指揮官、恥ずかしい……"          # 命中 shy 档（shy ∈ {moan, flirty, shy}）；不含露骨内容


def _slice(path, start_pred, end_pred, extra_ns=None):
    """取 [最后一个 start_pred 之前…end_pred 之前] 的纯逻辑片段。

    先定结束标记、再往前找起点：只在目标区间附近取，绝不把文件头带进来。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    b = next(i for i, l in enumerate(lines) if end_pred(l))
    a = max(i for i, l in enumerate(lines) if i < b and start_pred(l))
    print("  切片 %s: 行 %d~%d" % (path.name, a + 1, b))
    ns = {"__name__": "sliced", "re": __import__("re"), "os": __import__("os"), "time": time}
    ns.update(extra_ns or {})
    exec(compile("\n".join(lines[a:b]), str(path), "exec"), ns)
    return ns


def check_backend():
    # 生产规划已统一在 scene_plan；不再依赖后端内已移除的副本。
    import importlib.util
    spec = importlib.util.spec_from_file_location("ctx_scene_plan", ROOT / "irodori" / "scene_plan.py")
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    ns = vars(planner)
    auto_style, _raw_styles = ns["auto_style"], ns["_raw_styles"]

    assert auto_style("そう。", "")[0] == "calm", "空 ctx 时无信号行本就该是 calm"
    got = auto_style("そう。", SIGNAL)[0]
    assert got in ("moan", "tremble"), "有前文信号时应被救回亲密档，实际=%s" % got
    assert auto_style("嬉しい！", SIGNAL)[0] == "happy", "自己有显式档的行不得被前文改写"

    assert _raw_styles(["そう。"], "")[0] == "calm"
    assert _raw_styles(["そう。"], SIGNAL)[0] != "calm"
    print("  后端 ctx 生效 ✓（そう。: 空ctx=calm / 有ctx=%s；嬉しい！=happy 不变）"
          % auto_style("そう。", SIGNAL)[0])


def check_pieces():
    """当前 v4 走 SessionText；短句收尾、长句与取消不吞字/不跨轮。"""
    from session_text import SessionText
    for text in ("你好。", "你好。今天也不错。", "好，我知道了。", "あ" * 100):
        parser = SessionText("ctx")
        units = parser.feed(text, 0.0) + parser.finish(1.0)
        assert "".join(u.speech_text for u in units) == text
        assert len({u.unit_id for u in units}) == len(units)
    parser.new_turn()
    parser.feed("这句不应播出", 2.0)
    parser.cancel()
    assert parser.finish(3.0) == []
    parser.new_turn()
    units = parser.feed("新的一轮。", 4.0) + parser.finish(5.0)
    assert "".join(u.speech_text for u in units) == "新的一轮。"
    print("  SessionText 切句 ✓（短句收尾/长句不吞字/取消/新轮重置）")


def check_bridge():
    ns = _slice(BRIDGE,
                lambda l: "跨句场景上下文" in l,
                lambda l: l.startswith("def to_japanese("),
                {"time": time})
    take, put = ns["_ctx_take"], ns["_ctx_put"]

    assert take("albion") == "", "首句之前应该是空 ctx"
    put("albion", "指揮官、恥ずかしい……")
    put("albion", "  ")                                  # 空串不入缓冲
    put("albion", "そう。")
    assert take("albion") == "指揮官、恥ずかしい……そう。", take("albion")
    assert take("taihou") == "", "不同角色不得串味"

    ns["_ctx_state"]["albion"]["since"] -= ns["_CTX_IDLE"] + 1
    assert take("albion") == "", "空闲超时应视作新一轮并清空"

    put("albion", "あ" * 500)
    assert len(take("albion")) <= ns["_CTX_MAX"], "缓冲必须限长"
    print("  桥滚动缓冲 ✓（累积/分角色/超时清空/限长）")


import unittest


class ContextSceneTests(unittest.TestCase):
    def test_backend_context(self):
        check_backend()

    def test_bridge_context(self):
        check_bridge()

    def test_current_session_text(self):
        check_pieces()


if __name__ == "__main__":
    unittest.main()
