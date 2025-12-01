import pythoncom
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioEndpointVolume, IAudioSessionManager2, IAudioSessionControl2, IAudioSessionEnumerator, IAudioSessionControl, IAudioMeterInformation,IAudioSessionEvents
import os
from dotenv import load_dotenv
import requests
import json
import base64
import webbrowser
import urllib.parse
import time
import sys
from comtypes import CLSCTX_ALL
import serial
load_dotenv()

spotify_data = {
    "title": "",
    "artist": "",
    "album": "",
    "artwork": "",
    "is_playing": False,
    "progress_ms": 0,
    "duration_ms": 0,
}


CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
encoded = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

# AUDIO SECTION STARTS HERE
def is_app_playing(app_name):
    pythoncom.CoInitialize()
    sessions = AudioUtilities.GetAllSessions()
    for session in sessions:
        if session.Process and session.Process.name().lower() == app_name.lower():
            volume = session._ctl.QueryInterface(ISimpleAudioVolume)
            if not volume.GetMute() and volume.GetMasterVolume() > 0:
                return True
    return False

def get_current_media():
    spotify_active = is_app_playing("spotify.exe")
    chrome_active = is_app_playing("chrome.exe")
    brave_active = is_app_playing("brave.exe")
    
    if spotify_active:
        return "Spotify"
    elif chrome_active:
        return "Chrome"
    elif brave_active:
        return "Brave"
    else:
        return "Unknown"

# This gets all sessions that can have audio
def get_all_media():
    sessions = AudioUtilities.GetAllSessions()
    print("\n[🎚 ACTIVE AUDIO SESSIONS]")
    for i, session in enumerate(sessions):
        if session.Process:
            vol = session._ctl.QueryInterface(ISimpleAudioVolume)
            print(f"{i+1}. {session.Process.name()} - Volume: {vol.GetMasterVolume()*100:.0f}% - Muted: {vol.GetMute()}")

# All application volume control
def set_volume(volume):
    try:
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            volume.SetMasterVolume(volume, None)
    except Exception as e:
        return f"Error: {str(e)}"
    return "Volume set successfully"






# SPOTIFY SECTION STARTS HERE
def Auth():
    encoded_redirect_uri = urllib.parse.quote(REDIRECT_URI, safe='')
    auth_url = f"https://accounts.spotify.com/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={encoded_redirect_uri}&scope=user-read-playback-state user-modify-playback-state"
    webbrowser.open(auth_url)

def getProfile():
    if authorized_req():
        tokens=load_tokens
        access_token = tokens["access_token"]
        response = requests.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code == 200:
            data = response.json()
        print(json.dumps(data, indent=3))

