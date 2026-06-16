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


def _is_bilibili(url: str) -> bool:
    return "bilibili.com" in url or "b23.tv" in url


def _safe_filename(name: str) -> str:
    """去掉文件名中不合法的字符。"""
    return re.sub(r'[\\/*?:"<>|\x00-\x1f]', '', name).strip()


def predicted_name(title: str, artist: str = "") -> str:
    """预测 download_youtube 给该曲目生成的歌库名(=文件名去后缀)，用于下载前判重。
    必须与 _download_youtube 的命名规则保持一致：有歌手则“歌名-歌手”，否则用歌名。"""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if title and artist:
        return _safe_filename(f"{title}-{artist}")
    return _safe_filename(title)


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


def _download_youtube(url: str, cancel=None, on_progress=None,
                      song_title: str = "", song_artist: str = "") -> str:
    import yt_dlp

    _ensure_downloads()
    outtmpl = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")
    hooks = [_progress_hook]
    if cancel is not None:
        def _cancel_hook(d):
            if cancel.is_set():
                raise KeyboardInterrupt("下载已中止")
        hooks.append(_cancel_hook)
    if on_progress is not None:
        def _pct_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    try:
                        on_progress(d.get("downloaded_bytes", 0) / total * 100)
                    except Exception:  # noqa: BLE001
                        pass
        hooks.append(_pct_hook)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": hooks,
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
        if not os.path.exists(mp3):
            if os.path.exists(path):
                mp3 = os.path.abspath(path)
            else:
                raise RuntimeError("YouTube 下载后未找到音频文件")
        mp3 = os.path.abspath(mp3)
    if song_title and song_artist:
        safe_name = _safe_filename(f"{song_title}-{song_artist}") + ".mp3"
        dest = os.path.join(DOWNLOADS_DIR, safe_name)
        if os.path.abspath(dest) != mp3:
            os.replace(mp3, dest)
        mp3 = dest
    return mp3


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


# 标题里出现这些词通常不是“原版录音室版本”，自动选歌时降权
_BAD_WORDS = [
    "live", "cover", "karaoke", "instrumental", "remix", "sped", "slow",
    "nightcore", "8d", "reverb", "mashup", "medley", "acoustic", "piano",
    "现场", "翻唱", "伴奏", "纯音乐", "remastered live",
]


def best_match_index(results) -> int:
    """从搜索候选里挑“最可能是这首歌原版”的一个，返回下标。
    规则：靠前(相关度高)加分；标题含 live/cover/remix 等降权；时长过短/过长降权。"""
    best_i, best_score = 0, float("-inf")
    for i, r in enumerate(results):
        title = (r.get("title") or "").lower()
        score = -i * 0.5  # 越靠前越好
        if any(w in title for w in _BAD_WORDS):
            score -= 5
        d = r.get("duration") or 0
        if d and (d < 60 or d > 360):  # 典型歌曲 1~6 分钟之外降权
            score -= 2
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def _download_spotify(url: str) -> str:
    """用 spotdl 下载 Spotify 单曲（底层走 YouTube Music 匹配）。"""
    _ensure_downloads()
    before = set(glob.glob(os.path.join(DOWNLOADS_DIR, "*")))
    print(f"从 Spotify（经 spotdl）下载: {url}")
    cmd = [
        sys.executable, "-m", "spotdl", "download", url,
        "--output", os.path.join(DOWNLOADS_DIR, "{title}-{artist}.{output-ext}"),
    ]
    subprocess.run(cmd, check=True)
    after = set(glob.glob(os.path.join(DOWNLOADS_DIR, "*")))
    new_files = [f for f in (after - before)
                 if f.lower().endswith((".mp3", ".m4a", ".wav", ".flac", ".opus"))]
    if not new_files:
        raise RuntimeError("spotdl 未产出新音频文件（可能是歌单或下载失败）")
    return os.path.abspath(max(new_files, key=os.path.getmtime))


# 子进程引导代码：先强制 IPv4（否则子进程会复现 ~120s IPv6 冷启动而超时），
# 再以 `python -m spotdl` 的方式运行 spotdl。
_SPOTDL_BOOT = (
    "import socket;_o=socket.getaddrinfo;"
    "socket.getaddrinfo=lambda *a,**k:[r for r in _o(*a,**k) if r[0]==socket.AF_INET] or _o(*a,**k);"
    "import runpy,sys;sys.argv=['spotdl','save',{url!r},'--save-file',{tmp!r}];"
    "runpy.run_module('spotdl',run_name='__main__')"
)


