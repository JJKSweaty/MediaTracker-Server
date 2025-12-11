# ESP32 Dashboard System

This project is a modular ESP32-based dashboard designed for media playback monitoring, process management, and future Discord integration. It features a touchscreen UI powered by LVGL (v9.4), efficient communication via UART or Wi-Fi, real-time system metrics, music playback with artwork, and queue management.

## Features

* Modular main application design
* Dark-themed LVGL GUI with tabbed layout
* Real-time FPS counter and system usage info
* Music playback display with artwork and metadata
* Interactive task manager with memory display and kill buttons
* Wi-Fi and optional BLE communication support
* Playlist queue management with drag-and-drop
* Incoming support for Discord call visuals and controls

---

## Project Structure

```
├── src/
│   ├── main.cpp         # Main entry point
│   ├── ui.cpp           # Handles LVGL UI logic
│   ├── comms.cpp        # Communication layer (UART/Wi-Fi)
│   ├── task_manager.cpp # Process management logic
│   ├── music.cpp        # Music metadata and artwork
│   └── ...              # Additional modules
├── include/
│   └── *.h              # Header files
├── assets/
│   ├── fonts/
│   └── images/          # Album covers, UI icons
├── data/
│   └── spiffs/          # SPIFFS image for filesystem assets
├── globals.xml          # LVGL global styles and bindings
└── README.md            # Project readme
```

---

## Dependencies

* LVGL 9.4 (UI rendering)
* Arduino for ESP32 or ESP-IDF (depending on setup)
* `lvgl/lvgl`, `lvgl/lv_drivers`, `lvgl/lv_examples`
* `lvgl/lv_png` for PNG support
* Async TCP/Wi-Fi libraries if using Wi-Fi

---

## Build & Flash

Ensure PlatformIO or ESP-IDF is set up.

### PlatformIO (Recommended)

```bash
pio run --target upload
```

### ESP-IDF (Manual)

```bash
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## Server Integration

The ESP32 expects real-time data over serial or Wi-Fi:

* JSON packets for:

  * Music metadata (`title`, `artist`, `album`, `artwork`)
  * Process info (`pid`, `name`, `mem`, `cpu`)
* Album artwork in JPG base64 format
* Optional: Playlist queue metadata

---

## Music Queue Features

* Drag-and-drop support for song queue
* Queue limit (default: 10 tracks)
* Visual artwork preview
* Button to open/close queue from music tab
* Synchronization with server playlist updates

---

## Known Issues

* System Idle may show high % due to inaccurate CPU metrics
* JPEG decoding may consume extra memory
* Queue drag handles could overlap on smaller screens


