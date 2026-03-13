# Cat Monitor

Self-hosted cat behavior monitoring system using Kasa EC70 cameras, Frigate NVR, Moondream2 vision AI, and OpenWebUI chat.

```
Kasa EC70 (cam1)  ──┐
                     ├──► go2rtc ──► Frigate ──► MQTT ──► cat-observer
Kasa EC70 (cam2)  ──┘                    │                     │
                                   snapshot API          Ollama/Moondream2
                                                               │
                                                         PostgreSQL
                                                               │
                                                      OpenWebUI + qwen2.5:3b
```

**Hardware:** Jetson Orin Nano 8GB (TensorRT detection). CPU fallback available.

---

## First-time setup

### 1. Clone and configure

```bash
git clone <repo-url> cat-monitor
cd cat-monitor
cp .env.example .env
```

Edit `.env` and fill in all values:

- `CAM1_USER` / `CAM2_USER` — URL-percent-encode your email (`@` → `%40`)
- `CAM1_PASS` / `CAM2_PASS` — base64-encode your password:
  ```bash
  echo -n 'yourpassword' | base64
  ```
- `OPENWEBUI_SECRET_KEY` — generate a random key:
  ```bash
  openssl rand -hex 32
  ```

### 2. Generate Mosquitto password file (one-time)

```bash
source .env
docker run --rm eclipse-mosquitto:2.0 \
  mosquitto_passwd -c -b /dev/stdout "$MQTT_USER" "$MQTT_PASSWORD" \
  > ./mosquitto/passwd
chmod 600 ./mosquitto/passwd
```

### 3. Confirm NVIDIA container toolkit

```bash
# On Jetson:
sudo apt show nvidia-jetpack
nvidia-container-cli info
```

### 4. Start everything

```bash
docker compose up -d
```

### 5. Wait for TensorRT compilation (~10 min, first run only)

```bash
docker logs frigate -f
# Wait for: "Frigate is running"
```

The `frigate-model-cache` volume persists the compiled TRT model so subsequent restarts are fast.

### 6. Ollama model pull (automatic)

The `ollama-init` service automatically pulls `moondream` and `qwen2.5:3b` on first start.
Monitor progress:

```bash
docker logs ollama-init -f
```

### 7. Upload the OpenWebUI tool

1. Open OpenWebUI at `http://<jetson-ip>:3000`
2. Go to **Settings → Tools → Add Tool**
3. Paste the contents of `openwebui-tools/cat_query_tool.py`
4. Save

In chat, click the **tools icon** to enable the tool, then ask:
> "What have my cats been doing today?"

---

## Services & ports

| Service | Port | URL |
|---|---|---|
| Frigate UI | 5000 | `http://<ip>:5000` |
| OpenWebUI | 3000 | `http://<ip>:3000` |
| cat-observer API | 8088 | `http://<ip>:8088` |
| Ollama API | 11434 | `http://<ip>:11434` |
| MQTT | 1883 | — |

---

## Verification

```bash
# System health
curl http://<jetson-ip>:8088/health

# Recent observations
curl http://<jetson-ip>:8088/observations?limit=5

# Today's activity summary
curl http://<jetson-ip>:8088/summary
```

Expected health response:
```json
{"status":"ok","db":"connected","cameras":{"living_room":"online","bedroom":"online"},"queue_depth":0}
```

---

## CPU fallback (non-Jetson)

If you don't have a Jetson, remove `runtime: nvidia` from the `frigate` and `ollama` services in `docker-compose.yml`, and change the detector in `frigate/config.yml`:

```yaml
detectors:
  cpu:
    type: cpu
    num_threads: 3
```

---

## Camera logical names

| Stream name | go2rtc key | Frigate camera | Location |
|---|---|---|---|
| Camera 1 | `living_room` | `living_room` | Living room |
| Camera 2 | `bedroom` | `bedroom` | Bedroom |

Change these in `go2rtc/config.yaml.tpl` and `frigate/config.yml` to match your layout.

---

## Secrets

All credentials live in `.env` (gitignored). The committed `.env.example` contains only placeholder values. The Mosquitto `passwd` file is also gitignored and generated locally.
