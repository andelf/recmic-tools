# OLYMPUS DR Series HID Tools

[简体中文](README.zh-CN.md) | English

This repository contains practical tools and reverse-engineering scripts for an OLYMPUS DR Series USB/HID device on Windows.

## What Is Stable Now

Primary workflow at repository root:

- `dictation.py`
  - Main dictation runtime (`sherpa-onnx` + SenseVoice, routed through the ASR gateway).
  - Hold `FAST_BACKWARD` to start recording, release to run ASR and type text.
  - Shows a floating on-screen indicator while recording (see below).
- `overlay.py`
  - The always-on-top translucent "pill" that visualises the microphone level.
  - Standalone tkinter + ctypes; no extra dependency.
- `asr_service/`
  - HTTP ASR service. Prefers Qwen3-ASR on the GPU box (`192.0.2.175`),
    falls back to local SenseVoice when that host is unreachable.
  - See `asr_service/README.md` for the API and tuning knobs.
- `read_media_keys.py`
  - Real-time HID reader and bitmask parser.
  - Source of truth for current button-bit mapping.
- `keys_monitor.py`
  - Human-readable state transitions for key validation.
- `print_current_state.py`
  - Raw HID snapshot/debug output.
- `create_dictation_shortcut.py`
  - One-command setup helper (`.venv`, dependencies, desktop shortcut).
- `dev/`
  - Developer-only helpers, not needed to use the tool.
  - `overlay_demo.py`: drives the indicator with a synthetic envelope (no mic/HID).
  - `test_visual.py`: renders each indicator state at fixed levels and saves crops.
  - `test_live.py`: real microphone → indicator, still no HID device needed.
  - `shot.py`: tiny ctypes screen grabber (writes 24-bit BMP, no Pillow).

## Current Dictation Key Actions

Runtime actions configured in `dictation.py`:

- `FAST_BACKWARD`: hold to record, release to transcribe/type.
- `NEW`: sends `Esc`.
- `F1`: sends `Ctrl+C`.
- `F2`: types `continue`.
- `F3`: sends `Ctrl+Enter`.
- `F4`: sends `Backspace`; hold to repeat (0.4 s delay, then ~22/s).
- `REW`: sends `Enter`.
- `FF`: mouse wheel down.
- `INSERT_OVER`: mouse wheel up.

## Repository Layout

- Root directory:
  - Runtime scripts, setup script, key mapping file, and capture files (`*.pcap`).
- `asr_service/`:
  - `remote_server.py`: Qwen3-ASR HTTP service, runs on the RTX 5080 box.
  - `gateway.py`: local gateway with remote-first / local-fallback routing.
  - `smoke_test.py`: end-to-end self check.
- `analysis/`:
  - Protocol/pcap analysis scripts and replay experiments.
  - `qwen3_integration_research.md`: quick feasibility research for Qwen3/Qwen3-ASR integration.
  - `_tmp_*.py` files are scratch probes used during reverse engineering.
- `dev/`:
  - Visual harnesses for `overlay.py` and the screen-grab helper. Run them from
    anywhere: `python dev/overlay_demo.py`.

## Current Device / Protocol Findings

- Target device: `VID:PID = 07B4:0256`.
- Input state comes from HID interrupt IN reports.
- `INSERT_OVER` transitions correlate with host HID `SET_REPORT(Output)` on `EP0`.
- Observed LED-related control pattern:
  - `bmRequestType=0x21`
  - `bRequest=0x09`
  - `wValue=0x0200`
  - `wIndex=0x0000`
  - `wLength=64`
- Replaying candidate 64-byte output reports still does not reproduce LED behavior reliably; device/session context is likely missing.

## Environment

- OS: Windows
- Python: 3.12
- Recommended: local `.venv`

Dependencies are listed in `requirements.txt`.

```powershell
python -m pip install -r .\requirements.txt
```

Main packages:

- `pywinusb`
- `sherpa-onnx`
- `sounddevice`
- `numpy`
- `pynput`

## On-Screen Recording Indicator

Holding `FAST_BACKWARD` pops a translucent pill near the bottom-centre of the
monitor that hosts the foreground window — not the one under the cursor — so it
appears wherever you are actually typing, and there is always visible feedback
that dictation is armed.

| State | Appearance |
| --- | --- |
| listening | White bars bounce with the live microphone level; a faint breathing motion even in silence proves it is listening |
| transcribing | Bars turn blue and a wave travels left→right |
| done | Bars flash green, then fade out (~0.55 s) |
| no speech | Bars collapse to grey dots, then fade out (~0.40 s) |

Implementation details worth knowing before you touch it:

- The window is `overrideredirect` + always-on-top, but **not** clipped with a
  window region — regions are binary and Windows never anti-aliases them, which
  left visible stair-steps on the rounded ends. The pixels instead come from a
  32-bit premultiplied ARGB DIB pushed with `UpdateLayeredWindow`, so the rim has
  a real 8-bit alpha ramp (softened further by `EDGE_FEATHER`).
