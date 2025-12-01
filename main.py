import threading
from flask import Flask, request, jsonify
from flask_socketio import SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

import os
import queue
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
from control_media import spotifyPlay, spotifyPause, spotifyNext, spotifyPrevious, media_play_pause, media_next, media_previous, get_spotify_progress, is_app_playing
from image_utils import get_artwork_rgb565_base64, get_artwork_png_b64, clear_cache as clear_image_cache

# Spotify Queue Manager for playlist/queue features
from spotify_queue import get_queue_manager, SpotifyQueueManager

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
_serial_queue = queue.Queue()
_serial_priority_queue = queue.Queue(maxsize=2)  # artwork and other priority messages
SERIAL_MAX_QUEUE = int(os.getenv("SERIAL_MAX_QUEUE", "8"))
SERIAL_MIN_INTERVAL = float(os.getenv("SERIAL_MIN_INTERVAL", "0.1"))  # min interval between serial snapshots (seconds)
_last_serial_sent_time = 0.0
_last_sent_snapshot_fingerprint = None
SERIAL_MAX_QUEUE = int(os.getenv("SERIAL_MAX_QUEUE", "8"))
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
    # Create display-friendly names (strip .exe and paths)
    cleaned = []
    for p in procs_sorted:
        mem_p, pid, name = p
        name_str = name or ""
        # If name is a path, use basename
        try:
            name_str = os.path.basename(name_str)
        except Exception:
            pass
        display_name = name_str
        if display_name.lower().endswith(".exe"):
            display_name = display_name[:-4]
        cleaned.append((mem_p, pid, name_str, display_name))

    data["cpu_top5_process"] = [f"{p[0]:.1f}% {p[3]}" for p in cleaned]
    # Rich object list for the ESP to parse PID + mem% + name + display_name
    data["proc_top5"] = [{"pid": p[1], "mem": round(p[0], 1), "name": p[2], "display_name": p[3]} for p in cleaned]
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


def system_monitor_loop(interval=2.0):
    """Background loop that broadcasts system info over Socket.IO and serial.
    The loop is scheduled using time.monotonic to maintain regular intervals and
    avoid jitter from blocking calls. Serial writes are enqueued to `_serial_queue`.
    """
    global ser, _artwork_state, _last_artwork_url, _last_sent_snapshot_fingerprint, _last_serial_sent_time
    
    interval = float(interval)
    next_time = time.monotonic()
    while True:
        loop_start = time.monotonic()
        
        snapshot = get_system_snapshot()

        # Attach media block from metadata globals
        media = get_media_snapshot()
        artwork_url = None
        
        if media is not None:
            artwork_url = media.pop("artwork_url", None)

            # Request artwork fetch in background (non-blocking)
            if artwork_url and artwork_url != _last_artwork_url:
                with _artwork_lock:
                    if not _artwork_state.get("fetching", False):
                        _artwork_state["pending_url"] = artwork_url
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
                    loop_time = (time.monotonic() - loop_start) * 1000
                    print(f"[LOOP] {loop_time:.0f}ms, JSON size: {len(line_bytes)} bytes")
                    # Warn if loop took unusually long
                    if loop_time > max(200, interval * 1000 * 5):
                        print(f"[LOOP WARNING] High jitter: {loop_time:.0f}ms")

                # Use background writer to avoid blocking the monitoring loop
                now_ts = time.monotonic()
                # Create a compact fingerprint of the snapshot to decide if we should send it
                try:
                    fp_source = json.dumps({
                        "cpu": snapshot.get("cpu_percent_total"),
                        "mem": snapshot.get("mem_percent"),
                        "media": snapshot.get("media"),
                        "time": snapshot.get("local_time")
                    }, sort_keys=True)
                except Exception:
                    fp_source = ""
                fingerprint = str(hash(fp_source))

                # If fingerprint hasn't changed and we recently sent, skip to reduce traffic
                should_send_snapshot = True
                if _last_sent_snapshot_fingerprint == fingerprint and (now_ts - _last_serial_sent_time) < SERIAL_MIN_INTERVAL:
                    should_send_snapshot = False

                if should_send_snapshot:
                    try:
                        if _serial_queue.qsize() >= SERIAL_MAX_QUEUE:
                            # Drop older message to make space and keep latest
                            try:
                                _serial_queue.get_nowait()
                                _serial_queue.task_done()
                            except Exception:
                                pass
                        _serial_queue.put({"type": "snapshot", "payload": line_bytes}, block=False)
                        _last_sent_snapshot_fingerprint = fingerprint
                        _last_serial_sent_time = now_ts
                    except Exception as e:
                        if SERIAL_DEBUG:
                            print(f"[SERIAL WRITER] Queue put failed: {e}")

                # Check if artwork is ready to send
                with _artwork_lock:
                    ready_b64 = _artwork_state["ready_b64"]
                    ready_url = _artwork_state["ready_url"]

                if ready_b64 and ready_url and ready_url != _last_artwork_url:
                    print(f"[ARTWORK] Queuing to ESP... ({len(ready_b64)} chars) url={ready_url}")
                    artwork_msg = json.dumps({"artwork_b64": ready_b64}) + "\n"
                    msg_bytes = artwork_msg.encode("utf-8")
                    try:
                        # If priority queue is full, remove older artwork to make room
                        if _serial_priority_queue.full():
                            try:
                                _serial_priority_queue.get_nowait()
                                _serial_priority_queue.task_done()
                            except Exception:
                                pass
                        _serial_priority_queue.put({"type": "artwork", "payload": msg_bytes, "url": ready_url}, block=False)
                    except Exception as e:
                        if SERIAL_DEBUG:
                            print(f"[SERIAL WRITER] Artwork queue put failed: {e}")
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

        # Precise periodic scheduling
        next_time += interval
        sleep_time = max(0, next_time - time.monotonic())
        socketio.sleep(sleep_time)