# Using this function to check if the token is still valid
def authorized_req():
    state=False
    while state==False:
        if not os.path.exists("tokens.json"):
            print("Error: No token.json file found")
            Auth()
        tokens=load_tokens()
        access_token = tokens["access_token"]
        # using a random spotify req to test if it needs to refreshed
        response = requests.get("https://api.spotify.com/v1/me/player/currently-playing", headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code == 200:
            state=True
            return state
        else:
            refresh()
            state=True
    return state

# It will refresh the token if its expired
def refresh():
    tokens=load_tokens()
    refresh_token = tokens["refresh_token"]
    response = requests.post("https://accounts.spotify.com/api/token", headers={"Authorization": "Basic "+encoded}, data={"grant_type":"refresh_token","refresh_token":refresh_token})
    # if valid api req then update the tokens
    if (response.status_code == 200):
        new_tokens = response.json()
        # checking if the refresh token is being sent in the response
        if "refresh_token" not in new_tokens:
            new_tokens["refresh_token"] = refresh_token
        save_tokens(new_tokens)
    # If even the refresh token is expired then reauthenticate the user
    else:
        print("Error: Could not refresh token")
        Auth()

def load_tokens():
    if not os.path.exists("tokens.json"):
        print("Error: No token.json file found")
        Auth()
    with open("tokens.json", "r") as f:
        return json.load(f)
    
def save_tokens(tokens):
    with open("tokens.json", "w") as f:
        json.dump(tokens, f, indent=3)

# Mainly trying to get the player info in a json format
def getPlayerInfo():
    if authorized_req:
        tokens=load_tokens()
        access_token = tokens["access_token"]
        response = requests.get("https://api.spotify.com/v1/me/player/currently-playing", headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code == 200:
            data = response.json()
            with open("player_info.json", "w") as f:
                json.dump(data, f, indent=3)
            # Storing the player info in a dictionary
            spotify_data["title"] = data["item"]["name"]
            spotify_data["artist"] = data["item"]["artists"][0]["name"]
            spotify_data["album"] = data["item"]["album"]["name"]
            spotify_data["artwork"] = data["item"]["album"]["images"][0]["url"]
            spotify_data["is_playing"] = data["is_playing"]
            # Add progress and duration (in milliseconds from API, convert to seconds)
            spotify_data["progress_ms"] = data.get("progress_ms", 0)
            spotify_data["duration_ms"] = data.get("item", {}).get("duration_ms", 0)
        else:
            Auth()
            print("Error: Could not get player info")
            print(response.text)

def printSpotifyInfo():
    # Getting the player info before i print it
    getPlayerInfo()
    print("\n--- [SPOTIFY PLAYER INFO] ---")
    print(f"Title: {spotify_data['title']}")
    print(f"Artist: {spotify_data['artist']}")
    print(f"Album: {spotify_data['album']}")
    print(f"Artwork: {spotify_data['artwork']}")
    print("----------------------------\n")


# Spotify play pause
def spotifyPlay():
    getPlayerInfo()
    if authorized_req():
        if not spotify_data["is_playing"]:
            tokens=load_tokens()
            access_token = tokens["access_token"]
            response = requests.put("https://api.spotify.com/v1/me/player/play", headers={"Authorization": f"Bearer {access_token}"})
            if response.ok:
                print("Playing")
            else:
                print("Error: Could not play")
                print(response.text)

def spotifyPause():
    getPlayerInfo()
    if authorized_req():
        if spotify_data["is_playing"]:
            tokens=load_tokens()
            access_token = tokens["access_token"]
            response = requests.put("https://api.spotify.com/v1/me/player/pause", headers={"Authorization": f"Bearer {access_token}"})
            if response.ok:
                print("Paused")
            else:
                print("Error: Could not pause")
                print(response.text)

def spotifyNext():
    getPlayerInfo()
    if authorized_req():
        tokens=load_tokens()
        access_token = tokens["access_token"]
        response = requests.post("https://api.spotify.com/v1/me/player/next", headers={"Authorization": f"Bearer {access_token}"})
        if response.ok:
            print("Next")
        else:
            print("Error: Could not play next")
            print(response.text)

def spotifyPrevious():
    getPlayerInfo()
    if authorized_req():
        tokens=load_tokens()
        access_token = tokens["access_token"]
        response = requests.post("https://api.spotify.com/v1/me/player/previous", headers={"Authorization": f"Bearer {access_token}"})
        if response.ok:
            print("Previous")
        else:
            print("Error: Could not play previous")
            print(response.text)

# this will work with a slider im not sure how tho
def spotifySeek(position_ms):
    getPlayerInfo()
    if authorized_req():
        tokens=load_tokens()
        access_token = tokens["access_token"]
        response = requests.put(f"https://api.spotify.com/v1/me/player/seek?position_ms={position_ms*1000}", headers={"Authorization": f"Bearer {access_token}"})
        if response.ok:
            print("Seeked")
        else:
            print("Error: Could not seek")
            print(response.text)

# Change it with hopefully the volume slider 
def spotifyVolume(volume_percent):
    getPlayerInfo()
    if authorized_req():
        tokens=load_tokens()
        access_token = tokens["access_token"]
        response = requests.put(f"https://api.spotify.com/v1/me/player/volume?volume_percent={volume_percent}", headers={"Authorization": f"Bearer {access_token}"})
        if response.ok:
            print("Volume changed")
        else:
            print("Error: Could not change volume")
            print(response.text)

# Cache for Spotify progress to avoid too many API calls
_spotify_cache = {
    "position": 0,
    "duration": 0,
    "is_playing": False,
    "last_fetch": 0,
    "last_position_update": 0
}
_SPOTIFY_CACHE_TTL = 2.0  # Only call API every 2 seconds

def get_spotify_progress():
    """
    Get current playback progress from Spotify API with caching.
    Returns (position_seconds, duration_seconds, is_playing) or (0, 0, False) on error.
    Interpolates position between API calls for smooth progress bar.
    """
    global _spotify_cache
    now = time.time()
    
    # If cached data is fresh enough, interpolate position
    if now - _spotify_cache["last_fetch"] < _SPOTIFY_CACHE_TTL:
        # Interpolate position if playing
        if _spotify_cache["is_playing"]:
            elapsed = now - _spotify_cache["last_position_update"]
            interpolated_pos = min(
                _spotify_cache["position"] + int(elapsed),
                _spotify_cache["duration"]
            )
            return (interpolated_pos, _spotify_cache["duration"], _spotify_cache["is_playing"])
        else:
            return (_spotify_cache["position"], _spotify_cache["duration"], _spotify_cache["is_playing"])
    
    # Fetch fresh data from API
    try:
        if authorized_req():
            tokens = load_tokens()
            access_token = tokens["access_token"]
            response = requests.get(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=2
            )
            if response.status_code == 200:
                data = response.json()
                progress_ms = data.get("progress_ms", 0)
                duration_ms = data.get("item", {}).get("duration_ms", 0)
                is_playing = data.get("is_playing", False)
                # Convert to seconds
                pos_sec = progress_ms // 1000
                dur_sec = duration_ms // 1000
                
                # Update cache
                _spotify_cache["position"] = pos_sec
                _spotify_cache["duration"] = dur_sec
                _spotify_cache["is_playing"] = is_playing
                _spotify_cache["last_fetch"] = now
                _spotify_cache["last_position_update"] = now
                
                return (pos_sec, dur_sec, is_playing)
            elif response.status_code == 204:
                # No content - nothing playing
                _spotify_cache["is_playing"] = False
                _spotify_cache["last_fetch"] = now
                return (0, 0, False)
            else:
                print(f"[SPOTIFY PROGRESS] API returned {response.status_code}")
        else:
            print("[SPOTIFY PROGRESS] Not authorized")
    except Exception as e:
        print(f"[SPOTIFY PROGRESS] Error: {e}")
    return (_spotify_cache["position"], _spotify_cache["duration"], _spotify_cache["is_playing"])

# SPOTIFY SECTION ENDS HERE


# ========================================
# KEYBOARD MEDIA KEY CONTROLS
# Works with any media player (Spotify, VLC, YouTube, etc.)
# ========================================
import ctypes

# Windows Virtual Key Codes for Media Keys
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

# Windows API constants
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

def press_media_key(vk_code):
    """Simulate a media key press using Windows API."""
    try:
        # Key down
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        time.sleep(0.05)
        # Key up
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        print(f"[MEDIA KEY] Error: {e}")
        return False

def media_play_pause():
    """Toggle play/pause for any active media player."""
    print("[MEDIA KEY] Play/Pause pressed")
    return press_media_key(VK_MEDIA_PLAY_PAUSE)

def media_next():
    """Skip to next track."""
    print("[MEDIA KEY] Next Track pressed")
    return press_media_key(VK_MEDIA_NEXT_TRACK)

def media_previous():
    """Go to previous track."""
    print("[MEDIA KEY] Previous Track pressed")
    return press_media_key(VK_MEDIA_PREV_TRACK)

def media_stop():
    """Stop playback."""
    print("[MEDIA KEY] Stop pressed")
    return press_media_key(VK_MEDIA_STOP)

def media_volume_up():
    """Increase system volume."""
    print("[MEDIA KEY] Volume Up pressed")
    return press_media_key(VK_VOLUME_UP)

def media_volume_down():
    """Decrease system volume."""
    print("[MEDIA KEY] Volume Down pressed")
    return press_media_key(VK_VOLUME_DOWN)

def media_mute():
    """Toggle mute."""
    print("[MEDIA KEY] Mute pressed")
    return press_media_key(VK_VOLUME_MUTE)

# ========================================
# KEYBOARD MEDIA KEY SECTION ENDS HERE
# ========================================