#!/usr/bin/env python3
"""跟着 magpie 写给 pi 的默认设置走：脑用什么模型不再写死在 server.py 里。

magpie 的「Pi · WSL Ubuntu」那一格写的就是 pi 的默认 agent 目录（/root/.pi/agent/）：
  settings.json  {"defaultProvider": "magpie", "defaultModel": "antigravity/gemini-3.8-flash"}
  models.json    providers.magpie（baseUrl http://127.0.0.1:3425/v1，一百多条模型）
阿尔比恩的脑有自己的 agent 目录（PI_CODING_AGENT_DIR=/root/albion-pi/agent），读不到那份
provider，所以这里做三件事：

  1. 解析要用的 (provider, model)：ALBION_PROVIDER / ALBION_MODEL > pi 默认设置 > commandcode/deepseek-v4-flash
  2. 把来源 models.json 里这个 provider 的整段搬进阿尔比恩的 agent/models.json（只增/替换这一个键）
  3. 把最终在用的 provider/model 写回阿尔比恩的 agent/settings.json（只动这两个键）

第 3 步是给「跟着脑走」的腿用的：cli.sh、delegate.ts 的子进程、TTS 翻译桥都从同一个
agent 目录的 settings.json 读脑现在用的模型，不必各自写死，也不必再问一遍 magpie。

合并时去掉每个模型条目上的 `api` / `baseUrl` 覆盖，统一成 provider 级的 openai-completions：
整理器 albion-guard 的 context 钩子和「重说」只发 OpenAI 形状的 payload（/chat/completions），
而 magpie 给 antigravity/* 写的覆盖是 google-generative-ai + /v1beta；统一到 OpenAI 形状后
聊天与工具调用实测都 200（2026-10-29 的 RECON）。

只用标准库；不打印任何 key。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import NamedTuple

DEFAULT_PROVIDER = "commandcode"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
# pi 默认 agent 目录里的设置（magpie 写的那一份）
DEFAULT_SETTINGS = "/root/.pi/agent/settings.json"
# 来源 provider 没写 baseUrl 时的兜底：magpie 自己
MAGPIE_BASE = "http://127.0.0.1:3425/v1"
OPENAI_API = "openai-completions"


class Route(NamedTuple):
    provider: str
    model: str
    source: str          # env / settings / default
    merged: bool         # 这次有没有改写 agent/models.json
    settings_written: bool   # 这次有没有改写 agent/settings.json

    @property
    def model_arg(self):
        """pi 的 --model 参数：provider/model。"""
        return "%s/%s" % (self.provider, self.model)


def settings_path(env=None):
    env = os.environ if env is None else env
    return env.get("ALBION_FOLLOW_SETTINGS") or DEFAULT_SETTINGS


def source_models_path(settings_file=None, env=None):
    """来源 models.json：默认跟 settings.json 同一个目录（pi 就是这么放的）。"""
    env = os.environ if env is None else env
    override = env.get("ALBION_FOLLOW_MODELS")
    if override:
        return override
    return os.path.join(os.path.dirname(settings_file or settings_path(env)), "models.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path, data):
    """同目录 tmp + os.replace，LF 结尾，尽量保留原文件权限。"""
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_defaults(settings_file):
    """pi 默认设置里的 (provider, model)；缺一个或读不出就返回 None。"""
    data = _read_json(settings_file)
    if not data:
        return None
    provider = data.get("defaultProvider")
    model = data.get("defaultModel")
    if not isinstance(provider, str) or not provider.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    provider, model = provider.strip(), model.strip()
    prefix = provider + "/"
    if model.startswith(prefix):      # 有的写法把 provider 也写进 model 里
        model = model[len(prefix):]
    return provider, model


def provider_block(models_file, provider):
    """从来源 models.json 取 provider 整段，去掉模型级的 api/baseUrl 覆盖。"""
    data = _read_json(models_file)
    if not data:
        return None
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return None
    block = providers.get(provider)
    if not isinstance(block, dict):
        return None
    block = json.loads(json.dumps(block))          # 深拷贝，别改到来源
    block["api"] = OPENAI_API
    if not block.get("baseUrl"):
        block["baseUrl"] = MAGPIE_BASE
    models = block.get("models")
    if isinstance(models, list):
        cleaned = []
        for entry in models:
            if isinstance(entry, dict):
                entry = dict(entry)
                entry.pop("api", None)
                entry.pop("baseUrl", None)
            cleaned.append(entry)
        block["models"] = cleaned
    return block


def merge_provider(agent_models_file, provider, block):
    """只增/替换 agent/models.json 里这一个 provider 键；内容没变就不落盘。"""
    data = _read_json(agent_models_file) or {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        data["providers"] = providers
    if providers.get(provider) == block:
        return False
    providers[provider] = block
    _atomic_write_json(agent_models_file, data)
    return True


def write_settings_defaults(settings_file, provider, model):
    """把脑实际在用的 provider/model 写回 agent/settings.json；只动这两个键。"""
    data = _read_json(settings_file)
    if data is None:
        data = {}
    if data.get("defaultProvider") == provider and data.get("defaultModel") == model:
        return False
    data["defaultProvider"] = provider
    data["defaultModel"] = model
    _atomic_write_json(settings_file, data)
    return True


def resolve(env=None, settings_file=None, source_models=None, agent_dir=None):
    """算出这一轮该用哪个 (provider, model)，顺手把 provider 段和 settings 默认值同步好。"""
    env = os.environ if env is None else env
    settings_file = settings_file or settings_path(env)
    source_models = source_models or source_models_path(settings_file, env)
    agent_dir = agent_dir or env.get("PI_CODING_AGENT_DIR") or "/root/albion-pi/agent"

    provider = model = None
    source = "default"
    defaults = read_defaults(settings_file)
    if defaults:
        provider, model = defaults
        source = "settings"
    if not provider:
        provider, model = DEFAULT_PROVIDER, DEFAULT_MODEL
    if env.get("ALBION_PROVIDER", "").strip():
        provider = env["ALBION_PROVIDER"].strip()
        source = "env"
    if env.get("ALBION_MODEL", "").strip():
        model = env["ALBION_MODEL"].strip()
        source = "env"
    prefix = provider + "/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    # 来源里没有这个 provider（比如手写 ALBION_PROVIDER=grokpool）就什么都不动，pi 用现成的段。
    merged = False
    block = provider_block(source_models, provider)
    if block:
        merged = merge_provider(os.path.join(agent_dir, "models.json"), provider, block)

    written = write_settings_defaults(os.path.join(agent_dir, "settings.json"), provider, model)
    return Route(provider=provider, model=model, source=source, merged=merged, settings_written=written)
