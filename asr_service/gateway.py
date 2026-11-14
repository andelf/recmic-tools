"""
Local ASR gateway — primary: Qwen3-ASR on gpu-box (192.0.2.175), fallback: SenseVoice.

Why a gateway instead of calling the remote directly:

  The RTX 5080 box recognises Chinese noticeably better (CER 2.4% vs 4.4%) but it
  sits on Wi-Fi and has gone offline before. This process keeps the good engine on
  the fast path while degrading to the local CPU model the moment the LAN link
  misbehaves — callers always get *some* transcript.

A circuit breaker avoids paying the socket timeout on every request while the
remote is down: after N consecutive failures the remote is skipped entirely for a
cooldown window, then probed again with a single request.

    GET  /health          → local model state + circuit breaker state
    GET  /engines         → probe the remote right now, report both engines
    POST /asr             → transcribe (multipart `file`, or raw body)
    POST /asr?engine=local→ force the local fallback (debugging)
    POST /asr?engine=auto → default behaviour

Response carries `engine` and `fallback` so callers can tell which one ran.

Run:
  .venv\\Scripts\\python.exe -m uvicorn asr_service.gateway:app --port 8799
  (or: .venv\\Scripts\\python.exe asr_service\\gateway.py)
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import sys
import threading
import time
import http.client
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import quote, urlsplit

import numpy as np
import soundfile as sf

# ── configuration ────────────────────────────────────────────────────────────
QWEN_URL = os.environ.get("QWEN_ASR_URL", "http://192.0.2.175:8790").rstrip("/")
# Separate connect and read budgets: a dead host must fail fast, while a long
# clip still gets room to finish inference on the GPU box.
QWEN_CONNECT_TIMEOUT = float(os.environ.get("QWEN_ASR_CONNECT_TIMEOUT", "2.5"))
QWEN_TIMEOUT = float(os.environ.get("QWEN_ASR_TIMEOUT", "8"))
QWEN_PROBE_TIMEOUT = float(os.environ.get("QWEN_ASR_PROBE_TIMEOUT", "3"))
QWEN_HEALTH_TIMEOUT = float(os.environ.get("QWEN_ASR_HEALTH_TIMEOUT", "2.5"))

_remote_parts = urlsplit(QWEN_URL)
REMOTE_HOST = _remote_parts.hostname or "192.0.2.175"
REMOTE_PORT = _remote_parts.port or 80
REMOTE_PREFIX = _remote_parts.path.rstrip("/")

BREAKER_THRESHOLD = int(os.environ.get("ASR_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN = float(os.environ.get("ASR_BREAKER_COOLDOWN", "20"))

DEFAULT_MODEL_DIR = os.environ.get(
    "SENSEVOICE_MODEL_DIR",
    r"C:\Users\<you>\.local\share\picc\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
)
SENSEVOICE_THREADS = int(os.environ.get("SENSEVOICE_THREADS", "2"))
TARGET_SR = 16_000
MAX_UPLOAD_BYTES = int(os.environ.get("ASR_MAX_UPLOAD_MB", "64")) * 1024 * 1024

# Default log path so the process is usable without the wrapper .bat — that
# matters because the scheduled task launches pythonw.exe directly (a wrapper
# script would make the task's lifetime end when the wrapper exits, and Task
# Scheduler then kills the gateway along with it).
LOG_PATH = os.environ.get(
    "ASR_GATEWAY_LOG",
    str(Path(__file__).resolve().parent.parent / "logs" / "asr_gateway.log"),
)

_log_handlers: list = []
if sys.stderr is not None:          # console present (interactive run)
    _log_handlers.append(logging.StreamHandler())
try:
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
except OSError:
    pass  # unwritable path — console logging only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=_log_handlers or [logging.NullHandler()],
)
log = logging.getLogger("asr-gateway")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_started_at = time.time()


# ── circuit breaker ──────────────────────────────────────────────────────────
class CircuitBreaker:
    """Skips the remote entirely after repeated failures, then probes again."""

    def __init__(self, threshold: int, cooldown: float):
        self.threshold = threshold
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._probing = False
        self._last_error: Optional[str] = None
        self.total_fallbacks = 0

    def allow(self) -> Tuple[bool, str]:
        """Return (allowed, reason). Only one half-open probe is let through."""
        with self._lock:
            if self._opened_at is None:
                return True, "closed"
            elapsed = time.time() - self._opened_at
            if elapsed < self.cooldown:
                return False, f"open ({self.cooldown - elapsed:.0f}s cooldown left)"
            if self._probing:
                return False, "half-open (probe in flight)"
            self._probing = True
            return True, "half-open probe"

    def success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                log.info("circuit closed — remote recovered")
            self._failures = 0
            self._opened_at = None
            self._probing = False
            self._last_error = None

    def failure(self, reason: str) -> None:
        with self._lock:
            self._probing = False
            self._failures += 1
            self._last_error = reason
            if self._opened_at is None and self._failures >= self.threshold:
                self._opened_at = time.time()
                log.warning(
                    "circuit opened after %d failures (%s) — local fallback for %.0fs",
                    self._failures, reason, self.cooldown,
                )

    def note_fallback(self) -> None:
        with self._lock:
            self.total_fallbacks += 1

    def snapshot(self) -> dict:
        with self._lock:
            state = "closed"
            if self._opened_at is not None:
                state = (
                    "open"
                    if time.time() - self._opened_at < self.cooldown
                    else "half-open"
                )
            return {
                "state": state,
                "consecutive_failures": self._failures,
                "last_error": self._last_error,
                "total_fallbacks": self.total_fallbacks,
            }


breaker = CircuitBreaker(BREAKER_THRESHOLD, BREAKER_COOLDOWN)


# ── local SenseVoice engine ──────────────────────────────────────────────────
class LocalSenseVoice:
    """Lazily loaded sherpa-onnx SenseVoice recogniser (CPU, int8)."""

    def __init__(self, model_dir: str, num_threads: int):
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self._recognizer: Any = None
        self._lock = threading.Lock()
        self._load_error: Optional[str] = None
        self.load_seconds: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self._recognizer is not None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def load(self) -> None:
        with self._lock:
            if self._recognizer is not None:
                return
            import sherpa_onnx

            model_file = None
            for candidate in ("model.int8.onnx", "model.onnx"):
                p = self.model_dir / candidate
                if p.exists():
                    model_file = p
                    break
            if model_file is None:
                raise FileNotFoundError(f"no model.int8.onnx/model.onnx in {self.model_dir}")
            tokens = self.model_dir / "tokens.txt"
            if not tokens.exists():
                raise FileNotFoundError(f"tokens.txt missing in {self.model_dir}")

            t0 = time.time()
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_file),
                tokens=str(tokens),
                use_itn=True,
                num_threads=self.num_threads,
                debug=False,
                language="auto",
            )
            self.load_seconds = round(time.time() - t0, 2)
            self._load_error = None
            log.info("local SenseVoice ready in %.2fs", self.load_seconds)

    def transcribe(self, audio: np.ndarray) -> str:
        if self._recognizer is None:
            raise RuntimeError("local model not loaded")
        with self._lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate=TARGET_SR, waveform=audio)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()


local_engine = LocalSenseVoice(DEFAULT_MODEL_DIR, SENSEVOICE_THREADS)


# ── audio helpers ────────────────────────────────────────────────────────────
def decode_audio(raw: bytes) -> Tuple[np.ndarray, int]:
    """Decode audio bytes to mono float32 @ 16 kHz using libsndfile."""
    if not raw:
        raise ValueError("empty audio payload")
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if sr != TARGET_SR:
        mono = resample_linear(mono, sr, TARGET_SR)
    return mono.astype(np.float32, copy=False), TARGET_SR


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Cheap linear resampler — adequate for speech at 16 kHz targets."""
    if src_sr == dst_sr or x.size == 0:
        return x
    duration = x.size / src_sr
    n_out = int(round(duration * dst_sr))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.arange(x.size, dtype=np.float64)
    dst_idx = np.linspace(0, x.size - 1, n_out, dtype=np.float64)
    return np.interp(dst_idx, src_idx, x).astype(np.float32)