# ========== QUEUE/PLAYLIST COMMAND HANDLERS ==========

def _handle_queue_action(cmd: dict):
    """
    Handle queue actions from ESP32.
    Commands:
      - {"type": "queue_action", "action": "play_now", "track_id": "spotify:track:XXX"}
      - {"type": "queue_action", "action": "remove", "track_id": "spotify:track:XXX", "playlist_id": "..."}
      - {"type": "queue_action", "action": "add_to_queue", "track_id": "spotify:track:XXX"}
      - {"type": "queue_action", "action": "reorder", "playlist_id": "...", "from_index": 0, "to_index": 5}
    """
    action = cmd.get("action", "")
    track_id = cmd.get("track_id", "")
    
    try:
        queue_mgr = get_queue_manager()
        
        if action == "play_now":
            if not track_id:
                print("[QUEUE CMD] play_now missing track_id")
                return
            # Check if it's a local track (can't play via API)
            if ":local:" in track_id:
                print(f"[QUEUE CMD] Cannot play local track via API: {track_id}")
                return
            success = queue_mgr.play_track(track_id)
            if SERIAL_DEBUG:
                print(f"[QUEUE CMD] play_now {track_id}: {'OK' if success else 'FAIL'}")
        
        elif action == "add_to_queue":
            if not track_id:
                print("[QUEUE CMD] add_to_queue missing track_id")
                return
            if ":local:" in track_id:
                print(f"[QUEUE CMD] Cannot queue local track: {track_id}")
                return
            success = queue_mgr.add_to_queue(track_id)
            if SERIAL_DEBUG:
                print(f"[QUEUE CMD] add_to_queue {track_id}: {'OK' if success else 'FAIL'}")
        
        elif action == "remove":
            playlist_id = cmd.get("playlist_id", "")
            snapshot_id = cmd.get("snapshot_id")
            if not track_id or not playlist_id:
                print("[QUEUE CMD] remove missing track_id or playlist_id")
                return
            new_snap = queue_mgr.remove_track_from_playlist(playlist_id, track_id, snapshot_id)
            if SERIAL_DEBUG:
                print(f"[QUEUE CMD] remove {track_id} from {playlist_id}: {'OK' if new_snap else 'FAIL'}")
        
        elif action == "reorder":
            playlist_id = cmd.get("playlist_id", "")
            from_idx = cmd.get("from_index", 0)
            to_idx = cmd.get("to_index", 0)
            snapshot_id = cmd.get("snapshot_id")
            if not playlist_id:
                print("[QUEUE CMD] reorder missing playlist_id")
                return
            new_snap = queue_mgr.reorder_playlist_tracks(playlist_id, from_idx, to_idx, 1, snapshot_id)
            if SERIAL_DEBUG:
                print(f"[QUEUE CMD] reorder {playlist_id} {from_idx}->{to_idx}: {'OK' if new_snap else 'FAIL'}")
        
        else:
            print(f"[QUEUE CMD] Unknown action: {action}")
    
    except Exception as e:
        print(f"[QUEUE CMD] Error: {e}")


