# techo5-yt-streamer

Play YouTube on an Echo Show running [TECHO5](https://github.com/HuskerMinion/techo5) firmware, by voice or from a Home Assistant script.

The TECHO5 `play_video` action wants a plain video URL it can fetch with ffmpeg. Direct YouTube (googlevideo) URLs get rejected, so this small container does the fetching instead: it runs `yt-dlp`, remuxes with `ffmpeg` to MPEG-TS, and serves the stream over HTTP on your LAN.

```
"play youtube <query>" -> HA -> GET /resolve?q=... -> {"title", "url"} -> esphome.<device>_play_video
                                       |
                              yt-dlp | ffmpeg (-c copy, mpegts) -> GET /stream?s=<session>
```

## Features
- Search by words (`ytsearch1`) or pass a YouTube URL
- Optional channel shortcuts: say a channel name, get its newest upload
- Pre-buffers before returning the URL, so the device doesn't time out waiting for the first byte
- Tolerates the device opening the stream twice (reconnects allowed, idle sessions are reaped)
- Title returned with channel / date / duration for the device's info screen

## Requirements
- Docker + Docker Compose on a host reachable from the Echo Show
- Home Assistant with a TECHO5 device exposing `esphome.<device>_play_video`

## Run
```
docker compose up -d --build
curl "http://<host>:8099/resolve?q=nasa+launch"
```
You should get JSON with a `title` and a `url`. The container updates `yt-dlp` on every start; YouTube breaks it regularly, so restart (`docker compose restart`) if searches begin failing.

## Home Assistant

`configuration.yaml`:
```yaml
rest_command:
  yt_resolve:
    url: "http://<RESOLVER_HOST>:8099/resolve?q={{ q | urlencode }}"
    method: get
    timeout: 60
```

`scripts.yaml` (replace `<device>` with your device name, slugified, e.g. `myshow`):
```yaml
show_play_youtube:
  alias: Play YouTube
  fields:
    q: {}
  sequence:
    - action: rest_command.yt_resolve
      data:
        q: "{{ q }}"
      response_variable: r
    - action: esphome.<device>_play_video
      data:
        title: "{{ r.content.title }}"
        url: "{{ r.content.url }}"
```

Voice: `custom_sentences/en/youtube.yaml`
```yaml
language: "en"
intents:
  YoutubePlay:
    data:
      - sentences:
          - "play youtube {query}"
          - "play {query} on youtube"
          - "youtube {query}"
lists:
  query:
    wildcard: true
```

`intent_scripts/youtube.yaml` (needs `intent_script: !include_dir_merge_named intent_scripts` in `configuration.yaml`):
```yaml
YoutubePlay:
  action:
    - action: script.turn_on
      target: { entity_id: script.show_play_youtube }
      data:
        variables:
          q: "{{ query }}"
  speech:
    text: "Okay"
```
Then run `conversation.reload` (or restart HA).

## Configuration
Edit the constants at the top of `yt_resolver.py`:

| Name | Default | Meaning |
|---|---|---|
| `PORT` | `8099` | Listen port |
| `FORMAT` | `18/best[vcodec^=avc1][acodec^=mp4a][height<=720]` | yt-dlp format; must be H.264 + AAC, muxed, ≤720p for the TECHO5 player |
| `START_DELAY` | `5` | Seconds to hold the stream after `/resolve` (keeps the device info screen visible) |
| `PREBUFFER_CHUNKS` | `8` | 64 KB chunks buffered before `/resolve` returns |
| `CHANNELS` | `{}` | `"spoken name": "https://www.youtube.com/@Channel/videos"` — plays the newest upload |

## Security
**LAN only.** There is no authentication. Anyone who can reach port 8099 can make this host download and stream arbitrary YouTube content. Do not port-forward it, and consider binding the published port to your LAN IP in `docker-compose.yaml` (`"192.168.x.x:8099:8099"`).

## Troubleshooting
- **Device times out / "error opening"** – check `docker logs yt-resolver`; a slow `/resolve` usually means yt-dlp is out of date (`docker compose restart`).
- **Works once, then 404 on `/stream`** – sessions expire 15 s after the last client leaves, or 120 s if nobody connects.
- **403 from YouTube** – restart to pull the latest yt-dlp; the `mweb` player client is used by default.

## License
Add your preferred license (e.g. MIT).
