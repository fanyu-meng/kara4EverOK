# 卡拉OK 工具（本地文件模式 · CLI）

拿一首歌 → Demucs 去人声分离出伴奏 → 命令行播放器边放边唱，可在**原唱 / 伴奏**之间一键无缝切换。

## 安装

```bash
cd karaoke
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

系统需已安装 `ffmpeg`（`brew install ffmpeg`）。

## 用法

```bash
./venv/bin/python karaoke.py <本地文件 | YouTube链接 | Spotify链接> [选项]
```

例子：

```bash
./venv/bin/python karaoke.py ~/Music/song.mp3
./venv/bin/python karaoke.py "https://www.youtube.com/watch?v=XXXX"
./venv/bin/python karaoke.py "https://open.spotify.com/track/XXXX"
```

选项：
- `--gui` 　用极简网页 GUI 代替命令行：自动开浏览器，显示歌名 + 一个原唱/伴奏切换按钮（声音仍从电脑扬声器出）。
- `--device mps|cpu|cuda` 　Demucs 运行设备，默认 `mps`（Apple Silicon），失败自动回退 CPU。
- `--no-cache` 　忽略缓存强制重新分离。

网页 GUI 例子：

```bash
./venv/bin/python karaoke.py ~/Music/song.mp3 --gui
```

## 播放器按键

| 键 | 作用 |
|----|------|
| `v` | 原唱 ⇄ 伴奏 切换 |
| `空格` | 暂停 / 继续 |
| `←` / `→` | 后退 / 快进 5 秒 |
| `q` | 退出 |

## 工作原理

1. **acquire.py** — 本地文件直接用；YouTube 用 yt-dlp 下载转 wav；Spotify 用 spotdl（经 YouTube Music 匹配，因 Spotify 音频有 DRM 无法直接取流）。
2. **separate.py** — `demucs --two-stems=vocals` 离线分离出 `vocals.wav` 和 `no_vocals.wav`（伴奏）。按文件内容 hash 缓存到 `cache/`，同一首歌第二次秒开。
3. **player.py** — 把伴奏与「人声+伴奏」两条轨载入内存，sounddevice 回调按当前模式取样，切换瞬时且不改变播放进度。

## 性能记录

| 设备 | 一首约 3 分钟歌曲分离耗时 |
|------|--------------------------|
| MPS (Apple Silicon) | ~44 秒 |
| CPU | 12 分钟以上 |
| 缓存命中（同一首第二次） | <0.1 秒 |

（实测：Benson Boone - Beautiful Things，192s 立体声。首次还需下载 ~80MB 模型。）

## 后续可加（非当前版本）

- 同步滚动歌词（LRCLIB / syncedlyrics）
- 麦克风跟唱混音 / 录音回放
- 实时流模式（BlackHole 捕获系统音频 + 滚动缓冲分离）
