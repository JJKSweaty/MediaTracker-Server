import threading
from flask import Flask, request, jsonify
from flask_socketio import SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

import os
import base64
from dotenv import load_dotenv
import requests
import json
import webbrowser
import urllib.parse

# === TASK MANAGER ADDITIONS ===
import psutil
import datetime
import time
import serial
from serial import SerialException, SerialTimeoutException
from control_media import spotifyPlay, spotifyPause, spotifyNext, spotifyPrevious, media_play_pause, media_next, media_previous, get_spotify_progress
from image_utils import get_artwork_rgb565_base64, get_artwork_png_b64, clear_cache as clear_image_cache

# GPU monitoring (optional - works with NVIDIA GPUs)
try:
    import GPUtil
    HAS_GPU = True
except ImportError:
    HAS_GPU = False
    print("[INFO] GPUtil not installed - GPU monitoring disabled. Install with: pip install GPUtil")
# ===============================

load_dotenv()
pending_command = None

CLIENT_ID = os.getenv("CLIENT_ID", "").strip('"')
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip('"')
REDIRECT_URI = os.getenv("REDIRECT_URI", "").strip('"')
connected_clients = {}
title_data = ""
artist_data = ""
album_data = ""
artwork_data = ""
# Progress data from browser extension (for YouTube, etc.)
position_data = 0  # Current position in seconds
duration_data = 0  # Total duration in seconds
is_playing_data = False  # Whether media is currently playing
media_source = ""  # Track which source the media is from (e.g., "youtube", "spotify")
client_creds = f"{CLIENT_ID}:{CLIENT_SECRET}"
encoded = base64.b64encode(client_creds.encode()).decode()

# === SERIAL CONFIG FOR ESP ===
# Set this to the actual COM your ESP shows up as in Device Manager.
# You can override via .env: SERIAL_PORT=COM4
SERIAL_PORT = os.getenv("SERIAL_PORT", "COM3")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
ser = None
SERIAL_DEBUG = os.getenv("SERIAL_DEBUG", "1") in ("1", "true", "True")
DISABLE_AUTO_SERIAL = os.getenv("DISABLE_AUTO_SERIAL", "0") in ("1", "true", "True")
# =============================

# Basic validation to help debug Spotify auth issues quickly
if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
    print("[ERROR] Missing Spotify credentials. Create a `.env` file in the project root with the following keys:")
    print("  CLIENT_ID=your_spotify_client_id")
    print("  CLIENT_SECRET=your_spotify_client_secret")
    print("  REDIRECT_URI=http://localhost:8080/callback")
    print("Make sure the Redirect URI exactly matches the value configured in your Spotify developer app.")
    raise SystemExit(1)

# Print the effective config values for debugging (CLIENT_SECRET is not printed)
masked_client = CLIENT_ID[:4] + '...' + CLIENT_ID[-4:] if CLIENT_ID else 'None'
print(f"[CONFIG] CLIENT_ID={masked_client}")
print(f"[CONFIG] REDIRECT_URI={REDIRECT_URI}")
print(f"[CONFIG] SERIAL_PORT={SERIAL_PORT} BAUD={SERIAL_BAUD}")


# === TASK MANAGER ADDITIONS ===
# Cache for expensive process list (only update every 2 seconds)
_proc_cache = {"data": [], "last_update": 0}
_PROC_CACHE_TTL = 2.0

