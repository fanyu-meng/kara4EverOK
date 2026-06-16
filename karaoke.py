#!/usr/bin/env python3
"""
卡拉OK CLI 入口。

    python karaoke.py <本地文件 | YouTube链接 | Spotify链接> [选项]

流程: 获取音频 -> Demucs 分离伴奏(带缓存) -> 播放器(原唱/伴奏切换)。
"""

import argparse
import os
import sys

from acquire import acquire
from separate import separate
from player import play


def main():
    parser = argparse.ArgumentParser(
        description="本地卡拉OK：去人声 + 原唱/伴奏切换",
    )
    parser.add_argument("source",
                        help="本地音频文件路径 / YouTube 链接 / Spotify 链接")
    parser.add_argument("--device", default="mps",
                        choices=["mps", "cpu", "cuda"],
                        help="Demucs 运行设备（默认 mps，Apple Silicon）")
    parser.add_argument("--no-cache", action="store_true",
                        help="忽略缓存，强制重新分离")
    parser.add_argument("--gui", action="store_true",
                        help="用网页 GUI（歌名 + 切换按钮）代替命令行播放")
    args = parser.parse_args()

    print("=== 1/3 获取音频 ===")
    audio = acquire(args.source)
    print(f"原始音频: {audio}\n")

    print("=== 2/3 分离人声/伴奏 ===")
    vocals, no_vocals = separate(audio, device=args.device,
                                 use_cache=not args.no_cache)
    print(f"伴奏: {no_vocals}\n")

    song_name = os.path.splitext(os.path.basename(audio))[0]

    print("=== 3/3 播放 ===")
    if args.gui:
        from webgui import play as play_gui
        play_gui(vocals, no_vocals, song_name)
    else:
        play(vocals, no_vocals)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
        sys.exit(0)
