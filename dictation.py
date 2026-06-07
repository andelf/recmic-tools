"""
Hands-free dictation using OLYMPUS DR Series + sherpa-onnx SenseVoice.

Workflow:
  1. Hold FAST_BACKWARD on the device  → starts audio capture
  2. Release FAST_BACKWARD             → stops capture, runs ASR, types result

Model setup:
  Download and extract to the workspace (or pass --model-dir):
    https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2

Requirements:
    pip install sherpa-onnx sounddevice numpy pynput pywinusb
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import List

import numpy as np
import sherpa_onnx
import sounddevice as sd
from pynput.keyboard import Controller as KbController, Key
from pynput.mouse import Controller as MouseController

import pywinusb.hid as hid
from read_media_keys import TARGET_VID, TARGET_PID, BITMAP_HINTS

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

# ──────────────────────────────────────────────────────────────────────────────
# Audio recording helpers
# ──────────────────────────────────────────────────────────────────────────────

class AudioCapture:
    """Accumulates float32 mono 16 kHz samples while open."""

    def __init__(self):
        self._chunks: List[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self._chunks.clear()
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
            print(f"[REC] ■ stopped  ({len(audio)/SAMPLE_RATE:.2f}s)")
            return audio

    def _cb(self, indata, frames, time_info, status):
        self._chunks.append(indata.copy())


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


def transcribe(recognizer: sherpa_onnx.OfflineRecognizer, audio: np.ndarray) -> str:
    if len(audio) == 0:
        return ""
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio)
    recognizer.decode_stream(stream)
    return stream.result.text.strip()


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


def run(model_dir: str, vid: int, pid: int):
    devices = hid.HidDeviceFilter(vendor_id=vid, product_id=pid).get_devices()
    if not devices:
        sys.exit(f"[!] Device VID:{vid:04X} PID:{pid:04X} not found.")

    recognizer = build_recognizer(model_dir)
    capture    = AudioCapture()
    keyboard   = KbController()
    mouse      = MouseController()

    last_bits: set = set()
    recording  = False

    def on_raw(data):
        nonlocal last_bits, recording
        if not data:
            return
        payload  = bytes(data)
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
            keyboard.press(Key.backspace)
            keyboard.release(Key.backspace)
            print("[KEY] Backspace (F4)")

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
            capture.start()

        if FAST_BACKWARD in released and recording:
            recording = False
            audio = capture.stop()

            def _recognize():
                print("[ASR] Recognizing …")
                text = transcribe(recognizer, audio)
                if text:
                    print(f"[ASR] → {text!r}")
                    keyboard.type(text)
                else:
                    print("[ASR] (no speech detected)")

            threading.Thread(target=_recognize, daemon=True).start()

    opened = []
    try:
        for dev in devices:
            dev.open()
            dev.set_raw_data_handler(on_raw)
            opened.append(dev)
            print(f"[+] Opened: {dev.vendor_name} / {dev.product_name}")

        print("\n[*] Ready — hold FAST_BACKWARD to dictate. Ctrl+C to quit.\n")
        while True:
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n[*] Exiting.")
    finally:
        if recording:
            capture.stop()
        for dev in opened:
            try:
                dev.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    default_model = r"C:\Users\andel\.local\share\picc\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
    parser = argparse.ArgumentParser(description="OLYMPUS DR dictation tool")
    parser.add_argument(
        "--model-dir",
        default=default_model,
        help=f"Path to SenseVoice model directory (default: {default_model})",
    )
    args = parser.parse_args()
    run(args.model_dir, TARGET_VID, TARGET_PID)
