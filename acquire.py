#!/usr/bin/env python3
"""
获取原始音频：本地文件直接用；YouTube 用 yt-dlp；Spotify 用 spotdl。
返回本地音频文件的绝对路径。
"""

import os
import re
import sys
import glob
import socket
import subprocess

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


def _force_ipv4():
    """强制 IPv4：本机 IPv6 不通时，首个外连会在 IPv6 上等约 120s 才回退，
    导致搜索/下载冷启动巨慢。过滤掉 getaddrinfo 的 IPv6 结果即可彻底避免。
    仅在仍有 IPv4 地址时过滤，避免误伤 IPv6-only 主机。"""
    if getattr(socket, "_ipv4_forced", False):
        return
    _orig = socket.getaddrinfo

    def ipv4_only(*args, **kwargs):
        res = _orig(*args, **kwargs)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res

    socket.getaddrinfo = ipv4_only
    socket._ipv4_forced = True


_force_ipv4()


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


# 公开别名，供 webgui 后台任务调用
download_youtube = _download_youtube


_ytmusic = None


def _get_ytmusic():
    global _ytmusic
    if _ytmusic is None:
        from ytmusicapi import YTMusic
        _ytmusic = YTMusic()
    return _ytmusic


def _dur_to_seconds(s):
    """'3:01' / '1:02:03' → 秒；已是数字则原样返回。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    parts = str(s).split(":")
    try:
        sec = 0
        for p in parts:
            sec = sec * 60 + int(p)
        return sec
    except ValueError:
        return None


def search_youtube(query: str, n: int = 6):
    """在 YouTube Music 搜索，返回 n 个候选（不下载），供用户确认。

    用 ytmusicapi（直连 YouTube Music 内部 API）：比 yt-dlp 抓取快得多（<1s），
    且结果干净（歌名 / 歌手 分开），更适合卡拉OK挑歌。
    返回 [{id, title, uploader, duration, url, thumbnail}]。
    """
    yt = _get_ytmusic()
    try:
        res = yt.search(query, filter="songs", limit=n)
    except Exception:
        res = yt.search(query, filter="videos", limit=n)  # 兜底

    results = []
    for r in res:
        vid = r.get("videoId")
        if not vid:
            continue
        artists = ", ".join(a.get("name", "") for a in r.get("artists", []) if a.get("name"))
        thumbs = r.get("thumbnails") or []
        thumb = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        dur = r.get("duration_seconds") or _dur_to_seconds(r.get("duration"))
        results.append({
            "id": vid,
            "title": r.get("title") or "(无标题)",
            "uploader": artists,
            "duration": dur,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": thumb,
        })
        if len(results) >= n:
            break
    return results


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
