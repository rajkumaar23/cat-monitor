"""
cat-observer v2: go2rtc frame polling → VILA1.5-3b (nano-llm) → PostgreSQL
"""

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from io import BytesIO
from typing import Optional

import asyncpg
import httpx
import numpy as np
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# ─── Config ───────────────────────────────────────────────────────────────────

DATABASE_URL     = os.environ["DATABASE_URL"]
GO2RTC_URL       = os.environ.get("GO2RTC_URL", "http://host.docker.internal:1984")
NANO_LLM_URL     = os.environ.get("NANO_LLM_URL", "http://nano-llm:8085")
CAMERAS          = [c.strip() for c in os.environ.get("CAMERAS", "living_room,bedroom").split(",")]
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "2"))
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "0.02"))
LOG_LEVEL        = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_NEW_TOKENS   = int(os.environ.get("MAX_NEW_TOKENS", "96"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cat-observer")

PROMPT = (
    "You are a real-time home security camera observer. "
    "Describe exactly what you see happening RIGHT NOW in this frame. "
    "Be specific and observational — note movement, positions, actions, and any changes. "
    "Focus on cats first: how many, where they are, and what they are actively doing. "
    "Also note people, objects, or anything unusual. "
    "Do NOT give generic descriptions — describe the specific scene in this exact moment. "
    "Keep it to 2 sentences max."
)

# ─── State ────────────────────────────────────────────────────────────────────

db_pool: Optional[asyncpg.Pool] = None
last_frames: dict[str, bytes] = {}

# ─── Database ─────────────────────────────────────────────────────────────────

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS observations (
        id           SERIAL PRIMARY KEY,
        timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        camera_name  TEXT NOT NULL,
        description  TEXT,
        has_cat      BOOLEAN DEFAULT FALSE,
        cat_count    INT,
        activity_tag TEXT,
        location_tag TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_ts  ON observations (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_obs_cam ON observations (camera_name)",
    "CREATE INDEX IF NOT EXISTS idx_obs_cat ON observations (has_cat)",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS description  TEXT",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS has_cat      BOOLEAN DEFAULT FALSE",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS cat_count    INT",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS activity_tag TEXT",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS location_tag TEXT",
]


async def get_db() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        async with db_pool.acquire() as conn:
            for stmt in MIGRATIONS:
                await conn.execute(stmt)
        log.info("Database pool ready")
    return db_pool


# ─── Motion detection ─────────────────────────────────────────────────────────

def has_motion(frame_bytes: bytes, camera: str) -> bool:
    prev = last_frames.get(camera)
    last_frames[camera] = frame_bytes
    if prev is None:
        return True
    try:
        def to_arr(b):
            return np.array(Image.open(BytesIO(b)).convert("L").resize((160, 90)))
        diff = np.abs(to_arr(prev).astype(float) - to_arr(frame_bytes).astype(float)).mean() / 255.0
        return diff > MOTION_THRESHOLD
    except Exception:
        return True


# ─── go2rtc frame grab ────────────────────────────────────────────────────────

async def grab_frame(client: httpx.AsyncClient, camera: str) -> Optional[bytes]:
    try:
        r = await client.get(
            f"{GO2RTC_URL}/api/frame.jpeg",
            params={"src": camera},
            timeout=10,
        )
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning("Frame grab failed (%s): %s", camera, e)
        return None


# ─── VILA analysis ────────────────────────────────────────────────────────────

async def analyze(client: httpx.AsyncClient, frame_bytes: bytes) -> Optional[str]:
    try:
        r = await client.post(
            f"{NANO_LLM_URL}/analyze",
            json={
                "image_b64": base64.b64encode(frame_bytes).decode(),
                "prompt": PROMPT,
                "max_new_tokens": MAX_NEW_TOKENS,
            },
            timeout=90,
        )
        r.raise_for_status()
        return r.json().get("response", "")
    except Exception as e:
        log.error("VILA analysis failed: %s", e)
        return None


# ─── Description parsing ──────────────────────────────────────────────────────

def parse(text: str) -> dict:
    t = text.lower()
    has_cat = any(w in t for w in ["cat", "kitten", "feline"])

    cat_count = None
    for phrase, n in [("one cat", 1), ("1 cat", 1), ("a cat", 1),
                      ("two cat", 2), ("2 cat", 2),
                      ("three cat", 3), ("3 cat", 3)]:
        if phrase in t:
            cat_count = n
            break
    if has_cat and cat_count is None:
        cat_count = 1

    activity = next(
        (a for a in ["sleeping", "resting", "playing", "grooming",
                     "eating", "drinking", "walking", "jumping", "sitting"]
         if a in t), None
    )
    location = next(
        (loc for loc in ["couch", "sofa", "floor", "bed", "window",
                         "food bowl", "litter box", "stairs", "chair", "table"]
         if loc in t), None
    )
    if location == "sofa":
        location = "couch"

    return {"has_cat": has_cat, "cat_count": cat_count,
            "activity_tag": activity, "location_tag": location}


# ─── Per-camera poll ──────────────────────────────────────────────────────────

async def poll_camera(client: httpx.AsyncClient, camera: str):
    frame = await grab_frame(client, camera)
    if frame is None:
        return

    if not has_motion(frame, camera):
        log.debug("%s: no motion, skipping", camera)
        return

    log.info("%s: motion detected, sending to VILA", camera)
    description = await analyze(client, frame)
    if not description:
        return

    parsed = parse(description)
    log.info("%s [has_cat=%s activity=%s]: %s",
             camera, parsed["has_cat"], parsed["activity_tag"], description[:120])

    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO observations
                (camera_name, description, has_cat, cat_count, activity_tag, location_tag)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            camera, description,
            parsed["has_cat"], parsed["cat_count"],
            parsed["activity_tag"], parsed["location_tag"],
        )


# ─── Polling loop ─────────────────────────────────────────────────────────────

async def polling_loop():
    log.info("Polling loop: cameras=%s interval=%ds", CAMERAS, POLL_INTERVAL)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(f"{NANO_LLM_URL}/health", timeout=5)
                if r.json().get("model_loaded"):
                    log.info("nano-llm ready, starting observation loop")
                    break
            except Exception:
                pass
            log.info("Waiting for nano-llm model to load...")
            await asyncio.sleep(15)

    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.gather(
                *[poll_camera(client, cam) for cam in CAMERAS],
                return_exceptions=True,
            )
            await asyncio.sleep(POLL_INTERVAL)


# ─── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_db()
    task = asyncio.create_task(polling_loop())
    log.info("cat-observer started")
    yield
    task.cancel()
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
        "cameras": CAMERAS,
        "poll_interval_seconds": POLL_INTERVAL,
        "nano_llm_url": NANO_LLM_URL,
    }


@app.get("/observations")
async def list_observations(
    limit: int = Query(20, ge=1, le=200),
    camera: Optional[str] = None,
    activity: Optional[str] = None,
    has_cat: Optional[bool] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
):
    pool = await get_db()
    filters, args, i = ["1=1"], [], 1
    for col, val in [("camera_name", camera), ("activity_tag", activity), ("has_cat", has_cat)]:
        if val is not None:
            filters.append(f"{col} = ${i}"); args.append(val); i += 1
    if since:
        filters.append(f"timestamp >= ${i}"); args.append(since); i += 1
    if until:
        filters.append(f"timestamp <= ${i}"); args.append(until); i += 1
    args.append(limit)
    sql = f"""
        SELECT id, timestamp, camera_name, description, has_cat, cat_count, activity_tag, location_tag
        FROM observations WHERE {" AND ".join(filters)}
        ORDER BY timestamp DESC LIMIT ${i}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@app.get("/observations/today")
async def observations_today():
    today = date.today().isoformat()
    return await list_observations(
        limit=200,
        since=datetime.fromisoformat(f"{today}T00:00:00+00:00"),
    )


@app.get("/summary")
async def summary(
    date_str: Optional[str] = Query(None, alias="date"),
    camera: Optional[str] = None,
):
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    pool = await get_db()
    args = [target_date]
    cam_filter = ""
    if camera:
        cam_filter = " AND camera_name = $2"
        args.append(camera)

    async with pool.acquire() as conn:
        total     = await conn.fetchval(
            f"SELECT COUNT(*) FROM observations WHERE timestamp::date=$1{cam_filter}", *args)
        cat_total = await conn.fetchval(
            f"SELECT COUNT(*) FROM observations WHERE timestamp::date=$1 AND has_cat=TRUE{cam_filter}", *args)
        by_act    = await conn.fetch(f"""
            SELECT activity_tag, COUNT(*) AS count FROM observations
            WHERE timestamp::date=$1 AND has_cat=TRUE{cam_filter}
            GROUP BY activity_tag ORDER BY count DESC""", *args)
        by_cam    = await conn.fetch(f"""
            SELECT camera_name, COUNT(*) AS count FROM observations
            WHERE timestamp::date=$1{cam_filter}
            GROUP BY camera_name ORDER BY count DESC""", *args)
        timeline  = await conn.fetch(f"""
            SELECT timestamp, camera_name, description, has_cat, activity_tag, location_tag
            FROM observations WHERE timestamp::date=$1{cam_filter}
            ORDER BY timestamp ASC""", *args)

    return {
        "date": target_date.isoformat(),
        "camera": camera,
        "total_observations": total,
        "cat_observations": cat_total,
        "by_activity": [dict(r) for r in by_act],
        "by_camera": [dict(r) for r in by_cam],
        "timeline": [dict(r) for r in timeline],
    }
