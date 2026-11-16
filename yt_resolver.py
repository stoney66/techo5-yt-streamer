#!/usr/bin/env python3
"""YouTube -> techo5 play_video bridge (yt-dlp downloads, ffmpeg remuxes to MPEG-TS).
GET /resolve?q=<search words or youtube URL>
    Starts the pipeline, waits until ~500 KB is buffered, then returns
    {"title": ..., "url": "http://<this host>:8099/stream?s=<session>"}
GET /search?q=...   -> top NRESULTS (12) results; thumbnails at /thumb/1.jpg .. /thumb/12.jpg (title drawn on)
GET /pick?n=1..9    -> play result n of the last search (same JSON as /resolve)
GET /stream?s=<session>   -> MPEG-TS (buffer first, then live).
    Reconnects are allowed (the newest client wins); the session is killed
    15 s after the last client leaves, or 120 s after /resolve if nobody connects.
Needs: yt-dlp[default], deno, ffmpeg. LAN only!
"""
import datetime
import io
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8099"))
BIND = os.environ.get("BIND", "0.0.0.0")
# 18 = 360p H.264+AAC muxed (most reliable); fallback: muxed H.264/AAC <=720p
FORMAT = "18/best[vcodec^=avc1][acodec^=mp4a][height<=720]"
POT = ["--extractor-args", "youtube:player_client=mweb"]  # extra yt-dlp args (faster than the default client)
YT_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PREBUFFER_CHUNKS = 4      # 8 x 64 KB
QUEUE_CHUNKS = 96         # ~6 MB backpressure
START_TIMEOUT = 40
IDLE_REAP = 120           # nobody ever connected
DETACH_GRACE = 15         # no client for this long -> kill
START_DELAY = 2           # seconds to hold the stream after /resolve returns (keeps the Show's info screen up)
SESS = {}
# --- tile picker: last search results + their thumbnails ---
NRESULTS = int(os.environ.get("NRESULTS", "12"))   # tiles per search; lower it for small screens
RESULTS = {}   # n -> {"vid", "title"}
THUMBS = {}    # n -> jpeg bytes
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def lookup(q, n=1):
    """Return up to n [{vid,title}] for a search or a YouTube URL; None on timeout."""
    extra = []
    if YT_URL.match(q):
        target = q
    else:
        target = f"ytsearch{n}:{q}"
    try:
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-warnings", *extra, "--print",
             "%(id)s\t%(title)s\t%(channel,uploader|)s\t%(upload_date|)s\t%(duration_string|)s",
             "--", target],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    res = []
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        vid, vtitle, channel, udate, dur = [p.strip() for p in (line.split("\t") + [""] * 5)[:5]]
        if not VID.match(vid):
            continue
        when = ""
        if re.fullmatch(r"\d{8}", udate):
            when = datetime.datetime.strptime(udate, "%Y%m%d").strftime("%b %-d, %Y")
        info = " | ".join(x for x in (channel, when, dur) if x and x != "NA")
        res.append({"vid": vid, "title": f"{vtitle} ({info})" if info else vtitle, "short": vtitle})
    return res[:n], out.stderr