def _handle_playlist_action(cmd: dict):
    """
    Handle playlist actions from ESP32.
    Commands:
      - {"type": "playlist_action", "action": "set_active", "playlist_id": "..."}
      - {"type": "playlist_action", "action": "follow", "playlist_id": "..."}
      - {"type": "playlist_action", "action": "unfollow", "playlist_id": "..."}
      - {"type": "playlist_action", "action": "get_playlists"}
    """
    action = cmd.get("action", "")
    playlist_id = cmd.get("playlist_id", "")
    
    try:
        queue_mgr = get_queue_manager()
        
        if action == "set_active":
            if not playlist_id:
                print("[PLAYLIST CMD] set_active missing playlist_id")
                return
            success = queue_mgr.set_active_playlist(playlist_id)
            if SERIAL_DEBUG:
                print(f"[PLAYLIST CMD] set_active {playlist_id}: {'OK' if success else 'FAIL'}")
        
        elif action == "follow":
            if not playlist_id:
                print("[PLAYLIST CMD] follow missing playlist_id")
                return
            success = queue_mgr.follow_playlist(playlist_id)
            if SERIAL_DEBUG:
                print(f"[PLAYLIST CMD] follow {playlist_id}: {'OK' if success else 'FAIL'}")
        
        elif action == "unfollow":
            if not playlist_id:
                print("[PLAYLIST CMD] unfollow missing playlist_id")
                return
            success = queue_mgr.unfollow_playlist(playlist_id)
            if SERIAL_DEBUG:
                print(f"[PLAYLIST CMD] unfollow {playlist_id}: {'OK' if success else 'FAIL'}")
        
        elif action == "get_playlists":
            playlists = queue_mgr.get_user_playlists(limit=10)
            # Send playlists list back to ESP (could use a separate message)
            # For now just log
            if SERIAL_DEBUG:
                print(f"[PLAYLIST CMD] get_playlists: {len(playlists)} playlists")
            # TODO: Send playlist list to ESP via serial
        
        else:
            print(f"[PLAYLIST CMD] Unknown action: {action}")
    
    except Exception as e:
        print(f"[PLAYLIST CMD] Error: {e}")


def _handle_like_track(cmd: dict):
    """
    Handle like/unlike track actions from ESP32.
    Commands:
      - {"type": "like_track", "action": "like", "track_id": "spotify:track:XXX"}
      - {"type": "like_track", "action": "unlike", "track_id": "spotify:track:XXX"}
    """
    action = cmd.get("action", "like")
    track_id = cmd.get("track_id", "")
    
    if not track_id:
        print("[LIKE CMD] Missing track_id")
        return
    
    try:
        queue_mgr = get_queue_manager()
        
        if action == "like":
            success = queue_mgr.save_track(track_id)
            if SERIAL_DEBUG:
                print(f"[LIKE CMD] like {track_id}: {'OK' if success else 'FAIL'}")
        elif action == "unlike":
            success = queue_mgr.remove_saved_track(track_id)
            if SERIAL_DEBUG:
                print(f"[LIKE CMD] unlike {track_id}: {'OK' if success else 'FAIL'}")
        else:
            print(f"[LIKE CMD] Unknown action: {action}")
    
    except Exception as e:
        print(f"[LIKE CMD] Error: {e}")


# =======================================================

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
                            # Also emit a socketio command to browser extension as a fallback
                            try:
                                socketio.emit("command", {"command": "play"})
                            except Exception as e:
                                if SERIAL_DEBUG:
                                    print(f"[SERIAL CMD] socketio emit play failed: {e}")
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL CMD] Play error: {e}")
                    elif typ == "pause":
                        try:
                            # Use keyboard media key - works with any player
                            media_play_pause()
                            # Also emit a socketio command to browser extension as a fallback
                            try:
                                socketio.emit("command", {"command": "pause"})
                            except Exception as e:
                                if SERIAL_DEBUG:
                                    print(f"[SERIAL CMD] socketio emit pause failed: {e}")
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
                    
                    # ========== QUEUE/PLAYLIST COMMANDS ==========
                    elif typ == "queue_action":
                        _handle_queue_action(cmd)
                    elif typ == "playlist_action":
                        _handle_playlist_action(cmd)
                    elif typ == "like_track":
                        _handle_like_track(cmd)
                    # ==============================================
                    
                    else:
                        if SERIAL_DEBUG:
                            print(f"[SERIAL CMD] Unknown cmd: {cmd}")

            except Exception as e:
                if SERIAL_DEBUG:
                    print(f"[SERIAL CMD] Reader exception: {e}")
            time.sleep(0.05)


