# -*- coding: utf-8 -*-
"""Irodori-TTS 后端服务（v2）：调用方只发文本，**风格全由后端自动决定**。

设计要点
1) 自动风格：对每一行/句判断情绪（emoji → 拟声词/暧昧词 → 关键词规则），自动生成日语 caption
   并设置对应的 cfg-scale-caption（风格强度）。调用方不需要懂 caption。
2) 常驻模型：monkeypatch 掉 InferenceRuntime.from_key 做缓存 → 模型只加载一次，
   每句仍走官方 infer.py 完整逻辑（LoRA / 参考音 / caption 都不变）。单句约 2-5 秒。
3) 新旧合成端点共用 render_queue；规划由 scene_plan 提供，句间保留 0.10s 静音。
4) 兼容 GPT-SoVITS /tts 协议（voice 名 albion-irodori），也支持情感覆盖字段 style_override。
"""
import glob
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))  # 相对本文件，搬到哪都能用
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

CKPT = os.path.join(ROOT, "checkpoints", "model.safetensors")
AUDIO_ROOT = r"D:/AI/Wife/tts/GPT-sovits/audio"
SHIPS = ("albion", "taihou", "shinano", "brest", "jinshi")
DEFAULT_SHIP = "albion"


LORA_OFF = {}   # 2026-09-14：大凤改用"干净 53 条 + 低步数 LoRA@200"（盲听定稿 B 方案）→ 现在五船全挂 LoRA

# 2026-09-13：四船改用条件匹配版 LoRA（emo 7档 caption + speaker_id 参考 latent），500 步
# 金狮/大凤例外（盲听定稿）：干净子集 + 仅 200 步 —— 低步数漂移小，既有自然停顿、又不带电音/沙哑
LORA_NAME = {"albion": "albion_lora_emo_spk", "shinano": "shinano_lora_emo_spk",
             "brest": "brest_lora_emo_spk", "jinshi": "jinshi_lora_clean200",
             "taihou": "taihou_lora_clean200"}

# 2026-09-14：大凤保留 joint 引导 —— 去掉后整篇实测"发糙/发飘"（用户判定），加回。
# 大凤定稿 = 干净 53 条嵌入 + 干净 LoRA@200 + joint 统一 cfg 4.5
JOINT_CFG = {"taihou": 4.5}


# 2026-09-13：金狮/大凤改用手挑干净子集重训的嵌入（电音/沙哑消失；金狮 32 条、大凤 53 条）
INV_NAME = {"jinshi": "jinshi_inv_clean", "taihou": "taihou_inv_clean"}


# ---------------- 船只资产配置：ships.json 覆盖 > 代码内默认 ----------------
# 加/改一条船：编辑仓库根的 ships.json 即可，不用改代码、不用重启（按 mtime 热载）。
# 字段：lora(LoRA 目录名，null=不挂) / inv(嵌入目录名，null=按 {ship}_inv 约定找)
#       joint_cfg(统一 cfg，null=用族 cfg) / lora_off(强制不挂，默认 false)
SHIPS_JSON = os.path.join(ROOT, "ships.json")
_ships_cfg = (None, {})                 # (mtime, 文件内容)


