# -*- coding: utf-8 -*-
"""阿尔比恩的耳朵：OpenAI 兼容的 /v1/audio/transcriptions（faster-whisper）。

用 GSV runtime 的 python 跑（它自带 faster_whisper + torch + CUDA）：
  D:/AI/Wife/tts/GPT-sovits/runtime/python.exe whisper_serve.py
监听 127.0.0.1:9882。AIRI 里选 openai-audio-transcription，BaseURL 填 http://127.0.0.1:9882/v1。

引擎（ALBION_STT_ENGINE）：
  paraformer  默认。中文专用 paraformer-zh + fsmn-vad + ct-punc，跑 **CPU**，不吃显存；
              9 秒语音约 0.6s。只有中文输入时用它。
  whisper     faster-whisper（多语种，CUDA ~3G 显存）。要听日语/英文时切它。

环境变量：
  ALBION_STT_ENGINE  paraformer | whisper（默认 paraformer）
  ALBION_STT_MODEL   whisper 引擎的模型路径/名（默认 small；本机用 models/faster-whisper-large-v3）
  ALBION_STT_DEVICE  auto|cuda|cpu（默认 auto：paraformer 走 cpu，whisper 有 CUDA 就 float16）
  ALBION_FUNASR_ROOT paraformer 三件套所在目录（默认 models/modelscope/iic）
  ALBION_STT_PRELOAD 默认 1：启动时后台线程预热模型（否则第一句要等 ~6-8s）
  ALBION_STT_PORT    默认 9882
"""
import io
import os
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

# 引擎：whisper（多语种，吃 ~3G 显存）| paraformer（中文专用，CPU 就够快，不吃显存）
ENGINE = os.environ.get('ALBION_STT_ENGINE', 'paraformer').lower()   # 本机只有中文输入 → 默认中文专用小模型
MODEL_NAME = os.environ.get('ALBION_STT_MODEL', 'small')
DEVICE = os.environ.get('ALBION_STT_DEVICE', 'auto')
PORT = int(os.environ.get('ALBION_STT_PORT', '9882'))
# paraformer 三件套（本地已下好，见 models/modelscope/iic/）
_FUNASR_ROOT = os.environ.get('ALBION_FUNASR_ROOT', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'modelscope', 'iic'))
_FUNASR_MODEL = os.path.join(_FUNASR_ROOT, 'speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch')
_FUNASR_VAD = os.path.join(_FUNASR_ROOT, 'speech_fsmn_vad_zh-cn-16k-common-pytorch')
_FUNASR_PUNC = os.path.join(_FUNASR_ROOT, 'punc_ct-transformer_zh-cn-common-vocab272727-pytorch')

app = FastAPI(title='albion-ear')
# AIRI 前端从 http://localhost:5173 调本机耳朵 → 必须放行跨域（本地开发服务，全放开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)
_model = None
_model_lock = threading.Lock()   # 预热线程和首个请求不能各加载一份
_load_error = None


def get_model():
    """加载失败会抛出并记进 _load_error；下次调用再重试。"""
    global _model, _load_error
    with _model_lock:
        if _model is None:
            try:
                _model = _load_model()
                _load_error = None
            except Exception as e:
                _load_error = f'{type(e).__name__}: {str(e)[:200]}'
                print(f'[ear] 模型加载失败：{_load_error}', flush=True)
                raise
    return _model


def _load_model():
    t0 = time.time()
    if ENGINE == 'paraformer':
        from funasr import AutoModel
        model = AutoModel(
            model=_FUNASR_MODEL,
            vad_model=_FUNASR_VAD if os.path.isdir(_FUNASR_VAD) else None,
            punc_model=_FUNASR_PUNC if os.path.isdir(_FUNASR_PUNC) else None,
            device='cpu' if DEVICE in ('auto', 'cpu') else DEVICE,
            disable_update=True, disable_pbar=True,
        )
        print(f'[ear] paraformer 三件套就绪 device={DEVICE} 用时 {time.time() - t0:.1f}s', flush=True)
        return model
    from faster_whisper import WhisperModel
    dev, ct = DEVICE, 'int8'
    if dev == 'auto':
        try:
            import ctranslate2
            dev = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
        except Exception:
            dev = 'cpu'
    if dev == 'cuda':
        ct = 'float16'
    model = WhisperModel(MODEL_NAME, device=dev, compute_type=ct)
    print(f'[ear] 模型 {MODEL_NAME} 就绪 device={dev} compute={ct} 用时 {time.time() - t0:.1f}s', flush=True)
    return model


