#!/usr/bin/env python3
"""
网页 GUI：在浏览器里完成 搜索 / 下载 / 歌库 / 播放控制 全流程。
音频在本机扬声器播放，浏览器是控制面板。用已装好的 FastAPI + uvicorn，无需新依赖。

接口:
  GET  /            主页
  GET  /library     已处理歌曲列表
  POST /load        {id}  载入并播放某首
  GET  /status      当前播放状态(歌名/模式/暂停/进度)
  POST /toggle      原唱⇄伴奏
  POST /pause       播放/暂停
  POST /seek        {seconds} 跳转
  GET  /search?q=   YouTube 搜索候选（前端也可直接粘贴 YouTube 视频链接）
  POST /download    {url,title} 起后台任务(下载+分离), 返回 job_id
  GET  /job/{id}    查询后台任务进度
"""

import os
import uuid
import shutil
import threading
import webbrowser

import uvicorn
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from player import KaraokePlayer
from separate import separate, INSTRUMENTALS_DIR
from acquire import (search_youtube, download_youtube, best_match_index,
                     list_playlist, to_mp3, predicted_name, DOWNLOADS_DIR)
import library
import lyrics

DEVICE = "mps"

STATE = {"player": None, "song": None, "song_id": None, "lyrics": [],
         "device": None, "queue": []}  # queue 元素: {"id", "name"}
JOBS = {}
BATCHES = {}


# ---------------- 播放控制辅助 ----------------
def _load_song(song):
    """停掉旧 player，载入新歌并开始播放。song = {name, vocals, no_vocals}。"""
    old = STATE["player"]
    if old is not None:
        old.stop()
    p = KaraokePlayer(song["vocals"], song["no_vocals"])
    p.start(device=STATE["device"])
    STATE["player"] = p
    STATE["song"] = song["name"]
    STATE["song_id"] = song["id"]
    STATE["lyrics"] = lyrics.parse(lyrics.load(song["id"]) or "")


# ---------------- 点歌队列 ----------------
def _play_song_id(song_id) -> bool:
    """按 id 立即载入并播放某首；找不到返回 False。"""
    song = library.get_song(song_id)
    if not song:
        return False
    _load_song(song)
    return True


def _enqueue(song_id) -> dict:
    """排队：空闲（无歌/当前已放完）则立即开唱，否则排到队尾。"""
    song = library.get_song(song_id)
    if not song:
        return {"ok": False}
    p = STATE["player"]
    idle = p is None or p.done.is_set()
    if idle:
        _play_song_id(song_id)
        return {"ok": True, "started": True}
    STATE["queue"].append({"id": song_id, "name": song["name"]})
    return {"ok": True, "started": False}


def _advance() -> bool:
    """连播下一首：取队首播放并返回 True；遇到失效的歌则跳过继续，队列空返回 False。"""
    while STATE["queue"]:
        nxt = STATE["queue"].pop(0)
        if _play_song_id(nxt["id"]):
            return True
    return False


def _playback_watcher():
    """后台线程：当前歌放完且队列有歌时，自动连播下一首。"""
    import time
    while True:
        p = STATE["player"]
        if p is None:
            time.sleep(0.3)
            continue
        if not p.done.wait(timeout=0.5):
            continue                       # 还没放完，继续等
        if STATE["player"] is p and not getattr(p, "_ended", False):
            p._ended = True                # 防止队列为空时重复触发/忙等
            _advance()
        else:
            time.sleep(0.3)


class _Cancelled(Exception):
    pass


def _new_job(title):
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "stage": "queued", "song_id": None,
                    "error": None, "title": title, "pct": None,
                    "cancel": threading.Event()}
    return job_id


def _job_view(j):
    """给前端的可序列化视图（去掉 threading.Event）。"""
    return {k: v for k, v in j.items() if k != "cancel"}


def _run_job(job_id, url=None, local_path=None, title="", artist=""):
    """统一任务：在线下载 或 本地文件 → 分离 → 入库。支持中止。"""
    job = JOBS[job_id]
    cancel = job["cancel"]

    def prog(p):
        job["pct"] = round(p)

    try:
        if cancel.is_set():
            raise _Cancelled()
        if url:
            existing = library.find_by_name(predicted_name(title, artist))
            if existing:  # 歌库已有，直接复用，免下载+分离
                job["song_id"] = existing["id"]
                job["stage"] = "done"
                job["status"] = "done"
                return
            job["stage"] = "downloading"
            job["pct"] = 0
            audio = download_youtube(url, cancel=cancel, on_progress=prog,
                                     song_title=title, song_artist=artist)
        else:
            job["stage"] = "importing"
            audio = local_path
        if cancel.is_set():
            raise _Cancelled()
        job["stage"] = "separating"
        job["pct"] = 0
        _, no_vocals = separate(audio, device=DEVICE, use_cache=True,
                                cancel=cancel, on_progress=prog)
        job["song_id"] = library.id_for_stems(no_vocals)
        # best-effort 抓同步歌词：失败/查无都不影响歌曲入库
        job["stage"] = "fetching_lyrics"
        try:
            lrc = lyrics.fetch(title, artist)
            if lrc:
                lyrics.save(job["song_id"], lrc)
        except Exception:  # noqa: BLE001
            pass
        job["stage"] = "done"
        job["status"] = "done"
    except (_Cancelled, KeyboardInterrupt):
        job["status"] = "cancelled"
        job["stage"] = "cancelled"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


# ---------------- 歌单批量 ----------------
def _new_batch():
    bid = uuid.uuid4().hex[:12]
    BATCHES[bid] = {"status": "running", "stage": "enumerating",
                    "cancel": threading.Event(), "items": [],
                    "current": -1, "error": None}
    return bid


def _batch_view(b):
    return {
        "status": b["status"], "stage": b["stage"], "current": b["current"],
        "error": b["error"],
        "items": [{"title": it["title"], "status": it["status"],
                   "song_id": it.get("song_id"), "error": it.get("error"),
                   "pct": it.get("pct")}
                  for it in b["items"]],
    }