def _load_ships_json():
    """读 ships.json（改了自动生效）；文件不存在 → 空表，全部走代码内默认。"""
    global _ships_cfg
    try:
        mt = os.path.getmtime(SHIPS_JSON)
    except OSError:
        mt = None
    if mt != _ships_cfg[0]:
        cfg = {}
        if mt is not None:
            try:
                with open(SHIPS_JSON, encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                print(f"[irodori] ships.json 已载入（覆盖 {len(cfg)} 船）", flush=True)
            except Exception as e:      # 编辑到一半/写坏：用上次的配置，别把服务带崩
                print(f"[irodori] ships.json 读取失败（沿用上次配置）: {e}", flush=True)
                _ships_cfg = (None, _ships_cfg[1])
                return _ships_cfg[1]
        _ships_cfg = (mt, cfg)
    return _ships_cfg[1]


def all_ships():
    """默认五船 + ships.json 里新增的船（默认在前）"""
    return list(SHIPS) + [k for k in _load_ships_json() if k not in SHIPS and not k.startswith("_")]


def ship_cfg(ship: str) -> dict:
    """该船生效的资产配置（ships.json 覆盖 > 代码内默认）"""
    o = _load_ships_json().get(ship) or {}
    return {"lora": o.get("lora", LORA_NAME.get(ship, f"{ship}_lora_cap")),
            "inv": o.get("inv", INV_NAME.get(ship)),
            "joint_cfg": o.get("joint_cfg", JOINT_CFG.get(ship)),
            "lora_off": o.get("lora_off", ship in LORA_OFF)}


def ship_assets(ship: str):
    """该船的 (LoRA 适配器目录, Speaker-Inversion 嵌入, 兜底参考音列表)——缺则 None/空。
    单进程常驻 1 份模型，换船只换 LoRA/嵌入，不重载模型。配置来源见 ship_cfg()。"""
    c = ship_cfg(ship)
    lora = os.path.join(ROOT, "outputs", c["lora"], "checkpoint_final") if c["lora"] else None
    if lora and (c["lora_off"] or not os.path.isdir(lora)):
        if c["lora_off"]:
            print(f"[irodori/{ship}] ships.json 指定不挂 LoRA", flush=True)
        lora = None
    emb = None
    for d in [d for d in (c["inv"], f"{ship}_inv", f"{ship}_inv2", f"{ship}_inv3") if d]:
        dd = os.path.join(ROOT, "outputs", d)
        p = os.path.join(dd, "checkpoint_0000500.speaker.safetensors")   # 定稿：500 步
        if os.path.isfile(p):
            emb = p; break
        found = sorted(glob.glob(os.path.join(dd, "**", "*.speaker.safetensors"), recursive=True))
        if found:
            emb = found[-1]; break
    au = os.path.join(AUDIO_ROOT, ship)
    pref = [os.path.join(au, f) for f in ("004.wav", "068.wav", "072.wav", "003.wav")]
    refs = [r for r in pref if os.path.isfile(r)]
    if not refs:
        refs = sorted(glob.glob(os.path.join(au, "*.wav")))[:4]
    return lora, emb, refs

# ---------------- 常驻模型：缓存 InferenceRuntime ----------------
import infer as infer_mod

_cache = {}
_orig_from_key = infer_mod.InferenceRuntime.from_key


def _cached_from_key(key):
    k = (key.checkpoint, key.model_device, str(key.model_precision), str(key.codec_precision))
    if k not in _cache:
        print(f"[irodori] 首次加载模型（{os.path.basename(key.checkpoint)}）...", flush=True)
        _cache[k] = _orig_from_key(key)
        print("[irodori] 模型已常驻，后续请求不再重载", flush=True)
    return _cache[k]


infer_mod.InferenceRuntime.from_key = staticmethod(_cached_from_key)


def _run_infer(text, caption, cfg_caption, out_wav, ship=DEFAULT_SHIP, seed=None):
    """在进程内以 CLI 等价参数调用 infer.py（模型已缓存，LoRA/嵌入按船切换）。

    `seed=None` 时**不传** `--seed` ⇒ 保持生产原有的随机采样（astra-3 §4.1：
    客户端多写一个 seed 不等于生效，要记录实际用了什么）。
    """
    lora, emb, refs = ship_assets(ship)
    argv = [os.path.join(ROOT, "infer.py"),          # argv[0] 必须是"程序名"，不能是 python 路径
            "--checkpoint", CKPT,
            "--model-precision", "bf16", "--codec-precision", "bf16",
            "--text", text, "--caption", caption,
            "--cfg-scale-caption", str(cfg_caption),
            "--output-wav", out_wav, "--no-show-timings",
            # 2026-09-20：采样步数做成可调（默认仍 40，与作者一致）。
            # 步数直接决定合成耗时（实测 40 步 ≈ 2.1s/句，是全链路的大头）。
            # 用环境变量 IRODORI_STEPS 覆盖，方便 A/B 听感对比。
            "--num-steps", os.environ.get("IRODORI_STEPS", "40")]
    if seed is not None:
        argv += ["--seed", str(int(seed))]        # 只在显式给了配对种子时出现（生产默认不带）
    if emb:
        # B 方案（用户定稿）：Speaker Inversion 嵌入定音色
        argv += ["--ref-embed", emb]
    elif refs:
        argv += ["--ref-wavs", *refs]
    if lora:
        argv += ["--lora-adapter", lora]             # 情感/风格微调照旧生效
    joint = ship_cfg(ship)["joint_cfg"]
    if joint:
        # 统一 cfg + joint 引导（覆盖 --cfg-scale-caption）
        argv += ["--cfg-scale", str(joint), "--cfg-guidance-mode", "joint"]
        print(f"[irodori/{ship}] joint 引导生效 cfg={joint}", flush=True)
    old = sys.argv
    sys.argv = argv
    try:
        infer_mod.main()
    finally:
        sys.argv = old
        _release_vram()
    return out_wav


_VRAM_N = 0


def _release_vram():
    """每句合成完把 PyTorch 缓着没用的显存还给系统。

    2026-10-14 实测：8GB 卡上 Irodori 常驻 ~7GB（模型 bf16 只占 ~1.5GB，大头是缓存分配器留着的
    中间张量），再加 Sunshine / 桌面 / 壳就超过 8GB，Windows 把超出部分挤到共享内存，
    单句合成从 7–15s 拖到 30–60s、超时吞句。句间还回去，别的程序就挤不掉它的工作集。
    """
    global _VRAM_N
    try:
        import torch
        if not torch.cuda.is_available():
            return
        peak = torch.cuda.max_memory_reserved() / 2**20
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        _VRAM_N += 1
        if _VRAM_N <= 3 or _VRAM_N % 20 == 0:   # 头几句与每 20 句记一次，日志不刷屏
            print(f"[irodori/vram] 本句峰值 reserved={peak:.0f}MB 还回后 "
                  f"allocated={torch.cuda.memory_allocated() / 2**20:.0f}MB "
                  f"reserved={torch.cuda.memory_reserved() / 2**20:.0f}MB", flush=True)
    except Exception:   # noqa: BLE001 — 记账失败不能让合成失败
        pass


from fastapi import FastAPI
import time
import uvicorn

PORT = 9889
app = FastAPI()


@app.get("/health")
def health():
    out = {}
    for s in all_ships():
        lora, emb, refs = ship_assets(s)
        out[s] = {"lora": bool(lora), "ref_embed": bool(emb), "refs": len(refs),
                  "joint_cfg": ship_cfg(s)["joint_cfg"]}
    return {"ok": True, "ships": out}


# ── token 预算守卫（astra-3 §2.3.4）：只数 token，不动任何合成参数 ──
_tok_cache = {}


def _token_count(text: str) -> int:
    """正文 token 数（含 BOS）。懒加载 checkpoint 旁的本地 tokenizer（离线、CPU、只读 JSON 头）。

    只为长度守卫：模型对超预算文本是**静默右截断**（尾部整句丢），渲染队列宁可显式失败。
    """
    if "count" not in _tok_cache:
        import struct
        from irodori_tts.tokenizer import PretrainedTextTokenizer
        from irodori_tts.text_normalization import normalize_text
        with open(CKPT, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            cfg = json.loads(json.loads(f.read(n))["__metadata__"]["config_json"])
        root = os.path.dirname(CKPT)
        tokdir = next(p for p in (os.path.join(root, "tokenizer"),
                                  os.path.join(os.path.dirname(root), "tokenizer"))
                      if os.path.exists(os.path.join(p, "tokenizer_config.json")))
        tok = PretrainedTextTokenizer.from_pretrained(
            str(tokdir), add_bos=bool(cfg.get("text_add_bos")), local_files_only=True)

        def _c(t):
            _ids, mask = tok.batch_encode([normalize_text(t).strip()], max_length=10 ** 6)
            return int(mask[0].sum())
        _tok_cache["count"] = _c
    return _tok_cache["count"](text)


# ── v2 接线（P8）：新 /v2/*；旧 /tts、/tts_stream 先摘后挂，排同一条 GPU 队列 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_queue as _rq, scene_plan as _sp, serve_v2 as _sv2         # noqa: E402
_queue = _rq.RenderQueue(
    synth=lambda unit, out_wav: _rq.run_infer_synth(unit, out_wav, _run_infer, DEFAULT_SHIP))
_rq.install(app, _queue, all_ships=all_ships())
_rq.TOKEN_COUNT = _token_count     # 真 tokenizer 数：超预算显式失败，不让模型静默截断
_sp.install(app)          # /v2/plan —— 漏了 S 侧第一步就 404（P7 真机实测踩到）
_sv2.install(app, _queue, all_ships=all_ships(), default_ship=DEFAULT_SHIP)


def _warmup():
    """启动监听前走生产规划与合成腿，预热 WAV 用完即删。"""
    segs, plan, scenes, styles = _sv2.legacy_plan("こんにちは。")
    with tempfile.TemporaryDirectory(prefix="irodori_warmup_") as directory:
        for i, seg in enumerate(segs):
            unit = _sv2._seg_unit(i, seg, plan, styles, scenes, DEFAULT_SHIP)
            _rq.run_infer_synth(unit, os.path.join(directory, "seg.wav"), _run_infer, DEFAULT_SHIP)


if __name__ == "__main__":
    print(f"[irodori] 船: {', '.join(all_ships())}  (单进程常驻模型，换船只换 LoRA/嵌入)", flush=True)
    for s in all_ships():
        lora, emb, refs = ship_assets(s)
        print(f"  {s:8s} lora={'有' if lora else '无'} embed={'有' if emb else '无'} 兜底refs={len(refs)}", flush=True)
    # 冷启动预热：模型是懒加载的（`_cached_from_key`），不预热的话**第一个用户单元**要独自承担
    # 模型加载 + 首推理 —— 实测首单元合成 22.0s，而预热后的单句请求只要 2.2s，首单元必然撞
    # S 侧 20s render 请求超时（codex 从两侧日志定的冷启动线索）。
    # **不要**在这里写"后续段都是 1.9~3.0s"之类的范围：同一会话里也有 10/36 次 >10s 的慢段，
    # 写死范围会误导排障。预热吃掉的是**加载**，不是慢段。
    # ponytail: 用真实合成而不是空跑 —— 只有真跑才触发同一套加载路径；不碰任何模型质量参数。
    _t_warm = time.monotonic()
    try:
        print("[irodori] 预热中（加载模型 + 首推理）…", flush=True)
        _warmup()
        print("[irodori] 预热完成 %.1fs —— 此后首个请求不再承担模型加载"
              % (time.monotonic() - _t_warm), flush=True)
    except Exception as e:
        print("[irodori] 预热失败: %s: %s" % (type(e).__name__, str(e)[:160]), flush=True)
        # **必须**让本次启动失败：否则端口可用会被当成模型就绪，而实际上第一个请求仍要承担加载。
        # 「服务 up 即已就绪」这句承诺只有在预热真的成功时才成立（codex 复核）。
        raise
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
