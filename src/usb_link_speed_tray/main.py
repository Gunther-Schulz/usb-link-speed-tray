"""Tray app showing USB storage link speed."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


SYS_BLOCK = Path("/sys/block")
SYS_USB_DEVICES = Path("/sys/bus/usb/devices")
REFRESH_INTERVAL_MS = 3000


def _block_device_usb_path(block_name: str) -> str | None:
    """Resolve block device (e.g. sda) to USB bus path (e.g. 2-3)."""
    device_path = SYS_BLOCK / block_name / "device"
    if not device_path.exists():
        return None
    try:
        target = device_path.resolve()
        # .../usb2/2-3 or .../usb1/1-6 etc.
        for part in target.parts:
            if re.match(r"^usb\d+$", part):
                idx = target.parts.index(part)
                if idx + 1 < len(target.parts):
                    return target.parts[idx + 1]  # e.g. 2-3
        return None
    except (OSError, ValueError):
        return None


def _read_speed_mbps(usb_path: str) -> int | None:
    """Read link speed in Mbps from sysfs."""
    speed_file = SYS_USB_DEVICES / usb_path / "speed"
    if not speed_file.exists():
        return None
    try:
        return int(speed_file.read_text().strip())
    except (OSError, ValueError):
        return None


def get_usb_storage_speeds() -> list[tuple[str, int | None]]:
    """Return list of (block_name, speed_mbps) for USB block devices."""
    result: list[tuple[str, int | None]] = []
    if not SYS_BLOCK.exists():
        return result
    for entry in SYS_BLOCK.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        usb_path = _block_device_usb_path(name)
        if usb_path is None:
            continue
        speed = _read_speed_mbps(usb_path)
        result.append((name, speed))
    return result


def format_speed(mbps: int | None) -> str:
    """Format speed for display (e.g. 5000 -> '5 Gbps')."""
    if mbps is None:
        return "?"
    if mbps >= 10000:
        return f"{mbps // 1000} Gbps"
    if mbps >= 1000:
        return f"{mbps / 1000:.1f} Gbps"
    return f"{mbps} Mbps"


def run() -> None:
    """Run the tray application."""
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    icon = QIcon.fromTheme("drive-removable-media")
    if icon.isNull():
        icon = QIcon.fromTheme("drive-harddisk")
    if icon.isNull():
        icon = QIcon.fromTheme("drive-removable-media-usb")

    tray = QSystemTrayIcon()
    tray.setIcon(icon)
    tray.setToolTip("USB storage link speed")

    menu = QMenu()

    def update_menu() -> None:
        menu.clear()
        devices = get_usb_storage_speeds()
        if not devices:
            action = QAction("No USB storage")
            action.setEnabled(False)
            menu.addAction(action)
        else:
            for block_name, speed in devices:
                text = f"{block_name}: {format_speed(speed)}"
                action = QAction(text)
                action.setEnabled(False)
                menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("Quit")
        quit_action.triggered.connect(app.quit)
        menu.addAction(quit_action)
        # Update tooltip with first device if any
        if devices:
            name, speed = devices[0]
            tray.setToolTip(f"{name}: {format_speed(speed)}")

    update_menu()
    tray.setContextMenu(menu)

    timer = QTimer()
    timer.timeout.connect(update_menu)
    timer.start(REFRESH_INTERVAL_MS)

    tray.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
