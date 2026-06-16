#!/usr/bin/env python3
"""
用 Demucs 把一首歌分离成 人声(vocals) 与 伴奏(no_vocals)，带缓存。
返回 (vocals_path, no_vocals_path)。
"""

import os
import re
import sys
import shutil
import hashlib
import threading
import subprocess

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
# 所有伴奏统一汇总到这一个文件夹（按歌名命名），方便集中查看/拷贝
INSTRUMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "伴奏")
_PCT = re.compile(r"(\d+)%")


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name).strip()


def mirror_instrumental(no_vocals_path: str):
    """把伴奏文件复制一份到统一的 伴奏/ 文件夹，命名为 <歌名>.<后缀>。"""
    try:
        os.makedirs(INSTRUMENTALS_DIR, exist_ok=True)
        name = os.path.basename(os.path.dirname(no_vocals_path))
        ext = os.path.splitext(no_vocals_path)[1] or ".mp3"
        dest = os.path.join(INSTRUMENTALS_DIR, _safe_name(name) + ext)
        if not (os.path.exists(dest)
                and os.path.getsize(dest) == os.path.getsize(no_vocals_path)):
            shutil.copy2(no_vocals_path, dest)
        return dest
    except OSError:
        return None
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
    """在 demucs 输出目录里定位 vocals / no_vocals。
    优先 mp3（新版输出），兼容历史的 wav 缓存。"""
    model_dir = os.path.join(out_root, MODEL)
    if not os.path.isdir(model_dir):
        return None
    for track in os.listdir(model_dir):
        d = os.path.join(model_dir, track)
        for ext in ("mp3", "wav"):
            vocals = os.path.join(d, f"vocals.{ext}")
            no_vocals = os.path.join(d, f"no_vocals.{ext}")
            if os.path.exists(vocals) and os.path.exists(no_vocals):
                return vocals, no_vocals
    return None


def _run_demucs(cmd, cancel=None, on_progress=None):
    """跑 demucs 子进程；cancel(threading.Event) 置位时杀进程并抛 KeyboardInterrupt。
    on_progress(pct) 给定时，捕获并解析 demucs 进度条(tqdm 用 \\r)上报百分比。
    用 os.read 按块快速读取 stderr，避免逐字节读取拖慢/堵住子进程。"""
    capture = on_progress is not None
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE if capture else None,
        stdout=subprocess.DEVNULL if capture else None,
    )
    reader = None
    if capture:
        def _read():
            fd = proc.stderr.fileno()
            buf = b""
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                segs = re.split(rb"[\r\n]", buf)
                buf = segs[-1]  # 残段留到下次
                for seg in segs[:-1]:
                    m = _PCT.search(seg.decode("utf-8", "ignore"))
                    if m:
                        try:
                            on_progress(int(m.group(1)))
                        except Exception:  # noqa: BLE001
                            pass
        reader = threading.Thread(target=_read, daemon=True)
        reader.start()

    while True:
        try:
            proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise KeyboardInterrupt("分离已中止")
            continue
        break
    if reader is not None:
        reader.join(timeout=1)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def separate(audio_path: str, device: str = "mps", use_cache: bool = True,
             cancel=None, on_progress=None):
    """分离人声/伴奏。device: 'mps'(Apple Silicon) | 'cpu' | 'cuda'。
    cancel: 可选 threading.Event，置位则中止分离。
    on_progress(pct): 可选回调，上报分离百分比。"""
    digest = _file_hash(audio_path)
    out_root = os.path.join(CACHE_DIR, digest)

    if use_cache:
        stems = _find_stems(out_root)
        if stems:
            print(f"命中缓存: {out_root}")
            mirror_instrumental(stems[1])
            return stems

    os.makedirs(out_root, exist_ok=True)
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",
        "--mp3", "--mp3-bitrate", "320",   # 输出 mp3 而非 wav，省约 4/5 空间
        "-n", MODEL,
        "-d", device,
        "-o", out_root,
        audio_path,
    ]
    print(f"运行 Demucs 分离（device={device}），首次较慢，请稍候…")
    try:
        _run_demucs(cmd, cancel=cancel, on_progress=on_progress)
    except subprocess.CalledProcessError:
        if device != "cpu":
            print(f"device={device} 失败，回退到 CPU 重试…")
            cmd[cmd.index(device)] = "cpu"
            _run_demucs(cmd, cancel=cancel, on_progress=on_progress)
        else:
            raise

    stems = _find_stems(out_root)
    if not stems:
        raise RuntimeError(f"Demucs 完成但未找到分离结果: {out_root}")
    mirror_instrumental(stems[1])
    return stems


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python separate.py <音频文件> [device]")
        sys.exit(1)
    dev = sys.argv[2] if len(sys.argv) > 2 else "mps"
    v, nv = separate(sys.argv[1], device=dev)
    print("人声:", v)
    print("伴奏:", nv)
