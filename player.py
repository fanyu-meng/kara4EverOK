#!/usr/bin/env python3
"""
卡拉OK 播放器：把 原唱(原曲) 与 伴奏 两条轨载入内存，
用 sounddevice 回调播放，按键可在 原唱 ⇄ 伴奏 之间无缝切换。

原唱 = vocals + no_vocals（与伴奏逐样本对齐，保证切换不跳进度）。
伴奏 = no_vocals。
"""

import sys
import time
import select
import threading

import numpy as np
import soundfile as sf
import sounddevice as sd

SEEK_SECONDS = 5


class KaraokePlayer:
    def __init__(self, vocals_path: str, no_vocals_path: str):
        vocals, sr1 = sf.read(vocals_path, dtype="float32", always_2d=True)
        instr, sr2 = sf.read(no_vocals_path, dtype="float32", always_2d=True)
        if sr1 != sr2:
            raise ValueError("两条轨采样率不一致")
        n = min(len(vocals), len(instr))
        self.samplerate = sr1
        self.channels = instr.shape[1]
        self.instrumental = instr[:n]                 # 伴奏
        self.original = (vocals[:n] + instr[:n])       # 原唱 = 人声 + 伴奏
        self.total = n

        self.pos = 0
        self.mode = "instrumental"     # 'instrumental' | 'original'
        self.paused = False
        self.lock = threading.Lock()
        self.done = threading.Event()

    # ---------- 音频回调 ----------
    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if self.paused:
                outdata[:] = 0
                return
            src = self.original if self.mode == "original" else self.instrumental
            start = self.pos
            end = min(start + frames, self.total)
            chunk = src[start:end]
            outdata[: len(chunk)] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):] = 0
                self.done.set()
                raise sd.CallbackStop()
            self.pos = end

    # ---------- 控制 ----------
    def toggle_mode(self):
        with self.lock:
            self.mode = "original" if self.mode == "instrumental" else "instrumental"

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused

    def seek(self, seconds: float):
        with self.lock:
            delta = int(seconds * self.samplerate)
            self.pos = max(0, min(self.total - 1, self.pos + delta))

    # ---------- 供 GUI 使用的非阻塞控制 ----------
    def start(self):
        """打开音频流并开始播放（不阻塞，供 GUI 调用）。"""
        self.stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        """停止并关闭音频流。"""
        stream = getattr(self, "stream", None)
        if stream is not None:
            stream.stop()
            stream.close()
            self.stream = None

    def _fmt(self, samples: int) -> str:
        s = samples / self.samplerate
        return f"{int(s)//60:02d}:{int(s)%60:02d}"

    def _status_line(self):
        with self.lock:
            pos, total, mode, paused = self.pos, self.total, self.mode, self.paused
        ratio = pos / total if total else 0
        bar_len = 30
        filled = int(bar_len * ratio)
        bar = "█" * filled + "─" * (bar_len - filled)
        mode_cn = "原唱 🎤" if mode == "original" else "伴奏 🎵"
        flag = "⏸ 暂停" if paused else "▶ 播放"
        return (f"\r{flag} | {mode_cn} | [{bar}] "
                f"{self._fmt(pos)}/{self._fmt(total)}   ")

    # ---------- 主循环 ----------
    def run(self):
        print("\n控制: [v] 原唱/伴奏切换  [空格] 暂停/继续  "
              "[←/→] 退/进 5秒  [q] 退出\n")
        stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            callback=self._callback,
        )
        with stream, _raw_terminal():
            while not self.done.is_set():
                self._handle_keys()
                sys.stdout.write(self._status_line())
                sys.stdout.flush()
                time.sleep(0.1)
        print("\n播放结束。")

    def _handle_keys(self):
        # 非阻塞读取标准输入
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return
        ch = sys.stdin.read(1)
        if ch == "v":
            self.toggle_mode()
        elif ch == " ":
            self.toggle_pause()
        elif ch in ("q", "\x03"):  # q 或 Ctrl-C
            self.done.set()
        elif ch == "\x1b":  # 方向键转义序列
            seq = sys.stdin.read(2)
            if seq == "[C":      # →
                self.seek(SEEK_SECONDS)
            elif seq == "[D":    # ←
                self.seek(-SEEK_SECONDS)


class _raw_terminal:
    """把终端切到 raw 模式以便逐键读取，退出时恢复。"""
    def __enter__(self):
        import termios
        import tty
        self._termios = termios
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self.old)


def play(vocals_path: str, no_vocals_path: str):
    KaraokePlayer(vocals_path, no_vocals_path).run()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python player.py <vocals.wav> <no_vocals.wav>")
        sys.exit(1)
    play(sys.argv[1], sys.argv[2])