def make_thumb(vid, title):
    """640x480 (4:3) JPEG: YouTube thumbnail on top, title underneath (never overlaid, never cropped
    at the sides because the Show crops 16:9 pictures to squarer tiles). Needs Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    W, H, PH = 640, 480, 360
    im = Image.new("RGB", (W, H), (16, 24, 32))
    if vid:
        try:
            raw = urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", timeout=10).read()
            im.paste(Image.open(io.BytesIO(raw)).convert("RGB").resize((W, PH)), (0, 0))
        except Exception:
            pass
    try:
        font = ImageFont.truetype(FONT, 30)
    except Exception:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(im)
    # drop emoji / symbols the font can't draw (they show as boxes)
    title = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200d]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    maxlines, maxw, x0 = 3, 540, 50      # side margins survive the Show's crop
    lines, cur = [], ""
    for w in title.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) > maxw and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]
        while lines[-1] and d.textlength(lines[-1] + "...", font=font) > maxw:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "..."
    for i, l in enumerate(lines):
        d.text((x0, PH + 6 + 37 * i), l, font=font, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Session:
    def __init__(self, vid):
        self.sid = None
        self.q = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.ready = threading.Event()
        self.done = False
        self.closed = False
        self.ever = False
        self.clients = 0
        self.owner = None
        self.last_detach = time.time()
        self.ready_at = time.time()
        self.ytdlp = subprocess.Popen(
            ["yt-dlp", "--no-playlist", "--quiet", "-f", FORMAT, *POT,
             "-o", "-", "--", f"https://www.youtube.com/watch?v={vid}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.ff = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-probesize", "65536",
             "-analyzeduration", "0", "-i", "pipe:0", "-c", "copy",
             "-flush_packets", "1", "-f", "mpegts", "pipe:1"],
            stdin=self.ytdlp.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.ytdlp.stdout.close()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Timer(IDLE_REAP, self._reap).start()

    def _pump(self):
        try:
            while not self.closed:
                chunk = self.ff.stdout.read(65536)
                if not chunk:
                    break
                while not self.closed:
                    try:
                        self.q.put(chunk, timeout=1)
                        break
                    except queue.Full:
                        continue
                if self.q.qsize() >= PREBUFFER_CHUNKS:
                    self.ready.set()
        finally:
            self.done = True
            self.ready.set()

    def _reap(self):
        if not self.ever:
            log("reap (never connected)", self.sid)
            self.kill()

    def maybe_kill(self):
        if self.clients == 0 and time.time() - self.last_detach >= DETACH_GRACE - 0.5:
            log("kill (no clients)", self.sid)
            self.kill()

    def kill(self):
        self.closed = True
        SESS.pop(self.sid, None)
        for p in (self.ff, self.ytdlp):
            try:
                p.kill()
            except Exception:
                pass
        for p in (self.ff, self.ytdlp):
            try:
                p.wait(timeout=5)
            except Exception:
                pass


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        u = urlparse(self.path)
        log("HEAD", self.path, self.headers.get("User-Agent"))
        sid = (parse_qs(u.query).get("s", [""])[0]).strip()
        if u.path == "/stream" and sid in SESS:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        log("GET", self.path, self.headers.get("User-Agent"),
            "Range=" + str(self.headers.get("Range")))
        if u.path == "/resolve":
            return self._resolve(u)
        if u.path == "/stream":
            return self._stream(u)
        if u.path == "/search":
            return self._search(u)
        if u.path == "/pick":
            return self._pick(u)
        m = re.fullmatch(r"/thumb/(\d+)\.jpg", u.path)
        if m:
            return self._thumb(int(m.group(1)))
        if u.path == "/loading":
            return self._loading(u)
        self._json(404, {"error": "not found"})

    def _loading(self, u):
        t = (parse_qs(u.query).get("t", [""])[0])[:60]
        text = re.sub(r"[^A-Za-z0-9 ,.!?-]", "", t).strip() or "video"
        draw = (
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"text='Loading {text}...':fontcolor=white:fontsize=30:"
            "x=(w-text_w)/2:y=(h-text_h)/2"
        )
        ff = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "color=c=0x101820:s=640x360:r=15",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", draw, "-t", "90",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k",
             "-f", "mpegts", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = ff.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                ff.kill()
                ff.wait(timeout=5)
            except Exception:
                pass

    def _play(self, vid, title):
        s = Session(vid)
        s.sid = secrets.token_urlsafe(9)
        SESS[s.sid] = s
        s.ready.wait(timeout=START_TIMEOUT)
        if not s.ready.is_set() or (s.done and s.q.empty()):
            s.kill()
            return self._json(502, {"error": "no data from yt-dlp (timeout or failure)"})
        host = self.headers.get("Host") or f"localhost:{PORT}"
        s.ready_at = time.time()
        log("session ready", s.sid, vid)
        self._json(200, {"title": title, "url": f"http://{host}/stream?s={s.sid}"})

    def _resolve(self, u):
        q = (parse_qs(u.query).get("q", [""])[0]).strip()
        if not q:
            return self._json(400, {"error": "missing q"})
        r = lookup(q, 1)
        if r is None:
            return self._json(504, {"error": "search timed out"})
        res, err = r
        if not res:
            return self._json(502, {"error": (err or "no result")[-300:]})
        self._play(res[0]["vid"], res[0]["title"])

    def _search(self, u):
        """Store the top NRESULTS hits + thumbnails for the dashboard picker."""
        q = (parse_qs(u.query).get("q", [""])[0]).strip()
        if not q:
            return self._json(400, {"error": "missing q"})
        r = lookup(q, NRESULTS)
        if r is None:
            return self._json(504, {"error": "search timed out"})
        res, err = r
        if not res:
            return self._json(502, {"error": (err or "no result")[-300:]})
        thumbs = {}

        def work(i, item):
            thumbs[i] = make_thumb(item["vid"] if item else "", item["short"] if item else "")

        items = {i + 1: (res[i] if i < len(res) else None) for i in range(NRESULTS)}
        ts = [threading.Thread(target=work, args=(i, it)) for i, it in items.items()]
        [t.start() for t in ts]
        [t.join() for t in ts]
        RESULTS.clear()
        RESULTS.update({i: it for i, it in items.items() if it})
        THUMBS.clear()
        THUMBS.update(thumbs)
        log("search", q, "->", len(res), "results")
        self._json(200, {"count": len(res), "results": [{"n": i, "title": it["title"]} for i, it in RESULTS.items()]})

    def _pick(self, u):
        try:
            n = int(parse_qs(u.query).get("n", ["0"])[0])
        except ValueError:
            n = 0
        it = RESULTS.get(n)
        if not it:
            return self._json(404, {"error": "no such result"})
        self._play(it["vid"], it["title"])

    def _thumb(self, n):
        b = THUMBS.get(n)
        if not b:
            return self._json(404, {"error": "no thumbnail"})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _stream(self, u):
        sid = (parse_qs(u.query).get("s", [""])[0]).strip()
        s = SESS.get(sid)
        if not s or s.closed:
            log("stream 404", sid)
            return self._json(404, {"error": "no such session"})
        me = object()
        s.owner = me
        s.ever = True
        s.clients += 1
        log("stream attach", sid, "clients=", s.clients)
        try:
            wait = s.ready_at + START_DELAY - time.time()
            if wait > 0:
                time.sleep(wait)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            while s.owner is me and not s.closed:
                try:
                    chunk = s.q.get(timeout=1)
                except queue.Empty:
                    if s.done:
                        break
                    continue
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            s.clients -= 1
            s.last_detach = time.time()
            log("stream detach", sid, "clients=", s.clients)
            if s.done and s.q.empty():
                s.kill()
            else:
                threading.Timer(DETACH_GRACE, s.maybe_kill).start()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
