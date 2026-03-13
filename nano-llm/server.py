#!/usr/bin/env python3
"""
VILA1.5-3b HTTP API server.
Runs inside dustynv/nano_llm container on Jetson.
Single worker thread ensures serial GPU access.
"""

import asyncio
import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nano-llm-server")

VILA_MODEL = os.environ.get("VILA_MODEL", "Efficient-Large-Model/VILA1.5-3b")
MAX_NEW_TOKENS = int(os.environ.get("MAX_NEW_TOKENS", "96"))

app = FastAPI(title="nano-llm-server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

model = None
executor = ThreadPoolExecutor(max_workers=1)


def _load_model():
    global model
    import os
    os.makedirs("/data/models/mlc/dist/models", exist_ok=True)
    from nano_llm import NanoLLM
    log.info("Loading %s ...", VILA_MODEL)
    model = NanoLLM.from_pretrained(VILA_MODEL, api="mlc", quantization="q4f16_1")
    log.info("Model ready.")


def _infer(image: Image.Image, prompt: str, max_tokens: int) -> str:
    from nano_llm import ChatHistory
    chat = ChatHistory(model)
    chat.append(role="user", msg=prompt, image=image)
    embedding, _ = chat.embed_chat()
    response = model.generate(embedding, streaming=False, max_new_tokens=max_tokens)
    return (response or "").strip()


@app.on_event("startup")
async def startup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, _load_model)


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model is not None, "model": VILA_MODEL}


class AnalyzeRequest(BaseModel):
    image_b64: str
    prompt: str = "Describe what you see, focusing on any cats and their activities."
    max_new_tokens: int = MAX_NEW_TOKENS


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if model is None:
        raise HTTPException(503, "Model not loaded yet")
    try:
        image = Image.open(BytesIO(base64.b64decode(req.image_b64))).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"Invalid image: {e}")

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(executor, _infer, image, req.prompt, req.max_new_tokens)
    return {"response": response, "model": VILA_MODEL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8085, log_level="info")
