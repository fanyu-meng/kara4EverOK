#!/usr/bin/env python3
"""
获取原始音频：本地文件直接用；YouTube 用 yt-dlp；Spotify 用 spotdl。
返回本地音频文件的绝对路径。
"""

import os
import re
import sys
import glob
import subprocess

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


def _ensure_downloads():
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _is_spotify(url: str) -> bool:
    return "spotify.com" in url or url.startswith("spotify:")


def _progress_hook(d):
    """yt-dlp 下载进度（沿用 download_YouTube/download_audio.py 的写法）。"""
    if d["status"] == "downloading":
        total = d.get("total_bytes", 0) or d.get("total_bytes_estimate", 0)
        downloaded = d.get("downloaded_bytes", 0)
        if total > 0:
            pct = downloaded / total * 100
            speed = d.get("speed") or 0
            speed_str = f"{speed/1024/1024:.1f} MB/s" if speed else "..."
            print(f"\r  下载中: {pct:.1f}% | {speed_str}", end="", flush=True)
    elif d["status"] == "finished":
        print("\n  下载完成，转码中…")


def _download_youtube(url: str) -> str:
    import yt_dlp

    _ensure_downloads()
    outtmpl = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    }
    print(f"从 YouTube 下载: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # 取转码后的最终文件名
        path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(path)
        mp3 = base + ".mp3"
        if os.path.exists(mp3):
            return os.path.abspath(mp3)
        # 兜底：找最近生成的同名文件
        if os.path.exists(path):
            return os.path.abspath(path)
    raise RuntimeError("YouTube 下载后未找到音频文件")


def _download_spotify(url: str) -> str:
    """用 spotdl 下载 Spotify 单曲（底层走 YouTube Music 匹配）。"""
    _ensure_downloads()
    before = set(glob.glob(os.path.join(DOWNLOADS_DIR, "*")))
    print(f"从 Spotify（经 spotdl）下载: {url}")
    cmd = [
        sys.executable, "-m", "spotdl", "download", url,
        "--output", os.path.join(DOWNLOADS_DIR, "{title} - {artist}.{output-ext}"),
    ]
    subprocess.run(cmd, check=True)
    after = set(glob.glob(os.path.join(DOWNLOADS_DIR, "*")))
    new_files = [f for f in (after - before)
                 if f.lower().endswith((".mp3", ".m4a", ".wav", ".flac", ".opus"))]
    if not new_files:
        raise RuntimeError("spotdl 未产出新音频文件（可能是歌单或下载失败）")
    return os.path.abspath(max(new_files, key=os.path.getmtime))


def acquire(source: str) -> str:
    """把任意输入（本地路径 / YouTube / Spotify）解析为本地音频文件路径。"""
    if _is_url(source) or source.startswith("spotify:"):
        if _is_youtube(source):
            return _download_youtube(source)
        if _is_spotify(source):
            return _download_spotify(source)
        raise ValueError(f"不支持的链接类型: {source}")
    # 本地文件
    path = os.path.abspath(os.path.expanduser(source))
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到文件: {path}")
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python acquire.py <本地文件|YouTube链接|Spotify链接>")
        sys.exit(1)
    print(acquire(sys.argv[1]))