def _serial_writer_loop():
    global ser
    global _last_artwork_url
    last_print_qsize = -1
    last_print_time = 0
    while True:
        try:
            # Prioritize artwork messages
            try:
                priority = _serial_priority_queue.get_nowait()
                if priority is not None:
                    dat = priority
                    if ser is not None and dat is not None:
                        try:
                            ser.write(dat["payload"])
                            # Once artwork message is sent, update last artwork url so we don't resend
                            try:
                                _last_artwork_url = dat.get("url")
                            except Exception:
                                pass
                        except Exception as e:
                            if SERIAL_DEBUG:
                                print(f"[SERIAL WRITER] Priority write error: {e}")
                    _serial_priority_queue.task_done()
                    # go to top of loop to check for more priority items
                    continue
            except Exception:
                pass

            data = _serial_queue.get()
            if ser is not None and data is not None:
                try:
                    if isinstance(data, dict) and data.get("payload"):
                        ser.write(data["payload"])
                    else:
                        ser.write(data)
                except Exception as e:
                    if SERIAL_DEBUG:
                        print(f"[SERIAL WRITER] Write error: {e}")
            _serial_queue.task_done()
        except Exception as e:
            if SERIAL_DEBUG:
                print(f"[SERIAL WRITER] Exception: {e}")
        # Debug: print queue size occasionally
        try:
            if SERIAL_DEBUG and _serial_queue.qsize() > 0:
                print(f"[SERIAL WRITER] Queue size: {_serial_queue.qsize()}")
        except Exception:
            pass
        # Small sleep to avoid busy loop
        time.sleep(0.001)
        # Print queue size occasionally if it changes or every 2 seconds
        try:
            qsize = _serial_queue.qsize()
            now_ts = time.time()
            if SERIAL_DEBUG and (qsize != last_print_qsize or (now_ts - last_print_time) > 2):
                print(f"[SERIAL WRITER] Queue size: {qsize}")
                last_print_qsize = qsize
                last_print_time = now_ts
        except Exception:
            pass


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
        # Add obtained_at timestamp to help token validation/refresh logic
        try:
            tokens["obtained_at"] = int(time.time())
        except Exception:
            pass
        with open("tokens.json", "w") as f:
            json.dump(tokens, f, indent=3)
        print("[CALLBACK] Tokens saved to tokens.json")
        print(f"[CALLBACK] REDIRECT_URI used: {REDIRECT_URI}")
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


@app.route("/trigger_media", methods=["POST"])  # quick local test endpoint
def trigger_media():
    data = request.json or {}
    cmd = data.get("command")
    if cmd not in ("play", "pause", "next", "previous"):
        return jsonify({"error": "invalid command"}), 400
    # Try to run the media action locally
    if cmd in ("play", "pause"):
        media_play_pause()
    elif cmd == "next":
        media_next()
    elif cmd == "previous":
        media_previous()
    # Also emit via socketio to connected extension clients
    try:
        socketio.emit("command", {"command": cmd})
    except Exception as e:
        print(f"[TRIGGER MEDIA] socketio emit failed: {e}")
    return jsonify({"status": "triggered", "command": cmd})


@app.route("/auth", methods=["GET"])  # helper to return the auth URL (useful for manual copy/paste)
def auth_url():
    from control_media import get_auth_url
    try:
        url = get_auth_url()
    except Exception as e:
        print(f"[AUTH] Error constructing auth URL: {e}")
        return jsonify({"error": "failed to construct auth url", "details": str(e)}), 500
    print(f"[AUTH] Returning auth URL: {url}")
    return jsonify({"auth_url": url, "redirect_uri": REDIRECT_URI})


@app.route("/authpage", methods=["GET"])  # small HTML auth page with clickable link for convenience
def auth_page():
    from control_media import get_auth_url
    try:
        url = get_auth_url()
    except Exception as e:
        return f"<p>Error constructing auth URL: {e}</p>", 500
    html = f"""
    <!doctype html>
    <html>
        <head>
            <title>Spotify Auth</title>
        </head>
        <body>
            <h2>Spotify Authorization</h2>
            <p>Click the link below to log into Spotify and authorize the app:</p>
            <p><a href=\"{url}\" target=\"_blank\">Click here to authorize</a></p>
            <p>Redirect URI: <code>{REDIRECT_URI}</code></p>
        </body>
    </html>
    """
    return html


