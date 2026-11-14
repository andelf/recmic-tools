"""End-to-end-ish test: real microphone → overlay, no HID device required."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shot
from dictation import AudioCapture
from overlay import create_overlay, HEIGHT, WIDTH

cap = AudioCapture()
ov = create_overlay()
gx, gy = (int(v) for v in ov.root.geometry().split("+")[1:3])
crop = (gx - 16, gy - 16, WIDTH + 32, HEIGHT + 32)

TICK = 0.016
peaks = []

print("[test] speak now — recording 4 s")
cap.start()
ov.show_listening()

t0 = time.perf_counter()
shots = 0
while True:
    t = time.perf_counter() - t0
    lvl = cap.level
    peaks.append(lvl)
    ov.tick(lvl)
    ov.root.update()
    if t > 1.2 and shots < 3 and int(t * 10) % 12 == 0:
        shot.grab(f"logs/live{shots}.bmp", crop)
        shots += 1
    if t > 4.0:
        break
    time.sleep(TICK)

audio = cap.stop()
print(f"[test] captured {len(audio)/16000:.2f}s, peak level {max(peaks):.2f}, "
      f"mean {sum(peaks)/len(peaks):.2f}")

ov.show_thinking()
for _ in range(60):
    ov.tick(0)
    ov.root.update()
    time.sleep(TICK)
shot.grab("logs/live_think.bmp", crop)

ov.show_done()
for _ in range(12):
    ov.tick(0)
    ov.root.update()
    time.sleep(TICK)
shot.grab("logs/live_done.bmp", crop)

ov.shutdown()
print("[test] ok")
