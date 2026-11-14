"""
Hands-free dictation using OLYMPUS DR Series + ASR gateway.

Workflow:
  1. Hold FAST_BACKWARD on the device  → starts audio capture
  2. Release FAST_BACKWARD             → stops capture, runs ASR, types result

ASR routing:
  By default audio is sent to the local ASR gateway (asr_service/gateway.py),
  which prefers the Qwen3-ASR GPU box (192.0.2.175) and falls back to the
  in-process SenseVoice model. That gateway is started on a worker thread of
  this process, so there is no second process to lose track of — if this app is
  running, the gateway is running. If some *other* process already serves the
  gateway, that one is reused and left untouched on exit.

  --local             bypass the gateway entirely, always run SenseVoice here
  --asr-url URL       point at a different gateway
  --no-gateway-autostart
                      probe the gateway but never start one

Model setup (only needed for the in-process fallback):
  Download and extract to the workspace (or pass --model-dir):
    https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2

Requirements:
    pip install sherpa-onnx sounddevice numpy pynput pywinusb
"""

from __future__ import annotations

import argparse
import atexit
import http.client
import io
import json
import math
import os
import queue
import signal
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlsplit

import numpy as np
import sherpa_onnx
import sounddevice as sd
from pynput.keyboard import Controller as KbController, Key
from pynput.mouse import Controller as MouseController

import pywinusb.hid as hid
from read_media_keys import TARGET_VID, TARGET_PID, BITMAP_HINTS
from overlay import create_overlay

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 16_000
CHANNELS      = 1
FAST_BACKWARD = (2, 0x08)   # hold-to-record trigger key
NEW_KEY       = (2, 0x04)   # trigger one escape on press
F1_KEY        = (2, 0x40)   # trigger Ctrl+C on press
F2_KEY        = (2, 0x80)   # type 'continue' on press
F3_KEY        = (3, 0x08)   # trigger Ctrl+Enter on press
F4_KEY        = (3, 0x40)   # trigger one backspace on press
REW_KEY       = (1, 0x04)   # trigger one enter on press
FF_KEY        = (1, 0x08)   # trigger mouse scroll down
INSERT_OVER   = (4, 0x01)   # trigger mouse scroll up

F4_REPEAT_DELAY    = 0.40   # hold F4 this long before backspace auto-repeats
F4_REPEAT_INTERVAL = 0.045  # gap between auto-repeated backspaces

TICK_MS       = 16          # overlay refresh interval (~60 fps)

# ──────────────────────────────────────────────────────────────────────────────
# Audio recording helpers
# ──────────────────────────────────────────────────────────────────────────────

class AudioCapture:
    """Accumulates float32 mono 16 kHz samples while open.

    Also exposes ``level`` — a 0..1 loudness reading for the on-screen
    indicator.  It is written from the PortAudio callback thread and read from
    the UI thread; a plain float attribute is atomic enough under the GIL.

    Two mappings are blended, because a fixed dBFS curve looks dead on a quiet
    microphone and clips on a hot one:

    * absolute  — dBFS between ``_DB_FLOOR`` and ``_DB_CEIL``; correct from the
      very first audio block, before anything is known about this microphone.
    * relative  — level against a slow-decaying peak, so speaking always fills
      the display whatever the gain happens to be.  The ``_PEAK_FLOOR`` guard
      stops pure noise from being amplified into a fake waveform.
    """

    _DB_FLOOR = -55.0
    _DB_CEIL = -10.0
    _PEAK_FLOOR = 0.02

    def __init__(self):
        self._chunks: List[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self.level: float = 0.0
        self._peak: float = 0.0

    def start(self):
        with self._lock:
            self._chunks.clear()
            self.level = 0.0
            self._peak = 0.0
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._cb,
            )
            self._stream.start()
            print("[REC] ● recording …")

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            if self._chunks:
                audio = np.concatenate(self._chunks, axis=0).flatten()
            else:
                audio = np.zeros(0, dtype="float32")
            self.level = 0.0
            print(f"[REC] ■ stopped  ({len(audio)/SAMPLE_RATE:.2f}s)")
            return audio

    def _cb(self, indata, frames, time_info, status):
        self._chunks.append(indata.copy())
        rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float32))))

        db = 20.0 * math.log10(rms + 1e-9)
        absolute = (db - self._DB_FLOOR) / (self._DB_CEIL - self._DB_FLOOR)

        if rms > self._peak:
            self._peak += (rms - self._peak) * 0.6   # snap up to new peaks
        else:
            self._peak *= 0.9992                     # ~5 %/s decay
        relative = rms / max(self._peak, self._PEAK_FLOOR)

        self.level = min(1.0, max(0.0, max(absolute, relative * 0.95)))


