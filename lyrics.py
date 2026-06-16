#!/usr/bin/env python3
"""
歌词：用 syncedlyrics 抓带时间轴的同步歌词(LRC)，存到 cache/<song_id>/lyrics.lrc，
并提供把 LRC 解析成 [{t, text}] 的工具，供 webgui 跟随进度高亮滚动。
"""

import os
import re

from separate import CACHE_DIR

LRC_NAME = "lyrics.lrc"

# [mm:ss.xx] / [mm:ss] 时间标签；同一行可有多个时间标签。
_TIME_TAG = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def lrc_path(song_id: str) -> str:
    return os.path.join(CACHE_DIR, song_id, LRC_NAME)


def has_lyrics(song_id: str) -> bool:
    p = lrc_path(song_id)
    return os.path.exists(p) and os.path.getsize(p) > 0


def fetch(title: str, artist: str = "", duration=None):
    """搜同步歌词，返回 LRC 文本或 None。best-effort：任何异常都吞掉返回 None。"""
    term = f"{title} {artist}".strip()
    if not term:
        return None
    try:
        from acquire import _force_ipv4  # 否则 IPv6 冷启动会让请求 ~120s 超时
        _force_ipv4()
        import syncedlyrics
        lrc = syncedlyrics.search(term, synced_only=True)
        return lrc or None
    except Exception:  # noqa: BLE001
        return None


def save(song_id: str, lrc_text: str):
    """把 LRC 文本写到 cache/<song_id>/lyrics.lrc。"""
    p = lrc_path(song_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(lrc_text)
    return p


def load(song_id: str):
    """读出 LRC 文本，没有则返回 None。"""
    p = lrc_path(song_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def parse(lrc_text: str):
    """把 LRC 解析成 [{"t": 秒(float), "text": str}]，按时间升序。
    跳过 [ar:]/[ti:]/[by:] 等元数据标签与空行。一行多个时间标签会展开成多条。"""
    if not lrc_text:
        return []
    lines = []
    for raw in lrc_text.splitlines():
        tags = list(_TIME_TAG.finditer(raw))
        if not tags:
            continue  # 元数据标签或无时间行，忽略
        text = _TIME_TAG.sub("", raw).strip()
        for m in tags:
            mm, ss = int(m.group(1)), int(m.group(2))
            frac = m.group(3) or "0"
            t = mm * 60 + ss + int(frac) / (10 ** len(frac))
            lines.append({"t": round(t, 2), "text": text})
    lines.sort(key=lambda x: x["t"])
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python lyrics.py <歌名> [歌手]")
        sys.exit(1)
    txt = fetch(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    if not txt:
        print("没找到同步歌词")
    else:
        print(txt[:500])
