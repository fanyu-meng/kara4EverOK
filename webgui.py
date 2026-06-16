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
                     list_playlist, to_mp3, DOWNLOADS_DIR)
import library

DEVICE = "mps"

STATE = {"player": None, "song": None}
JOBS = {}
BATCHES = {}


# ---------------- 播放控制辅助 ----------------
def _load_song(song):
    """停掉旧 player，载入新歌并开始播放。song = {name, vocals, no_vocals}。"""
    old = STATE["player"]
    if old is not None:
        old.stop()
    p = KaraokePlayer(song["vocals"], song["no_vocals"])
    p.start()
    STATE["player"] = p
    STATE["song"] = song["name"]


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
            it["status"] = "downloading"
            it["pct"] = 0
            if it.get("url"):
                audio = download_youtube(it["url"], cancel=cancel, on_progress=prog)
            else:
                results = search_youtube(it["query"])
                if not results:
                    raise RuntimeError("搜不到这首歌")
                best = results[best_match_index(results)]
                audio = download_youtube(best["url"], cancel=cancel, on_progress=prog,
                                         song_title=best["title"],
                                         song_artist=best.get("uploader", ""))
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


class DownloadReq(BaseModel):
    url: str
    title: str = ""
    artist: str = ""


# ---------------- 应用 ----------------
def build_app():
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/library")
    def get_library():
        return JSONResponse(
            [{"id": s["id"], "name": s["name"]} for s in library.list_songs()])

    @app.post("/load")
    def load(req: LoadReq):
        song = library.get_song(req.id)
        if not song:
            return JSONResponse({"error": "找不到该歌曲"}, status_code=404)
        _load_song(song)
        return {"ok": True, "song": song["name"]}

    @app.get("/status")
    def status():
        p = STATE["player"]
        if p is None:
            return {"has_song": False}
        with p.lock:
            pos, total, mode, paused = p.pos, p.total, p.mode, p.paused
        return {
            "has_song": True,
            "song": STATE["song"],
            "mode": mode,
            "paused": paused,
            "pos_sec": pos / p.samplerate,
            "total_sec": total / p.samplerate,
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

    return app


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卡拉OK</title>
<style>
  :root { --bg:#0f1020; --panel:#1a1c34; --line:#2a2d4d; --txt:#fff;
          --blue:#2563eb; --pink:#db2777; --mut:#9aa0c0; }
  * { box-sizing:border-box; }
  body { margin:0; height:100vh; display:flex; color:var(--txt);
         font-family:-apple-system,system-ui,sans-serif; background:var(--bg); }
  .side { width:340px; flex:none; background:var(--panel); border-right:1px solid var(--line);
          display:flex; flex-direction:column; }
  .side h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
             color:var(--mut); margin:18px 18px 8px; }
  .searchbox { padding:12px 18px; border-bottom:1px solid var(--line); }
  .searchbox input { width:100%; padding:11px 12px; border-radius:10px; border:1px solid var(--line);
                     background:#0f1124; color:var(--txt); font-size:14px; }
  .searchbox button { margin-top:8px; width:100%; padding:10px; border:none; border-radius:10px;
                      background:var(--blue); color:#fff; font-size:14px; cursor:pointer; }
  .searchbox .local { background:#374151; }
  .sep { text-align:center; color:var(--mut); font-size:11px; margin:10px 0 2px; }
  .results, .lib { overflow:auto; }
  .results { border-bottom:1px solid var(--line); max-height:42%; }
  .lib { flex:1; }
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
                     padding:3px 6px; border:1px solid var(--line); border-radius:6px; }
  .libitem .stemdl:hover { color:#fff; border-color:var(--blue); }
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
    <div class="searchbox">
      <h2 style="margin:0 0 8px">搜索或粘贴 YouTube 链接</h2>
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
    <h2>我的歌库</h2>
    <div id="lib" class="lib"></div>
  </div>
  <div class="main">
    <div id="song" class="song empty">从右边歌库选一首，或上方搜索下载</div>
    <div class="progress">
      <input id="bar" type="range" min="0" max="100" value="0" step="0.1" disabled>
      <div class="times"><span id="cur">00:00</span><span id="dur">00:00</span></div>
    </div>
    <div class="controls">
      <button id="play" onclick="togglePause()" disabled>▶ 播放</button>
      <button id="vocal" class="instrumental" onclick="toggleVocal()" disabled>🎵 伴奏</button>
    </div>
    <div class="hint">声音从电脑扬声器播放</div>
  </div>

<script>
let seeking = false;
let activeId = null;

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

async function loadLibrary(){
  const songs = await (await fetch('/library')).json();
  const el = document.getElementById('lib');
  el.innerHTML = songs.length ? '' : '<div class="libitem empty">还没有处理过的歌</div>';
  for(const s of songs){
    const d = document.createElement('div');
    d.className = 'libitem' + (s.id===activeId?' active':'');
    const name = document.createElement('span');
    name.className = 'ln'; name.textContent = s.name; name.title = s.name;
    name.onclick = () => loadSong(s.id);
    const dli = document.createElement('a');
    dli.className = 'stemdl'; dli.textContent = '⬇伴奏'; dli.title = '下载伴奏文件';
    dli.href = '/download_stem/'+s.id+'/instrumental';
    const dlv = document.createElement('a');
    dlv.className = 'stemdl'; dlv.textContent = '⬇原唱'; dlv.title = '下载原唱文件';
    dlv.href = '/download_stem/'+s.id+'/original';
    d.append(name, dli, dlv);
    el.appendChild(d);
  }
}

async function loadSong(id){
  activeId = id;
  await fetch('/load', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})});
  loadLibrary();
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
        loadLibrary().then(()=>{ if(j.song_id) loadSong(j.song_id); }); },
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

const STAGE_TXT = {queued:'排队中', downloading:'下载', importing:'读取', separating:'分离'};

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
const BSTAGE = {queued:'⏳', downloading:'⬇️', separating:'🎛', done:'✓', error:'✗', cancelled:'⏹'};

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
    const done = b.items ? b.items.filter(it=>it.status==='done').length : 0;
    if(done !== lastDone){ lastDone = done; loadLibrary(); }
    if(['done','error','cancelled'].includes(b.status)){ clearInterval(batchTimer); batchTimer=null; }
  }, 1500);
}