def _run_batch(batch_id, url):
    b = BATCHES[batch_id]
    cancel = b["cancel"]
    try:
        tracks = list_playlist(url)
    except Exception as e:  # noqa: BLE001
        b["status"] = "error"
        b["error"] = "解析歌单失败: " + str(e)
        b["stage"] = "done"
        return
    if not tracks:
        b["status"] = "error"
        b["error"] = "歌单为空或无法解析"
        b["stage"] = "done"
        return

    b["items"] = [{"title": t["title"], "status": "queued",
                   "url": t.get("url"), "query": t.get("query")} for t in tracks]
    b["stage"] = "processing"

    for i, it in enumerate(b["items"]):
        if cancel.is_set():
            break
        b["current"] = i

        def prog(p, _it=it):
            _it["pct"] = round(p)

        try:
            # 先定下下载地址和目标歌名；歌库已有则跳过下载+分离
            if it.get("url"):
                dl_url, st, sa = it["url"], "", ""
                predicted = predicted_name(it.get("title", ""))
            else:
                results = search_youtube(it["query"])
                if not results:
                    raise RuntimeError("搜不到这首歌")
                best = results[best_match_index(results)]
                dl_url = best["url"]
                st, sa = best["title"], best.get("uploader", "")
                predicted = predicted_name(st, sa)

            existing = library.find_by_name(predicted)
            if existing:
                it["song_id"] = existing["id"]
                it["status"] = "exists"
                continue

            it["status"] = "downloading"
            it["pct"] = 0
            audio = download_youtube(dl_url, cancel=cancel, on_progress=prog,
                                     song_title=st, song_artist=sa)
            if cancel.is_set():
                it["status"] = "cancelled"
                break
            it["status"] = "separating"
            it["pct"] = 0
            _, no_vocals = separate(audio, device=DEVICE, use_cache=True,
                                    cancel=cancel, on_progress=prog)
            it["song_id"] = library.id_for_stems(no_vocals)
            it["status"] = "done"
        except (_Cancelled, KeyboardInterrupt):
            it["status"] = "cancelled"
            break
        except Exception as e:  # noqa: BLE001
            it["status"] = "error"
            it["error"] = str(e)

    b["status"] = "cancelled" if cancel.is_set() else "done"
    b["stage"] = "done"


# ---------------- 请求体 ----------------
class LoadReq(BaseModel):
    id: str


class PlaylistReq(BaseModel):
    url: str


class SeekReq(BaseModel):
    seconds: float


class DeviceReq(BaseModel):
    device: int | None = None  # None=跟随系统默认输出


class DownloadReq(BaseModel):
    url: str
    title: str = ""
    artist: str = ""


class QueueIndexReq(BaseModel):
    index: int


