#!/usr/bin/env python3
"""YouTube -> techo5 play_video bridge (yt-dlp downloads, ffmpeg remuxes to MPEG-TS).
GET /resolve?q=<search words or youtube URL>
    Starts the pipeline, waits until ~500 KB is buffered, then returns
    {"title": ..., "url": "http://<this host>:8099/stream?s=<session>"}
GET /stream?s=<session>   -> MPEG-TS (buffer first, then live).
    Reconnects are allowed (the newest client wins); the session is killed
    15 s after the last client leaves, or 120 s after /resolve if nobody connects.
Needs: yt-dlp[default], deno, ffmpeg. LAN only!
"""
import datetime
import json
import queue
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8099
# 18 = 360p H.264+AAC muxed (most reliable); fallback: muxed H.264/AAC <=720p
FORMAT = "18/best[vcodec^=avc1][acodec^=mp4a][height<=720]"
POT = ["--extractor-args", "youtube:player_client=mweb"]  # extra yt-dlp args (faster than the default client)
YT_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)
VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PREBUFFER_CHUNKS = 8      # 8 x 64 KB
QUEUE_CHUNKS = 96         # ~6 MB backpressure
START_TIMEOUT = 40
IDLE_REAP = 120           # nobody ever connected
DETACH_GRACE = 15         # no client for this long -> kill
START_DELAY = 5           # seconds to hold the stream after /resolve returns (keeps the Show's info screen up)
SESS = {}
# spoken name (lowercase, no leading "the") -> channel /videos page; plays the NEWEST upload
CHANNELS = {
    # "some channel": "https://www.youtube.com/@SomeChannel/videos",
    "secular talk": "https://www.youtube.com/@SecularTalk/videos",
}


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

    def _resolve(self, u):
        q = (parse_qs(u.query).get("q", [""])[0]).strip()
        if not q:
            return self._json(400, {"error": "missing q"})
        key = re.sub(r"^the ", "", q.lower()).strip()
        extra = []
        if key in CHANNELS:
            target = CHANNELS[key]
            extra = ["--playlist-items", "1"]
        elif YT_URL.match(q):
            target = q
        else:
            target = f"ytsearch1:{q}"
        try:
            out = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--no-warnings", *extra,
                 "--print",
                 "%(id)s\t%(title)s\t%(channel,uploader|)s\t%(upload_date|)s\t%(duration_string|)s",
                 "--", target],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "search timed out"})
        line = next((l for l in out.stdout.splitlines() if "\t" in l), "")
        parts = (line.split("\t") + ["", "", "", "", ""])[:5]
        vid, vtitle, channel, udate, dur = [p.strip() for p in parts]
        if not VID.match(vid):
            return self._json(502, {"error": (out.stderr or "no result")[-300:]})
        when = ""
        if re.fullmatch(r"\d{8}", udate):
            when = datetime.datetime.strptime(udate, "%Y%m%d").strftime("%b %-d, %Y")
        extra_info = " | ".join(x for x in (channel, when, dur) if x and x != "NA")
        title = f"{vtitle} ({extra_info})" if extra_info else vtitle
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
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
