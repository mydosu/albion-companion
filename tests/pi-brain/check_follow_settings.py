#!/usr/bin/env python3
"""脑跟着 magpie 写给 pi 的默认设置走 —— 解析、合并、回写三件事的自检（2026-10-30）。

不联网、不碰生产：全在临时目录里造「magpie 的 pi 默认目录」和「阿尔比恩的 agent 目录」。

验的规则（对应 bin/follow_settings.py）：
  1) 优先级：ALBION_PROVIDER/ALBION_MODEL > pi 默认设置的 defaultProvider/defaultModel > 兜底 commandcode/deepseek-v4-flash
  2) 合并 provider：只增/替换这一个键，别的 provider（commandcode 的明文 key 等）原样不动
  3) 合并时去掉模型级 api/baseUrl 覆盖，统一 provider 级 openai-completions
  4) 回写 settings.json：只改 defaultProvider/defaultModel，其它键（transport/retry/…）原样
  5) 原子写 + LF、内容没变不落盘（幂等），坏 JSON 不抛

跑：python3 tests/pi-brain/check_follow_settings.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "integrations", "pi-brain", "bin"))

import follow_settings as fs  # noqa: E402

MAGPIE_SETTINGS = {
    "defaultProvider": "magpie",
    "defaultModel": "antigravity/gemini-3.8-flash",
    "theme": "dark",
}
MAGPIE_MODELS = {
    "providers": {
        "magpie": {
            "name": "magpie",
            "baseUrl": "http://127.0.0.1:3425/v1",
            "api": "openai-completions",
            "apiKey": "magpie",
            "compat": {"sendSessionAffinityHeaders": True},
            "models": [
                {
                    "id": "antigravity/gemini-3.8-flash",
                    "name": "Gemini 3.8 Flash · Antigravity",
                    "api": "google-generative-ai",                 # 模型级覆盖：合并时要被去掉
                    "baseUrl": "http://127.0.0.1:3425/v1beta",
                    "reasoning": True,
                    "contextWindow": 1048576,
                    "maxTokens": 65536,
                },
                {"id": "commandcode/google/gemini-3.8-flash", "reasoning": True},
            ],
        }
    }
}
AGENT_SETTINGS = {
    "transport": "sse",
    "defaultProvider": "commandcode",
    "defaultModel": "deepseek/deepseek-v4-flash",
    "defaultThinkingLevel": "medium",
    "retry": {"enabled": True, "maxRetries": 2},
}
AGENT_MODELS = {
    "providers": {
        "commandcode": {
            "baseUrl": "http://127.0.0.1:8798/v1",
            "api": "openai-completions",
            "apiKey": "prod-literal-key-do-not-touch",
            "models": [{"id": "deepseek/deepseek-v4-flash"}],
        },
        "grokpool": {"baseUrl": "http://127.0.0.1:8000/v1", "api": "openai-completions", "models": []},
    }
}


def write(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def raw(path):
    with open(path, "rb") as fh:
        return fh.read()


def check(cond, why):
    if not cond:
        raise AssertionError(why)


def make_dir():
    d = tempfile.mkdtemp(prefix="follow-settings-")
    tiny = os.path.join(d, "pi-default")          # magpie 写的那份
    agent = os.path.join(d, "agent")              # 阿尔比恩自己的
    os.makedirs(tiny)
    os.makedirs(agent)
    write(os.path.join(tiny, "settings.json"), MAGPIE_SETTINGS)
    write(os.path.join(tiny, "models.json"), MAGPIE_MODELS)
    write(os.path.join(agent, "settings.json"), AGENT_SETTINGS)
    write(os.path.join(agent, "models.json"), AGENT_MODELS)
    return d, os.path.join(tiny, "settings.json"), agent


def main():
    d, settings, agent = make_dir()
    try:
        # 1) 没环境变量 ⇒ 跟 magpie 的设置走
        env = {"PI_CODING_AGENT_DIR": agent,
               "ALBION_FOLLOW_SETTINGS": settings}
        r = fs.resolve(env)
        check((r.provider, r.model) == ("magpie", "antigravity/gemini-3.8-flash"), r)
        check(r.source == "settings", r.source)
        check(r.merged is True and r.settings_written is True, r)

        models = read(os.path.join(agent, "models.json"))
        # 2) 只动 magpie 键：别的 provider 逐字节保留
        check(models["providers"]["commandcode"] == AGENT_MODELS["providers"]["commandcode"],
              "commandcode provider 被改动了")
        check(models["providers"]["grokpool"] == AGENT_MODELS["providers"]["grokpool"], "grokpool 被改动了")
        check(set(models["providers"]) == {"commandcode", "grokpool", "magpie"}, list(models["providers"]))

        # 3) 模型级 api/baseUrl 去掉，provider 级统一 openai-completions
        mag = models["providers"]["magpie"]
        check(mag["api"] == "openai-completions", mag.get("api"))
        check(mag["baseUrl"] == "http://127.0.0.1:3425/v1", mag.get("baseUrl"))
        check("api" not in mag["models"][0] and "baseUrl" not in mag["models"][0], mag["models"][0])
        check(mag["models"][0]["contextWindow"] == 1048576 and mag["models"][0]["maxTokens"] == 65536,
              "模型字段没带过来")
        check("compat" in mag, "provider 级 compat 该保留")

        # 4) 回写 settings：只改两个键，别的原样
        got = read(os.path.join(agent, "settings.json"))
        check(got["defaultProvider"] == "magpie" and got["defaultModel"] == "antigravity/gemini-3.8-flash", got)
        for k, v in AGENT_SETTINGS.items():
            if k not in ("defaultProvider", "defaultModel"):
                check(got.get(k) == v, "settings 里的 %s 被改动了" % k)

        # 5) LF、无残留 tmp、幂等
        for name in ("models.json", "settings.json"):
            data = raw(os.path.join(agent, name))
            check(b"\r\n" not in data, name + " 不是 LF")
            check(data.endswith(b"\n"), name + " 没有结尾换行")
        check(not [n for n in os.listdir(agent) if n.startswith(".") and n.endswith(".tmp")], "留下了 tmp 文件")
        before = raw(os.path.join(agent, "models.json")), raw(os.path.join(agent, "settings.json"))
        r2 = fs.resolve(env)
        check(r2.merged is False and r2.settings_written is False, "内容没变还在写盘")
        check((raw(os.path.join(agent, "models.json")), raw(os.path.join(agent, "settings.json"))) == before,
              "幂等写盘内容变了")

        # 6) 环境变量最优先（且不拿 magpie 的段去盖 commandcode）
        env2 = dict(env, ALBION_PROVIDER="commandcode", ALBION_MODEL="deepseek/deepseek-v4.1-flash")
        r3 = fs.resolve(env2)
        check((r3.provider, r3.model) == ("commandcode", "deepseek/deepseek-v4.1-flash"), r3)
        check(r3.source == "env", r3.source)
        check(r3.merged is False, "env 指定的 provider 不在来源里，不该合并")
        check(read(os.path.join(agent, "models.json"))["providers"]["commandcode"]
              == AGENT_MODELS["providers"]["commandcode"], "env 模式下 commandcode 被改动了")
        check(read(os.path.join(agent, "settings.json"))["defaultProvider"] == "commandcode",
              "env 模式没回写 settings")

        # 7) 设置文件不在 / 坏了 ⇒ 兜底，不抛
        missing = dict(env, ALBION_FOLLOW_SETTINGS=os.path.join(d, "nope", "settings.json"))
        r4 = fs.resolve(missing)
        check((r4.provider, r4.model) == (fs.DEFAULT_PROVIDER, fs.DEFAULT_MODEL), r4)
        check(r4.source == "default", r4.source)
        bad = os.path.join(d, "pi-default", "broken.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        r5 = fs.resolve(dict(env, ALBION_FOLLOW_SETTINGS=bad))
        check(r5.source == "default", r5.source)

        # 8) defaultModel 里带 provider 前缀也要认
        pref = os.path.join(d, "pi-default", "prefixed.json")
        write(pref, {"defaultProvider": "magpie", "defaultModel": "magpie/antigravity/gemini-3.8-flash"})
        r6 = fs.resolve(dict(env, ALBION_FOLLOW_SETTINGS=pref))
        check(r6.model == "antigravity/gemini-3.8-flash", r6.model)
        print("FOLLOW SETTINGS CHECK OK (8 cases)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
