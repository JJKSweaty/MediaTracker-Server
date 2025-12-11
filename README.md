# ESP32 Multimedia Dashboard

An advanced UI-driven ESP32 project combining real-time music playback data, process monitoring, and Discord status integration. Designed with LVGL 9.4 and built for modularity, performance, and visual polish. The UI is XML-ready and optimized for efficient touch-based navigation on embedded displays.

---

## Features

### Music Dashboard
- Displays current track metadata: title, artist, album
- Shows album artwork (JPG-based, base64 decoded)
- Animated music visualizer and playback progress
- Music queue panel with drag and drop support
- Optional playlist metadata exchange with server
- Wi-Fi-based communication with backend for low-latency updates

### System Monitor
- Real-time process usage display (top 5 tasks)
- Memory stats with visual indicators
- Buttons to terminate individual processes
- Optimized layout to avoid UI overlap and maintain readability

### Discord Integration
- Displays mic volume and up to 5 active call participants
- Mute and deafen toggle buttons
- Shows live user profile (supports animated profile pictures)
- Status indicator for online, idle, or do-not-disturb
- Fully compliant with Discord API (no self-bot usage)
- Modular agentic AI can control or extend this functionality

---

## UI Architecture

- Built with LVGL 9.4 using Pro XML Editor-compatible layout
- Supports runtime-loaded `globals.xml` for:
  - Shared fonts, styles, images, and data bindings
- Tabbed interface for:
  - Music
  - Processes
  - Discord
- All screens created with reusable components and responsive layout

---

## Getting Started

### Requirements
- ESP32 board with PSRAM (4MB or more recommended)
- SPI/I2C LCD display (e.g., ILI9488 or ILI9341)
- LVGL 9.4 with Arduino or ESP-IDF
- Python 3 backend server for music and system info
- Optional: VS Code with LVGL Pro extension

### Installation

1. Clone the repository
   ```bash
   git clone https://github.com/yourname/esp32-dashboard
   cd esp32-dashboard
Install PlatformIO dependencies

pio run


Configure Wi-Fi

In main.cpp or config.h, set your Wi-Fi SSID and password

Start the backend Python server

python3 server/music_process_discord.py


Upload and run

pio run -t upload -t monitor

Communication Protocol

Configurable between UART and Wi-Fi (default is Wi-Fi)

JSON-based messaging for:

Music metadata and artwork

Process lists and memory usage

Discord voice and presence state

Supports queue operations: next, insert, remove, reorder

Agentic AI Integration

This project supports AI control via a structured and extensible backend:

Playlist control and metadata injection

Discord presence and participant updates

UI adaptation via LVGL XML editing

Live synchronization with embedded display

Development Tools

LVGL Pro XML Editor (standalone or VS Code extension)

Real-time UI preview using XML files

ESP32 debug monitor via UART or Wi-Fi

Asset preparation tools (JPG conversion, font generation)

Folder Structure
/src
 ├── main.cpp              # System entry point
 ├── ui.cpp / ui.h         # LVGL tab-based UI
 ├── music.cpp             # Music handling logic
 ├── discord.cpp           # Discord API integration
 ├── process.cpp           # System process UI logic
/assets
 └── images/, fonts/, xml/ # UI assets
/server
 └── music_process_discord.py  # Backend interface

Future Improvements

Microphone activity LED

Voice waveform visualization

Playlist artwork carousel

Notification overlay for Discord or system alerts