- `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` make it click-through,
  focus-free and absent from the taskbar / Alt-Tab, so it never disturbs typing.
- Topmost is re-asserted once per second, which survives most full-screen apps.
- DPI awareness is set at import time; anything created before that freezes the
  process's scaling mode and the pill comes out blurry.
- The pill is rasterised from a rounded-box signed distance field, so edge
  softness is one number: `EDGE_FEATHER` in `overlay.py` (1.0 = plain AA,
  4.0 = current soft rim). The window is padded by `PAD` px so the falloff is
  not clipped away at the window bounds.
- `AudioCapture.level` blends an absolute dBFS curve with a slow-decaying peak,
  so the bars look right on both quiet and hot microphones.

Preview it without the hardware attached:

```powershell
python .\dev\overlay_demo.py 6   # 6 s of synthetic "speech", then transcribing/done
```

Debug switches on the runtime itself:

```powershell
python .\dictation.py --dry-run              # print the transcript, never type it
python .\dictation.py --dry-run --simulate 4 # synthesise one 4 s press/release cycle
```

## Quick Start

Create environment + install deps + desktop shortcut:

```powershell
python .\create_dictation_shortcut.py
```

Monitor HID reports:

```powershell
python .\read_media_keys.py
```

Run readable key monitor:

```powershell
python .\keys_monitor.py
```

Start dictation runtime:

```powershell
python .\dictation.py
```

## ASR Service (HTTP, remote-first)

The recognition backend is now reachable over HTTP so other tools can use it too.

```powershell
.\start_asr_gateway.bat                                  # gateway on 127.0.0.1:8799
python .\asr_service\smoke_test.py                       # verify both engines
```

```bash
curl -F "file=@sample.wav" http://127.0.0.1:8799/asr
```

Routing: `POST /asr` tries Qwen3-ASR on `192.0.2.175:8790` first (RTX 5080),
and falls back to in-process SenseVoice int8 when the host is down. A circuit
breaker stops the gateway from re-paying the connect timeout on every request
while the remote is unreachable. Responses carry `engine` and `fallback` so
callers can tell which one ran.

`dictation.py --local` bypasses the gateway; with no flag it uses the gateway by
default and still falls back to a local model if the gateway itself is down.

**The gateway runs on a worker thread of `dictation.py` itself** — no child
process, no scheduled task, no registry entry, no service registration. If the
dictation app is running, its gateway is running; when it exits, the port is
released with it. Nothing survives deleting this folder.

On startup the app probes `http://127.0.0.1:8799/health`. If nothing answers it
imports `asr_service.gateway:app` and serves it with uvicorn on an `asr-gateway`
thread (about 0.5 s to first response; the SenseVoice fallback warms up in the
background afterwards). If *another* process already serves that port, it is
reused and left untouched on exit.

Two useful consequences of sharing one process:

- Because the gateway's SenseVoice instance is reachable directly, the client
  borrows it instead of loading a second copy of the weights.
- The "gateway unreachable" path becomes nearly unreachable, so a missed key
  press cannot silently degrade to CPU-only recognition without a log line.

A 16 ms UI loop keeps a 16.0 ms median frame gap while SenseVoice decodes
(worst frame 29 ms — one skipped frame), and the 1.5 s model load never blocks
it past 32 ms, because sherpa-onnx releases the GIL inside `decode_stream`.
The trade-off: an interpreter-level crash in the gateway now takes the app with
it. That is fine for a personal tool where both halves die on the same Ctrl+C.

```powershell
python .\dictation.py --no-gateway-autostart   # probe but never spawn
```

## ASR Model Setup

Download and extract SenseVoice, or point `--model-dir` at an existing copy:

```
https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models
sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
```

Covers Chinese, English, Japanese, Korean and Cantonese with automatic language
detection and inverse text normalisation (ITN).

## Notes

- Analysis scripts are intentionally separated under `analysis/`.
- `logs/` and `*.bmp` are runtime artefacts and screen captures; both are
  gitignored.
- Scratch scripts can be cleaned up later if you want a stricter production-only repository layout.

## License

[MIT](LICENSE) © 2026 Andelf.

The MIT terms cover this repository's own code. Two things sit outside it:

**Models.** ASR weights are downloaded separately and keep their upstream
licenses — SenseVoice under FunASR's terms, Qwen3-ASR under Alibaba's. Check
those before redistributing or using them commercially.

**Dependencies.** Pulled in at install time, each under its own license:
`sherpa-onnx` (Apache-2.0), `fastapi` (MIT), `python-multipart` (Apache-2.0),
`uvicorn` / `numpy` / `soundfile` (BSD-3-Clause), `sounddevice` (MIT),
`pywinusb` (BSD), `pynput` (LGPLv3 — imported at runtime, never modified).

The USB captures and protocol analysis under `analysis/` are original work
derived from observing a device you own; they are provided for interoperability
and are covered by the same MIT terms.