@app.route("/auth_status", methods=["GET"])  # simple status that shows auth config and token state
def auth_status():
    status = {"client_id": None, "redirect_uri": REDIRECT_URI, "tokens": False}
    if CLIENT_ID:
        status["client_id"] = CLIENT_ID[:4] + "..." + CLIENT_ID[-4:]
    if os.path.exists("tokens.json"):
        try:
            with open("tokens.json", "r") as f:
                t = json.load(f)
            status["tokens"] = True
            status["token_expires_in"] = t.get("expires_in")
            status["token_obtained_at"] = t.get("obtained_at")
        except Exception as e:
            status["tokens"] = False
            status["token_error"] = str(e)
    return jsonify(status)


def get_metadata():
    return {
        "title": title_data,
        "artist": artist_data,
        "album": album_data,
        "artwork": artwork_data
    }
# Track last sent artwork URL to avoid redundant sends
_last_artwork_url = None
_last_seen_artwork_url = None
_last_seen_artwork_ts = 0
_ARTWORK_TTL = float(os.getenv("ARTWORK_TTL_SECONDS", "5"))

# Cache for Spotify playback state (used to determine priority)
_spotify_playback_cache = {
    "is_active": False,
    "last_check": 0.0,
    "data": None
}
_SPOTIFY_PLAYBACK_CHECK_TTL = 2.0  # seconds

def _check_spotify_active() -> tuple:
    """
    Check if Spotify is actively playing using the API.
    Returns (is_active, playback_data) tuple.
    Cached to avoid excessive API calls.
    """
    global _spotify_playback_cache
    now = time.time()
    
    # Return cached result if fresh
    if (now - _spotify_playback_cache["last_check"]) < _SPOTIFY_PLAYBACK_CHECK_TTL:
        return (_spotify_playback_cache["is_active"], _spotify_playback_cache["data"])
    
    # Also check if Spotify.exe is running with audio
    spotify_audio_active = False
    try:
        spotify_audio_active = is_app_playing("spotify.exe")
    except Exception:
        pass
    
    if not spotify_audio_active:
        _spotify_playback_cache["is_active"] = False
        _spotify_playback_cache["data"] = None
        _spotify_playback_cache["last_check"] = now
        return (False, None)
    
    # Spotify app is playing audio - get playback state from API
    try:
        from control_media import load_tokens, authorized_req
        if authorized_req():
            tokens = load_tokens()
            access_token = tokens.get("access_token")
            resp = requests.get(
                "https://api.spotify.com/v1/me/player",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=2
            )
            if resp.status_code == 200:
                data = resp.json()
                is_playing = data.get("is_playing", False)
                _spotify_playback_cache["is_active"] = is_playing
                _spotify_playback_cache["data"] = data if is_playing else None
                _spotify_playback_cache["last_check"] = now
                return (is_playing, data if is_playing else None)
            elif resp.status_code == 204:
                # No active device
                _spotify_playback_cache["is_active"] = False
                _spotify_playback_cache["data"] = None
                _spotify_playback_cache["last_check"] = now
                return (False, None)
    except Exception as e:
        if SERIAL_DEBUG:
            print(f"[SPOTIFY CHECK] Error: {e}")
    
    _spotify_playback_cache["last_check"] = now
    return (_spotify_playback_cache["is_active"], _spotify_playback_cache["data"])


