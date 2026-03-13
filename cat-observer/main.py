"""
cat-observer: MQTT → Frigate snapshot → Moondream2 → PostgreSQL pipeline.
Exposes a REST API for querying observations.
"""

import asyncio
import base64
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional

import asyncpg
import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

# ─── Config ───────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ["DATABASE_URL"]
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "moondream")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

TRACKED_LABELS = {"cat", "dog", "bird"}
MIN_SCORE = 0.6

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cat-observer")

# ─── Shared state ─────────────────────────────────────────────────────────────

event_queue: asyncio.Queue = asyncio.Queue()
camera_availability: dict[str, str] = {}
db_pool: Optional[asyncpg.Pool] = None

# ─── Database ─────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS observations (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    camera_name     TEXT NOT NULL,
    event_id        TEXT UNIQUE NOT NULL,
    raw_description TEXT,
    activity_tag    TEXT,
    location_tag    TEXT,
    cat_count       INT,
    snapshot_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_timestamp ON observations (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_observations_camera ON observations (camera_name);
CREATE INDEX IF NOT EXISTS idx_observations_activity ON observations (activity_tag);
"""


async def get_db() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        async with db_pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        log.info("Database pool ready")
    return db_pool


# ─── Moondream2 via Ollama ─────────────────────────────────────────────────────

VISION_PROMPT = """Look at this image and respond with EXACTLY this format (no extra text):
CATS: <number of cats visible, 0 if none>
LOCATION: <one of: couch, floor, bed, window, food bowl, litter box, stairs, other>
ACTIVITY: <one of: sleeping, resting, playing, grooming, eating, drinking, walking, jumping, other>
DESCRIPTION: <1-2 sentences describing what you see>"""


def parse_moondream_response(text: str) -> dict:
    result = {"cat_count": None, "location_tag": None, "activity_tag": None, "raw_description": text}
    for line in text.splitlines():
        line = line.strip()
        if m := re.match(r"CATS:\s*(\d+)", line, re.IGNORECASE):
            result["cat_count"] = int(m.group(1))
        elif m := re.match(r"LOCATION:\s*(.+)", line, re.IGNORECASE):
            result["location_tag"] = m.group(1).strip().lower()
        elif m := re.match(r"ACTIVITY:\s*(.+)", line, re.IGNORECASE):
            result["activity_tag"] = m.group(1).strip().lower()
        elif m := re.match(r"DESCRIPTION:\s*(.+)", line, re.IGNORECASE):
            result["raw_description"] = m.group(1).strip()
    return result


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def analyze_with_moondream(client: httpx.AsyncClient, image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": VISION_MODEL,
        "prompt": VISION_PROMPT,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=60)
    resp.raise_for_status()
    text = resp.json().get("response", "")
    log.debug("Moondream raw response: %s", text)
    return parse_moondream_response(text)


# ─── Frigate snapshot fetch ────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def fetch_snapshot(client: httpx.AsyncClient, event_id: str) -> bytes:
    url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg"
    params = {"crop": "0", "quality": "85"}
    resp = await client.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.content


# ─── Event processing worker ──────────────────────────────────────────────────

async def event_worker():
    log.info("Event worker started")
    async with httpx.AsyncClient() as client:
        while True:
            event = await event_queue.get()
            try:
                await process_event(client, event)
            except Exception as e:
                log.error("Failed to process event %s: %s", event.get("id"), e)
            finally:
                event_queue.task_done()


async def process_event(client: httpx.AsyncClient, event: dict):
    event_id = event["id"]
    camera_name = event.get("camera", "unknown")
    label = event.get("label", "unknown")
    score = event.get("score", 0.0)

    log.info("Processing event %s — %s on %s (score=%.2f)", event_id, label, camera_name, score)

    pool = await get_db()

    # Deduplicate
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM observations WHERE event_id = $1", event_id
        )
        if existing:
            log.debug("Skipping duplicate event %s", event_id)
            return

    # Fetch snapshot
    try:
        image_bytes = await fetch_snapshot(client, event_id)
    except Exception as e:
        log.error("Could not fetch snapshot for %s: %s", event_id, e)
        return

    # Analyze with Moondream
    try:
        analysis = await analyze_with_moondream(client, image_bytes)
    except Exception as e:
        log.error("Moondream analysis failed for %s: %s", event_id, e)
        analysis = {"cat_count": None, "location_tag": None, "activity_tag": None, "raw_description": None}

    snapshot_path = f"/media/frigate/{camera_name}/{event_id}-snapshot.jpg"

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO observations
                (camera_name, event_id, raw_description, activity_tag, location_tag, cat_count, snapshot_path)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (event_id) DO NOTHING
            """,
            camera_name,
            event_id,
            analysis["raw_description"],
            analysis["activity_tag"],
            analysis["location_tag"],
            analysis["cat_count"],
            snapshot_path,
        )

    log.info(
        "Saved observation: camera=%s activity=%s location=%s cats=%s",
        camera_name, analysis["activity_tag"], analysis["location_tag"], analysis["cat_count"]
    )


