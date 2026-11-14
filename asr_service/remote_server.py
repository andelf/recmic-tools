"""
Qwen3-ASR HTTP service — runs on gpu-box (RTX 5080 16GB, sm_120).

Exposes a single-endpoint ASR API so that other machines on the LAN can use the
GPU instead of running a recogniser locally.

    GET  /health   → engine status, VRAM usage, uptime
    POST /asr      → transcribe audio

Request body for /asr accepts either:
  * multipart/form-data with a `file` field (plus optional `language`, `context`)
  * a raw audio byte stream (Content-Type: application/octet-stream),
    with optional `?language=` / `?context=` query params

Response:
  {"text": str, "language": str|null, "duration_s": float,
   "elapsed_ms": int, "engine": "qwen3-asr-0.6b", "backend": "cuda"}

Run:
  ASR_PORT=8790 /opt/voice/.venv/bin/python remote_server.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional, Tuple

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ── configuration ────────────────────────────────────────────────────────────
MODEL_DIR = os.environ.get("ASR_MODEL_DIR", "/opt/voice/models/Qwen3-ASR-0.6B")
HOST = os.environ.get("ASR_HOST", "0.0.0.0")
PORT = int(os.environ.get("ASR_PORT", "8790"))
MAX_NEW_TOKENS = int(os.environ.get("ASR_MAX_NEW_TOKENS", "1024"))
TARGET_SR = 16_000
MAX_UPLOAD_BYTES = int(os.environ.get("ASR_MAX_UPLOAD_MB", "64")) * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("qwen-asr")

# ── global state ─────────────────────────────────────────────────────────────
_model: Any = None
_model_lock = threading.Lock()   # serialises GPU inference
_started_at = time.time()
_infer_count = 0
_ffmpeg = shutil.which("ffmpeg")


# ── model lifecycle ──────────────────────────────────────────────────────────
def load_model() -> None:
    global _model
    if _model is not None:
        return
    from qwen_asr import Qwen3ASRModel

    log.info("loading %s ...", MODEL_DIR)
    t0 = time.time()
    _model = Qwen3ASRModel.from_pretrained(
        MODEL_DIR,
        dtype=torch.bfloat16,
        device_map="cuda:0",
        max_new_tokens=MAX_NEW_TOKENS,
    )
    log.info("model ready in %.1fs", time.time() - t0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _ffmpeg:
        log.error("ffmpeg not found in PATH — only raw PCM uploads will work")
    # Warm up so the first real request is not penalised by kernel autotuning.
    await asyncio.get_running_loop().run_in_executor(None, load_model)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _model.transcribe((np.zeros(TARGET_SR // 2, dtype=np.float32), TARGET_SR))
        )
        log.info("warm-up done")
    except Exception as exc:            # pragma: no cover - warm-up is best effort
        log.warning("warm-up failed: %s", exc)
    yield
    log.info("shutting down")


app = FastAPI(title="Qwen3-ASR Service", version="1.0.0", lifespan=lifespan)


# ── audio decoding ───────────────────────────────────────────────────────────
def decode_audio(raw: bytes) -> Tuple[np.ndarray, int]:
    """Decode arbitrary audio bytes into mono float32 @ 16 kHz using ffmpeg."""
    if not raw:
        raise ValueError("empty audio payload")
    if not _ffmpeg:
        raise RuntimeError("ffmpeg unavailable on server")

    fd, src = tempfile.mkstemp(suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        proc = subprocess.run(
            [
                _ffmpeg, "-nostdin", "-loglevel", "error",
                "-i", src,
                "-f", "f32le", "-acodec", "pcm_f32le",
                "-ac", "1", "-ar", str(TARGET_SR), "-",
            ],
            capture_output=True,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
            raise ValueError(f"ffmpeg decode failed: {detail}")
        audio = np.frombuffer(proc.stdout, dtype=np.float32)
        if audio.size == 0:
            raise ValueError("decoded audio is empty")
        return audio, TARGET_SR
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


# ── inference ────────────────────────────────────────────────────────────────
def run_inference(
    audio: np.ndarray,
    language: Optional[str],
    context: str,
) -> dict:
    global _infer_count
    with _model_lock:
        t0 = time.time()
        results = _model.transcribe(
            (audio, TARGET_SR),
            context=context or "",
            language=language,
        )
        elapsed = time.time() - t0
        _infer_count += 1

    first = results[0] if results else None
    text = getattr(first, "text", None)
    if text is None:
        text = str(first) if first is not None else ""
    detected_lang = getattr(first, "language", None)

    return {
        "text": str(text).strip(),
        "language": str(detected_lang) if detected_lang else None,
        "duration_s": round(len(audio) / TARGET_SR, 3),
        "elapsed_ms": int(elapsed * 1000),
        "engine": "qwen3-asr-0.6b",
        "backend": "cuda",
    }


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> JSONResponse:
    vram_gb = None
    gpu_name = None
    if torch.cuda.is_available():
        vram_gb = round(torch.cuda.memory_allocated() / 1e9, 2)
        gpu_name = torch.cuda.get_device_name(0)
    return JSONResponse(
        {
            "ok": _model is not None,
            "engine": "qwen3-asr-0.6b",
            "engine_version": "qwen-asr",
            "model_dir": MODEL_DIR,
            "gpu": gpu_name,
            "vram_allocated_gb": vram_gb,
            "ffmpeg": bool(_ffmpeg),
            "uptime_s": int(time.time() - _started_at),
            "requests_served": _infer_count,
        }
    )


@app.post("/asr")
async def asr(request: Request) -> JSONResponse:
    if _model is None:
        raise HTTPException(status_code=503, detail="model not loaded")

    content_type = (request.headers.get("content-type") or "").lower()
    language: Optional[str] = None
    context = ""

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart body requires a 'file' field")
        raw = await upload.read()
        language = (form.get("language") or None) or None
        context = str(form.get("context") or "")
    else:
        raw = await request.body()
        language = request.query_params.get("language") or None
        context = request.query_params.get("context") or ""

    if not raw:
        raise HTTPException(status_code=400, detail="empty request body")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    try:
        audio, sr = await asyncio.get_running_loop().run_in_executor(None, decode_audio, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"decode error: {exc}") from exc

    if audio.size < int(0.05 * sr):
        raise HTTPException(status_code=400, detail="audio too short (< 50 ms)")

    try:
        payload = await asyncio.get_running_loop().run_in_executor(
            None, run_inference, audio, language, context
        )
    except Exception as exc:
        log.exception("inference failed")
        raise HTTPException(status_code=500, detail=f"inference error: {exc}") from exc

    log.info(
        "asr ok dur=%.2fs infer=%dms chars=%d",
        payload["duration_s"], payload["elapsed_ms"], len(payload["text"]),
    )
    return JSONResponse(payload)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, workers=1, log_level="info")