def get_system_snapshot():
    """Return a task-manager style snapshot for the UI and ESP."""
    global _proc_cache
    data = {}

    # Time info
    ts = time.time()
    utc_offset = (datetime.datetime.fromtimestamp(ts) -
                  datetime.datetime.utcfromtimestamp(ts)).total_seconds()
    data["utc_offset"] = int(utc_offset)

    now = datetime.datetime.now()
    data["local_time"] = int(now.timestamp())

    # CPU and memory (non-blocking - uses cached value from last call)
    data["cpu_percent_total"] = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    data["mem_percent"] = mem.percent

    # GPU monitoring (NVIDIA only via GPUtil)
    if HAS_GPU:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                # Use first GPU's load percentage
                data["gpu_percent"] = gpus[0].load * 100
            else:
                data["gpu_percent"] = 0.0
        except Exception:
            data["gpu_percent"] = 0.0
    else:
        data["gpu_percent"] = 0.0

    # Battery
    battery = psutil.sensors_battery()
    if battery is not None:
        data["battery_percent"] = battery.percent
        data["power_plugged"] = bool(battery.power_plugged)
    else:
        data["battery_percent"] = None
        data["power_plugged"] = None

    # Per-process Memory percentage - cache this since it's expensive
    now = time.time()
    if now - _proc_cache["last_update"] > _PROC_CACHE_TTL:
        # Time to refresh process list
        n_cpus = psutil.cpu_count(logical=True) or 1
        processes = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info.get("name") or ""
                # Skip idle/system idle noise
                if name.lower() in ("system idle process", "idle"):
                    continue

                # Use memory percentage for sorting
                try:
                    mem_p = proc.memory_percent()
                except Exception:
                    mem_p = 0.0
                # Clamp to [0, 100] for display sanity
                if mem_p < 0:
                    mem_p = 0.0
                if mem_p > 100.0:
                    mem_p = 100.0

                if mem_p > 0.1:
                    processes.append((mem_p, proc.pid, name))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        procs_sorted = sorted(processes, key=lambda x: x[0], reverse=True)[:5]
        _proc_cache["data"] = procs_sorted
        _proc_cache["last_update"] = now
    else:
        procs_sorted = _proc_cache["data"]

    # Provide both a backward-compatible `cpu_top5_process` (string list) and a richer 'proc_top5'
    data["cpu_top5_process"] = [f"{p[0]:.1f}% {p[2]}" for p in procs_sorted]
    # Rich object list for the ESP to parse PID + mem% + name
    data["proc_top5"] = [{"pid": p[1], "mem": round(p[0], 1), "name": p[2]} for p in procs_sorted]
    return data


# Artwork fetching state (shared between threads)
_artwork_state = {
    "pending_url": None,
    "ready_b64": None,
    "ready_url": None,
    "fetching": False
}
_artwork_lock = threading.Lock()

def artwork_fetch_worker():
    """Background thread that fetches artwork without blocking main loop."""
    global _artwork_state
    print("[ARTWORK WORKER] Thread started")
    while True:
        url_to_fetch = None
        
        with _artwork_lock:
            if _artwork_state["pending_url"] and not _artwork_state["fetching"]:
                url_to_fetch = _artwork_state["pending_url"]
                _artwork_state["fetching"] = True
        
        if url_to_fetch:
            try:
                print(f"[ARTWORK WORKER] Fetching: {url_to_fetch[:60]}...")
                b64 = get_artwork_png_b64(url_to_fetch)
                with _artwork_lock:
                    if b64:
                        _artwork_state["ready_b64"] = b64
                        _artwork_state["ready_url"] = url_to_fetch
                        print(f"[ARTWORK WORKER] Ready! {len(b64)} chars")
                    else:
                        print("[ARTWORK WORKER] Failed to get b64")
                    _artwork_state["fetching"] = False
                    _artwork_state["pending_url"] = None
            except Exception as e:
                print(f"[ARTWORK WORKER] Error: {e}")
                import traceback
                traceback.print_exc()
                with _artwork_lock:
                    _artwork_state["fetching"] = False
                    _artwork_state["pending_url"] = None
        
        time.sleep(0.1)

# Start artwork worker thread
_artwork_thread = threading.Thread(target=artwork_fetch_worker, daemon=True)
_artwork_thread.start()


