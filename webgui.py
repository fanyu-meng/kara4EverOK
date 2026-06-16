#!/usr/bin/env python3
"""
极简网页 GUI：只显示歌名 + 一个切换按钮（原唱 ⇄ 伴奏）。
音频在本机扬声器播放，浏览器只是控制面板。用已装好的 FastAPI + uvicorn，无需新依赖。
"""

import threading
import webbrowser

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from player import KaraokePlayer

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卡拉OK</title>
<style>
  body {{ margin:0; height:100vh; display:flex; flex-direction:column;
          align-items:center; justify-content:center; gap:32px;
          font-family:-apple-system,system-ui,sans-serif;
          background:#0f1020; color:#fff; }}
  .song {{ font-size:24px; font-weight:600; text-align:center; padding:0 24px; opacity:.9; }}
  #btn {{ font-size:22px; padding:22px 40px; border:none; border-radius:18px;
          cursor:pointer; color:#fff; transition:transform .08s, background .2s;
          min-width:280px; }}
  #btn:active {{ transform:scale(.97); }}
  .instrumental {{ background:#2563eb; }}
  .original {{ background:#db2777; }}
  .hint {{ font-size:14px; opacity:.45; }}
</style>
</head>
<body>
  <div class="song">🎶 {song}</div>
  <button id="btn" class="instrumental">载入中…</button>
  <div class="hint">点击切换 · 声音从电脑扬声器播放</div>
<script>
const btn = document.getElementById('btn');
function render(mode) {{
  if (mode === 'original') {{
    btn.className = 'original';
    btn.textContent = '🎤 原唱（点击切到伴奏）';
  }} else {{
    btn.className = 'instrumental';
    btn.textContent = '🎵 伴奏（点击切到原唱）';
  }}
}}
async function refresh() {{
  const r = await fetch('/status'); render((await r.json()).mode);
}}
btn.onclick = async () => {{
  const r = await fetch('/toggle', {{method:'POST'}}); render((await r.json()).mode);
}};
refresh();
setInterval(refresh, 1500);  // 保持与播放状态同步（如歌曲结束）
</script>
</body>
</html>"""


def play(vocals_path: str, no_vocals_path: str, song_name: str,
         port: int = 8765):
    player = KaraokePlayer(vocals_path, no_vocals_path)
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE.format(song=song_name)

    @app.get("/status")
    def status():
        return JSONResponse({"mode": player.mode, "song": song_name})

    @app.post("/toggle")
    def toggle():
        player.toggle_mode()
        return JSONResponse({"mode": player.mode})

    url = f"http://127.0.0.1:{port}"
    print(f"打开浏览器: {url}")
    player.start()
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        player.stop()


if __name__ == "__main__":
    import sys
    import os
    if len(sys.argv) < 3:
        print("用法: python webgui.py <vocals.wav> <no_vocals.wav> [歌名]")
        sys.exit(1)
    name = sys.argv[3] if len(sys.argv) > 3 else os.path.basename(
        os.path.dirname(sys.argv[2]))
    play(sys.argv[1], sys.argv[2], name)
