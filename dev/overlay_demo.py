"""Visual smoke test for overlay.py — no microphone or HID device needed."""

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overlay import create_overlay

TICK_MS = 16
T_LISTEN = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
T_THINK = T_LISTEN + 2.5
T_DONE = T_THINK + 2.0

ov = create_overlay()
ov.show_listening()
t0 = time.perf_counter()
state = "listening"


def pump():
    global state
    t = time.perf_counter() - t0
    # synthetic speech envelope: ~0.3 s syllables inside 1.2 s phrases
    syl = max(0.0, math.sin(t * 9.0))
    gate = 1.0 if (t % 2.4) < 1.5 else 0.06
    level = min(1.0, syl * gate * (0.75 + 0.25 * math.sin(t * 3.1)))

    if t > T_DONE:
        ov.shutdown()
        return
    if t > T_THINK and state != "done":
        ov.show_done()
        state = "done"
    elif t > T_LISTEN and state == "listening":
        ov.show_thinking()
        state = "thinking"

    ov.tick(level)
    ov.schedule(pump, TICK_MS)


ov.schedule(pump, TICK_MS)
ov.root.mainloop()
print("[demo] done")