def system_monitor_loop(interval=2):
    """Background loop that broadcasts system info over Socket.IO and serial."""
    global ser, _artwork_state
    last_artwork_url = None
    artwork_sent_url = None
    
    while True:
        loop_start = time.time()
        
        snapshot = get_system_snapshot()

        # Attach media block from metadata globals
        media = get_media_snapshot()
        artwork_url = None
        
        if media is not None:
            artwork_url = media.pop("artwork_url", None)
            
            # Request artwork fetch in background (non-blocking)
            if artwork_url and artwork_url != last_artwork_url:
                with _artwork_lock:
                    if not _artwork_state["fetching"]:
                        _artwork_state["pending_url"] = artwork_url
                        last_artwork_url = artwork_url
                        if SERIAL_DEBUG:
                            print(f"[ARTWORK] Queued for fetch: {artwork_url[:60]}...")
            
            snapshot["media"] = media

        # 1) Emit to web clients via Socket.IO
        socketio.emit("system_info", snapshot)

        # 2) Send to ESP via serial as newline-terminated JSON (fast, no artwork)
        if ser is not None:
            try:
                line = json.dumps(snapshot) + "\n"
                line_bytes = line.encode("utf-8")
                
                if SERIAL_DEBUG:
                    loop_time = (time.time() - loop_start) * 1000
                    print(f"[LOOP] {loop_time:.0f}ms, JSON size: {len(line_bytes)} bytes")
                
                ser.write(line_bytes)
                
                # Check if artwork is ready to send
                with _artwork_lock:
                    ready_b64 = _artwork_state["ready_b64"]
                    ready_url = _artwork_state["ready_url"]
                
                if ready_b64 and ready_url and ready_url != artwork_sent_url:
                    print(f"[ARTWORK] Sending to ESP... ({len(ready_b64)} chars)")
                    artwork_msg = json.dumps({"artwork_b64": ready_b64}) + "\n"
                    ser.write(artwork_msg.encode("utf-8"))
                    artwork_sent_url = ready_url
                    print(f"[ARTWORK SENT] {len(artwork_msg)} bytes")
                    
            except SerialTimeoutException:
                print("[SERIAL ERROR] Timeout writing to ESP.")
            except SerialException as e:
                print(f"[SERIAL ERROR] {e}")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
        else:
            # Debug: show if serial is not connected
            if loop_start % 10 < 1:  # Print every ~10 seconds
                print("[WARNING] Serial not connected, data not sent to ESP")

        socketio.sleep(interval)


def serial_command_loop():
        """Background loop that reads commands from the ESP via serial and acts on them."""
        global ser
        if ser is None:
            return
        while True:
            try:
                if ser.in_waiting:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    # Try to parse JSON commands
                    try:
                        cmd = json.loads(line)
                    except Exception:
                        # Non-JSON lines can be ignored or print for debugging
                        if SERIAL_DEBUG:
                            print(f"[SERIAL IN] {line}")
                        continue

                    if not isinstance(cmd, dict):
                        continue

                    typ = cmd.get("cmd") or cmd.get("type")
                    if not typ:
                        continue

                    if typ == "kill":
                        pid = cmd.get("pid")
                        if pid is not None:
                            try:
                                pid = int(pid)
                                # Reuse existing logic: kill by pid using psutil
                                proc = psutil.Process(pid)
                                proc.terminate()
                                try:
                                    proc.wait(timeout=3)
                                    status = "terminated"
                                except psutil.TimeoutExpired:
                                    proc.kill()
                                    status = "killed"
                                if SERIAL_DEBUG:
                                    print(f"[SERIAL CMD] Killed pid={pid} status={status}")
                            except Exception as e:
                                if SERIAL_DEBUG:
                                    print(f"[SERIAL CMD] Error killing pid {pid}: {e}")

                    elif typ == "play":
                        try:
                            # Use keyboard media key - works with any player
                            media_play_pause()
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL CMD] Play error: {e}")
                    elif typ == "pause":
                        try:
                            # Use keyboard media key - works with any player
                            media_play_pause()
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL CMD] Pause error: {e}")
                    elif typ == "next":
                        try:
                            # Use keyboard media key - works with any player
                            media_next()
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL CMD] Next error: {e}")
                    elif typ == "previous":
                        try:
                            # Use keyboard media key - works with any player
                            media_previous()
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL CMD] Prev error: {e}")
                    else:
                        if SERIAL_DEBUG:
                            print(f"[SERIAL CMD] Unknown cmd: {cmd}")

            except Exception as e:
                if SERIAL_DEBUG:
                    print(f"[SERIAL CMD] Reader exception: {e}")
            time.sleep(0.05)


def prime_psutil():
    """Prime cpu_percent counters so the first reading isn't 0.0."""
    try:
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                proc.cpu_percent(interval=None)
            except Exception:
                continue
    except Exception:
        pass
# ===============================


@app.route("/callback")
def callback():
    codes = request.args.get("code")
    if codes:
        print(f"[CALLBACK] Received code: {codes}")
    else:
        return "Error: No code received"
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": "Basic " + encoded},
        data={"grant_type": "authorization_code", "code": codes, "redirect_uri": REDIRECT_URI}
    )
    # Better error visibility for token exchange
    if response.ok:
        tokens = response.json()
        with open("tokens.json", "w") as f:
            json.dump(tokens, f, indent=3)
        print("[CALLBACK] Tokens saved to tokens.json")
        return "Authorization successful. You can close this window."
    else:
        print(f"[CALLBACK ERROR] Status: {response.status_code} Response: {response.text}")
        return f"Authorization failed: {response.status_code} - check server logs for details.", 500


