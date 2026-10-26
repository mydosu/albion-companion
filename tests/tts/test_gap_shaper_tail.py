# -*- coding: utf-8 -*-
"""句尾不被掐掉（2026-10-26）：清化音/气声的自然衰减要留住，裁点要淡出，不能硬拼数字零。

生产 :9889 实测 32 句：旧判据（峰值 -40dB）把 6 句的句尾裁在 -46～-54dB，去掉 27–59ms 高于 -60dBFS 的
尾音（「です」的 す、「〜ですか」的气声收尾）。这里用合成波形复现那个形状：一个响元音 + 一段从 -20dB
线性落到 -70dB 的噪声尾 + 一段底噪，然后检查整形结果。
"""
import io
import pathlib
import sys
import unittest
import wave

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tts" / "stream"))

import gap_shaper as GS  # noqa: E402

SR = 48000


def wav_bytes(pcm, sr=SR, ch=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    return out.getvalue()


def read_pcm(blob):
    with wave.open(io.BytesIO(blob), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels())
        return w.getframerate(), pcm


def decaying_tail(peak=0.8, vowel_s=0.6, decay_s=0.18, floor_s=0.4, seed=7):
    """响元音 → 从 -20dB 落到 -70dB 的噪声收尾（模拟「です」的 す / 气声）→ 低底噪。返回 int16。"""
    rs = np.random.RandomState(seed)
    t = np.arange(int(vowel_s * SR)) / SR
    vowel = peak * np.sin(2 * np.pi * 180 * t)
    n = int(decay_s * SR)
    env = 10 ** ((-20 - 50 * np.arange(n) / n) / 20.0)      # -20dB → -70dB
    tail = env * rs.uniform(-1, 1, n)
    floor = rs.uniform(-1, 1, int(floor_s * SR)) * 10 ** (-72 / 20.0)
    return (np.clip(np.concatenate([vowel, tail, floor]), -1, 1) * 32767).astype("<i2")


def last_above(pcm, db_rel_peak):
    peak = float(np.abs(pcm).max()) or 1.0
    idx = np.flatnonzero(np.abs(pcm) > peak * 10 ** (db_rel_peak / 20.0))
    return (int(idx[-1]) + 1) if len(idx) else 0


def audible_count(pcm, db_rel_peak):
    """高于「峰值 -db」的样本数（看有没有被裁掉）。"""
    peak = float(np.abs(pcm).max()) or 1.0
    return len(np.flatnonzero(np.abs(pcm.astype("float32")) > peak * 10 ** (db_rel_peak / 20.0)))


class TailReleaseTest(unittest.TestCase):
    def test_quiet_release_survives_shaping(self):
        """-20→-70dB 的收尾：高于 -60dB 的部分一条都不许丢。"""
        src = wav_bytes(decaying_tail())
        out = GS.shape(src, "sentence")
        _, a = read_pcm(src)
        _, b = read_pcm(out)
        src_audible = audible_count(a, -60)
        self.assertGreater(src_audible, 0)
        # 淡出本身会把最后不到 1ms 压到 -60dB 以下（那是斜坡，不是裁剪），所以留 10ms 余量
        self.assertGreaterEqual(audible_count(b, -60), src_audible - int(0.01 * SR),
                                "句尾高于 -60dB 的样本被裁掉了 %d 个" % (src_audible - audible_count(b, -60)))

    def test_quiet_release_is_not_cut_at_minus_40db(self):
        """旧实现只在「峰值-40dB」之后留 30ms：噪声尾从 -40dB 落到 -60dB 的那几十毫秒会被切掉。"""
        src = wav_bytes(decaying_tail())
        out = GS.shape(src, "sentence")
        _, a = read_pcm(src)
        _, b = read_pcm(out)
        old_end = last_above(a, -40) + int(GS.TAIL_KEEP_S * SR)
        tail = np.abs(a[old_end:].astype("float32"))
        # 旧裁点之后确实还有可听内容（说明这一版测得出问题）
        self.assertGreater(len(np.flatnonzero(tail > 1e-3)), int(0.02 * SR))
        # 新实现：这些内容都还在
        for thresh in (1e-3, 3e-3):
            want = len(np.flatnonzero(tail > thresh))
            got = len(np.flatnonzero(np.abs(b.astype("float32")) > thresh))
            self.assertGreaterEqual(got, want, "高于 %g 的尾部样本少了 %d 个" % (thresh, want - got))

    def test_splice_is_faded_to_zero(self):
        """裁点前 30ms 线性淡出：最后一个样本必须为 0，淡出窗口的包络单调下降（不是一刀切）。"""
        src = wav_bytes(decaying_tail())
        out = GS.shape(src, "sentence")
        sr, b = read_pcm(out)
        mono = np.abs(b[:, 0].astype("float32"))
        peak = float(mono.max()) or 1.0
        body_end = len(mono) - int(GS.TAIL_S["sentence"] * sr)   # 补的静音之前
        n = int(GS.FADE_S * sr)
        self.assertLess(mono[body_end - 1], 2)                   # 硬拼零的台阶没了
        self.assertLess(mono[body_end - int(0.001 * sr):body_end].max(), peak * 10 ** (-60 / 20.0))
        win = int(0.005 * sr)
        env = [mono[body_end - n + i:body_end - n + i + win].max() for i in range(0, n - win, win)]
        self.assertGreater(len(env), 3)
        for a, c in zip(env, env[1:]):
            self.assertLessEqual(c, a * 1.05 + 1, "淡出段包络在上升：%r" % env)

    def test_loud_ending_is_left_alone(self):
        """上游真把话截在响处时（-30dB 以上）不许淡出：那会变成新的「压尾」，硬边留给上游修。"""
        loud = (0.8 * np.sin(2 * np.pi * 180 * np.arange(int(0.5 * SR)) / SR) * 32767).astype("<i2")
        src = wav_bytes(loud)                      # 没有任何尾静音，最后一个样本仍是响的
        out = GS.shape(src, "sentence")
        sr, a = read_pcm(src)
        _, b = read_pcm(out)
        body_end = len(b) - int(GS.TAIL_S["sentence"] * sr)
        self.assertAlmostEqual(float(b[body_end - 1, 0]), float(a[-1, 0]), delta=2)

    def test_tail_gap_length_unchanged(self):
        """气口长度口径不变：补齐的静音仍是 TAIL_S[kind]，首静音仍是 LEAD_S。"""
        for kind, want in GS.TAIL_S.items():
            with self.subTest(kind=kind):
                sr, b = read_pcm(GS.shape(wav_bytes(decaying_tail()), kind))
                mono = np.abs(b).max(axis=1)
                quiet = np.flatnonzero(mono > 32767 * 10 ** (-55 / 20.0))
                lead = quiet[0] / sr
                tail = (len(b) - 1 - quiet[-1]) / sr
                self.assertAlmostEqual(lead, GS.LEAD_S, delta=0.02)
                # tail = 补的静音（want）+ 收尾里低于 -55dB 的余量（≤0.15s）
                self.assertGreaterEqual(tail, want - 0.05)
                self.assertLessEqual(tail, want + 0.15)

    def test_tail_threshold_is_lower_than_onset_threshold(self):
        self.assertGreater(GS.TAIL_DB, GS.VOICED_DB)
        self.assertLess(GS.TAIL_FLOOR_DB, GS.FLOOR_DB)
        self.assertGreater(GS.FADE_S, 0)


if __name__ == "__main__":
    unittest.main()