# ── remote engine ────────────────────────────────────────────────────────────
def _new_connection(read_timeout: float) -> http.client.HTTPConnection:
    """Connect with a short budget, then relax the socket for the inference wait.

    http.client is used instead of urllib on purpose: it never consults
    HTTP_PROXY (which is commonly set on developer machines and would hijack a
    LAN address), and it lets us bound the connect phase separately.
    """
    conn = http.client.HTTPConnection(REMOTE_HOST, REMOTE_PORT, timeout=QWEN_CONNECT_TIMEOUT)
    conn.connect()
    if conn.sock is not None:
        conn.sock.settimeout(read_timeout)
    return conn


def call_remote(
    raw: bytes,
    language: Optional[str],
    context: str,
    read_timeout: Optional[float] = None,
) -> dict:
    """POST raw audio bytes to the gpu-box Qwen3-ASR service."""
    qs = []
    if language:
        qs.append("language=" + quote(language))
    if context:
        qs.append("context=" + quote(context))
    path = f"{REMOTE_PREFIX}/asr" + (("?" + "&".join(qs)) if qs else "")

    conn = _new_connection(read_timeout or QWEN_TIMEOUT)
    try:
        conn.request(
            "POST",
            path,
            body=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(raw)),
            },
        )
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
    except socket.timeout as exc:
        raise RuntimeError(f"timeout after {read_timeout or QWEN_TIMEOUT}s") from exc
    except ConnectionRefusedError as exc:
        raise RuntimeError("connection refused (service down?)") from exc
    except OSError as exc:
        raise RuntimeError(f"unreachable: {exc}") from exc
    finally:
        conn.close()

    if status != 200:
        raise RuntimeError(f"HTTP {status}: {body.decode('utf-8', 'replace')[:200]}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("malformed response from remote") from exc
    if not isinstance(payload, dict) or "text" not in payload:
        raise RuntimeError("malformed response from remote")
    return payload


def probe_remote() -> dict:
    conn = None
    try:
        conn = _new_connection(QWEN_HEALTH_TIMEOUT)
        conn.request("GET", f"{REMOTE_PREFIX}/health")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return {"reachable": True, "detail": payload}
    except Exception as exc:  # noqa: BLE001 - any failure means unreachable
        reason = getattr(exc, "reason", None) or exc
        return {"reachable": False, "error": str(reason)[:200]}
    finally:
        if conn is not None:
            conn.close()


# ── app ──────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    def _warm():
        try:
            local_engine.load()
        except Exception as exc:  # noqa: BLE001
            local_engine._load_error = str(exc)  # noqa: SLF001
            log.error("local model failed to load: %s", exc)

    # Load the fallback engine in the background so startup is instant and the
    # remote stays the fast path.
    asyncio.get_running_loop().run_in_executor(None, _warm)
    log.info("gateway up — remote=%s local=%s", QWEN_URL, DEFAULT_MODEL_DIR)
    yield


app = FastAPI(title="ASR Gateway", version="1.0.0", lifespan=lifespan)


async def _read_request(request: Request) -> Tuple[bytes, Optional[str], str]:
    content_type = (request.headers.get("content-type") or "").lower()
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
    return raw, language, context


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "remote": {"url": QWEN_URL, "circuit": breaker.snapshot()},
            "local": {
                "engine": "sensevoice-int8",
                "model_dir": DEFAULT_MODEL_DIR,
                "ready": local_engine.ready,
                "load_seconds": local_engine.load_seconds,
                "load_error": local_engine.load_error,
            },
            "uptime_s": int(time.time() - _started_at),
        }
    )


