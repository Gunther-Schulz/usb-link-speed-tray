"""Tray app showing USB storage link speed. Uses AppIndicator3 (SNI) + GTK like rclone-bisync-manager."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from pathlib import Path

# AppIndicator3 (SNI) + GTK; same stack as rclone-bisync-manager.
APPINDICATOR_AVAILABLE = False
try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3, GLib, Gtk

    APPINDICATOR_AVAILABLE = True
except (ImportError, ValueError):
    Gtk = GLib = AppIndicator3 = None  # type: ignore[misc, assignment]

# In-code SVG for tray icon (USB flash drive: connector, body, spoked indicator).
_TRAY_ICON_SVG = '''
<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
  <!-- USB-A connector -->
  <rect x="6.5" y="0.5" width="11" height="5" rx="0.6" fill="none" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
  <circle cx="9" cy="2.5" r="0.55" fill="{color}"/>
  <circle cx="15" cy="2.5" r="0.55" fill="{color}"/>
  <!-- body (flat top, semicircle bottom) -->
  <path d="M 5.5,5 L 18.5,5 L 18.5,15 A 6.5,6.5 0 0 1 5.5,15 Z" fill="none" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round" stroke-linejoin="round"/>
  <!-- indicator circle with spoked center -->
  <circle cx="12" cy="13" r="2.2" fill="none" stroke="{color}" stroke-width="{thickness}"/>
  <line x1="12" y1="11.2" x2="12" y2="14.8" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
  <line x1="9.8" y1="13" x2="14.2" y2="13" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
  <line x1="10.5" y1="11.5" x2="13.5" y2="14.5" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
  <line x1="13.5" y1="11.5" x2="10.5" y2="14.5" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
</svg>
'''
_TRAY_ICON_COLOR = "#5c6bc0"
_TRAY_ICON_STROKE = 1
_TRAY_ICON_SIZE = 64
_APP_ID = "usb-link-speed-tray"
_TRAY_ICON_FILENAME = "usb-link-speed-tray-icon.png"

SYS_BLOCK = Path("/sys/block")
SYS_USB_DEVICES = Path("/sys/bus/usb/devices")
PROC_MOUNTINFO = Path("/proc/self/mountinfo")
REFRESH_INTERVAL_MS = 3000

logger = logging.getLogger(__name__)


def _render_tray_icon_to_path(path: str | Path) -> None:
    """Render in-code SVG to PNG at path (for tray icon). Same pattern as rclone-bisync-manager."""
    from io import BytesIO

    from cairosvg import svg2png
    from PIL import Image

    svg_code = _TRAY_ICON_SVG.format(
        color=_TRAY_ICON_COLOR,
        thickness=_TRAY_ICON_STROKE,
    )
    png_data = svg2png(
        bytestring=svg_code.strip(),
        output_width=_TRAY_ICON_SIZE,
        output_height=_TRAY_ICON_SIZE,
    )
    img = Image.open(BytesIO(png_data)).convert("RGBA")
    img.save(path, "PNG")


def _block_device_usb_path(block_name: str) -> str | None:
    """Resolve block device (e.g. sda) to USB bus path (e.g. 2-3)."""
    device_path = SYS_BLOCK / block_name / "device"
    if not device_path.exists():
        return None
    try:
        target = device_path.resolve()
        for part in target.parts:
            if re.match(r"^usb\d+$", part):
                idx = target.parts.index(part)
                if idx + 1 < len(target.parts):
                    return target.parts[idx + 1]
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


def _get_block_dev_numbers(block_name: str, *, _debug: bool = False) -> set[str]:
    """Return set of major:minor for this block device and its partitions (e.g. {'8:0', '8:1'})."""
    result: set[str] = set()
    block_dir = SYS_BLOCK / block_name
    if not block_dir.is_dir():
        return result
    dev_file = block_dir / "dev"
    if dev_file.exists():
        try:
            result.add(dev_file.read_text().strip())
        except OSError:
            pass
    prefix = block_name
    for entry in block_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(prefix) and len(name) > len(prefix) and name[len(prefix)].isdigit():
            part_dev = entry / "dev"
            if part_dev.exists():
                try:
                    result.add(part_dev.read_text().strip())
                except OSError:
                    pass
    return result


def _device_path_matches_block(device_path: str, block_name: str) -> bool:
    """True if device path (after resolve) is this block or a partition (e.g. /dev/sda1 -> sda)."""
    try:
        name = Path(device_path).resolve().name
    except (OSError, ValueError):
        return False
    if name == block_name:
        return True
    if name.startswith(block_name) and len(name) > len(block_name) and name[len(block_name)].isdigit():
        return True
    return False


def _dm_device_backed_by_block(device_path: str, block_name: str) -> bool:
    """True if device is a dm (e.g. /dev/mapper/veracrypt1 -> dm-0) backed by this block (sda/sda1)."""
    if not device_path.startswith("/dev/"):
        return False
    try:
        name = Path(device_path).resolve().name
    except (OSError, ValueError):
        return False
    if not name.startswith("dm-"):
        return False
    slaves_dir = SYS_BLOCK / name / "slaves"
    if not slaves_dir.is_dir():
        return False
    for slave in slaves_dir.iterdir():
        slave_name = slave.name
        if slave_name == block_name:
            return True
        if slave_name.startswith(block_name) and len(slave_name) > len(block_name) and slave_name[len(block_name)].isdigit():
            return True
    return False


def _parse_mountinfo_line(line: str):
    """Parse one mountinfo line. Returns (major_minor, mount_point, device_path) or None."""
    parts = line.split()
    if len(parts) < 10:
        return None
    major_minor = parts[2]
    mount_point = parts[4].replace("\\040", " ")
    try:
        hyphen_idx = parts.index("-")
        device_path = parts[hyphen_idx + 2] if hyphen_idx + 2 < len(parts) else ""
    except ValueError:
        device_path = ""
    return (major_minor, mount_point, device_path)


def get_mount_points(block_name: str, *, _debug: bool = False) -> list[str]:
    """Return mount points for this block device from /proc/self/mountinfo.
    Matches by major:minor (8:0, 8:1), by device path (/dev/sda, /dev/sda1), or by dm device backed by this block (e.g. VeraCrypt)."""
    dev_numbers = _get_block_dev_numbers(block_name, _debug=_debug)
    if _debug:
        logger.debug("get_mount_points: %s dev_numbers %s", block_name, dev_numbers)
    result: list[str] = []
    if not PROC_MOUNTINFO.exists():
        return result
    try:
        for line in PROC_MOUNTINFO.read_text().splitlines():
            parsed = _parse_mountinfo_line(line)
            if parsed is None:
                continue
            major_minor, mount_point, device_path = parsed
            matched = (
                major_minor in dev_numbers
                or (device_path.startswith("/dev/") and _device_path_matches_block(device_path, block_name))
                or (device_path.startswith("/dev/") and _dm_device_backed_by_block(device_path, block_name))
            )
            if matched:
                result.append(mount_point)
    except (OSError, ValueError):
        pass
    if _debug:
        logger.debug("get_mount_points: %s -> %s", block_name, result)
    return result


def get_usb_storage_speeds() -> list[tuple[str, int | None]]:
    """Return list of (block_name, speed_mbps) for USB block devices."""
    result: list[tuple[str, int | None]] = []
    if not SYS_BLOCK.exists():
        logger.debug("sys block path does not exist: %s", SYS_BLOCK)
        return result
    for entry in SYS_BLOCK.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        usb_path = _block_device_usb_path(name)
        if usb_path is None:
            logger.debug("block %s is not USB, skipping", name)
            continue
        speed = _read_speed_mbps(usb_path)
        logger.debug("USB block %s path %s speed %s Mbps", name, usb_path, speed)
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


# Menu spec keys (same pattern as rclone-bisync-manager _build_gtk_menu).
_SPEC_TYPE = "type"
_SPEC_LABEL = "label"
_SPEC_ENABLED = "enabled"
_SPEC_CALLBACK = "callback"
_SPEC_ITEM = "item"
_SPEC_SEPARATOR = "separator"


def _build_gtk_menu(spec: list[dict]) -> Gtk.Menu | None:
    """Build Gtk.Menu from menu spec list (AppIndicator backend)."""
    if not APPINDICATOR_AVAILABLE:
        return None
    menu = Gtk.Menu()
    for s in spec or []:
        if not isinstance(s, dict):
            continue
        if s.get(_SPEC_TYPE) == _SPEC_SEPARATOR:
            menu.append(Gtk.SeparatorMenuItem())
        elif s.get(_SPEC_TYPE) == _SPEC_ITEM:
            label = s.get(_SPEC_LABEL, "")
            item = Gtk.MenuItem.new_with_label(str(label))
            item.set_sensitive(s.get(_SPEC_ENABLED, True))
            if s.get(_SPEC_CALLBACK):
                cb = s[_SPEC_CALLBACK]
                item.connect("activate", lambda w, c=cb: c(w) if c else None)
            menu.append(item)
    menu.show_all()
    return menu


def _menu_state(devices: list[tuple[str, int | None]], *, debug: bool = False) -> tuple[tuple[str, int | None, tuple[str, ...]], ...]:
    """Return comparable state (devices + speeds + mount points) for change detection."""
    rows: list[tuple[str, int | None, tuple[str, ...]]] = []
    for block_name, speed in devices:
        mounts = tuple(sorted(get_mount_points(block_name, _debug=debug)))
        rows.append((block_name, speed, mounts))
    return tuple(rows)


def _get_menu_spec(devices: list[tuple[str, int | None]], *, debug: bool = False) -> list[dict]:
    """Build menu spec: device rows, separator, Quit."""
    spec: list[dict] = []
    if not devices:
        spec.append({_SPEC_TYPE: _SPEC_ITEM, _SPEC_LABEL: "No USB storage", _SPEC_ENABLED: False})
    else:
        for block_name, speed in devices:
            label = f"{block_name}: {format_speed(speed)}"
            mounts = get_mount_points(block_name, _debug=debug)
            if mounts:
                label += " — " + ", ".join(mounts)
            spec.append({
                _SPEC_TYPE: _SPEC_ITEM,
                _SPEC_LABEL: label,
                _SPEC_ENABLED: False,
            })
    spec.append({_SPEC_TYPE: _SPEC_SEPARATOR})
    spec.append({_SPEC_TYPE: _SPEC_ITEM, _SPEC_LABEL: "Quit", _SPEC_ENABLED: True, _SPEC_CALLBACK: _exit_tray})
    return spec


def _exit_tray(widget: Gtk.Widget | None = None) -> None:
    Gtk.main_quit()
    sys.exit(0)


def run() -> None:
    """Run the tray application (AppIndicator3 + GTK)."""
    parser = argparse.ArgumentParser(prog="usb-link-speed-tray")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to console")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
        logger.debug("debug logging enabled")
    if not APPINDICATOR_AVAILABLE:
        print(
            "Tray requires AppIndicator3 + GTK3. On Arch: pacman -S libappindicator-gtk3 gtk3",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.debug:
        logger.debug("tray app starting, refresh interval %s ms", REFRESH_INTERVAL_MS)

    # Runtime SVG → PNG in temp (same pattern as rclone-bisync-manager).
    icon_path = Path(tempfile.gettempdir()) / _TRAY_ICON_FILENAME
    _render_tray_icon_to_path(icon_path)
    if args.debug:
        logger.debug("tray icon rendered to %s", icon_path)

    indicator = AppIndicator3.Indicator.new(
        _APP_ID,
        str(icon_path),
        AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    _last_menu_state: list[tuple[tuple[str, int | None, tuple[str, ...]], ...] | None] = [None]

    def update_menu() -> bool:
        devices = get_usb_storage_speeds()
        state = _menu_state(devices, debug=args.debug)
        if state == _last_menu_state[0]:
            return True  # no change, skip set_menu to avoid flicker
        _last_menu_state[0] = state
        if args.debug:
            logger.debug("update_menu: %s device(s), menu refreshed", len(devices))
        spec = _get_menu_spec(devices, debug=args.debug)
        menu = _build_gtk_menu(spec)
        if menu is not None:
            indicator.set_menu(menu)
        return True  # keep timer

    # Initial menu (same path as timer, so we only have one place that scans + builds)
    update_menu()
    GLib.timeout_add(REFRESH_INTERVAL_MS, update_menu)

    def quit_on_signal(*args: object) -> None:
        Gtk.main_quit()

    import signal as sig

    sig.signal(sig.SIGINT, quit_on_signal)
    sig.signal(sig.SIGTERM, quit_on_signal)

    if args.debug:
        logger.debug("tray shown, running event loop")
    Gtk.main()


if __name__ == "__main__":
    run()