@app.get('/health')
def health():
    return {'ok': True, 'engine': ENGINE, 'model': _FUNASR_MODEL if ENGINE == 'paraformer' else MODEL_NAME,
            'loaded': _model is not None, 'error': _load_error}


@app.get('/v1/models')
def models():
    """AIRI 的 provider 校验会拉这个表；没有它，语音识别会被判成"服务不可用"。"""
    ids = ['paraformer-zh', 'whisper-1'] if ENGINE == 'paraformer' else ['whisper-1']
    return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": 'albion-ear'} for i in ids]}


@app.post('/v1/audio/transcriptions')
async def transcribe(
    file: UploadFile = File(...),
    model_name: str = Form('whisper-1', alias='model'),
    language: str = Form(''),
    response_format: str = Form('json'),
    prompt: str = Form(''),
):
    data = await file.read()
    try:
        model = await run_in_threadpool(get_model)
    except Exception:
        # 模型没起来不能装作"没听见"：回 503 让前端知道耳朵坏了
        raise HTTPException(status_code=503, detail=f'ear model not loaded: {_load_error}')
    # 推理是阻塞的，放线程池里跑，免得卡住 /health 等其它请求
    return await run_in_threadpool(_transcribe, model, data, language, response_format, prompt)


def _transcribe(model, data, language, response_format, prompt):
    t0 = time.time()
    if ENGINE == 'paraformer':
        # 中文专用：写临时文件交给 funasr（它自己按 VAD 切段 + 加标点）
        import tempfile
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                f.write(data)
                tmp = f.name
            res = model.generate(input=tmp, batch_size_s=60, cache={})
            text = (res[0].get('text') or '').strip()
        except Exception as e:
            print(f'[ear] 跳过无法解码的音频（{len(data)}B）：{type(e).__name__}: {str(e)[:120]}', flush=True)
            text = ''
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        print(f'[ear] {len(data)}B -> {text!r} 用时 {time.time() - t0:.1f}s', flush=True)
        if response_format == 'text':
            return PlainTextResponse(text)
        if response_format == 'verbose_json':
            return {'task': 'transcribe', 'language': 'zh', 'duration': 0.0, 'text': text, 'segments': []}
        return {'text': text}
    try:
        # beam_size=1 + 免时间戳：短句识别提速 3~5 倍，质量几乎不变（long-form 场景才需要 beam 搜索）
        segments, info = model.transcribe(
            io.BytesIO(data),
            language=language or None,
            beam_size=1,
            vad_filter=True,
            without_timestamps=(response_format != 'verbose_json'),
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        segs = [{'start': s.start, 'end': s.end, 'text': s.text} for s in segments]
        text = ''.join(s['text'] for s in segs).strip()
        lang = info.language
        duration = info.duration
    except Exception as e:
        # 空录音 / 坏音频不该报 500：对前端来说这就是"没听见"，回空文本即可
        # （否则 AIRI 会弹 "voice input failed to transcribe speech" 吓人）
        print(f'[ear] 跳过无法解码的音频（{len(data)}B）：{type(e).__name__}: {str(e)[:120]}', flush=True)
        if response_format == 'text':
            return PlainTextResponse('')
        if response_format == 'verbose_json':
            return {'task': 'transcribe', 'language': '', 'duration': 0.0, 'text': '', 'segments': []}
        return {'text': ''}
    print(f'[ear] {len(data)}B -> {text!r} lang={lang} 用时 {time.time() - t0:.1f}s', flush=True)
    if response_format == 'text':
        return PlainTextResponse(text)
    if response_format == 'verbose_json':
        return {
            'task': 'transcribe',
            'language': lang,
            'duration': duration,
            'text': text,
            'segments': segs,
        }
    return {'text': text}


if __name__ == '__main__':
    print(f'[ear] 启动 :{PORT} engine={ENGINE} model={MODEL_NAME} device={DEVICE}', flush=True)
    if os.environ.get('ALBION_STT_PRELOAD', '1') != '0':
        # 懒加载会让开机后第一句慢 ~6s；后台线程先把它拉进内存（paraformer 只要 2.4s）
        def _preload():
            try:
                get_model()
            except Exception:
                pass   # 已记进 _load_error，请求来时会重试
        threading.Thread(target=_preload, daemon=True).start()
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')
