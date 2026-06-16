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
- `--gui` 　打开**网页应用**（推荐）：歌库、搜索下载、播放控制全在浏览器里。声音仍从电脑扬声器出。
- `--device mps|cpu|cuda` 　Demucs 运行设备，默认 `mps`（Apple Silicon），失败自动回退 CPU。
- `--no-cache` 　忽略缓存强制重新分离。

## 网页应用（`--gui`）

直接打开应用，在浏览器里完成一切——**可以不带歌曲来源**：

```bash
./venv/bin/python karaoke.py --gui          # 空启动，进去搜索/选库
./venv/bin/python karaoke.py ~/Music/song.mp3 --gui   # 预处理一首再打开
```

功能：
- **搜索下载**：输入「歌名 + 歌手」→ YouTube 搜出多个候选（带封面/频道/时长）→ 点哪个下哪个 → 自动下载 + 去人声 + 入库 + 播放。
- **我的歌库**：列出所有处理过的歌，点一下即播。
- **播放控制**：播放 / 暂停、进度条拖动快进快退、原唱 ⇄ 伴奏切换。

> 搜索与下载需联网；歌库 / 分离 / 播放 / 切换 / 进度条都是本地操作。

## 命令行播放器按键（不带 `--gui` 时）

| 键 | 作用 |
|----|------|
| `v` | 原唱 ⇄ 伴奏 切换 |
| `空格` | 暂停 / 继续 |
| `←` / `→` | 后退 / 快进 5 秒 |
| `q` | 退出 |

## 工作原理

1. **acquire.py** — 本地文件直接用；YouTube 用 yt-dlp 下载转 320k mp3；Spotify 用 spotdl（经 YouTube Music 匹配，因 Spotify 音频有 DRM 无法直接取流）。`search_youtube()` 提供搜索候选。
2. **separate.py** — `demucs --two-stems=vocals` 离线分离出 `vocals.wav` 和 `no_vocals.wav`（伴奏）。按文件内容 hash 缓存到 `cache/`，同一首歌第二次秒开。
3. **library.py** — 扫描 `cache/` 列出所有已处理歌曲（歌库）。
4. **player.py** — 把伴奏与「人声+伴奏」两条轨载入内存，sounddevice 回调按当前模式取样，切换瞬时且不改变播放进度。
5. **webgui.py** — FastAPI 单页应用，串起歌库/搜索/下载/播放控制；下载+分离在后台线程跑，前端轮询进度。

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
