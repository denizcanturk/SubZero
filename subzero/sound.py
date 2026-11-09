"""Optional audio: terminal bell, or tiny synthesized PCM clips via paplay/pw-play/aplay."""
from __future__ import annotations

import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import wave

RATE = 22050


def _env(n, attack, decay):
    a = max(1, int(attack * RATE))
    return [min(1.0, i / a) * math.exp(-max(0, i - a) / (decay * RATE)) for i in range(n)]


def _ping():
    n = int(2.2 * RATE)
    out = [0.0] * n
    for delay, gain in ((0.0, 1.0), (0.9, 0.25), (1.5, 0.08)):
        off = int(delay * RATE)
        env = _env(n - off, 0.004, 0.35)
        for i in range(n - off):
            t = i / RATE
            out[off + i] += gain * env[i] * math.sin(2 * math.pi * 1180 * t) * (1 + 0.15 * math.sin(2 * math.pi * 7 * t))
    return out


def _alarm():
    n = int(1.2 * RATE)
    return [0.45 * (1 if math.sin(2 * math.pi * (880 if (i // (RATE // 5)) % 2 == 0 else 660) * i / RATE) > 0 else -1)
            * (1.0 if (i % (RATE // 5)) < RATE // 6 else 0.0) for i in range(n)]


def _creak():
    n = int(1.6 * RATE)
    rnd = random.Random(7)
    out, ph = [], 0.0
    for i in range(n):
        t = i / RATE
        f = 55 + 25 * math.sin(t * 2.3) + rnd.uniform(-6, 6)
        ph += 2 * math.pi * f / RATE
        a = math.sin(math.pi * t / 1.6) ** 2
        out.append(0.5 * a * (math.sin(ph) * 0.7 + 0.3 * math.sin(3 * ph + rnd.uniform(-.3, .3))))
    return out


def _boom():
    n = int(2.5 * RATE)
    rnd = random.Random(3)
    env = _env(n, 0.01, 0.6)
    out, y = [], 0.0
    for i in range(n):
        y = 0.985 * y + 0.12 * rnd.uniform(-1, 1)
        out.append(env[i] * y * 3.0)
    return out


def _blip():
    n = int(0.25 * RATE)
    env = _env(n, 0.003, 0.06)
    return [0.5 * env[i] * math.sin(2 * math.pi * 520 * i / RATE) for i in range(n)]


CLIPS = {"ping": _ping, "alarm": _alarm, "creak": _creak, "boom": _boom, "blip": _blip}
BELL = {"alarm", "boom", "ping"}


class Sound:
    def __init__(self, pcm: bool, bell: bool, beep_fn=None):
        self.bell = bell
        self.beep_fn = beep_fn
        self.player = None
        self.files = {}
        self.last = {}
        if pcm:
            for cand in (["paplay"], ["pw-play"], ["aplay", "-q"]):
                if shutil.which(cand[0]):
                    self.player = cand
                    break
            if self.player:
                self.dir = tempfile.mkdtemp(prefix="subzero_snd_")
                for name, fn in CLIPS.items():
                    path = os.path.join(self.dir, name + ".wav")
                    self._write(path, fn())
                    self.files[name] = path

    @staticmethod
    def _write(path, samples):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 30000)) for s in samples))

    @property
    def pcm(self):
        return bool(self.files)

    def play(self, name, min_gap=0.6):
        now = time.monotonic()
        if now - self.last.get(name, 0) < min_gap:
            return
        self.last[name] = now
        if self.files.get(name):
            try:
                subprocess.Popen(self.player + [self.files[name]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                return
            except OSError:
                pass
        if self.bell and name in BELL and self.beep_fn:
            self.beep_fn()

    def close(self):
        for p in self.files.values():
            try:
                os.remove(p)
            except OSError:
                pass
        if self.files:
            try:
                os.rmdir(self.dir)
            except OSError:
                pass
