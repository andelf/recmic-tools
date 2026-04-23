# OLYMPUS DR Series HID Tools

This repository contains reverse-engineering notes and executable tools for the OLYMPUS DR Series USB/HID device on Windows.

## Core Scripts

These are the main entry points worth keeping at the repository root.

- `dictation.py`
  - Dictation workflow using sherpa-onnx SenseVoice.
  - Hold `FAST_BACKWARD` to record, release to recognize and type text.
- `read_media_keys.py`
  - Real-time HID reader and bitmask parser.
  - Contains the current confirmed key mapping table.
- `keys_monitor.py`
  - Human-readable key/state monitor for quick validation.
- `print_current_state.py`
  - Dumps current HID raw reports and state snapshots.

## Analysis Scripts

Analysis and reverse-engineering helpers are kept under `analysis/`.

Typical uses:

- Parse USBPcap captures
- Diff `INSERT_OVER` state transitions
- Inspect candidate LED control traffic
- Summarize follow-up logs

## Captures

The `.pcap` files in the repository root are the original USBPcap captures used during investigation.

Current captures include:

- `usb-1.3.2.pcap`
- `usb13.pcap`
- `usb233。.pcap`

## Current Findings

- Device: `VID:PID = 07B4:0256`
- Input state is reported through HID interrupt IN reports.
- `INSERT_OVER` transitions correlate with host-side HID `SET_REPORT(Output)` control transfers on `EP0`.
- Candidate LED-related writes use:
  - `bmRequestType=0x21`
  - `bRequest=0x09`
  - `wValue=0x0200`
  - `wIndex=0x0000`
  - `wLength=64`
- Direct replay of candidate 64-byte output reports has not yet reproduced LED activation, so some missing context likely still exists.

## Environment

Tested on Windows with Python 3.12 and a local virtual environment.

Common packages used in this repository:

- `pywinusb`
- `sherpa-onnx`
- `sounddevice`
- `numpy`
- `pynput`

## Quick Start

Read HID state:

```powershell
python .\read_media_keys.py
```

Monitor readable key transitions:

```powershell
python .\keys_monitor.py
```

Run dictation:

```powershell
python .\dictation.py
```

## Repository Notes

- Temporary scratch scripts are not part of the main workflow.
- Generated logs and cache files are ignored by git.
- The repository currently focuses on reliable monitoring and protocol analysis; LED replay remains unresolved.
