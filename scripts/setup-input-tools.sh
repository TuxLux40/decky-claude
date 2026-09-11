#!/bin/bash
# One-time setup for the input backends send_key/type_text/mouse_move_click
# rely on (xdotool primary, ydotool fallback). Run this yourself in a real
# terminal — it needs sudo, and Decky plugin backends run unprivileged, so
# this can't be done silently from inside the plugin itself.
set -e

if command -v steamos-readonly >/dev/null 2>&1; then
    echo "This looks like a real Steam Deck (SteamOS, read-only rootfs)."
    echo "Installing packages with pacman here means disabling steamos-readonly,"
    echo "which this script deliberately will not do on its own."
    echo "Check whether xdotool is already present first: which xdotool"
    echo "If it's missing, see https://wiki.archlinux.org/title/SteamOS for the"
    echo "supported way to add packages on your SteamOS version, then re-run"
    echo "just the ydotool.service step below manually."
    exit 1
fi

if command -v xdotool >/dev/null 2>&1; then
    echo "xdotool: already installed ($(xdotool --version))"
else
    echo "xdotool: not installed — installing now (will prompt for sudo)"
    sudo pacman -S --needed --noconfirm xdotool
fi

if systemctl --user is-active --quiet ydotool.service 2>/dev/null; then
    echo "ydotool.service: already running"
else
    echo "ydotool.service: enabling as the fallback input backend"
    systemctl --user enable --now ydotool.service
fi

echo
echo "Done. Verify with: xdotool key -- Escape"
