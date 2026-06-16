#!/usr/bin/env python3
"""
歌库：扫描分离缓存，列出所有已处理（已分离出 vocals + no_vocals）的歌。
"""

import os
import glob

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
    }


def sync_instrumentals():
    """把歌库里所有伴奏汇总到统一的 伴奏/ 文件夹（缺的补上）。返回处理数量。"""
    n = 0
    for s in list_songs():
        if mirror_instrumental(s["no_vocals"]):
            n += 1
    return n


def id_for_stems(no_vocals_path: str):
    """从 stems 路径反推出缓存 hash（=song id）。"""
    rel = os.path.relpath(no_vocals_path, CACHE_DIR)
    return rel.split(os.sep)[0]


if __name__ == "__main__":
    for s in list_songs():
        print(f"{s['id']}  {s['name']}")
