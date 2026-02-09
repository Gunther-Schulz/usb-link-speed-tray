# usb-link-speed-tray

Tray app showing link speed of attached external USB storage (e.g. 480 Mbps vs 5 Gbps).

## Run

From the project root:

```bash
pip install -e .
usb-link-speed-tray
```

Or without installing (from project root):

```bash
pip install PyGObject cairosvg Pillow
PYTHONPATH=src python -m usb_link_speed_tray.main
```

**System dependencies (tray uses AppIndicator3 + GTK3):** On Arch: `pacman -S libappindicator-gtk3 gtk3`.

The app appears in the system tray; plug in USB storage to see its link speed (e.g. 480 Mbps, 5 Gbps).
