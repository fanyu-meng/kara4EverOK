#!/usr/bin/env python3
"""
歌库：扫描分离缓存，列出所有已处理（已分离出 vocals + no_vocals）的歌。
"""

import os
import re
import glob

import lyrics
from separate import CACHE_DIR, MODEL, _find_stems, mirror_instrumental


def list_songs():
    """返回 [{id, name, vocals, no_vocals}]，按名称排序。

    id = 缓存目录的 hash；name = 分离出的曲目文件夹名（即原文件名）。
    """
    songs = []
    if not os.path.isdir(CACHE_DIR):
        return songs
    for digest in os.listdir(CACHE_DIR):
        out_root = os.path.join(CACHE_DIR, digest)
        if not os.path.isdir(out_root):
            continue
        stems = _find_stems(out_root)
        if not stems:
            continue
        vocals, no_vocals = stems
        name = os.path.basename(os.path.dirname(no_vocals))
        songs.append({
            "id": digest,
            "name": name,
            "vocals": vocals,
            "no_vocals": no_vocals,
            "has_lyrics": lyrics.has_lyrics(digest),
        })
    songs.sort(key=lambda s: s["name"].lower())
    return songs


def get_song(song_id: str):
    """按 id 取单首歌，找不到返回 None。"""
    out_root = os.path.join(CACHE_DIR, song_id)
    stems = _find_stems(out_root)
    if not stems:
        return None
    vocals, no_vocals = stems
    return {
        "id": song_id,
        "name": os.path.basename(os.path.dirname(no_vocals)),
        "vocals": vocals,
        "no_vocals": no_vocals,
        "has_lyrics": lyrics.has_lyrics(song_id),
    }


def sync_instrumentals():
    """把歌库里所有伴奏汇总到统一的 伴奏/ 文件夹（缺的补上）。返回处理数量。"""
    n = 0
    for s in list_songs():
        if mirror_instrumental(s["no_vocals"]):
            n += 1
    return n


def _norm_name(name: str) -> str:
    """规范化歌名用于判重：合并空白、去首尾、忽略大小写。"""
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def find_by_name(name: str):
    """按歌名(忽略大小写/多余空白)查歌库里已处理的歌，找不到返回 None。
    供下载前判重：已有则跳过下载+分离。"""
    target = _norm_name(name)
    if not target:
        return None
    for s in list_songs():
        if _norm_name(s["name"]) == target:
            return s
    return None


def id_for_stems(no_vocals_path: str):
    """从 stems 路径反推出缓存 hash（=song id）。"""
    rel = os.path.relpath(no_vocals_path, CACHE_DIR)
    return rel.split(os.sep)[0]


if __name__ == "__main__":
    for s in list_songs():
        print(f"{s['id']}  {s['name']}")
