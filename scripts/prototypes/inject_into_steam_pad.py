#!/usr/bin/env python3
"""Prototype (route A): press buttons on the user's own controller slot by
writing events into Steam's existing virtual gamepad.

Steam creates one virtual Xbox pad per connected controller (USB IDs
28de:11ff, "Microsoft X-Box 360 pad N"). Games read that pad, not the
physical controller, so events written into its /dev/input/eventN node look
exactly like the user pressing the button. Steam's udev rules give the
logged-in user write access — no root.

Usage: inject_into_steam_pad.py <button|dpad-direction> [...]
  buttons: a b x y lb rb back start guide ls rs
  d-pad:   up down left right
See docs/game-input.md.
"""

import glob
import os
import struct
import sys
import time

FMT = "llHHi"
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BUTTONS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134, "lb": 0x136, "rb": 0x137,
    "back": 0x13A, "start": 0x13B, "guide": 0x13C, "ls": 0x13D, "rs": 0x13E,
}
DPAD = {"up": (0x11, -1), "down": (0x11, 1), "left": (0x10, -1), "right": (0x10, 1)}
TAP_SECONDS = 0.1


def steam_pads() -> list[str]:
    """Event nodes of Steam's virtual gamepads, player 1 first."""
    found = []
    for sysdir in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(f"{sysdir}/device/id/vendor") as f:
                vendor = f.read().strip()
            with open(f"{sysdir}/device/id/product") as f:
                product = f.read().strip()
            with open(f"{sysdir}/device/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if (vendor, product) == ("28de", "11ff"):
            found.append((name, "/dev/input/" + os.path.basename(sysdir)))
    return [node for _, node in sorted(found)]


def send(fd: int, etype: int, code: int, value: int) -> None:
    os.write(fd, struct.pack(FMT, 0, 0, etype, code, value))
    os.write(fd, struct.pack(FMT, 0, 0, EV_SYN, 0, 0))


def tap(fd: int, name: str) -> None:
    if name in BUTTONS:
        send(fd, EV_KEY, BUTTONS[name], 1)
        time.sleep(TAP_SECONDS)
        send(fd, EV_KEY, BUTTONS[name], 0)
    elif name in DPAD:
        axis, value = DPAD[name]
        send(fd, EV_ABS, axis, value)
        time.sleep(TAP_SECONDS)
        send(fd, EV_ABS, axis, 0)
    else:
        raise SystemExit(f"unknown input {name!r}")


def main() -> None:
    pads = steam_pads()
    if not pads:
        raise SystemExit("no Steam virtual gamepad found — is a controller connected and Steam running?")
    fd = os.open(pads[0], os.O_WRONLY)
    try:
        for name in sys.argv[1:]:
            tap(fd, name.lower())
            time.sleep(0.2)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