@socketio.event("connect")
def handle_connect():
    print(f"[CONNECTED] Client connected")
    connected_clients["client"] = True
    socketio.emit("my_response", {"msg": "Hello from server"})


@socketio.on("disconnect")
def handle_disconnect():
    print(f"[DISCONNECTED] Client disconnected")
    connected_clients.pop("client", None)


@socketio.on("sendTitle")
def receive_title(data):
    global title_data
    title_data = data
    print(f"[RECEIVED TITLE]: {title_data}")


@socketio.on("sendArtist")
def receive_artist(data):
    global artist_data
    artist_data = data
    print(f"[RECEIVED ARTIST]: {artist_data}")


@socketio.on("sendAlbum")
def receive_album(data):
    global album_data
    album_data = data
    print(f"[RECEIVED ALBUM]: {album_data}")


@socketio.on("sendArtwork")
def receive_artwork(data):
    global artwork_data
    artwork_data = data
    print(f"[RECEIVED ARTWORK]: {artwork_data}")


@socketio.on("sendPosition")
def receive_position(data):
    """Receive current playback position in seconds from browser extension."""
    global position_data
    try:
        position_data = int(float(data))
    except (ValueError, TypeError):
        position_data = 0
    # Only print occasionally to avoid spam
    # print(f"[RECEIVED POSITION]: {position_data}s")


@socketio.on("sendDuration")
def receive_duration(data):
    """Receive total duration in seconds from browser extension."""
    global duration_data
    try:
        duration_data = int(float(data))
    except (ValueError, TypeError):
        duration_data = 0
    print(f"[RECEIVED DURATION]: {duration_data}s")


@socketio.on("sendPlaying")
def receive_playing(data):
    """Receive play/pause state from browser extension."""
    global is_playing_data
    if isinstance(data, bool):
        is_playing_data = data
    elif isinstance(data, str):
        is_playing_data = data.lower() in ("true", "1", "playing")
    else:
        is_playing_data = bool(data)
    print(f"[RECEIVED PLAYING STATE]: {is_playing_data}")


@socketio.on("sendSource")
def receive_source(data):
    """Receive media source identifier (e.g., 'youtube', 'spotify')."""
    global media_source
    media_source = str(data).lower() if data else ""
    print(f"[RECEIVED SOURCE]: {media_source}")


# === TASK MANAGER ADDITIONS: HTTP + SOCKET ENDPOINTS ===
@app.route("/system_info", methods=["GET"])
def system_info():
    """REST endpoint to get a one-shot system snapshot."""
    snapshot = get_system_snapshot()
    return jsonify(snapshot)


@socketio.on("request_system_info")
def handle_request_system_info():
    """Client can emit 'request_system_info' to get a snapshot."""
    snapshot = get_system_snapshot()
    socketio.emit("system_info", snapshot)