# ─── MQTT client (runs in background thread) ──────────────────────────────────

def make_mqtt_client(loop: asyncio.AbstractEventLoop) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cat-observer")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected")
            c.subscribe("frigate/events")
            c.subscribe("frigate/+/availability")
        else:
            log.warning("MQTT connect failed: reason_code=%s", reason_code)

    def on_message(c, userdata, msg):
        topic = msg.topic
        try:
            if topic == "frigate/events":
                payload = json.loads(msg.payload)
                evt_type = payload.get("type")
                after = payload.get("after", {})
                label = after.get("label", "")
                score = after.get("score") or after.get("top_score") or 0.0
                if evt_type == "new" and label in TRACKED_LABELS and score >= MIN_SCORE:
                    event_data = {**after, "score": score}
                    asyncio.run_coroutine_threadsafe(event_queue.put(event_data), loop)
            elif "/availability" in topic:
                camera = topic.split("/")[1]
                status = msg.payload.decode()
                camera_availability[camera] = status
                log.info("Camera %s is %s", camera, status)
        except Exception as e:
            log.error("MQTT message error on %s: %s", topic, e)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):
        log.warning("MQTT disconnected (reason=%s), will auto-reconnect", reason_code)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    return client


def start_mqtt(loop: asyncio.AbstractEventLoop):
    client = make_mqtt_client(loop)
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    log.info("MQTT loop started (host=%s port=%s)", MQTT_HOST, MQTT_PORT)
    return client


# ─── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    await get_db()
    mqtt_client = start_mqtt(loop)
    worker_task = asyncio.create_task(event_worker())
    log.info("cat-observer started")
    yield
    worker_task.cancel()
    mqtt_client.loop_stop()
    if db_pool:
        await db_pool.close()


app = FastAPI(title="cat-observer", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    pool = await get_db()
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "ok",
        "db": db_status,
        "cameras": camera_availability,
        "queue_depth": event_queue.qsize(),
    }


@app.get("/observations")
async def list_observations(
    limit: int = Query(20, ge=1, le=200),
    camera: Optional[str] = None,
    activity: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
):
    pool = await get_db()
    filters = ["1=1"]
    args = []
    i = 1
    if camera:
        filters.append(f"camera_name = ${i}"); args.append(camera); i += 1
    if activity:
        filters.append(f"activity_tag = ${i}"); args.append(activity); i += 1
    if since:
        filters.append(f"timestamp >= ${i}"); args.append(since); i += 1
    if until:
        filters.append(f"timestamp <= ${i}"); args.append(until); i += 1

    where = " AND ".join(filters)
    args.append(limit); i += 1
    sql = f"""
        SELECT id, timestamp, camera_name, event_id, raw_description,
               activity_tag, location_tag, cat_count
        FROM observations
        WHERE {where}
        ORDER BY timestamp DESC
        LIMIT ${i - 1}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    return [dict(r) for r in rows]


@app.get("/observations/today")
async def observations_today():
    today = date.today().isoformat()
    return await list_observations(
        limit=100,
        since=datetime.fromisoformat(f"{today}T00:00:00+00:00"),
    )


@app.get("/summary")
async def summary(
    date_str: Optional[str] = Query(None, alias="date"),
    camera: Optional[str] = None,
):
    target = date_str or date.today().isoformat()
    pool = await get_db()

    base_filter = "timestamp::date = $1"
    args: list = [target]
    if camera:
        base_filter += " AND camera_name = $2"
        args.append(camera)

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM observations WHERE {base_filter}", *args
        )
        by_activity = await conn.fetch(
            f"""
            SELECT activity_tag, COUNT(*) as count
            FROM observations WHERE {base_filter}
            GROUP BY activity_tag ORDER BY count DESC
            """, *args
        )
        by_camera = await conn.fetch(
            f"""
            SELECT camera_name, COUNT(*) as count
            FROM observations WHERE {base_filter}
            GROUP BY camera_name ORDER BY count DESC
            """, *args
        )
        timeline = await conn.fetch(
            f"""
            SELECT timestamp, camera_name, activity_tag, location_tag,
                   cat_count, raw_description
            FROM observations WHERE {base_filter}
            ORDER BY timestamp ASC
            """, *args
        )

    return {
        "date": target,
        "camera": camera,
        "total_events": total,
        "by_activity": [dict(r) for r in by_activity],
        "by_camera": [dict(r) for r in by_camera],
        "timeline": [dict(r) for r in timeline],
    }
