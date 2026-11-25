# Raspberry Pi Zero 2 W Digital Photo Frame

A lightweight, self-contained fullscreen digital photo frame built for the **Raspberry Pi Zero 2 W**. It renders photos directly over the Pi's HDMI video output using SDL2/KMSDRM (no heavy desktop environment or browser needed) and includes a mobile-friendly web interface for uploading, managing, and configuring your slideshow over WiFi.

## Features

- **Fullscreen Direct-to-Display Rendering**: Runs smoothly on Raspberry Pi OS Lite (~40 MB RAM) using Pygame/SDL2 over KMSDRM.
- **Web Configuration UI** (`http://photoframe.local:5000`):
  - **Photo Duration**: Set how many seconds each photo is shown.
  - **Playback Order**: Cycle (Alphabetical), Cycle (Newest First), or Random (Shuffle without immediate repeats).
  - **Transitions**: `Fade`, `None`, `Slide Left`, `Slide Right`, `Slide Up`, `Slide Down`, `Zoom Fade`, or `Random`, with adjustable transition speed.
  - **Photo Scaling**:
    - *Fit with Blurred Background* (fills widescreen displays gracefully for portrait phone photos)
    - *Fit Inside Screen* (letterbox/pillarbox)
    - *Fill Screen* (center crop)
  - **Screen Rotation**: `0°`, `90°`, `180°`, or `270°` for landscape or portrait frames.
  - **Brightness Dimmer**: Software dimming from 10% to 100%.
  - **Overlays**: Optional Clock & Date (12h/24h) and Photo Date/Caption.
  - **Sleep Schedule (Night Mode)**: Automatically blanks the screen (and powers off HDMI where supported) during quiet hours.
  - **Live Remote Control**: Pause/Resume, Next, Previous, or jump directly to any photo.
- **Photo Management via Web UI**:
  - Drag-and-drop or mobile multi-file upload (`JPG`, `PNG`, `WEBP`, `BMP`, `GIF`).
  - Automatic EXIF orientation handling (smartphone photos won't appear sideways).
  - Cached thumbnails for fast gallery browsing.
  - Rotate photos 90° clockwise, delete single photos, or bulk-delete selected photos.
  - Free SD card storage indicator.

---

## Step-by-Step Setup on Raspberry Pi Zero 2 W

### 1. Flash Raspberry Pi OS Lite with WiFi Enabled

1. Download and open **Raspberry Pi Imager** on your computer.
2. Choose Device: **Raspberry Pi Zero 2 W**.
3. Choose OS: **Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)** (or 32-bit Lite).
   - *Lite is strongly recommended because the Pi Zero 2 W has 512 MB RAM and no desktop is needed.*
4. Choose your microSD card, click **Next**, and click **Edit Settings** (OS Customization):
   - **Hostname**: `photoframe` (so you can reach it at `http://photoframe.local:5000`)
   - **Username & Password**: e.g., `pi` and a password you remember
   - **Configure wireless LAN**: Enter your WiFi SSID, password, and country
   - **Services tab**: Enable **SSH** (Use password authentication)
5. Write the image to the microSD card.

### 2. Boot the Pi & Connect via SSH

1. Insert the microSD card into your Raspberry Pi Zero 2 W, connect the mini-HDMI cable to your display, and plug in power.
2. Wait ~60–90 seconds for the first boot to finish and connect to WiFi.
3. From your computer's terminal, SSH into the Pi:
   ```bash
   ssh pi@photoframe.local
   ```
   *(If `photoframe.local` doesn't resolve, check your router's DHCP list for the Pi's IP address and run `ssh pi@<PI_IP>`.)*

### 3. Copy the Project Files to the Pi

From your computer (in the directory containing this project), copy the folder to the Pi:
```bash
scp -r digital-photo-frame pi@photoframe.local:~/
```

### 4. Run the Automated Setup Script

On the Raspberry Pi (via SSH):
```bash
cd ~/digital-photo-frame
chmod +x setup.sh photoframe.py
./setup.sh
```

This script will:
- Install `python3`, `pygame`, `Pillow`, `Flask`, and SDL2 libraries via `apt` (using prebuilt ARM wheels/packages so installation is fast on a Pi Zero 2 W).
- Add your user to the `video`, `render`, and `input` groups so Pygame can render directly to HDMI without X11.
- Create a Python virtual environment.
- Install and enable the `photoframe.service` systemd unit so the frame starts automatically on every boot.

### 5. Reboot & Upload Photos

```bash
sudo reboot
```

After reboot, the display will show a welcome screen with the exact URL to open, e.g.:
- `http://photoframe.local:5000`
- or `http://<PI_IP_ADDRESS>:5000`

Open that URL on your phone or computer (connected to the same WiFi network), upload some photos, and tweak your slideshow settings!

---

## Useful Commands

- **Check service status**:
  ```bash
  sudo systemctl status photoframe.service
  ```
- **View live logs**:
  ```bash
  journalctl -u photoframe.service -f
  ```
- **Restart the photo frame**:
  ```bash
  sudo systemctl restart photoframe.service
  ```
- **Test in a window on a desktop machine**:
  ```bash
  python3 photoframe.py --windowed
  ```
- **Run web UI only (no display)**:
  ```bash
  python3 photoframe.py --headless
  ```

## Troubleshooting

- **Black screen or `No available video device` error**:
  - Make sure you rebooted after running `setup.sh` so the `video`/`render` group permissions take effect.
  - Ensure your HDMI cable is plugged in **before** powering on the Pi Zero 2 W (or add `hdmi_force_hotplug=1` to `/boot/firmware/config.txt`).
- **Running on Raspberry Pi OS Desktop (X11/Wayland) instead of Lite**:
  - Edit `/etc/systemd/system/photoframe.service` and replace `Environment=SDL_VIDEODRIVER=kmsdrm` with `Environment=DISPLAY=:0` (for X11), then run `sudo systemctl daemon-reload && sudo systemctl restart photoframe.service`.
- **Console cursor blinking behind images**:
  - On Raspberry Pi OS Lite, you can hide the boot console cursor cleanly by appending `vt.global_cursor_default=0` to `/boot/firmware/cmdline.txt`.