@app.route("/kill_process", methods=["POST"])
def kill_process():
    """
    Kill a process by pid or by name.
    Body examples:
        { "pid": 1234 }
        { "name": "chrome.exe" }
    """
    data = request.json or {}
    pid = data.get("pid")
    name = data.get("name")

    if pid is None and not name:
        return jsonify({"error": "pid or name required"}), 400

    # Kill by PID
    if pid is not None:
        try:
            pid = int(pid)
            proc = psutil.Process(pid)
            proc_name = proc.name()
            proc.terminate()
            try:
                proc.wait(timeout=3)
                status = "terminated"
            except psutil.TimeoutExpired:
                proc.kill()
                status = "killed"
            return jsonify({"status": status, "pid": pid, "name": proc_name})
        except psutil.NoSuchProcess:
            return jsonify({"error": "process not found"}), 404
        except psutil.AccessDenied:
            return jsonify({"error": "access denied"}), 403
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Kill by name (all matches, except this server process)
    killed = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if not proc.info["name"]:
                    continue
                if proc.info["name"].lower() == name.lower():
                    if proc.pid == os.getpid():
                        continue  # do not kill this server
                    proc.terminate()
                    killed.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not killed:
            return jsonify({"status": "no_matching_process"}), 404

        return jsonify({"status": "terminated_by_name", "name": name, "pids": killed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@socketio.on("kill_process")
def handle_kill_process_socket(data):
    """
    Socket.IO version.
    Client emits:
        socket.emit("kill_process", { pid: 1234 })
    or:
        socket.emit("kill_process", { name: "chrome.exe" })
    """
    data = data or {}
    pid = data.get("pid")
    name = data.get("name")

    with app.test_request_context():
        if pid is None and not name:
            socketio.emit("kill_process_result", {"error": "pid or name required"})
            return

        if pid is not None:
            try:
                pid = int(pid)
                proc = psutil.Process(pid)
                proc_name = proc.name()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                    status = "terminated"
                except psutil.TimeoutExpired:
                    proc.kill()
                    status = "killed"
                socketio.emit("kill_process_result", {"status": status, "pid": pid, "name": proc_name})
                return
            except psutil.NoSuchProcess:
                socketio.emit("kill_process_result", {"error": "process not found"})
                return
            except psutil.AccessDenied:
                socketio.emit("kill_process_result", {"error": "access denied"})
                return
            except Exception as e:
                socketio.emit("kill_process_result", {"error": str(e)})
                return

        killed = []
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if not proc.info["name"]:
                        continue
                    if proc.info["name"].lower() == name.lower():
                        if proc.pid == os.getpid():
                            continue
                        proc.terminate()
                        killed.append(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if not killed:
                socketio.emit("kill_process_result", {"status": "no_matching_process"})
            else:
                socketio.emit("kill_process_result", {"status": "terminated_by_name", "name": name, "pids": killed})
        except Exception as e:
            socketio.emit("kill_process_result", {"error": str(e)})
# =======================================================


@app.route("/send_command", methods=["POST"])
def set_command():
    global pending_command
    data = request.json
    command = data.get("command")
    if command in ["play", "pause", "next", "previous"]:
        print(f"[COMMAND SET BY USER]: {command}")
        pending_command = command
        return jsonify({"status": "command set", "command": command})
    return jsonify({"error": "invalid command"}), 400


@app.route("/get_command", methods=["GET"])
def get_command():
    global pending_command
    if pending_command:
        cmd = pending_command
        pending_command = None  # Clear it after sending
        return jsonify({"command": cmd})
    return jsonify({"command": None})


def get_metadata():
    return {
        "title": title_data,
        "artist": artist_data,
        "album": album_data,
        "artwork": artwork_data
    }
# Track last sent artwork URL to avoid redundant sends
_last_artwork_url = None

def get_media_snapshot():
    """
    Build a media snapshot from current metadata.
    Uses Spotify API for progress if Spotify is active, otherwise uses browser extension data.
    """
    global title_data, artist_data, album_data, artwork_data
    global position_data, duration_data, is_playing_data, media_source

    # If nothing is set yet, skip media entirely
    if not title_data and not artist_data and not album_data and not artwork_data:
        return None

    # Determine if we should use Spotify API or browser extension data
    # Use Spotify API only if media source is spotify or if we don't have browser progress data
    use_spotify_api = (media_source == "spotify" or 
                       ("spotify" in artist_data.lower() if artist_data else False))
    
    if use_spotify_api:
        # Try Spotify API for progress
        position_sec, duration_sec, is_playing = get_spotify_progress()
        # If Spotify returns no data, fall back to browser data
        if duration_sec == 0 and duration_data > 0:
            position_sec = position_data
            duration_sec = duration_data
            is_playing = is_playing_data
    else:
        # Use browser extension data (YouTube, etc.)
        position_sec = position_data
        duration_sec = duration_data
        is_playing = is_playing_data

    media = {
        "title": title_data or "",
        "artist": artist_data or "",
        "album": album_data or "",
        "position_seconds": position_sec,
        "duration_seconds": duration_sec,
        "is_playing": is_playing,
    }

    # Extract artwork URL - artwork_data can be a URL string or {"src": url} object
    artwork_url = None
    if artwork_data:
        if isinstance(artwork_data, dict) and "src" in artwork_data:
            artwork_url = artwork_data["src"]
        elif isinstance(artwork_data, str) and artwork_data.startswith("http"):
            artwork_url = artwork_data
    
    # Just indicate if artwork exists (actual image sent separately)
    media["has_artwork"] = artwork_url is not None
    media["artwork_url"] = artwork_url  # Store for separate image send

    return media


def send_artwork_to_esp(url: str):
    """
    Download image, convert to RGB565, and send to ESP in chunks.
    Sends as: IMG:<base64_chunk>\n
    Final chunk: IMG_END\n
    """
    global ser, _last_artwork_url
    
    if ser is None or not url:
        return
    
    # Skip if same URL already sent
    if url == _last_artwork_url:
        return
    
    try:
        if SERIAL_DEBUG:
            print(f"[IMAGE] Downloading: {url[:60]}...")
        
        rgb565_b64 = get_artwork_rgb565_base64(url)
        if rgb565_b64 is None:
            print("[IMAGE] Failed to get artwork")
            return
        
        if SERIAL_DEBUG:
            print(f"[IMAGE] Sending {len(rgb565_b64)} bytes base64...")
        
        # Send in chunks (512 bytes per chunk to be safe)
        CHUNK_SIZE = 512
        for i in range(0, len(rgb565_b64), CHUNK_SIZE):
            chunk = rgb565_b64[i:i + CHUNK_SIZE]
            line = f"IMG:{chunk}\n"
            ser.write(line.encode("utf-8"))
            time.sleep(0.01)  # Small delay between chunks
        
        # Signal end of image
        ser.write(b"IMG_END\n")
        
        _last_artwork_url = url
        
        if SERIAL_DEBUG:
            print("[IMAGE] Sent successfully")
            
    except Exception as e:
        print(f"[IMAGE] Error sending: {e}")

def handle_user_input():
    from control_media import (
        Auth,
        getPlayerInfo,
        printSpotifyInfo,
        spotifyPlay,
        spotifyPause,
        spotifyNext,
        spotifyPrevious,
        spotifyVolume,
        spotifySeek,
        get_current_media
    )
    from metadata import (
        send_play,
        send_pause,
        print_stored_metadata
    )
    print("\n[ CONTROL PANEL]")
    print("Type:")
    print("  1 -  Play & get current media")
    print("  2 -  Pause")
    print("  3 -  Next")
    print("  4 -  Previous")
    print("  5 -  Show Current Metadata")
    print("  6 -  Show Stored Metadata ")
    print("  7 -  Exit")
    print("  8 -  Re-auth (Manual Auth)")
    print("  9 -  Set Volume")
    print(" 10 -  Seek to position (ms)\n")

    while True:
        try:
            choice = input("> ").strip()

            if choice == "1":
                send_play()
                print(f"[CURRENT MEDIA]: {get_current_media()}")
            elif choice == "2":
                spotifyPause()
            elif choice == "3":
                spotifyNext()
            elif choice == "4":
                spotifyPrevious()
            elif choice == "5":
                metadata = get_metadata()
                print_stored_metadata(metadata)
            elif choice == "6":
                printSpotifyInfo()
            elif choice == "7":
                print("[EXIT] Stopping user input loop.")
                break
            elif choice == "8":
                Auth()
            elif choice == "9":
                vol = input("Enter volume (0–100): ").strip()
                spotifyVolume(int(vol))
            elif choice == "10":
                pos = input("Enter seek position (in s): ").strip()
                spotifySeek(int(pos))
            else:
                print("[ERROR] Invalid choice.")
        except Exception as e:
            print(f"[ERROR] {str(e)}")


if __name__ == "__main__":
    # CLI control panel
    threading.Thread(target=handle_user_input, daemon=True).start()

    # === OPEN SERIAL PORT TO ESP ===
    if DISABLE_AUTO_SERIAL:
        print("[SERIAL] Auto-open disabled via DISABLE_AUTO_SERIAL")
        ser = None
    else:
        try:
            print(f"[SERIAL] Opening {SERIAL_PORT} at {SERIAL_BAUD}...")
            ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
            print("[SERIAL] Connected to ESP.")
        except Exception as e:
            print(f"[SERIAL ERROR] Could not open port {SERIAL_PORT}: {e}")
            ser = None
    # =================================

    # Start serial command reader (if serial opened)
    if ser is not None:
        threading.Thread(target=serial_command_loop, daemon=True).start()

    # Prime psutil counters
    prime_psutil()

    # Start background system monitor (1.0s = 1 Hz to match progress bar updates)
    socketio.start_background_task(system_monitor_loop, 1.0)

    print("[SERVER RUNNING] Flask-SocketIO on port 8080...")
    # IMPORTANT: no reloader, single process => only one serial owner
    socketio.run(app, host="0.0.0.0", port=8080, debug=False, use_reloader=False)
  