# ──────────────────────────────────────────────────────────────────────────────
# ASR
# ──────────────────────────────────────────────────────────────────────────────

def build_recognizer(model_dir: str) -> sherpa_onnx.OfflineRecognizer:
    d = Path(model_dir)
    # Prefer int8 quantized model when available
    for candidate in ("model.int8.onnx", "model.onnx"):
        model_file = d / candidate
        if model_file.exists():
            break
    else:
        sys.exit(f"[!] No model.onnx / model.int8.onnx found in {d}")

    tokens_file = d / "tokens.txt"
    if not tokens_file.exists():
        sys.exit(f"[!] tokens.txt not found in {d}")

    print(f"[ASR] Loading {model_file.name} from {d} …")
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_file),
        tokens=str(tokens_file),
        use_itn=True,
        num_threads=2,
        debug=False,
        language="auto",
    )
    print("[ASR] Model ready.")
    return recognizer


def encode_wav(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Pack float32 mono samples into a 16-bit PCM WAV container."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16.tobytes())
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Gateway bootstrap
#
# The gateway runs on a worker thread of *this* process.  No child process, no
# scheduled task, no registry keys: nothing survives deleting the folder and
# nothing is orphaned when this app exits.
#
# Why a thread is good enough (measured on this machine, RTX-5080 box offline):
#   · a 16 ms UI loop keeps a 16.0 ms median frame gap while SenseVoice decodes;
#     worst frame 29 ms — one skipped frame, invisible to the eye
#   · the 1.5 s onnxruntime model load never blocks the loop past 32 ms
#   · uvicorn 0.54 skips signal-handler installation off the main thread, so it
#     shuts down cleanly via ``server.should_exit`` in ~135 ms
# sherpa-onnx releases the GIL inside decode, which is what makes this work.
#
# The trade-off, stated plainly: an interpreter-level crash in the gateway now
# takes the dictation app with it.  That is acceptable here because both halves
# are ours and both die with the same Ctrl+C anyway; it would not be for a
# gateway shared with unrelated clients.
# ──────────────────────────────────────────────────────────────────────────────
GATEWAY_SCRIPT = Path(__file__).resolve().parent / "asr_service" / "gateway.py"
WORKSPACE_ROOT = Path(__file__).resolve().parent

# Set only when *we* are serving the gateway from this process.
_gateway_server: Optional[Any] = None
_gateway_thread: Optional[threading.Thread] = None
_gateway_error: Optional[str] = None


def _gateway_alive(asr_url: str, timeout: float = 1.5) -> bool:
    """Cheap liveness probe.  http.client so a system HTTP_PROXY cannot interfere."""
    parts = urlsplit(asr_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — refused, timeout, DNS … all mean "not up"
        return False


def _serve_gateway(host: str, port: int) -> None:
    """Thread body: run uvicorn until ``should_exit`` is set."""
    global _gateway_server, _gateway_error

    if str(WORKSPACE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKSPACE_ROOT))
    try:
        import uvicorn

        from asr_service.gateway import app as gateway_app
    except Exception as exc:  # noqa: BLE001
        _gateway_error = f"import failed: {type(exc).__name__}: {exc}"
        return

    try:
        config = uvicorn.Config(
            gateway_app, host=host, port=port, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        _gateway_server = server
        server.run()
    except Exception as exc:  # noqa: BLE001 — port in use, import race, …
        _gateway_error = f"{type(exc).__name__}: {exc}"


def ensure_gateway(asr_url: str, wait_seconds: float = 15.0) -> bool:
    """Bring the ASR gateway up unless something is already serving it.

    Returns True once ``/health`` answers.  A gateway owned by another process
    is reused and deliberately left running when we exit.
    """
    global _gateway_thread, _gateway_error

    if _gateway_alive(asr_url):
        print("[GW] gateway already running (external) — reusing it")
        return True
    if _gateway_thread is not None and _gateway_thread.is_alive():
        return _gateway_alive(asr_url, timeout=3.0)
    if not GATEWAY_SCRIPT.exists():
        print(f"[GW] {GATEWAY_SCRIPT} missing — skipping autostart")
        return False

    parts = urlsplit(asr_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80

    _gateway_error = None
    thread = threading.Thread(
        target=_serve_gateway, args=(host, port), name="asr-gateway", daemon=True
    )
    _gateway_thread = thread
    thread.start()
    atexit.register(shutdown_gateway)

    print("[GW] starting gateway", end="", flush=True)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.2)
        print(".", end="", flush=True)
        if _gateway_alive(asr_url):
            print(" ok")
            return True
        if _gateway_error:
            print(f" failed ({_gateway_error})")
            return False
        if not thread.is_alive():
            print(" failed (thread exited)")
            return False
    print(" timed out — will fall back to in-process SenseVoice")
    return False


def gateway_engine():
    """The in-process SenseVoice engine, when this process serves the gateway.

    Sharing it avoids loading a second copy of the model purely for the
    "gateway unreachable" path — which, in-process, is nearly unreachable.
    """
    if _gateway_server is None:
        return None
    try:
        from asr_service.gateway import local_engine

        return local_engine
    except Exception:  # noqa: BLE001
        return None


def shutdown_gateway() -> None:
    """Stop the gateway thread — only if this process is the one serving it."""
    global _gateway_server, _gateway_thread

    server, thread = _gateway_server, _gateway_thread
    _gateway_server = _gateway_thread = None
    if thread is None:
        return
    print("[GW] stopping gateway")
    if server is not None:
        server.should_exit = True
    thread.join(timeout=5)
    if thread.is_alive():
        print("[GW] gateway thread did not stop in time (process exit will reap it)")



class AsrClient:
    """Transcribes via the local gateway, falling back to an in-process model.

    The gateway is what actually decides between the GPU box and local
    SenseVoice, so this class only needs to handle "gateway unreachable".
    """

    def __init__(
        self,
        model_dir: str,
        asr_url: Optional[str],
        timeout: float = 60.0,
        shared_engine: Any = None,
    ):
        self.model_dir = model_dir
        self.asr_url = asr_url
        self.timeout = timeout
        self._recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None
        self._recognizer_lock = threading.Lock()
        self._gateway_warned = False
        # When the gateway lives in this very process we can borrow its
        # SenseVoice instance instead of loading a second copy of the weights.
        self._shared = shared_engine

        if asr_url:
            parts = urlsplit(asr_url)
            self._host = parts.hostname or "127.0.0.1"
            self._port = parts.port or 80
            self._path = parts.path or "/asr"
            print(f"[ASR] Gateway: {asr_url}")
        else:
            print("[ASR] --local: using in-process SenseVoice")

    # -- gateway path --------------------------------------------------------
    def _transcribe_http(self, audio: np.ndarray) -> str:
        body = encode_wav(audio)
        # http.client (not urllib) so a system-wide HTTP_PROXY cannot intercept
        # traffic aimed at the loopback gateway.
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        try:
            conn.request(
                "POST",
                self._path,
                body=body,
                headers={"Content-Type": "audio/wav", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {raw.decode('utf-8', 'replace')[:150]}")
            payload = json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

        engine = payload.get("engine", "?")
        ms = payload.get("gateway_ms") or payload.get("elapsed_ms")
        tag = "FALLBACK" if payload.get("fallback") else "primary"
        print(f"[ASR] {engine} ({tag}, {ms}ms)")
        return (payload.get("text") or "").strip()

    # -- local path ----------------------------------------------------------
    def _transcribe_local(self, audio: np.ndarray) -> str:
        if self._shared is not None:
            # The gateway's own engine: load() is a no-op once it is ready, and
            # blocks otherwise (the gateway warms it up in the background).
            self._shared.load()
            return self._shared.transcribe(audio)
        with self._recognizer_lock:
            if self._recognizer is None:
                self._recognizer = build_recognizer(self.model_dir)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) == 0:
            return ""
        if self.asr_url:
            try:
                return self._transcribe_http(audio)
            except Exception as exc:  # noqa: BLE001
                if not self._gateway_warned:
                    print(f"[ASR] gateway unavailable ({exc}) — using in-process SenseVoice")
                    self._gateway_warned = True
                else:
                    print(f"[ASR] gateway still down ({type(exc).__name__}) — local")
        return self._transcribe_local(audio)


# ──────────────────────────────────────────────────────────────────────────────
# HID → dictation glue
# ──────────────────────────────────────────────────────────────────────────────

def active_bits(payload: bytes):
    result = set()
    for i, b in enumerate(payload):
        if b == 0:
            continue
        for n in range(8):
            mask = 1 << n
            if b & mask:
                result.add((i, mask))
    return result


def _repeat_backspace(held: threading.Event, stop: threading.Event, emit) -> None:
    """Hold-to-repeat driver for F4.

    The HID report only flips state on the edges, so repetition cannot come from
    the device — it is synthesised here. A tap produces nothing beyond the one
    backspace already sent on press; holding past F4_REPEAT_DELAY starts a burst
    at F4_REPEAT_INTERVAL. ``emit`` is called once per repeat.
    """
    while not stop.is_set():
        if not held.wait(0.05):
            continue
        # Initial delay, interruptible by a release.
        deadline = time.monotonic() + F4_REPEAT_DELAY
        while time.monotonic() < deadline:
            if not held.is_set() or stop.is_set():
                break
            time.sleep(0.01)
        else:
            while held.is_set() and not stop.is_set():
                try:
                    emit()
                except Exception as exc:  # noqa: BLE001
                    print(f"[HID] ! backspace repeat failed: {type(exc).__name__}: {exc}")
                    break
                # Sleep in slices so a release is noticed promptly.
                deadline = time.monotonic() + F4_REPEAT_INTERVAL
                while time.monotonic() < deadline:
                    if not held.is_set() or stop.is_set():
                        break
                    time.sleep(0.005)


def run(client: AsrClient, vid: int, pid: int, dry_run: bool = False, simulate: float = 0.0):
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        sys.exit(f"[!] Device VID:{vid:04X} PID:{pid:04X} not found.")

    capture    = AudioCapture()
    keyboard   = KbController()
    mouse      = MouseController()

    # The indicator owns the main thread's event loop from here on; every
    # hardware and worker-thread callback talks to it through this queue.
    ui         = create_overlay()
    events: "queue.Queue[tuple]" = queue.Queue()
    stop       = threading.Event()
    session    = {"id": 0}

    last_bits: set = set()
    recording  = False

    # F4 is hold-to-repeat backspace; f4_held mirrors "is the bit currently on".
    f4_held = threading.Event()

    def _backspace() -> None:
        keyboard.press(Key.backspace)
        keyboard.release(Key.backspace)

    def type_text(text: str) -> None:
        if dry_run:
            print(f"[DRY] would type: {text!r}")
        else:
            keyboard.type(text)

    # -- worker: transcribe --------------------------------------------------
    def recognize(sid: int, audio: np.ndarray) -> None:
        print("[ASR] Recognizing …")
        try:
            text = client.transcribe(audio)
        except Exception as exc:  # noqa: BLE001
            print(f"[ASR] failed: {type(exc).__name__}: {exc}")
            events.put(("empty", sid))
            return
        if text:
            print(f"[ASR] → {text!r}")
            type_text(text)
            events.put(("done", sid))
        else:
            print("[ASR] (no speech detected)")
            events.put(("empty", sid))

    # -- HID callback (pywinusb reader thread) -------------------------------
    def on_raw(data):
        nonlocal last_bits, recording
        if not data:
            return
        payload = bytes(data)
        # pywinusb's processing thread runs this with no exception guard of
        # its own (core.py -> InputReportProcessingThread.run), so one bad
        # report would otherwise kill the only thread delivering key events
        # and leave every button dead until restart. Log and stay alive.
        try:
            current  = active_bits(payload)
            pressed  = current - last_bits
            released = last_bits - current
            last_bits = current

            if NEW_KEY in pressed:
                keyboard.press(Key.esc)
                keyboard.release(Key.esc)
                print("[KEY] Esc (NEW)")

            if F1_KEY in pressed:
                keyboard.press(Key.ctrl)
                keyboard.press("c")
                keyboard.release("c")
                keyboard.release(Key.ctrl)
                print("[KEY] Ctrl+C (F1)")

            if F4_KEY in pressed:
                f4_held.set()
                _backspace()
                print("[KEY] Backspace (F4, hold to repeat)")

            if F4_KEY in released:
                f4_held.clear()

            if F2_KEY in pressed:
                keyboard.type("continue")
                print("[KEY] Type 'continue' (F2)")

            if F3_KEY in pressed:
                keyboard.press(Key.ctrl)
                keyboard.press(Key.enter)
                keyboard.release(Key.enter)
                keyboard.release(Key.ctrl)
                print("[KEY] Ctrl+Enter (F3)")

            if REW_KEY in pressed:
                keyboard.press(Key.enter)
                keyboard.release(Key.enter)
                print("[KEY] Enter (REW)")

            if FF_KEY in pressed:
                mouse.scroll(0, -4)
                print("[MOUSE] ScrollDown (FF)")

            if INSERT_OVER in pressed:
                mouse.scroll(0, 4)
                print("[MOUSE] ScrollUp (INSERT_OVER)")

            if FAST_BACKWARD in pressed and not recording:
                recording = True
                session["id"] += 1
                events.put(("press", session["id"]))

            if FAST_BACKWARD in released and recording:
                recording = False
                events.put(("release", session["id"]))
        except Exception as exc:  # noqa: BLE001
            print(f"[HID] ! handler error on {payload.hex()}: "
                  f"{type(exc).__name__}: {exc}")

    # -- main thread: drain events, animate ----------------------------------
    def pump() -> None:
        try:
            while True:
                kind, sid = events.get_nowait()
                if kind == "quit":
                    stop.set()
                    break
                stale = sid != session["id"]
                if kind == "press":
                    ui.show_listening()
                    try:
                        capture.start()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[REC] cannot open microphone: {exc}")
                        events.put(("empty", sid))
                elif kind == "release" and not stale:
                    ui.show_thinking()
                    audio = capture.stop()
                    if len(audio) < SAMPLE_RATE * 0.15:
                        print("[ASR] (too short, skipped)")
                        events.put(("empty", sid))
                    else:
                        threading.Thread(
                            target=recognize, args=(sid, audio), daemon=True
                        ).start()
                elif kind == "done" and not stale:
                    ui.show_done()
                elif kind == "empty" and not stale:
                    ui.show_empty()
        except queue.Empty:
            pass

        ui.tick(capture.level)

        if stop.is_set():
            ui.shutdown()
            return
        ui.schedule(pump, TICK_MS)

    # -- Ctrl+C: tkinter's own loop eats it, so watch a flag instead ---------
    def _on_sigint(signum, frame):  # noqa: ARG001
        stop.set()

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:  # noqa: BLE001
        pass

    opened = []
    try:
        for dev in devices:
            dev.open()
            dev.set_raw_data_handler(on_raw)
            opened.append(dev)
            print(f"[+] Opened: {dev.vendor_name} / {dev.product_name}")

        print("\n[*] Ready — hold FAST_BACKWARD to dictate. Ctrl+C to quit.\n")

        threading.Thread(
            target=_repeat_backspace, args=(f4_held, stop, _backspace), daemon=True
        ).start()

        if simulate > 0:
            # Debug aid: feed a synthetic HID report through the real handler so
            # the whole press → record → ASR → overlay path can be exercised
            # without touching the hardware.
            def _sim():
                time.sleep(1.5)
                print("[SIM] press FAST_BACKWARD")
                on_raw(b"\x00\x00\x08")
                time.sleep(simulate)
                print("[SIM] release FAST_BACKWARD")
                on_raw(b"\x00\x00\x00")

            threading.Thread(target=_sim, daemon=True).start()

        if ui.root is not None:
            ui.schedule(pump, TICK_MS)
            ui.root.mainloop()
        else:  # windowless fallback: drive the same pump manually
            while not stop.is_set():
                pump()
                time.sleep(TICK_MS / 1000.0)

    except KeyboardInterrupt:
        print("\n[*] Exiting.")
    finally:
        stop.set()
        if recording:
            capture.stop()
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass
        ui.shutdown()


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    default_model = str(
        Path.home()
        / ".local"
        / "share"
        / "picc"
        / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    )
    parser = argparse.ArgumentParser(description="OLYMPUS DR dictation tool")
    parser.add_argument(
        "--model-dir",
        default=default_model,
        help=f"Path to SenseVoice model directory used for the offline fallback (default: {default_model})",
    )
    parser.add_argument(
        "--asr-url",
        default="http://127.0.0.1:8799/asr",
        help="ASR gateway endpoint (default: http://127.0.0.1:8799/asr)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Skip the gateway and always run SenseVoice in-process",
    )
    parser.add_argument(
        "--no-gateway-autostart",
        dest="gateway_autostart",
        action="store_false",
        help="Do not start the ASR gateway if it is not already running",
    )
    parser.add_argument(
        "--asr-timeout",
        type=float,
        default=60.0,
        help="Gateway request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the transcript instead of typing it into the focused window",
    )
    parser.add_argument(
        "--simulate",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Debug: synthesise one press/release cycle of this record length",
    )
    args = parser.parse_args()

    if not args.local and args.gateway_autostart:
        ensure_gateway(args.asr_url)

    client = AsrClient(
        model_dir=args.model_dir,
        asr_url=None if args.local else args.asr_url,
        timeout=args.asr_timeout,
        shared_engine=gateway_engine(),
    )
    try:
        run(client, TARGET_VID, TARGET_PID, dry_run=args.dry_run, simulate=args.simulate)
    finally:
        shutdown_gateway()
