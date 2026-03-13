# Cat Monitor

Self-hosted cat behavior monitoring using Kasa EC70 cameras, VILA1.5-3b vision AI on Jetson, and OpenWebUI chat.

```
Kasa EC70 cameras
      │
   go2rtc (:1984) ── RTSP proxy + HTTP snapshot API
      │
  cat-observer ──► nano-llm / VILA1.5-3b (:8085)
      │                  GPU inference on Jetson Orin Nano
  PostgreSQL
      │
  OpenWebUI (external) + cat tool
```

**Hardware:** Jetson Orin Nano 8GB. VILA1.5-3b runs fully on-device via nano_llm + MLC.

**External (not in this stack):** Ollama, OpenWebUI — already running on separate hosts.

---

## How it works

1. **go2rtc** proxies Kasa EC70 RTSP streams, exposing `/api/frame.jpeg?src=<camera>`
2. **cat-observer** polls each camera every `POLL_INTERVAL` seconds (default: 30s)
3. Simple pixel-diff motion detection skips unchanged frames
4. On motion, the JPEG frame is sent to **nano-llm** running VILA1.5-3b
5. VILA describes the scene in natural language, focusing on cats
6. Description + parsed metadata (has_cat, activity, location) stored in **PostgreSQL**
7. REST API and OpenWebUI tool let you query what happened

---

## Setup

### 1. Clone and configure (both hosts)

```bash
git clone <repo-url> cat-monitor
cd cat-monitor
cp .env.example .env
```

Edit `.env`:
- `CAM1_USER` / `CAM2_USER` — URL-percent-encode your email (`@` → `%40`)
- `CAM1_PASS` / `CAM2_PASS` — base64-encode: `echo -n 'pass' | base64`
- `JETSON_IP` — IP of the Jetson on your LAN
- `NANO_LLM_IMAGE` — match your JetPack version (see below)

### 2. Check JetPack version (on Jetson)

```bash
sudo apt show nvidia-jetpack 2>/dev/null | grep Version
```

| JetPack | L4T | `NANO_LLM_IMAGE` |
|---|---|---|
| 6.x | r36.x | `dustynv/nano_llm:r36.2.0` |
| 5.1 | r35.x | `dustynv/nano_llm:r35.4.1` |

### 3. Start nano-llm on the Jetson

```bash
docker compose -f docker-compose.jetson.yml build
docker compose -f docker-compose.jetson.yml up -d
```

Wait for VILA to load (~3–5 min first run, model downloads automatically):

```bash
docker logs cat-monitor-nano-llm-1 -f
# Wait for: "Model ready."
```

The model is cached in the `nano-llm-models` volume — subsequent starts are fast.

### 4. Start everything else on the main host

```bash
docker compose up -d
```

### 5. Verify

```bash
# nano-llm health (from anywhere)
curl http://<jetson-ip>:8085/health

# cat-observer (on main host)
curl http://<main-host-ip>:8088/health
curl http://<main-host-ip>:8088/observations
```

### 6. Add the OpenWebUI tool

1. Open your OpenWebUI instance
2. **Settings → Tools → Add Tool** → paste `openwebui-tools/cat_query_tool.py`
3. Update `CAT_OBSERVER_URL` at the top to `http://<main-host-ip>:8088`
4. Enable the tool in chat and ask: *"What have my cats been doing today?"*

---

## Services & ports

| Host | Service | Port | Purpose |
|---|---|---|---|
| Main | go2rtc UI + API | 1984 | Live stream viewer, snapshot API |
| Main | cat-observer API | 8088 | Observations REST API |
| Jetson | nano-llm API | 8085 | VILA1.5-3b HTTP inference |

---

## Tuning

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL` | `30` | Seconds between frame grabs per camera |
| `MOTION_THRESHOLD` | `0.02` | Pixel-diff sensitivity (lower = more sensitive) |
| `MAX_NEW_TOKENS` | `96` | Max tokens in VILA response |
| `CAMERAS` | `living_room,bedroom` | Comma-separated go2rtc stream names |

---

## API

```bash
# Health check
curl http://<ip>:8088/health

# Recent cat observations
curl "http://<ip>:8088/observations?has_cat=true&limit=10"

# Today's summary
curl http://<ip>:8088/summary

# Filter by camera and activity
curl "http://<ip>:8088/observations?camera=living_room&activity=sleeping"
```

---

## Camera names

Stream names in `go2rtc/config.yaml.tpl` must match the `CAMERAS` env var.
Defaults: `living_room` (cam1), `bedroom` (cam2).

---

## Secrets

All credentials in `.env` (gitignored). `.env.example` has placeholders only.