def get_media_snapshot():
    """
    Build a media snapshot from current metadata.
    
    PRIORITY: Spotify takes precedence over browser media (YouTube, etc.) when both are playing.
    Uses Spotify API for progress if Spotify is active, otherwise uses browser extension data.
    """
    global title_data, artist_data, album_data, artwork_data
    global position_data, duration_data, is_playing_data, media_source

    # Check if Spotify is actively playing - it takes priority
    spotify_active, spotify_data = _check_spotify_active()
    
    if spotify_active and spotify_data:
        # === SPOTIFY IS PLAYING - USE SPOTIFY DATA ===
        item = spotify_data.get("item", {})
        if item:
            # Extract Spotify metadata
            sp_title = item.get("name", "")
            sp_artists = item.get("artists", [])
            sp_artist = ", ".join(a.get("name", "") for a in sp_artists[:2]) if sp_artists else ""
            sp_album = item.get("album", {}).get("name", "")
            sp_duration = item.get("duration_ms", 0) // 1000
            sp_position = spotify_data.get("progress_ms", 0) // 1000
            sp_playing = spotify_data.get("is_playing", False)
            
            # Get artwork
            sp_images = item.get("album", {}).get("images", [])
            sp_artwork = sp_images[0].get("url") if sp_images else None
            
            media = {
                "title": sp_title,
                "artist": sp_artist,
                "album": sp_album,
                "position_seconds": sp_position,
                "duration_seconds": sp_duration,
                "is_playing": sp_playing,
                "source": "spotify",
                "has_artwork": sp_artwork is not None,
                "artwork_url": sp_artwork,
            }
            
            # Add queue data for Spotify (limit to 5 items to save ESP32 memory)
            try:
                queue_mgr = get_queue_manager()
                queue_data = queue_mgr.get_queue_for_esp(max_items=5)
                if queue_data.get("queue"):
                    media["queue"] = queue_data["queue"]
                if queue_data.get("playlist"):
                    media["playlist"] = queue_data["playlist"]
            except Exception as e:
                if SERIAL_DEBUG:
                    print(f"[QUEUE] Error getting queue: {e}")
            
            return media
    
    # === FALLBACK TO BROWSER EXTENSION DATA (YouTube, etc.) ===
    
    # If nothing is set yet, skip media entirely
    if not title_data and not artist_data and not album_data and not artwork_data:
        return None

    # Use browser extension data
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
        "source": media_source or "browser",
    }

    # Extract artwork URL - artwork_data can be a URL string or {"src": url} object
    artwork_url = None
    if artwork_data:
        if isinstance(artwork_data, dict) and "src" in artwork_data:
            artwork_url = artwork_data["src"]
        elif isinstance(artwork_data, str) and artwork_data.startswith("http"):
            artwork_url = artwork_data

    # Stabilize artwork against brief transients: if artwork disappears only briefly,
    # keep last seen artwork until TTL expires so the ESP doesn't flicker or lose image.
    global _last_seen_artwork_url, _last_seen_artwork_ts
    now_ts = time.time()
    if artwork_url:
        _last_seen_artwork_url = artwork_url
        _last_seen_artwork_ts = now_ts
    else:
        # If we have a last seen artwork and it's within TTL, use it instead
        if _last_seen_artwork_url and (now_ts - _last_seen_artwork_ts) <= _ARTWORK_TTL:
            artwork_url = _last_seen_artwork_url
    
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
        
        # Send in chunks (512 bytes per chunk to be safe), but use serial writer queue instead of direct writes
        CHUNK_SIZE = 512
        # Enqueue as priority messages - send small chunks to allow other writes to proceed
        for i in range(0, len(rgb565_b64), CHUNK_SIZE):
            chunk = rgb565_b64[i:i + CHUNK_SIZE]
            line = f"IMG:{chunk}\n"
            line_bytes = line.encode("utf-8")
            try:
                if _serial_priority_queue.full():
                    try:
                        _serial_priority_queue.get_nowait()
                        _serial_priority_queue.task_done()
                    except Exception:
                        pass
                _serial_priority_queue.put({"type":"artwork_chunk","payload": line_bytes, "url": url}, block=False)
            except Exception:
                # If priority queue is full or multiple serial writes, fallback to direct write as last resort
                try:
                    ser.write(line_bytes)
                    time.sleep(0.01)
                except Exception:
                    pass
        
        # Signal end of image - use priority queue
        try:
            end_line = b"IMG_END\n"
            if _serial_priority_queue.full():
                try:
                    _serial_priority_queue.get_nowait()
                    _serial_priority_queue.task_done()
                except Exception:
                    pass
            _serial_priority_queue.put({"type":"artwork_chunk","payload": end_line, "url": url}, block=False)
        except Exception:
            try:
                ser.write(b"IMG_END\n")
            except Exception:
                pass
        
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
        threading.Thread(target=_serial_writer_loop, daemon=True).start()

    # Prime psutil counters
    prime_psutil()

    # Start background system monitor (can be tuned: 0.05 = 20 Hz, 0.1 = 10 Hz)
    socketio.start_background_task(system_monitor_loop, 0.05)

    print("[SERVER RUNNING] Flask-SocketIO on port 8080...")
    # IMPORTANT: no reloader, single process => only one serial owner
    socketio.run(app, host="0.0.0.0", port=8080, debug=False, use_reloader=False)
  