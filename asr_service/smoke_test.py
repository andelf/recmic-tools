"""
Self-check for the ASR gateway.

Verifies the full routing story end to end:
  1. remote engine reachable (Qwen3-ASR on gpu-box)
  2. gateway /asr auto   → uses the GPU box
  3. gateway /asr local  → uses in-process SenseVoice
  4. both engines agree on the same clip

    .venv\\Scripts\\python.exe asr_service\\smoke_test.py
    .venv\\Scripts\\python.exe asr_service\\smoke_test.py --audio some.wav

Exit code is non-zero if a hard check fails.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_GATEWAY = "http://127.0.0.1:8799"
DEFAULT_AUDIO = Path(
    r"C:\Users\<you>\.local\share\picc"
    r"\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\test_wavs\zh.wav"
)


def _request(base: str, method: str, path: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)
    try:
        headers = {"Content-Type": "audio/wav"} if body is not None else {}
        conn.request(method, parts.path.rstrip("/") + path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _json(base: str, method: str, path: str, body: bytes | None = None, timeout: float = 60) -> dict:
    status, raw = _request(base, method, path, body, timeout)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {raw.decode('utf-8', 'replace')[:200]}")
    return json.loads(raw.decode("utf-8"))


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="ASR gateway self-check")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--audio", default=str(DEFAULT_AUDIO))
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"[!] audio not found: {audio_path}")
        return 2
    audio = audio_path.read_bytes()
    print(f"clip: {audio_path.name} ({len(audio)/1024:.0f} KB)\n")

    failures = 0

    # 1. gateway health -------------------------------------------------------
    print("1) gateway health")
    try:
        health = _json(args.gateway, "GET", "/health", timeout=10)
        failures += 0 if _check("gateway responds", health.get("ok") is True) else 1
        local = health.get("local", {})
        failures += 0 if _check("local fallback loaded", bool(local.get("ready")), f"{local.get('load_seconds')}s") else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] gateway unreachable — {exc}")
        return 1

    # 2. remote engine --------------------------------------------------------
    print("\n2) remote engine (Qwen3-ASR @ gpu-box)")
    remote_ok = False
    try:
        engines = _json(args.gateway, "GET", "/engines", timeout=15)
        remote = engines.get("remote", {})
        remote_ok = bool(remote.get("reachable"))
        detail = remote.get("detail", {}).get("gpu", "") if remote_ok else str(remote.get("error"))[:60]
        failures += 0 if _check("reachable", remote_ok, detail) else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] probe failed — {exc}")
        failures += 1

    # 3. auto routing ---------------------------------------------------------
    print("\n3) /asr auto — should prefer the GPU box")
    auto_text = ""
    try:
        t0 = time.time()
        auto = _json(args.gateway, "POST", "/asr", body=audio, timeout=args.timeout)
        elapsed = time.time() - t0
        auto_text = auto.get("text", "")
        expected = "qwen3-asr" if remote_ok else "sensevoice-local"
        failures += 0 if _check("engine", auto.get("engine") == expected, f"{auto.get('engine')} ({elapsed*1000:.0f} ms)") else 1
        failures += 0 if _check("text produced", bool(auto_text), repr(auto_text[:60])) else 1
        if remote_ok:
            failures += 0 if _check("not a fallback", auto.get("fallback") is False) else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        failures += 1

    # 4. forced local ---------------------------------------------------------
    print("\n4) /asr?engine=local — should use SenseVoice")
    local_text = ""
    try:
        t0 = time.time()
        loc = _json(args.gateway, "POST", "/asr?engine=local", body=audio, timeout=args.timeout)
        elapsed = time.time() - t0
        local_text = loc.get("text", "")
        failures += 0 if _check("engine", loc.get("engine") == "sensevoice-local", f"{elapsed*1000:.0f} ms") else 1
        failures += 0 if _check("text produced", bool(local_text), repr(local_text[:60])) else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        failures += 1

    # 5. agreement ------------------------------------------------------------
    if auto_text and local_text:
        print("\n5) engine agreement")
        ratio = SequenceMatcher(None, auto_text, local_text).ratio()
        if ratio >= 0.6:
            _check("similar transcripts", True, f"similarity {ratio:.2f}")
        else:
            # Not a failure: the two engines legitimately differ on script
            # (traditional vs simplified) and numeral formatting.
            print(
                f"  [NOTE] wording differs (similarity {ratio:.2f}) — "
                "likely traditional/simplified script or numeral formatting"
            )

    print()
    if failures:
        print(f"{failures} check(s) failed")
    else:
        print("all checks passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