@app.get("/engines")
async def engines() -> JSONResponse:
    import asyncio

    remote = await asyncio.get_running_loop().run_in_executor(None, probe_remote)
    return JSONResponse(
        {
            "remote": {"url": QWEN_URL, **remote, "circuit": breaker.snapshot()},
            "local": {"ready": local_engine.ready, "error": local_engine.load_error},
        }
    )


@app.post("/asr")
async def asr(request: Request) -> JSONResponse:
    import asyncio

    raw, language, context = await _read_request(request)
    mode = (request.query_params.get("engine") or "auto").lower()
    if mode not in {"auto", "local", "remote"}:
        raise HTTPException(status_code=400, detail="engine must be auto|local|remote")

    loop = asyncio.get_running_loop()
    started = time.time()
    upstream_error: Optional[str] = None
    fallback = False

    # ── try the GPU box first ────────────────────────────────────────────────
    if mode in {"auto", "remote"}:
        allowed, reason = breaker.allow()
        if not allowed:
            upstream_error = f"circuit {reason}"
        else:
            # A half-open probe only has to prove the service is alive — do not
            # make callers wait the full inference budget for that.
            read_timeout = QWEN_PROBE_TIMEOUT if reason.startswith("half-open") else QWEN_TIMEOUT
            try:
                payload = await loop.run_in_executor(
                    None, call_remote, raw, language, context, read_timeout
                )
                breaker.success()
                payload.update(
                    engine="qwen3-asr",
                    fallback=False,
                    engine_detail="Qwen3-ASR-0.6B @ 192.0.2.175",
                    gateway_ms=int((time.time() - started) * 1000),
                )
                log.info(
                    "remote ok dur=%.2fs infer=%sms total=%sms",
                    payload.get("duration_s", 0),
                    payload.get("elapsed_ms"),
                    payload["gateway_ms"],
                )
                return JSONResponse(payload)
            except Exception as exc:  # noqa: BLE001
                upstream_error = str(exc)
                breaker.failure(upstream_error)
                log.warning("remote failed (%s)", upstream_error)
                if mode == "remote":
                    raise HTTPException(status_code=502, detail=upstream_error) from exc

    # ── fall back to the local CPU model ────────────────────────────────────
    fallback = mode == "auto"
    breaker.note_fallback()
    try:
        if not local_engine.ready:
            await loop.run_in_executor(None, local_engine.load)
        audio, sr = await loop.run_in_executor(None, decode_audio, raw)
        if audio.size < int(0.05 * sr):
            raise HTTPException(status_code=400, detail="audio too short (< 50 ms)")
        t0 = time.time()
        text = await loop.run_in_executor(None, local_engine.transcribe, audio)
        elapsed = time.time() - t0
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("local fallback failed")
        raise HTTPException(
            status_code=503,
            detail=f"both engines failed — remote: {upstream_error}; local: {exc}",
        ) from exc

    payload = {
        "text": text,
        "language": None,
        "duration_s": round(audio.size / sr, 3),
        "elapsed_ms": int(elapsed * 1000),
        "engine": "sensevoice-local",
        "engine_detail": "sherpa-onnx SenseVoice int8 (CPU)",
        "backend": "cpu",
        "fallback": fallback,
        "upstream_error": upstream_error,
        "gateway_ms": int((time.time() - started) * 1000),
    }
    log.info(
        "local ok dur=%.2fs infer=%dms total=%dms fallback=%s",
        payload["duration_s"], payload["elapsed_ms"], payload["gateway_ms"], fallback,
    )
    return JSONResponse(payload)


if __name__ == "__main__":
    import uvicorn

    _run_kw: dict = {}
    if sys.stderr is None:
        # pythonw.exe: no console, so uvicorn's default stream handler would
        # raise on every record. Point its loggers at the same file instead.
        _run_kw["log_config"] = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "format": "%(asctime)s %(levelname)s %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": LOG_PATH,
                    "encoding": "utf-8",
                    "formatter": "plain",
                }
            },
            "loggers": {
                "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
            },
        }

    uvicorn.run(
        app,
        host=os.environ.get("ASR_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("ASR_GATEWAY_PORT", "8799")),
        workers=1,
        log_level="info",
        **_run_kw,
    )
