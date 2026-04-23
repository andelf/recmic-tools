# OLYMPUS DR Series HID Tools

This repository contains practical tools and reverse-engineering scripts for an OLYMPUS DR Series USB/HID device on Windows.

## What Is Stable Now

Primary workflow at repository root:

- `dictation.py`
  - Main dictation runtime (`sherpa-onnx` + SenseVoice).
  - Hold `FAST_BACKWARD` to start recording, release to run ASR and type text.
- `read_media_keys.py`
  - Real-time HID reader and bitmask parser.
  - Source of truth for current button-bit mapping.
- `keys_monitor.py`
  - Human-readable state transitions for key validation.
- `print_current_state.py`
  - Raw HID snapshot/debug output.
- `create_dictation_shortcut.py`
  - One-command setup helper (`.venv`, dependencies, desktop shortcut).

## Current Dictation Key Actions

Runtime actions configured in `dictation.py`:

- `FAST_BACKWARD`: hold to record, release to transcribe/type.
- `NEW`: sends `Esc`.
- `F1`: sends `Ctrl+C`.
- `F2`: types `continue`.
- `F4`: sends `Backspace`.
- `REW`: sends `Enter`.
- `FF`: mouse wheel down.
- `INSERT_OVER`: mouse wheel up.

## Repository Layout

- Root directory:
  - Runtime scripts, setup script, key mapping file, and capture files (`*.pcap`).
- `analysis/`:
  - Protocol/pcap analysis scripts and replay experiments.
  - `qwen3_integration_research.md`: quick feasibility research for Qwen3/Qwen3-ASR integration.
  - `_tmp_*.py` files are scratch probes used during reverse engineering.

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

## Notes

- Analysis scripts are intentionally separated under `analysis/`.
- Scratch scripts can be cleaned up later if you want a stricter production-only repository layout.
