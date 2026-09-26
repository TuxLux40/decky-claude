#!/usr/bin/env python3
"""Prototype: a virtual Xbox 360 controller via /dev/uinput, stdlib only.

No root needed: Steam's own udev rule (60-steam-input.rules) grants the
logged-in user access to /dev/uinput. Steam Input adopts the pad like a real
one and applies the running game's controller config.

Usage: virtual_pad.py [command-file]
Creates the pad, then polls command-file (default /tmp/vpad-cmd) for one of
a b x y lb rb back start guide ls rs up down left right quit, and taps it.
See docs/game-input.md for what was verified and the known quirks.
"""

import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3

BUTTONS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137,
    "back": 0x13A, "start": 0x13B, "guide": 0x13C, "ls": 0x13D, "rs": 0x13E,
}
# ABS_HAT0X / ABS_HAT0Y: -1 = left/up, +1 = right/down (evdev convention).
DPAD = {"up": (0x11, -1), "down": (0x11, 1), "left": (0x10, -1), "right": (0x10, 1)}
AXES = {
    0x00: (-32768, 32767), 0x01: (-32768, 32767),  # left stick
    0x03: (-32768, 32767), 0x04: (-32768, 32767),  # right stick
    0x02: (0, 255), 0x05: (0, 255),                # triggers
    0x10: (-1, 1), 0x11: (-1, 1),                  # d-pad hat
}
# Long enough for a game polling at 30 fps; 0.5 s already triggers menu auto-repeat.
TAP_SECONDS = 0.1


def create() -> int:
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for code in BUTTONS.values():
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    absmin, absmax = [0] * 64, [0] * 64
    for code, (lo, hi) in AXES.items():
        fcntl.ioctl(fd, UI_SET_ABSBIT, code)
        absmin[code], absmax[code] = lo, hi
    # Xbox 360 vendor/product so SDL and Steam pick a known mapping.
    dev = b"Microsoft X-Box 360 pad (decky-claude)".ljust(80, b"\0")
    dev += struct.pack("<HHHHi", 0x03, 0x045E, 0x028E, 0x0110, 0)
    dev += struct.pack("<64i", *absmax) + struct.pack("<64i", *absmin)
    dev += struct.pack("<64i", *[0] * 64) + struct.pack("<64i", *[0] * 64)
    os.write(fd, dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd: int, etype: int, code: int, value: int) -> None:
    os.write(fd, struct.pack("llHHi", 0, 0, etype, code, value))
    os.write(fd, struct.pack("llHHi", 0, 0, EV_SYN, 0, 0))


def tap(fd: int, name: str) -> bool:
    if name in BUTTONS:
        emit(fd, EV_KEY, BUTTONS[name], 1)
        time.sleep(TAP_SECONDS)
        emit(fd, EV_KEY, BUTTONS[name], 0)
    elif name in DPAD:
        axis, value = DPAD[name]
        emit(fd, EV_ABS, axis, value)
        time.sleep(TAP_SECONDS)
        emit(fd, EV_ABS, axis, 0)
    else:
        return False
    return True


def main() -> None:
    cmd_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vpad-cmd"
    fd = create()
    print("virtual pad up", flush=True)
    try:
        while True:
            if os.path.exists(cmd_file):
                with open(cmd_file) as f:
                    name = f.read().strip().lower()
                os.unlink(cmd_file)
                if name == "quit":
                    break
                print(("sent " if tap(fd, name) else "unknown ") + name, flush=True)
            time.sleep(0.05)
    finally:
        fcntl.ioctl(fd, UI_DEV_DESTROY)
        os.close(fd)
        print("virtual pad removed", flush=True)


if __name__ == "__main__":
    main()
