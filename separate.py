#!/usr/bin/env python3
"""
用 Demucs 把一首歌分离成 人声(vocals) 与 伴奏(no_vocals)，带缓存。
返回 (vocals_path, no_vocals_path)。
"""

import os
import sys
import hashlib
import subprocess

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
MODEL = "htdemucs"


def _file_hash(path: str, chunk=1 << 20) -> str:
    """对文件内容取 sha1（只读前 16MB 足以区分不同歌曲，且快）。"""
    h = hashlib.sha1()
    read = 0
    with open(path, "rb") as f:
        while read < (16 << 20):
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
            read += len(buf)
    h.update(str(os.path.getsize(path)).encode())
    return h.hexdigest()[:16]


def _find_stems(out_root: str):
    """在 demucs 输出目录里定位 vocals.wav / no_vocals.wav。"""
    model_dir = os.path.join(out_root, MODEL)
    if not os.path.isdir(model_dir):
        return None
    for track in os.listdir(model_dir):
        d = os.path.join(model_dir, track)
        vocals = os.path.join(d, "vocals.wav")
        no_vocals = os.path.join(d, "no_vocals.wav")
        if os.path.exists(vocals) and os.path.exists(no_vocals):
            return vocals, no_vocals
    return None


def separate(audio_path: str, device: str = "mps", use_cache: bool = True):
    """分离人声/伴奏。device: 'mps'(Apple Silicon) | 'cpu' | 'cuda'。"""
    digest = _file_hash(audio_path)
    out_root = os.path.join(CACHE_DIR, digest)

    if use_cache:
        stems = _find_stems(out_root)
        if stems:
            print(f"命中缓存: {out_root}")
            return stems

    os.makedirs(out_root, exist_ok=True)
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",
        "-n", MODEL,
        "-d", device,
        "-o", out_root,
        audio_path,
    ]
    print(f"运行 Demucs 分离（device={device}），首次较慢，请稍候…")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        if device != "cpu":
            print(f"device={device} 失败，回退到 CPU 重试…")
            cmd[cmd.index(device)] = "cpu"
            subprocess.run(cmd, check=True)
        else:
            raise

    stems = _find_stems(out_root)
    if not stems:
        raise RuntimeError(f"Demucs 完成但未找到分离结果: {out_root}")
    return stems


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python separate.py <音频文件> [device]")
        sys.exit(1)
    dev = sys.argv[2] if len(sys.argv) > 2 else "mps"
    v, nv = separate(sys.argv[1], device=dev)
    print("人声:", v)
    print("伴奏:", nv)