def _spotify_id(url: str):
    """从各种 Spotify URL/URI 里解析出 (类型, id)。
    支持 open.spotify.com/[intl-xx/]{playlist|album|track}/{id} 与 spotify:...:。"""
    m = re.search(r"(?:open\.spotify\.com/(?:embed/)?(?:intl-[\w-]+/)?|spotify:)"
                  r"(playlist|album|track)[:/]([A-Za-z0-9]+)", url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _find_track_list(o):
    """在 embed 的 __NEXT_DATA__ JSON 里递归找 trackList。"""
    if isinstance(o, dict):
        if isinstance(o.get("trackList"), list):
            return o["trackList"]
        for v in o.values():
            r = _find_track_list(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_track_list(v)
            if r:
                return r
    return None


def _list_spotify_embed(url: str):
    """用 Spotify 公开 embed 页枚举曲目（无需登录/token，快且稳）。
    embed 页内嵌 __NEXT_DATA__(JSON)，含 trackList[{title, subtitle(歌手)}]。
    返回 [{title, query}]。注意：超大歌单(几百首)embed 可能只给前一部分。"""
    import json
    import urllib.request

    typ, pid = _spotify_id(url)
    if not pid:
        raise RuntimeError("无法识别的 Spotify 链接")
    embed = f"https://open.spotify.com/embed/{typ}/{pid}"
    req = urllib.request.Request(embed, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Spotify embed 页结构变化，未找到曲目数据")
    tracks = _find_track_list(json.loads(m.group(1)))
    if not tracks:
        raise RuntimeError("embed 页未含曲目（歌单可能为空/私密）")

    out = []
    for t in tracks:
        title = (t.get("title") or "").strip()
        artist = (t.get("subtitle") or "").strip()
        q = f"{title} {artist}".strip()
        if q:
            out.append({"title": q, "query": q})
    return out


def _list_spotify_spotdl(url: str):
    """退路：用 spotdl save 导出歌单元数据(JSON)解析。
    依赖 spotapi 取 token，Spotify 反爬一升级就会坏，仅作兜底。"""
    import json
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".spotdl")
    os.close(fd)
    try:
        boot = _SPOTDL_BOOT.format(url=url, tmp=tmp)
        subprocess.run(
            [sys.executable, "-c", boot],
            check=True, capture_output=True, text=True, timeout=180)
        with open(tmp, encoding="utf-8") as f:
            songs = json.load(f)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    out = []
    for s in songs:
        name = s.get("name") or ""
        artists = s.get("artists")
        if isinstance(artists, list):
            artist = ", ".join(a for a in artists if a)
        else:
            artist = s.get("artist") or ""
        q = f"{name} {artist}".strip()
        if q:
            out.append({"title": q, "query": q})
    return out


def _list_spotify(url: str):
    """枚举 Spotify 歌单/专辑曲目，返回 [{title, query}]。
    首选公开 embed 页(无需 token)；失败再退回 spotdl save。"""
    try:
        return _list_spotify_embed(url)
    except Exception as e:  # noqa: BLE001
        print(f"Spotify embed 解析失败（{e}），改用 spotdl 兜底…")
        return _list_spotify_spotdl(url)


def list_playlist(url: str):
    """枚举歌单/列表里的每首曲目。
    Spotify → [{title, query}]（按名字搜 YouTube）；
    YouTube/Bilibili 等 → [{title, url}]（直接用链接下载）。"""
    if _is_spotify(url):
        return _list_spotify(url)

    import yt_dlp
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries")
    if not entries:  # 不是列表，当单曲处理
        entries = [info]
    out = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        u = e.get("url") or e.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if not u:
            continue
        out.append({"title": e.get("title") or "(未知)", "url": u})
    return out


def to_mp3(path: str, bitrate: str = "320k") -> str:
    """把任意音频转成 320k mp3（已是 mp3 则原样返回）。转好后删掉原文件。"""
    if path.lower().endswith(".mp3"):
        return path
    out = os.path.splitext(path)[0] + ".mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-vn", "-b:a", bitrate, out],
        check=True, capture_output=True)
    try:
        os.remove(path)
    except OSError:
        pass
    return out


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
