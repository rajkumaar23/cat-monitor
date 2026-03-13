# Cat Monitor

Self-hosted cat behavior monitoring system using Kasa EC70 cameras, Frigate NVR, Moondream2 vision AI, and OpenWebUI chat.

```
Kasa EC70 (cam1)  ──┐
                     ├──► go2rtc ──► Frigate ──► MQTT ──► cat-observer
Kasa EC70 (cam2)  ──┘                    │                     │
                                   snapshot API      Ollama/Moondream2 (external)
                                                               │
                                                         PostgreSQL
                                                               │
                                                  OpenWebUI (external) + cat tool
```

**Hardware:** Jetson Orin Nano 8GB (TensorRT detection). CPU fallback available.

**External dependencies (not managed by this stack):**
- Ollama — already running on the Jetson
- OpenWebUI — already running on a separate host

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
- `OLLAMA_URL` — point to your existing Ollama instance, e.g. `http://192.168.1.x:11434`

### 2. Pull moondream into your existing Ollama (if not already)

```bash
ollama pull moondream
```

### 3. Generate Mosquitto password file (one-time)

```bash
source .env
docker run --rm eclipse-mosquitto:2.0 \
  mosquitto_passwd -c -b /dev/stdout "$MQTT_USER" "$MQTT_PASSWORD" \
  > ./mosquitto/passwd
chmod 600 ./mosquitto/passwd
```

### 4. Confirm NVIDIA container toolkit

```bash
sudo apt show nvidia-jetpack
nvidia-container-cli info
```

### 5. Start the stack

```bash
docker compose up -d
```

### 6. Wait for TensorRT compilation (~10 min, first run only)

```bash
docker logs frigate -f
# Wait for: "Frigate is running"
```

The `frigate-model-cache` volume persists the compiled TRT model so subsequent restarts are fast.

### 7. Add the cat tool to OpenWebUI

1. Open your existing OpenWebUI instance
2. Go to **Settings → Tools → Add Tool**
3. Paste the contents of `openwebui-tools/cat_query_tool.py`
4. Update `CAT_OBSERVER_URL` at the top of the file to point to the Jetson IP, e.g. `http://192.168.1.x:8088`
5. Save

In chat, click the **tools icon** to enable the tool, then ask:
> "What have my cats been doing today?"

---

## Services & ports

| Service | Port | URL |
|---|---|---|
| Frigate UI | 5000 | `http://<jetson-ip>:5000` |
| cat-observer API | 8088 | `http://<jetson-ip>:8088` |
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

Remove `runtime: nvidia` from the `frigate` service in `docker-compose.yml`, and change the detector in `frigate/config.yml`:

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