# ---------------- 应用 ----------------
def build_app():
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/library")
    def get_library():
        return JSONResponse(
            [{"id": s["id"], "name": s["name"], "has_lyrics": s["has_lyrics"]}
             for s in library.list_songs()])

    @app.post("/load")
    def load(req: LoadReq):
        song = library.get_song(req.id)
        if not song:
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        _load_song(song)
        return {"ok": True, "song": song["name"]}

    @app.post("/queue/add")
    def queue_add(req: LoadReq):
        r = _enqueue(req.id)
        if not r["ok"]:
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        return r

    @app.post("/queue/play_now")
    def queue_play_now(req: LoadReq):
        if not _play_song_id(req.id):
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        return {"ok": True}

    @app.post("/queue/remove")
    def queue_remove(req: QueueIndexReq):
        if 0 <= req.index < len(STATE["queue"]):
            STATE["queue"].pop(req.index)
        return {"ok": True, "queue": STATE["queue"]}

    @app.post("/queue/prioritize")
    def queue_prioritize(req: QueueIndexReq):
        if 0 <= req.index < len(STATE["queue"]):
            STATE["queue"].insert(0, STATE["queue"].pop(req.index))
        return {"ok": True, "queue": STATE["queue"]}

    @app.post("/queue/next")
    def queue_next():
        if not _advance():           # 队列空：停掉当前，回到无歌状态
            p = STATE["player"]
            if p is not None:
                p.stop()
            STATE["player"] = None
            STATE["song"] = STATE["song_id"] = None
            STATE["lyrics"] = []
        return {"ok": True, "queue": STATE["queue"]}

    @app.get("/status")
    def status():
        p = STATE["player"]
        if p is None:
            return {"has_song": False, "queue": STATE["queue"]}
        with p.lock:
            pos, total, mode, paused = p.pos, p.total, p.mode, p.paused
        return {
            "has_song": True,
            "song": STATE["song"],
            "song_id": STATE["song_id"],
            "mode": mode,
            "paused": paused,
            "pos_sec": pos / p.samplerate,
            "total_sec": total / p.samplerate,
            "has_lyrics": bool(STATE["lyrics"]),
            "queue": STATE["queue"],
        }

    @app.post("/toggle")
    def toggle():
        p = STATE["player"]
        if p is None:
            return JSONResponse({"error": "无歌曲"}, status_code=409)
        p.toggle_mode()
        return {"mode": p.mode}

    @app.post("/pause")
    def pause():
        p = STATE["player"]
        if p is None:
            return JSONResponse({"error": "无歌曲"}, status_code=409)
        p.toggle_pause()
        return {"paused": p.paused}

    @app.post("/seek")
    def seek(req: SeekReq):
        p = STATE["player"]
        if p is None:
            return JSONResponse({"error": "无歌曲"}, status_code=409)
        p.seek_to(req.seconds)
        return {"ok": True}

    @app.get("/search")
    def search(q: str):
        try:
            results = search_youtube(q)
            return {"results": results, "best": best_match_index(results)}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/download")
    def download(req: DownloadReq):
        job_id = _new_job(req.title)
        threading.Thread(target=_run_job, daemon=True,
                         kwargs={"job_id": job_id, "url": req.url,
                                 "title": req.title, "artist": req.artist}).start()
        return {"job_id": job_id}

    @app.post("/import_local")
    async def import_local(file: UploadFile = File(...)):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        dest = os.path.join(DOWNLOADS_DIR, os.path.basename(file.filename))
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        dest = to_mp3(dest)  # 统一转 mp3
        job_id = _new_job(file.filename)
        threading.Thread(target=_run_job, daemon=True,
                         kwargs={"job_id": job_id, "local_path": dest,
                                 "title": file.filename}).start()
        return {"job_id": job_id}

    @app.post("/cancel/{job_id}")
    def cancel_job(job_id: str):
        j = JOBS.get(job_id)
        if not j:
            return JSONResponse({"error": "未知任务"}, status_code=404)
        j["cancel"].set()
        return {"ok": True}

    @app.get("/job/{job_id}")
    def job(job_id: str):
        j = JOBS.get(job_id)
        if not j:
            return JSONResponse({"error": "未知任务"}, status_code=404)
        return _job_view(j)

    @app.post("/import_playlist")
    def import_playlist(req: PlaylistReq):
        batch_id = _new_batch()
        threading.Thread(target=_run_batch, daemon=True,
                         kwargs={"batch_id": batch_id, "url": req.url}).start()
        return {"batch_id": batch_id}

    @app.get("/batch/{batch_id}")
    def batch(batch_id: str):
        b = BATCHES.get(batch_id)
        if not b:
            return JSONResponse({"error": "未知批次"}, status_code=404)
        return _batch_view(b)

    @app.post("/cancel_batch/{batch_id}")
    def cancel_batch(batch_id: str):
        b = BATCHES.get(batch_id)
        if not b:
            return JSONResponse({"error": "未知批次"}, status_code=404)
        b["cancel"].set()
        return {"ok": True}

    @app.get("/download_stem/{song_id}/{which}")
    def download_stem(song_id: str, which: str):
        song = library.get_song(song_id)
        if not song:
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        path = song["no_vocals"] if which == "instrumental" else song["vocals"]
        label = "伴奏" if which == "instrumental" else "原唱"
        ext = os.path.splitext(path)[1].lstrip(".") or "mp3"
        media = "audio/mpeg" if ext == "mp3" else "audio/wav"
        return FileResponse(path, filename=f"{song['name']} ({label}).{ext}",
                            media_type=media)

    @app.get("/devices")
    def get_devices():
        """列出所有可用输出声卡。current=null 表示“跟随系统默认”。"""
        import sounddevice as sd
        try:  # 重置 PortAudio，确保拿到最新的系统设备列表与默认
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001
            pass
        try:
            default_out = sd.default.device[1]
        except Exception:  # noqa: BLE001
            default_out = None
        devs = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                devs.append({"index": i, "name": d["name"],
                             "is_system_default": i == default_out})
        return {"devices": devs, "current": STATE["device"]}

    @app.post("/device")
    def set_device(req: DeviceReq):
        """切换输出声卡。device=null 跟随系统默认。正在播放则无缝切换、保持进度。"""
        STATE["device"] = req.device
        p = STATE["player"]
        if p is not None:
            try:
                p.set_device(req.device)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"切换声卡失败: {e}"}, status_code=500)
        return {"ok": True, "device": req.device}

    @app.get("/lyrics")
    def get_lyrics():
        """当前播放歌曲的同步歌词行 [{t, text}]。"""
        return {"lines": STATE["lyrics"]}

    @app.post("/fetch_lyrics/{song_id}")
    def fetch_lyrics(song_id: str):
        """给歌库里已有(或歌词不对)的歌补抓/重抓同步歌词。"""
        song = library.get_song(song_id)
        if not song:
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        # 歌名形如 "歌名-歌手"（acquire 下载命名）；拆不出就整名当搜索词
        title, _, artist = song["name"].rpartition("-")
        if not title:
            title, artist = song["name"], ""
        lrc = lyrics.fetch(title.strip(), artist.strip())
        if not lrc:
            return {"found": False}
        lyrics.save(song_id, lrc)
        if STATE["song_id"] == song_id:  # 正在播放这首，刷新内存里的歌词
            STATE["lyrics"] = lyrics.parse(lrc)
        return {"found": True}

    return app


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kara4EverOK · 卡拉永远OK</title>
<style>
  :root { --bg:#0f1020; --panel:#1a1c34; --line:#2a2d4d; --txt:#fff;
          --blue:#2563eb; --pink:#db2777; --mut:#9aa0c0; }
  * { box-sizing:border-box; }
  body { margin:0; height:100vh; display:flex; color:var(--txt);
         font-family:-apple-system,system-ui,sans-serif; background:var(--bg); }
  .side { width:340px; flex:none; background:var(--panel);
          display:flex; flex-direction:column; height:100vh; min-height:0; }
  .resizer { flex:none; width:6px; cursor:col-resize; background:var(--line);
             transition:background .12s; }
  .resizer:hover, .resizer.dragging { background:var(--blue); }
  .panel { display:flex; flex-direction:column; min-height:0; }
  .panelhead { display:flex; align-items:center; justify-content:space-between; gap:8px;
               width:100%; text-align:left; background:none; border:none; cursor:pointer;
               color:var(--mut); font-size:13px; text-transform:uppercase; letter-spacing:.08em;
               padding:11px 16px; border-bottom:1px solid var(--line); }
  .panelhead .chev { transition:transform .15s; }
  .panel.collapsed .panelhead .chev { transform:rotate(-90deg); }
  .panel.collapsed .panelbody { display:none; }
  .panelbody { display:flex; flex-direction:column; min-height:0; overflow:hidden; }
  .tabs-panel { flex:1 1 0; min-height:0; }
  .tabbar { display:flex; flex:none; border-bottom:1px solid var(--line); }
  .tab { flex:1; background:none; border:none; cursor:pointer; color:var(--mut);
         font-size:12px; text-transform:uppercase; letter-spacing:.06em; padding:11px 8px;
         border-bottom:2px solid transparent; }
  .tab.active { color:var(--txt); border-bottom-color:var(--blue); }
  .tab .cnt { font-weight:400; text-transform:none; color:var(--mut); }
  .tabpane { flex:1 1 0; min-height:0; overflow:auto; }
  .searchbox { padding:10px 16px; flex:none; }
  .searchbox input { width:100%; padding:9px 12px; border-radius:10px; border:1px solid var(--line);
                     background:#0f1124; color:var(--txt); font-size:14px; }
  .searchbox button { margin-top:6px; width:100%; padding:9px; border:none; border-radius:10px;
                      background:var(--blue); color:#fff; font-size:14px; cursor:pointer; }
  .searchbox .local { background:#374151; padding:8px; font-size:13px; }
  .sep { text-align:center; color:var(--mut); font-size:11px; margin:7px 0 1px; }
  .results { overflow:auto; border-bottom:1px solid var(--line); max-height:32vh; flex:none; }
  .item { display:flex; gap:10px; align-items:center; padding:9px 14px; cursor:pointer;
          border-bottom:1px solid var(--line); }
  .item:hover { background:#22254a; }
  .item img { width:64px; height:40px; object-fit:cover; border-radius:5px; flex:none; background:#000; }
  .item .meta { min-width:0; flex:1; }
  .item .t { font-size:13px; line-height:1.25; max-height:2.5em; overflow:hidden; }
  .item .s { font-size:11px; color:var(--mut); margin-top:2px; }
  .item .dl { font-size:12px; padding:6px 10px; border:none; border-radius:8px;
              background:var(--pink); color:#fff; cursor:pointer; flex:none; min-width:64px; }
  .item .dl.stop { background:#6b7280; }
  .item.best { background:#1e2550; }
  .item.best .meta .t::after { content:'  ★自动'; color:#fbbf24; font-size:11px; }
  .libitem { display:flex; align-items:center; gap:6px; padding:9px 12px 9px 16px;
             border-bottom:1px solid var(--line); font-size:14px; }
  .libitem:hover { background:#22254a; }
  .libitem.active { background:var(--blue); }
  .libitem .ln { flex:1; min-width:0; cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .libitem .stemdl { flex:none; font-size:11px; color:var(--mut); text-decoration:none;
                     padding:3px 6px; border:1px solid var(--line); border-radius:6px;
                     display:none; }
  .libitem:hover .stemdl, .libitem.active .stemdl { display:inline-block; }
  .libitem .stemdl:hover { color:#fff; border-color:var(--blue); }
  .libitem .pn { flex:none; font-size:12px; color:var(--mut); cursor:pointer;
                 padding:3px 6px; border:1px solid var(--line); border-radius:6px; background:none; }
  .libitem .pn:hover { color:#fff; border-color:var(--pink); }
  .qitem.empty, .libitem.empty { color:var(--mut); justify-content:center; padding:16px; }
  .qitem { display:flex; align-items:center; gap:8px; padding:8px 12px 8px 16px;
           border-bottom:1px solid var(--line); font-size:13px; }
  .qitem .qn { flex:none; width:18px; color:var(--mut); text-align:right; }
  .qitem .qt { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .qitem .qb { flex:none; cursor:pointer; background:none; border:none; color:var(--mut);
               font-size:14px; padding:2px 4px; }
  .qitem .qb:hover { color:#fff; }
  .main { flex:1; display:flex; flex-direction:column; align-items:center;
          justify-content:center; gap:26px; padding:30px; }
  .song { font-size:26px; font-weight:600; text-align:center; min-height:32px; }
  .controls { display:flex; gap:16px; align-items:center; }
  .controls button { font-size:18px; padding:16px 26px; border:none; border-radius:14px;
                     color:#fff; cursor:pointer; }
  #play { background:#374151; min-width:120px; }
  #vocal { min-width:240px; }
  .instrumental { background:var(--blue); }
  .original { background:var(--pink); }
  .progress { width:min(560px,80%); display:flex; flex-direction:column; gap:6px; }
  .progress input { width:100%; }
  .times { display:flex; justify-content:space-between; font-size:12px; color:var(--mut); }
  .empty { color:var(--mut); }
  .hint { font-size:13px; color:var(--mut); }
  .devrow { display:flex; gap:8px; align-items:center; font-size:13px; color:var(--mut); }
  .devrow select { background:#0f1124; color:var(--txt); border:1px solid var(--line);
                   border-radius:8px; padding:6px 10px; font-size:13px; max-width:300px; cursor:pointer; }
  .lyrics { width:min(640px,90%); height:38vh; overflow-y:auto; text-align:center;
            display:flex; flex-direction:column; gap:10px; padding:8vh 0;
            mask-image:linear-gradient(transparent, #000 18%, #000 82%, transparent);
            -webkit-mask-image:linear-gradient(transparent, #000 18%, #000 82%, transparent); }
  .lyrics .line { font-size:16px; color:var(--mut); line-height:1.4; transition:all .2s; }
  .lyrics .line.active { font-size:22px; font-weight:600; color:var(--pink); }
  .lyrics .placeholder { color:var(--mut); font-size:14px; }
  .lyrics .lyrbtn { margin:10px auto 0; padding:8px 16px; border:none; border-radius:10px;
                    background:var(--pink); color:#fff; font-size:13px; cursor:pointer; }
  .lyrics .lyrbtn:disabled { background:#6b7280; cursor:default; }
  .batchhead { display:flex; align-items:center; justify-content:space-between; gap:8px;
               padding:9px 14px; font-size:12px; color:var(--mut); border-bottom:1px solid var(--line); }
  .batchhead button { font-size:11px; padding:4px 8px; }
  .bitem { display:flex; gap:8px; align-items:center; padding:7px 14px; font-size:12px;
           border-bottom:1px solid var(--line); }
  .bitem .bi { flex:none; width:16px; text-align:center; }
  .bitem .bt { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bitem .bd { flex:none; font-size:11px; color:#8ab4ff; }
  .bitem.cur { background:#1e2550; }
</style>
</head>
<body>
  <div class="side">
    <div class="panel" id="searchpanel">
      <button class="panelhead" onclick="togglePanel('searchpanel')">
        <span>🔍 搜索 / 导入</span><span class="chev">▾</span>
      </button>
      <div class="panelbody">
        <div class="searchbox">
          <input id="q" placeholder="歌名、歌手，或 YouTube 视频链接"
                 onkeydown="if(event.key==='Enter')doSearch()">
          <button onclick="doSearch()">🔍 搜索 / 下载</button>
          <div class="sep">— 或 —</div>
          <input id="file" type="file" accept="audio/*" style="display:none" onchange="importLocal(this)">
          <button class="local" onclick="document.getElementById('file').click()">📁 选择本地文件分离</button>
          <div class="sep">— 或 整个歌单 —</div>
          <input id="pl" placeholder="粘贴 Spotify / YouTube / Bilibili 歌单链接"
                 onkeydown="if(event.key==='Enter')importPlaylist()">
          <button class="local" onclick="importPlaylist()">📜 导入整个歌单</button>
        </div>
        <div id="results" class="results"></div>
      </div>
    </div>
    <div class="panel tabs-panel">
      <div class="tabbar">
        <button class="tab active" id="tab-lib"   onclick="switchTab('lib')">🎤 我的歌库 <span id="libcount" class="cnt"></span></button>
        <button class="tab"        id="tab-queue" onclick="switchTab('queue')">🎵 待唱队列 <span id="qcount" class="cnt"></span></button>
      </div>
      <div id="lib"   class="lib tabpane"></div>
      <div id="queue" class="queue tabpane" style="display:none"></div>
    </div>
  </div>
  <div class="resizer" id="resizer" title="拖拽调整宽度，双击复位"></div>
  <div class="main">
    <div id="song" class="song empty">从右边歌库选一首，或上方搜索下载</div>
    <div id="lyrics" class="lyrics"></div>
    <div class="progress">
      <input id="bar" type="range" min="0" max="100" value="0" step="0.1" disabled>
      <div class="times"><span id="cur">00:00</span><span id="dur">00:00</span></div>
    </div>
    <div class="controls">
      <button id="play" onclick="togglePause()" disabled>▶ 播放</button>
      <button id="vocal" class="instrumental" onclick="toggleVocal()" disabled>🎵 伴奏</button>
      <button id="skip" onclick="skipNext()" disabled title="跳到队列下一首">⏭ 下一首</button>
    </div>
    <div class="devrow">
      <span>🔊 输出</span>
      <select id="dev" onchange="setDevice()"></select>
    </div>
  </div>

<script>
let seeking = false;
let activeId = null;
let lyricLines = [];   // [{t, text}]
let curLyricIdx = -1;
let fetchingLyrics = false;

function fmt(s){ s=Math.max(0,Math.floor(s||0)); return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); }

function isYouTubeUrl(s){
  try {
    const u = new URL(s);
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    return host === 'youtu.be' || host === 'youtube.com' || host.endsWith('.youtube.com');
  } catch(e) {
    return false;
  }
}

function switchTab(name){
  const isLib = name === 'lib';
  document.getElementById('lib').style.display   = isLib ? '' : 'none';
  document.getElementById('queue').style.display = isLib ? 'none' : '';
  document.getElementById('tab-lib').classList.toggle('active', isLib);
  document.getElementById('tab-queue').classList.toggle('active', !isLib);
  localStorage.setItem('activeTab', name);
}
function togglePanel(id){
  const p = document.getElementById(id);
  p.classList.toggle('collapsed');
  localStorage.setItem('panel.'+id, p.classList.contains('collapsed') ? '1' : '0');
}
function initResizer(){
  const side = document.querySelector('.side');
  const rez = document.getElementById('resizer');
  const saved = parseInt(localStorage.getItem('sideWidth'), 10);
  if(saved) side.style.width = saved + 'px';
  let startX = 0, startW = 0;
  function onMove(e){
    const w = Math.min(640, Math.max(240, startW + (e.clientX - startX)));
    side.style.width = w + 'px';
  }
  function onUp(){
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    rez.classList.remove('dragging');
    document.body.style.userSelect = '';
    localStorage.setItem('sideWidth', parseInt(side.getBoundingClientRect().width, 10));
  }
  rez.addEventListener('mousedown', e => {
    startX = e.clientX; startW = side.getBoundingClientRect().width;
    rez.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });
  rez.addEventListener('dblclick', () => {
    side.style.width = '340px'; localStorage.removeItem('sideWidth');
  });
}
function restoreUI(){
  if(localStorage.getItem('panel.searchpanel') === '1')
    document.getElementById('searchpanel').classList.add('collapsed');
  switchTab(localStorage.getItem('activeTab') || 'lib');
  initResizer();
}

async function loadLibrary(){
  const songs = await (await fetch('/library')).json();
  document.getElementById('libcount').textContent = songs.length ? '('+songs.length+')' : '';
  const el = document.getElementById('lib');
  el.innerHTML = songs.length ? '' : '<div class="libitem empty">还没有处理过的歌</div>';
  for(const s of songs){
    const d = document.createElement('div');
    d.className = 'libitem' + (s.id===activeId?' active':'');
    const name = document.createElement('span');
    name.className = 'ln'; name.textContent = (s.has_lyrics ? '🎤 ' : '') + s.name;
    name.title = s.name + (s.has_lyrics ? '（含歌词）' : '') + ' · 点击排入队列';
    name.onclick = () => enqueue(s.id);
    const pn = document.createElement('button');
    pn.className = 'pn'; pn.textContent = '▶'; pn.title = '立即播放（不进队列）';
    pn.onclick = (e) => { e.stopPropagation(); playNow(s.id); };
    const dli = document.createElement('a');
    dli.className = 'stemdl'; dli.textContent = '⬇伴奏'; dli.title = '下载伴奏文件';
    dli.href = '/download_stem/'+s.id+'/instrumental';
    const dlv = document.createElement('a');
    dlv.className = 'stemdl'; dlv.textContent = '⬇原唱'; dlv.title = '下载原唱文件';
    dlv.href = '/download_stem/'+s.id+'/original';
    d.append(name, pn, dli, dlv);
    el.appendChild(d);
  }
}

async function loadSong(id){
  activeId = id;
  await fetch('/load', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})});
  loadLibrary();
  loadLyrics();
}

const POSTJSON = (url, obj) => fetch(url, {method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(obj)});

async function enqueue(id){
  // 空闲时后端会立即开唱；tick() 会自动同步 activeId/歌词与队列显示
  await POSTJSON('/queue/add', {id});
}
async function playNow(id){
  activeId = id;
  await POSTJSON('/queue/play_now', {id});
  loadLibrary(); loadLyrics();
}
async function skipNext(){ await fetch('/queue/next', {method:'POST'}); loadLyrics(); }
async function queueRemove(i){ await POSTJSON('/queue/remove', {index:i}); }
async function queuePrioritize(i){ await POSTJSON('/queue/prioritize', {index:i}); }

let lastQueueKey = null;
function renderQueue(queue){
  queue = queue || [];
  const key = queue.map(q=>q.id).join('|');
  if(key === lastQueueKey) return;   // 没变就不重绘，避免闪烁
  lastQueueKey = key;
  const box = document.getElementById('queue');
  document.getElementById('qcount').textContent = queue.length ? '('+queue.length+')' : '';
  box.innerHTML = queue.length ? '' : '<div class="qitem empty">队列为空</div>';
  queue.forEach((q, i) => {
    const d = document.createElement('div');
    d.className = 'qitem';
    const n = document.createElement('span'); n.className='qn'; n.textContent = (i+1);
    const t = document.createElement('span'); t.className='qt'; t.textContent = q.name; t.title = q.name;
    const up = document.createElement('button'); up.className='qb'; up.textContent='⤴';
    up.title='插到下一首'; up.onclick = () => queuePrioritize(i);
    const rm = document.createElement('button'); rm.className='qb'; rm.textContent='✕';
    rm.title='移除'; rm.onclick = () => queueRemove(i);
    d.append(n, t, up, rm);
    box.appendChild(d);
  });
}

async function loadLyrics(){
  curLyricIdx = -1;
  try { lyricLines = (await (await fetch('/lyrics')).json()).lines || []; }
  catch(e){ lyricLines = []; }
  renderLyrics();
}

function renderLyrics(){
  const box = document.getElementById('lyrics');
  if(lyricLines.length){
    box.innerHTML = lyricLines.map((l,i) =>
      `<div class="line" data-i="${i}">${(l.text||'♪').replace(/</g,'&lt;')}</div>`).join('');
    return;
  }
  // 无歌词：占位 + 补抓按钮
  const dis = fetchingLyrics ? 'disabled' : '';
  const txt = fetchingLyrics ? '找歌词中…' : '🎤 下载歌词';
  box.innerHTML = `<div class="placeholder">暂无歌词</div>`+
    (activeId ? `<button class="lyrbtn" ${dis} onclick="downloadLyrics()">${txt}</button>` : '');
}

async function downloadLyrics(){
  if(!activeId || fetchingLyrics) return;
  fetchingLyrics = true; renderLyrics();
  let found = false;
  try { found = (await (await fetch('/fetch_lyrics/'+activeId, {method:'POST'})).json()).found; }
  catch(e){}
  fetchingLyrics = false;
  if(found){ await loadLyrics(); loadLibrary(); }
  else { renderLyrics(); }
}

function highlightLyric(posSec){
  if(!lyricLines.length) return;
  let idx = -1;
  for(let i=0;i<lyricLines.length;i++){ if(lyricLines[i].t <= posSec) idx = i; else break; }
  if(idx === curLyricIdx) return;
  curLyricIdx = idx;
  const box = document.getElementById('lyrics');
  box.querySelectorAll('.line.active').forEach(e => e.classList.remove('active'));
  if(idx < 0) return;
  const el = box.querySelector('.line[data-i="'+idx+'"]');
  if(el){ el.classList.add('active'); el.scrollIntoView({block:'center', behavior:'smooth'}); }
}

async function doSearch(){
  const q = document.getElementById('q').value.trim();
  if(!q) return;
  const box = document.getElementById('results');
  if(isYouTubeUrl(q)){
    box.innerHTML = '';
    const it = document.createElement('div');
    it.className = 'item best';
    const meta = document.createElement('div');
    meta.className = 'meta';
    const title = document.createElement('div');
    title.className = 't';
    title.textContent = 'YouTube 视频链接';
    const sub = document.createElement('div');
    sub.className = 's';
    sub.textContent = q;
    meta.append(title, sub);
    const btn = document.createElement('button');
    btn.className = 'dl';
    btn.textContent = '下载';
    btn.onclick = () => { if(btn.dataset.job) cancelDownload(btn); else startDownload(btn, q, 'YouTube 视频链接'); };
    it.append(meta, btn);
    box.appendChild(it);
    startDownload(btn, q, 'YouTube 视频链接');
    return;
  }
  box.innerHTML = '<div class="item">搜索中…</div>';
  let resp;
  try { resp = await (await fetch('/search?q='+encodeURIComponent(q))).json(); }
  catch(e){ box.innerHTML = '<div class="item">搜索失败</div>'; return; }
  if(resp.error){ box.innerHTML = '<div class="item">搜索失败：'+resp.error+'</div>'; return; }
  const data = resp.results || []; const best = resp.best || 0;
  box.innerHTML = '';
  const btns = [];
  data.forEach((r, i) => {
    const it = document.createElement('div');
    it.className = 'item' + (i===best ? ' best' : '');
    it.innerHTML = `<img src="${r.thumbnail}" onerror="this.style.visibility='hidden'">
      <div class="meta"><div class="t">${r.title}</div>
      <div class="s">${r.uploader||''}${r.duration?' · '+fmt(r.duration):''}</div></div>`;
    const btn = document.createElement('button');
    btn.className = 'dl'; btn.textContent = '下载';
    btn.onclick = () => { if(btn.dataset.job) cancelDownload(btn); else startDownload(btn, r.url, r.title, r.uploader||''); };
    it.appendChild(btn);
    box.appendChild(it);
    btns.push(btn);
  });
  // 自动下载”最像”的那一首，用户可点■停止后改下别的
  if(btns[best]) startDownload(btns[best], data[best].url, data[best].title, data[best].uploader||'');
}

function startDownload(btn, url, title, artist=''){
  btn.disabled = false; btn.classList.add('stop'); btn.textContent = '■ 处理中…';
  fetch('/download', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url, title, artist})}).then(r=>r.json()).then(({job_id}) => {
    btn.dataset.job = job_id;
    pollJob(job_id, {
      onStage: j => { if(!btn.dataset.job) return;
        btn.textContent = '■ '+stageLabel(j);
        const p = (j.pct!=null) ? j.pct : 0;
        btn.style.backgroundImage = `linear-gradient(90deg, var(--blue) ${p}%, transparent ${p}%)`; },
      onDone: j => { delete btn.dataset.job; btn.classList.remove('stop'); btn.disabled=true;
        btn.style.backgroundImage=''; btn.textContent='✓ 已入库';
        loadLibrary().then(()=>{ if(j.song_id) enqueue(j.song_id); }); },
      onError: j => { delete btn.dataset.job; btn.classList.remove('stop'); btn.disabled=false;
        btn.style.backgroundImage='';
        if(j.status==='cancelled'){ btn.textContent='下载'; }
        else { btn.textContent='重试'; alert('处理失败：'+(j.error||'')); } },
    });
  });
}

function cancelDownload(btn){
  const job = btn.dataset.job; if(!job) return;
  btn.textContent = '停止中…';
  fetch('/cancel/'+job, {method:'POST'});
}

const STAGE_TXT = {queued:'排队中', downloading:'下载', importing:'读取', separating:'分离', fetching_lyrics:'找歌词'};

function stageLabel(j){
  let t = STAGE_TXT[j.stage] || '处理';
  if(j.pct!=null && (j.stage==='downloading' || j.stage==='separating')) return t+' '+j.pct+'%';
  return j.stage==='queued' ? t+'…' : t+'中…';
}

function pollJob(job_id, {onStage, onDone, onError}={}){
  const t = setInterval(async () => {
    let j; try { j = await (await fetch('/job/'+job_id)).json(); } catch(e){ return; }
    onStage && onStage(j);
    if(j.status==='done'){ clearInterval(t); onDone && onDone(j); }
    else if(j.status==='error'){ clearInterval(t); onError && onError(j); }
    else if(j.status==='cancelled'){ clearInterval(t); onError && onError(j); }
  }, 1200);
  return t;
}

let curBatchId = null, batchTimer = null;
const BSTAGE = {queued:'⏳', downloading:'⬇️', separating:'🎛', done:'✓', exists:'📚', error:'✗', cancelled:'⏹'};

async function importPlaylist(){
  const url = document.getElementById('pl').value.trim(); if(!url) return;
  const box = document.getElementById('results');
  box.innerHTML = '<div class="item">解析歌单中…</div>';
  let batch_id;
  try { ({batch_id} = await (await fetch('/import_playlist',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})})).json()); }
  catch(e){ box.innerHTML='<div class="item">导入失败</div>'; return; }
  curBatchId = batch_id;
  if(batchTimer) clearInterval(batchTimer);
  let lastDone = -1;
  batchTimer = setInterval(async () => {
    let b; try { b = await (await fetch('/batch/'+batch_id)).json(); } catch(e){ return; }
    renderBatch(b);
    const done = b.items ? b.items.filter(it=>it.status==='done'||it.status==='exists').length : 0;
    if(done !== lastDone){ lastDone = done; loadLibrary(); }
    if(['done','error','cancelled'].includes(b.status)){ clearInterval(batchTimer); batchTimer=null; }
  }, 1500);
}

function renderBatch(b){
  const box = document.getElementById('results');
  if(b.error){ box.innerHTML = '<div class="item">'+b.error+'</div>'; return; }
  if(b.stage==='enumerating'){ box.innerHTML = '<div class="item">解析歌单中…</div>'; return; }
  const done = b.items.filter(it=>it.status==='done'||it.status==='exists').length;
  let html = `<div class="batchhead"><span>歌单 ${done}/${b.items.length} 完成</span>`;
  if(b.status==='running') html += `<button class="dl stop" onclick="cancelBatch()">■ 停止全部</button>`;
  html += `</div>`;
  b.items.forEach((it,i) => {
    const active = (it.status==='downloading' || it.status==='separating');
    const det = active ? (it.status==='downloading'?'下载':'分离') + (it.pct!=null?' '+it.pct+'%':'')
              : (it.status==='exists' ? '已有' : '');
    const fill = (i===b.current && active && it.pct!=null)
      ? ` style="background:linear-gradient(90deg,#27306a ${it.pct}%, transparent ${it.pct}%)"` : '';
    html += `<div class="bitem${i===b.current?' cur':''}"${fill}><span class="bi">${BSTAGE[it.status]||''}</span>`+
            `<span class="bt" title="${(it.error||'').replace(/"/g,'')}">${it.title}</span>`+
            (det?`<span class="bd">${det}</span>`:'')+`</div>`;
  });
  box.innerHTML = html;
}

async function cancelBatch(){
  if(curBatchId) await fetch('/cancel_batch/'+curBatchId, {method:'POST'});
}

async function importLocal(input){
  const f = input.files[0]; input.value=''; if(!f) return;
  const box = document.getElementById('results');
  box.innerHTML = '<div class="item">上传 “'+f.name+'” …</div>';
  const fd = new FormData(); fd.append('file', f);
  let job_id;
  try { ({job_id} = await (await fetch('/import_local',{method:'POST', body:fd})).json()); }
  catch(e){ box.innerHTML='<div class="item">上传失败</div>'; return; }
  pollJob(job_id, {
    onStage: j => box.innerHTML = '<div class="item">'+stageLabel(j)+' · '+f.name+'</div>',
    onDone: j => { box.innerHTML=''; loadLibrary().then(()=>{ if(j.song_id) enqueue(j.song_id); }); },
    onError: j => box.innerHTML='<div class="item">处理失败：'+(j.error||'')+'</div>',
  });
}

async function togglePause(){ const j = await (await fetch('/pause',{method:'POST'})).json(); renderPause(j.paused); }
async function toggleVocal(){ const j = await (await fetch('/toggle',{method:'POST'})).json(); renderVocal(j.mode); }

function renderPause(p){ document.getElementById('play').textContent = p ? '▶ 播放' : '⏸ 暂停'; }
function renderVocal(mode){
  const b = document.getElementById('vocal');
  if(mode==='original'){ b.className='original'; b.textContent='🎤 原唱（点击切伴奏）'; }
  else { b.className='instrumental'; b.textContent='🎵 伴奏（点击切原唱）'; }
}

const bar = document.getElementById('bar');
bar.addEventListener('input', () => { seeking = true; document.getElementById('cur').textContent = fmt(bar.value); });
bar.addEventListener('change', async () => {
  await fetch('/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seconds:parseFloat(bar.value)})});
  seeking = false;
});

async function tick(){
  let st; try { st = await (await fetch('/status')).json(); } catch(e){ return; }
  renderQueue(st.queue);
  const songEl = document.getElementById('song');
  const play = document.getElementById('play'), vocal = document.getElementById('vocal');
  const skip = document.getElementById('skip');
  const hasQueue = (st.queue && st.queue.length > 0);
  if(!st.has_song){
    songEl.textContent='从右边歌库选一首，或上方搜索下载'; songEl.className='song empty';
    play.disabled=vocal.disabled=bar.disabled=true;
    skip.disabled = !hasQueue;
    if(activeId){ activeId=null; loadLibrary(); }
    if(lyricLines.length){ lyricLines=[]; renderLyrics(); }
    return;
  }
  songEl.textContent='🎶 '+st.song; songEl.className='song';
  play.disabled=vocal.disabled=bar.disabled=false;
  skip.disabled = !hasQueue;
  renderPause(st.paused); renderVocal(st.mode);
  document.getElementById('dur').textContent = fmt(st.total_sec);
  if(!seeking){
    bar.max = st.total_sec; bar.value = st.pos_sec;
    document.getElementById('cur').textContent = fmt(st.pos_sec);
  }
  if(st.song_id && st.song_id !== activeId){  // 刷新页面/自动连播/外部载入：同步歌库高亮与歌词
    activeId = st.song_id; loadLyrics(); loadLibrary();
  }
  highlightLyric(st.pos_sec);
}

async function loadDevices(){
  let data; try { data = await (await fetch('/devices')).json(); } catch(e){ return; }
  const sel = document.getElementById('dev');
  const cur = data.current;  // null = 跟随系统
  let html = '<option value="">系统默认（跟随系统）</option>';
  for(const d of data.devices){
    const star = d.is_system_default ? ' ★系统' : '';
    html += `<option value="${d.index}">${d.name}${star}</option>`;
  }
  sel.innerHTML = html;
  sel.value = (cur==null) ? '' : String(cur);
}

async function setDevice(){
  const v = document.getElementById('dev').value;
  const device = (v==='') ? null : parseInt(v, 10);
  await fetch('/device', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({device})});
}

restoreUI();
loadLibrary();
loadDevices();
setInterval(tick, 500);
</script>
</body>
</html>"""


def _backfill_lyrics():
    """后台线程：为歌库里所有尚无歌词的歌逐首补抓 LRC，不阻塞主服务。"""
    songs = [s for s in library.list_songs() if not s["has_lyrics"]]
    if not songs:
        return
    print(f"[歌词] 开始为 {len(songs)} 首歌补抓同步歌词…")
    done = 0
    for s in songs:
        title, _, artist = s["name"].rpartition("-")
        if not title:
            title, artist = s["name"], ""
        lrc = lyrics.fetch(title.strip(), artist.strip())
        if lrc:
            lyrics.save(s["id"], lrc)
            done += 1
            print(f"[歌词] ✓ {s['name']}")
            # 如果正好在播这首，刷新内存歌词
            if STATE["song_id"] == s["id"]:
                STATE["lyrics"] = lyrics.parse(lrc)
        else:
            print(f"[歌词] - {s['name']}（未找到同步歌词）")
    print(f"[歌词] 补抓完成，成功 {done}/{len(songs)} 首")


def serve(initial=None, port: int = 8765, open_browser: bool = True):
    """启动 web 服务。initial = {name, vocals, no_vocals} 时预载一首。"""
    n = library.sync_instrumentals()  # 把已有伴奏汇总到 伴奏/ 文件夹
    print(f"伴奏已汇总到 {INSTRUMENTALS_DIR}（共 {n} 首）")
    threading.Thread(target=_backfill_lyrics, daemon=True, name="lyrics-backfill").start()
    threading.Thread(target=_playback_watcher, daemon=True, name="playback-watcher").start()
    if initial is not None:
        _load_song(initial)
    app = build_app()
    url = f"http://127.0.0.1:{port}"
    print(f"打开浏览器: {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        p = STATE["player"]
        if p is not None:
            p.stop()


def play(vocals_path: str, no_vocals_path: str, song_name: str, port: int = 8765):
    """向后兼容入口：预载一首后启动 web 服务。"""
    serve({"name": song_name, "vocals": vocals_path, "no_vocals": no_vocals_path}, port)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        name = sys.argv[3] if len(sys.argv) > 3 else os.path.basename(
            os.path.dirname(sys.argv[2]))
        play(sys.argv[1], sys.argv[2], name)
    else:
        serve()
