"""Render the overlay at fixed levels / states and grab a crop of each."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shot
from overlay import create_overlay, WIDTH, HEIGHT

ov = create_overlay()
gx, gy = (int(v) for v in ov.root.geometry().split("+")[1:3])
crop = (gx - 16, gy - 16, WIDTH + 32, HEIGHT + 32)
print("geom", ov.root.geometry(), "crop", crop)

ov.show_listening()
for _ in range(10):
    ov.tick(0.0)
    ov.root.update()
    time.sleep(0.02)

for name, lvl in (("lvl25", 0.25), ("lvl50", 0.50), ("lvl85", 0.85)):
    for _ in range(40):
        ov.tick(lvl)
        ov.root.update()
        time.sleep(0.014)
    shot.grab(f"logs/{name}.bmp", crop)

ov.show_thinking()
for _ in range(40):
    ov.tick(0.0)
    ov.root.update()
    time.sleep(0.014)
shot.grab("logs/think.bmp", crop)

ov.show_done()
for _ in range(10):
    ov.tick(0.0)
    ov.root.update()
    time.sleep(0.014)
shot.grab("logs/done.bmp", crop)

ov.shutdown()
print("ok")
