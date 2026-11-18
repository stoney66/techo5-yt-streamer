# techo5-yt-streamer

Search YouTube by voice on an Echo Show running [TECHO5](https://github.com/HuskerMinion/techo5) firmware, then tap a thumbnail to play it. Built for kids: say "play youtube super kitties", pick a tile.

```
voice -> HA -> /search?q=...  -> top 12 results + thumbnails (/thumb/1.jpg ... /thumb/12.jpg)
Show shows a "picker" dashboard of the thumbnails -> tap one -> /pick?n=N
  -> yt-dlp | ffmpeg (-c copy -> MPEG-TS) -> /stream -> esphome.<device>_play_video
```

The TECHO5 `play_video` action needs a plain video URL, and direct YouTube URLs get rejected, so this container does the fetching: it runs `yt-dlp`, remuxes with `ffmpeg`, and serves the stream on your LAN.

## Requirements
- Docker + Docker Compose on a host the Echo Show can reach
- Home Assistant with a TECHO5 Echo Show (`esphome.<device>_play_video`, `_stop_video`, `_dashboard_path`, `_dashboard_show`, `_dashboard_hide`) and a voice assistant on it
- The device's Home Assistant access set up (**Connect to Home Assistant** on its setup page), since the picker is a *drawn* dashboard
- The device's **Video** switch on

## 1. Run the container
```
docker compose up -d --build
curl "http://<host>:8099/search?q=nasa+launch"      # -> {"count": 12, "results": [...]}
```
Port `8099` is LAN only: there is no authentication. The container updates `yt-dlp` on every start; restart it if searches start failing.

## 2. Home Assistant

The device is never hard-coded: scripts use whichever Show is handling the voice request (its assist satellite is `processing`), falling back to `my_show` when run from elsewhere. Its name, slugified, must match its ESPHome actions (`MyShow` -> `esphome.myshow_play_video`). Change `my_show` below to your device's slug.

### configuration.yaml
```yaml
rest_command:
  yt_search:
    url: "http://<RESOLVER_HOST>:8099/search?q={{ q | urlencode }}"
    timeout: 60
  yt_pick:
    url: "http://<RESOLVER_HOST>:8099/pick?n={{ n }}"
    timeout: 60

input_text:
  yt_node:            # remembers which Show asked
    name: YT node
    max: 64

intent_script: !include_dir_merge_named intent_scripts   # if you don't already have this
```

### scripts.yaml
The Show has one **Dashboard** setting (Off / Drawn on the device / Streamed / Tessera). The picker needs *Drawn on the device*, so `youtube_search` switches to it and `youtube_pick` switches back. **Set the option in `youtube_pick` to your normal mode.** Check the exact entity id and option names under the device's Configuration card (`select.<node>_dashboard`).

```yaml
youtube_search:
  alias: YouTube search -> tiles
  mode: restart
  fields:
    q: {}
  sequence:
    - variables:
        sat_id: >-
          {{ states.assist_satellite | selectattr('state','eq','processing')
             | map(attribute='entity_id') | first | default('') }}
        node: >-
          {% if sat_id %}{{ device_attr(sat_id, 'name') | slugify }}{% else %}my_show{% endif %}
    - variables:
        dash: >-
          {% set ns = namespace(e='') %}
          {% for s in states.select if s.entity_id.endswith('_dashboard') and node in s.entity_id and 'Drawn on the device' in s.attributes.options %}
            {% set ns.e = s.entity_id %}
          {% endfor %}
          {{ ns.e }}
    - action: input_text.set_value
      target: { entity_id: input_text.yt_node }
      data: { value: "{{ node }}" }
    - parallel:
        # branch 1: the search (runs while the voice reply plays)
        - action: rest_command.yt_search
          data: { q: "{{ q }}" }
        # branch 2: once the voice turn is over, show a card so the screen isn't the clock
        - sequence:
            - wait_template: "{{ sat_id == '' or is_state(sat_id, 'idle') }}"
              timeout: "00:00:15"
              continue_on_timeout: true
            - action: "esphome.{{ node }}_play_video"
              continue_on_error: true
              data:
                title: "Searching {{ q }}"
                url: "http://<RESOLVER_HOST>:8099/loading?t={{ q | urlencode }}"
    - action: select.select_option
      target: { entity_id: "{{ dash }}" }
      data: { option: "Drawn on the device" }
    - action: "esphome.{{ node }}_dashboard_path"
      data: { path: yt-picker/tiles }      # <dashboard url>/<view path>
    - action: "esphome.{{ node }}_dashboard_show"
    - delay: "00:00:01"
    - action: "esphome.{{ node }}_stop_video"   # hand over from the card to the tiles
      continue_on_error: true
    # picker timeout: if nobody picked within 45 seconds, go back
    - delay: "00:00:45"
    - if: "{{ is_state(dash, 'Drawn on the device') }}"
      then:
        - action: "esphome.{{ node }}_dashboard_hide"
        - action: select.select_option
          target: { entity_id: "{{ dash }}" }
          data: { option: "Tessera" }          # your normal mode

youtube_pick:
  alias: YouTube pick tile
  fields:
    n: {}
  sequence:
    - variables:
        node: "{{ states('input_text.yt_node') }}"
        dash: >-
          {% set ns = namespace(e='') %}
          {% for s in states.select if s.entity_id.endswith('_dashboard') and node in s.entity_id and 'Drawn on the device' in s.attributes.options %}
            {% set ns.e = s.entity_id %}
          {% endfor %}
          {{ ns.e }}
    - action: "esphome.{{ node }}_dashboard_hide"
    - action: select.select_option
      target: { entity_id: "{{ dash }}" }
      data: { option: "Tessera" }          # your normal mode
    - action: "esphome.{{ node }}_play_video"
      continue_on_error: true
      data:
        title: "Loading"
        url: "http://<RESOLVER_HOST>:8099/loading?t=video"
    - action: rest_command.yt_pick
      data: { n: "{{ n }}" }
      response_variable: r
    - if: "{{ not (r.status == 200 and r.content.url is defined) }}"
      then:
        - action: "esphome.{{ node }}_stop_video"
          continue_on_error: true
        - stop: "YouTube pick failed"
    - action: "esphome.{{ node }}_play_video"
      data:
        title: "{{ r.content.title }}"
        url: "{{ r.content.url }}"

# The drawn dashboard doesn't pass a card's `data:` on tap, so each tile gets its own script.
youtube_pick_1: { alias: YT pick 1, sequence: [ { action: script.youtube_pick, data: { n: 1 } } ] }
youtube_pick_2: { alias: YT pick 2, sequence: [ { action: script.youtube_pick, data: { n: 2 } } ] }
youtube_pick_3: { alias: YT pick 3, sequence: [ { action: script.youtube_pick, data: { n: 3 } } ] }
youtube_pick_4: { alias: YT pick 4, sequence: [ { action: script.youtube_pick, data: { n: 4 } } ] }
youtube_pick_5: { alias: YT pick 5, sequence: [ { action: script.youtube_pick, data: { n: 5 } } ] }
youtube_pick_6: { alias: YT pick 6, sequence: [ { action: script.youtube_pick, data: { n: 6 } } ] }
youtube_pick_7: { alias: YT pick 7, sequence: [ { action: script.youtube_pick, data: { n: 7 } } ] }
youtube_pick_8: { alias: YT pick 8, sequence: [ { action: script.youtube_pick, data: { n: 8 } } ] }
youtube_pick_9: { alias: YT pick 9, sequence: [ { action: script.youtube_pick, data: { n: 9 } } ] }
youtube_pick_10: { alias: YT pick 10, sequence: [ { action: script.youtube_pick, data: { n: 10 } } ] }
youtube_pick_11: { alias: YT pick 11, sequence: [ { action: script.youtube_pick, data: { n: 11 } } ] }
youtube_pick_12: { alias: YT pick 12, sequence: [ { action: script.youtube_pick, data: { n: 12 } } ] }
```

`youtube_search` runs the search while the voice reply finishes, waits for the satellite to go back to `idle` (so the reply screen can't land on top), shows a "Searching ..." card, then shows the picker and stops the card. It puts the Show back to its normal mode after 45 seconds if nobody picks (`mode: restart` makes a new search cancel the old timer).

### Dashboard
Settings > Dashboards > Add dashboard > New from scratch, URL `yt-picker`. Open it, three-dot menu > Raw configuration editor, and paste this. Use your host's LAN IP for `<RESOLVER_HOST>`:

```yaml
views:
  - title: Tiles
    path: tiles
    cards:
      - type: grid
        columns: 2
        square: false
        cards:
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/1.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_1 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/2.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_2 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/3.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_3 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/4.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_4 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/5.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_5 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/6.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_6 }
      - type: grid
        columns: 2
        square: false
        cards:
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/7.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_7 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/8.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_8 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/9.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_9 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/10.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_10 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/11.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_11 }
          - type: picture
            image: http://<RESOLVER_HOST>:8099/thumb/12.jpg
            tap_action: { action: perform-action, perform_action: script.youtube_pick_12 }
```

The Show lays cards out in two columns, so two grids side by side with 2 tiles across each gives 4 across and 3 down. On a smaller screen (Show 5) lower the tile count: set `NRESULTS` (see below), and remove cards and the matching scripts.

### Voice
`custom_sentences/en/youtube.yaml`:
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

`intent_scripts/youtube.yaml`:
```yaml
YoutubePlay:
  action:
    - action: script.turn_on
      target: { entity_id: script.youtube_search }
      data:
        variables:
          q: "{{ query }}"
  speech:
    text: "Looking for results on {{ query }}"
```

Reload (`conversation.reload`, and reload intent scripts / scripts), then say **"play youtube super kitties"**.

## Settings (`yt_resolver.py`)

| Name | Default | Meaning |
|---|---|---|
| `PORT` (env) | `8099` | Listen port |
| `BIND` (env) | `0.0.0.0` | Listen address inside the container |
| `SHOW_DATES` (env) | `1` | Upload age badge on each tile ("12 min ago", "5 hr ago" up to 48 h, "3 days ago", then the date). Costs a few extra seconds per search; set `0` to turn off |
| `NRESULTS` (env) | `12` | Tiles per search; lower it for small screens |
| `FORMAT` | `18/best[vcodec^=avc1][acodec^=mp4a][height<=720]` | yt-dlp format; must be muxed H.264 + AAC, <=720p for the TECHO5 player |
| `START_DELAY` | `2` | Seconds to hold the stream after `/pick` returns |
| `PREBUFFER_CHUNKS` | `4` | 64 KB chunks buffered before `/pick` returns; raise it if playback stutters at the start |

Other endpoints: `/resolve?q=...` returns `{"title","url"}` for the top hit directly, for your own scripts; `/loading?t=text` serves a short "Loading ..." video (used by `youtube_pick`).

## Notes and limits
- **LAN only.** Anyone who can reach port 8099 can make the host download and stream YouTube video. Don't port-forward it; consider publishing the port on your LAN IP only in `docker-compose.yaml`.
- **One search at a time.** The 12 results and thumbnails are shared, and `input_text.yt_node` holds only the last Show that searched. Fine for one household; two Shows searching at once will overwrite each other's tiles.
- If the picker is closed without choosing, the Show stays in *Drawn on the device* until the 45 second timeout puts it back.
- Each tile has an upload-age badge (top left, e.g. "5 hr ago"), so you can tell which episode is newest. YouTube search results don't include dates, so the resolver looks each one up in parallel.
- Thumbnails are 4:3 with the title underneath, because the Show crops 16:9 pictures to squarer tiles.
- YouTube extraction breaks periodically; `docker compose restart` pulls the latest yt-dlp.