function renderBatch(b){
  const box = document.getElementById('results');
  if(b.error){ box.innerHTML = '<div class="item">'+b.error+'</div>'; return; }
  if(b.stage==='enumerating'){ box.innerHTML = '<div class="item">解析歌单中…</div>'; return; }
  const done = b.items.filter(it=>it.status==='done').length;
  let html = `<div class="batchhead"><span>歌单 ${done}/${b.items.length} 完成</span>`;
  if(b.status==='running') html += `<button class="dl stop" onclick="cancelBatch()">■ 停止全部</button>`;
  html += `</div>`;
  b.items.forEach((it,i) => {
    const active = (it.status==='downloading' || it.status==='separating');
    const det = active ? (it.status==='downloading'?'下载':'分离') + (it.pct!=null?' '+it.pct+'%':'') : '';
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
    onDone: j => { box.innerHTML=''; loadLibrary().then(()=>{ if(j.song_id) loadSong(j.song_id); }); },
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
  const songEl = document.getElementById('song');
  const play = document.getElementById('play'), vocal = document.getElementById('vocal');
  if(!st.has_song){
    songEl.textContent='从右边歌库选一首，或上方搜索下载'; songEl.className='song empty';
    play.disabled=vocal.disabled=bar.disabled=true; return;
  }
  songEl.textContent='🎶 '+st.song; songEl.className='song';
  play.disabled=vocal.disabled=bar.disabled=false;
  renderPause(st.paused); renderVocal(st.mode);
  document.getElementById('dur').textContent = fmt(st.total_sec);
  if(!seeking){
    bar.max = st.total_sec; bar.value = st.pos_sec;
    document.getElementById('cur').textContent = fmt(st.pos_sec);
  }
}

loadLibrary();
setInterval(tick, 500);
</script>
</body>
</html>"""


def serve(initial=None, port: int = 8765, open_browser: bool = True):
    """启动 web 服务。initial = {name, vocals, no_vocals} 时预载一首。"""
    n = library.sync_instrumentals()  # 把已有伴奏汇总到 伴奏/ 文件夹
    print(f"伴奏已汇总到 {INSTRUMENTALS_DIR}（共 {n} 首）")
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
