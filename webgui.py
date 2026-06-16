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
  GET  /search?q=   YouTube 搜索候选
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
from separate import separate
from acquire import search_youtube, download_youtube, DOWNLOADS_DIR
import library

DEVICE = "mps"

STATE = {"player": None, "song": None}
JOBS = {}


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
                    "error": None, "title": title, "cancel": threading.Event()}
    return job_id


def _job_view(j):
    """给前端的可序列化视图（去掉 threading.Event）。"""
    return {k: v for k, v in j.items() if k != "cancel"}


def _run_job(job_id, url=None, local_path=None, title=""):
    """统一任务：在线下载 或 本地文件 → 分离 → 入库。支持中止。"""
    job = JOBS[job_id]
    cancel = job["cancel"]
    try:
        if cancel.is_set():
            raise _Cancelled()
        if url:
            job["stage"] = "downloading"
            audio = download_youtube(url, cancel=cancel)
        else:
            job["stage"] = "importing"
            audio = local_path
        if cancel.is_set():
            raise _Cancelled()
        job["stage"] = "separating"
        _, no_vocals = separate(audio, device=DEVICE, use_cache=True, cancel=cancel)
        job["song_id"] = library.id_for_stems(no_vocals)
        job["stage"] = "done"
        job["status"] = "done"
    except _Cancelled:
        job["status"] = "cancelled"
        job["stage"] = "cancelled"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


# ---------------- 请求体 ----------------
class LoadReq(BaseModel):
    id: str


class SeekReq(BaseModel):
    seconds: float


class DownloadReq(BaseModel):
    url: str
    title: str = ""


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
            return JSONResponse(search_youtube(q))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/download")
    def download(req: DownloadReq):
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "stage": "queued",
                        "song_id": None, "error": None, "title": req.title}
        threading.Thread(target=_run_job, args=(job_id, req.url, req.title),
                         daemon=True).start()
        return {"job_id": job_id}

    @app.get("/job/{job_id}")
    def job(job_id: str):
        j = JOBS.get(job_id)
        if not j:
            return JSONResponse({"error": "未知任务"}, status_code=404)
        return j

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
              background:var(--pink); color:#fff; cursor:pointer; flex:none; }
  .libitem { padding:11px 16px; cursor:pointer; border-bottom:1px solid var(--line); font-size:14px; }
  .libitem:hover { background:#22254a; }
  .libitem.active { background:var(--blue); }
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
</style>
</head>
<body>
  <div class="side">
    <div class="searchbox">
      <h2 style="margin:0 0 8px">搜索歌曲</h2>
      <input id="q" placeholder="歌名 + 歌手，例如：Beautiful Things Benson Boone"
             onkeydown="if(event.key==='Enter')doSearch()">
      <button onclick="doSearch()">🔍 在 YouTube 搜索</button>
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

async function loadLibrary(){
  const songs = await (await fetch('/library')).json();
  const el = document.getElementById('lib');
  el.innerHTML = songs.length ? '' : '<div class="libitem empty">还没有处理过的歌</div>';
  for(const s of songs){
    const d = document.createElement('div');
    d.className = 'libitem' + (s.id===activeId?' active':'');
    d.textContent = s.name;
    d.onclick = () => loadSong(s.id);
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
  box.innerHTML = '<div class="item">搜索中…</div>';
  let data;
  try { data = await (await fetch('/search?q='+encodeURIComponent(q))).json(); }
  catch(e){ box.innerHTML = '<div class="item">搜索失败</div>'; return; }
  if(data.error){ box.innerHTML = '<div class="item">搜索失败：'+data.error+'</div>'; return; }
  box.innerHTML = '';
  for(const r of data){
    const it = document.createElement('div');
    it.className = 'item';
    it.innerHTML = `<img src="${r.thumbnail}" onerror="this.style.visibility='hidden'">
      <div class="meta"><div class="t">${r.title}</div>
      <div class="s">${r.uploader||''}${r.duration?' · '+fmt(r.duration):''}</div></div>`;
    const btn = document.createElement('button');
    btn.className = 'dl'; btn.textContent = '下载';
    btn.onclick = () => downloadOne(r.url, r.title, btn);
    it.appendChild(btn);
    box.appendChild(it);
  }
}

async function downloadOne(url, title, btn){
  btn.disabled = true; btn.textContent = '处理中…';
  const {job_id} = await (await fetch('/download', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({url, title})})).json();
  const poll = setInterval(async () => {
    const j = await (await fetch('/job/'+job_id)).json();
    if(j.stage==='downloading') btn.textContent='下载中…';
    else if(j.stage==='separating') btn.textContent='分离中…';
    if(j.status==='done'){ clearInterval(poll); btn.textContent='✓ 已入库';
      await loadLibrary(); if(j.song_id) loadSong(j.song_id); }
    else if(j.status==='error'){ clearInterval(poll); btn.disabled=false;
      btn.textContent='重试'; alert('处理失败：'+j.error); }
  }, 1500);
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
