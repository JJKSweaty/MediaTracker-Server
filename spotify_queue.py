"""
Spotify Playlist and Queue Management

This module handles:
- Fetching and caching playlists
- Reading queue from Spotify
- Playlist modifications (reorder, remove, etc.)
- Queue state management for ESP32
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import time
import requests
import json
import os
import base64
from io import BytesIO

# Try to import PIL for thumbnail processing
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[WARN] PIL not installed - playlist thumbnails disabled. Install with: pip install Pillow")


@dataclass
class TrackItem:
    """Represents a track in a playlist or queue."""
    track_id: str               # Spotify track URI or local URI
    source: str                 # "spotify" or "local"
    name: str
    artist: str
    album: str
    duration_sec: int
    is_local: bool = False
    playlist_index: Optional[int] = None
    playlist_id: Optional[str] = None
    playlist_snapshot_id: Optional[str] = None
    playlist_is_collaborative: bool = False
    playlist_is_public: bool = False
    image_url: Optional[str] = None
    thumb_b64: Optional[str] = None  # Small JPEG base64 for ESP

    def to_esp_dict(self, max_str_len: int = 48) -> Dict[str, Any]:
        """Convert to ESP-friendly dictionary with truncated strings."""
        def truncate(s: str, n: int) -> str:
            if not s:
                return ""
            return s[:n] if len(s) <= n else s[:n-2] + ".."
        
        return {
            "id": truncate(self.track_id, 64),
            "source": self.source,
            "name": truncate(self.name, max_str_len),
            "artist": truncate(self.artist, max_str_len),
            "album": truncate(self.album, max_str_len),
            "duration_seconds": self.duration_sec,
            "is_local": self.is_local
        }


@dataclass
class SpotifyPlaylistContext:
    """Represents playlist metadata."""
    playlist_id: str
    name: str
    is_public: bool
    is_collaborative: bool
    owner_id: str
    snapshot_id: str
    total_tracks: int
    image_url_60: Optional[str] = None
    image_url_300: Optional[str] = None
    thumb_b64: Optional[str] = None  # Preprocessed 60x60 JPEG base64

    def to_esp_dict(self, max_str_len: int = 48) -> Dict[str, Any]:
        """Convert to ESP-friendly dictionary."""
        def truncate(s: str, n: int) -> str:
            if not s:
                return ""
            return s[:n] if len(s) <= n else s[:n-2] + ".."
        
        return {
            "id": self.playlist_id,
            "name": truncate(self.name, max_str_len),
            "is_public": self.is_public,
            "is_collaborative": self.is_collaborative,
            "total_tracks": self.total_tracks,
            "snapshot_id": self.snapshot_id,
            "image_thumb_jpg_b64": self.thumb_b64 or ""
        }


@dataclass
class QueueState:
    """Current playback queue state."""
    current_track: Optional[TrackItem] = None
    up_next: List[TrackItem] = field(default_factory=list)
    playlist_context: Optional[SpotifyPlaylistContext] = None
    last_updated: float = 0.0

    def to_esp_dict(self, max_queue_items: int = 10) -> Dict[str, Any]:
        """Convert to ESP-friendly dictionary."""
        result = {}
        
        if self.playlist_context:
            result["playlist"] = self.playlist_context.to_esp_dict()
        
        # Limit queue items for ESP memory
        queue_items = self.up_next[:max_queue_items]
        result["queue"] = [t.to_esp_dict() for t in queue_items]
        
        return result


class SpotifyQueueManager:
    """Manages Spotify playlists and queue for ESP32 display."""
    
    # Required scopes for full functionality
    REQUIRED_SCOPES = [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-public",
        "playlist-modify-private",
    ]
    
    # Optional but nice scopes
    OPTIONAL_SCOPES = [
        "user-read-recently-played",
        "user-top-read",
        "user-library-modify",  # For liking tracks
        "user-library-read",
    ]
    
    def __init__(self, tokens_path: str = "tokens.json"):
        self.tokens_path = tokens_path
        self.queue_state = QueueState()
        self._playlists_cache: List[SpotifyPlaylistContext] = []
        self._playlists_cache_time: float = 0.0
        self._queue_cache_time: float = 0.0
        self._CACHE_TTL = 5.0  # seconds
        self._PLAYLIST_CACHE_TTL = 60.0  # seconds
    
    def _load_tokens(self) -> Optional[Dict]:
        """Load tokens from file."""
        if not os.path.exists(self.tokens_path):
            return None
        try:
            with open(self.tokens_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[QUEUE] Error loading tokens: {e}")
            return None
    
    def _get_access_token(self) -> Optional[str]:
        """Get current access token."""
        tokens = self._load_tokens()
        if not tokens:
            return None
        return tokens.get("access_token")
    
    def _api_get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make a GET request to Spotify API."""
        token = self._get_access_token()
        if not token:
            print("[QUEUE] No access token")
            return None
        
        try:
            resp = requests.get(
                f"https://api.spotify.com/v1{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=5
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 204:
                return {}  # No content but success
            else:
                print(f"[QUEUE] API error {resp.status_code}: {resp.text[:100]}")
                return None
        except Exception as e:
            print(f"[QUEUE] API exception: {e}")
            return None
    
    def _api_post(self, endpoint: str, data: Optional[Dict] = None, json_body: Optional[Dict] = None) -> Optional[Dict]:
        """Make a POST request to Spotify API."""
        token = self._get_access_token()
        if not token:
            return None
        
        try:
            resp = requests.post(
                f"https://api.spotify.com/v1{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                json=json_body,
                timeout=5
            )
            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.content else {}
            else:
                print(f"[QUEUE] POST error {resp.status_code}: {resp.text[:100]}")
                return None
        except Exception as e:
            print(f"[QUEUE] POST exception: {e}")
            return None
    
    def _api_put(self, endpoint: str, json_body: Optional[Dict] = None, params: Optional[Dict] = None) -> bool:
        """Make a PUT request to Spotify API."""
        token = self._get_access_token()
        if not token:
            return False
        
        try:
            resp = requests.put(
                f"https://api.spotify.com/v1{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
                timeout=5
            )
            return resp.status_code in (200, 202, 204)
        except Exception as e:
            print(f"[QUEUE] PUT exception: {e}")
            return False
    
    def _api_delete(self, endpoint: str, json_body: Optional[Dict] = None) -> bool:
        """Make a DELETE request to Spotify API."""
        token = self._get_access_token()
        if not token:
            return False
        
        try:
            resp = requests.delete(
                f"https://api.spotify.com/v1{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                timeout=5
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f"[QUEUE] DELETE exception: {e}")
            return False
    
    def _download_thumbnail(self, url: str, size: int = 60) -> Optional[str]:
        """Download and resize image to small JPEG base64."""
        if not HAS_PIL or not url:
            return None
        
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                return None
            
            img = Image.open(BytesIO(resp.content))
            img = img.convert("RGB")
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=60)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            print(f"[QUEUE] Thumbnail error: {e}")
            return None
    
    def get_user_playlists(self, limit: int = 20, force_refresh: bool = False) -> List[SpotifyPlaylistContext]:
        """
        Get list of user's playlists.
        Cached for efficiency.
        """
        now = time.time()
        if not force_refresh and self._playlists_cache and (now - self._playlists_cache_time) < self._PLAYLIST_CACHE_TTL:
            return self._playlists_cache
        
        data = self._api_get("/me/playlists", {"limit": limit})
        if not data or "items" not in data:
            return self._playlists_cache  # Return stale cache on error
        
        playlists = []
        for item in data["items"]:
            if not item:
                continue
            
            # Get images (Spotify provides up to 3 sizes)
            images = item.get("images", [])
            img_60 = None
            img_300 = None
            for img in images:
                w = img.get("width") or 0
                h = img.get("height") or 0
                url = img.get("url")
                if not url:
                    continue
                if w <= 64 or h <= 64:
                    img_60 = url
                elif w <= 320 or h <= 320:
                    img_300 = url
                elif not img_300:
                    img_300 = url
            
            # Use first image as fallback
            if not img_60 and images:
                img_60 = images[-1].get("url") if len(images) > 0 else None
            if not img_300 and images:
                img_300 = images[0].get("url") if len(images) > 0 else None
            
            pl = SpotifyPlaylistContext(
                playlist_id=item.get("id", ""),
                name=item.get("name", "Untitled"),
                is_public=item.get("public", False) or False,
                is_collaborative=item.get("collaborative", False) or False,
                owner_id=item.get("owner", {}).get("id", ""),
                snapshot_id=item.get("snapshot_id", ""),
                total_tracks=item.get("tracks", {}).get("total", 0),
                image_url_60=img_60,
                image_url_300=img_300
            )
            playlists.append(pl)
        
        self._playlists_cache = playlists
        self._playlists_cache_time = now
        return playlists
    
    def get_playlist_tracks(self, playlist_id: str, limit: int = 50, offset: int = 0) -> List[TrackItem]:
        """Get tracks from a specific playlist."""
        data = self._api_get(f"/playlists/{playlist_id}/tracks", {
            "limit": limit,
            "offset": offset,
            "fields": "items(track(id,uri,name,artists,album,duration_ms,is_local),added_at),total"
        })
        
        if not data or "items" not in data:
            return []
        
        tracks = []
        for idx, item in enumerate(data["items"]):
            track = item.get("track")
            if not track:
                continue
            
            is_local = track.get("is_local", False)
            uri = track.get("uri", "")
            
            # Build artist string
            artists = track.get("artists", [])
            artist_str = ", ".join(a.get("name", "") for a in artists[:2]) if artists else "Unknown"
            
            # Album info
            album = track.get("album", {})
            album_name = album.get("name", "")
            
            # Get album image
            images = album.get("images", [])
            img_url = images[-1].get("url") if images else None
            
            t = TrackItem(
                track_id=uri,
                source="local" if is_local else "spotify",
                name=track.get("name", "Unknown"),
                artist=artist_str,
                album=album_name,
                duration_sec=track.get("duration_ms", 0) // 1000,
                is_local=is_local,
                playlist_index=offset + idx,
                playlist_id=playlist_id,
                image_url=img_url
            )
            tracks.append(t)
        
        return tracks
    
    def get_current_queue(self, force_refresh: bool = False) -> QueueState:
        """
        Get current playback queue from Spotify.
        Uses the /me/player/queue endpoint.
        """
        now = time.time()
        if not force_refresh and (now - self._queue_cache_time) < self._CACHE_TTL:
            return self.queue_state
        
        # Get queue from Spotify
        data = self._api_get("/me/player/queue")
        if not data:
            return self.queue_state
        
        # Parse currently playing
        current = data.get("currently_playing")
        current_track = None
        if current and current.get("type") == "track":
            is_local = current.get("is_local", False)
            artists = current.get("artists", [])
            artist_str = ", ".join(a.get("name", "") for a in artists[:2]) if artists else ""
            album = current.get("album", {})
            images = album.get("images", [])
            
            current_track = TrackItem(
                track_id=current.get("uri", ""),
                source="local" if is_local else "spotify",
                name=current.get("name", ""),
                artist=artist_str,
                album=album.get("name", ""),
                duration_sec=current.get("duration_ms", 0) // 1000,
                is_local=is_local,
                image_url=images[-1].get("url") if images else None
            )
        
        # Parse queue
        queue_items = []
        for item in data.get("queue", [])[:20]:  # Limit to 20 items
            if item.get("type") != "track":
                continue
            
            is_local = item.get("is_local", False)
            artists = item.get("artists", [])
            artist_str = ", ".join(a.get("name", "") for a in artists[:2]) if artists else ""
            album = item.get("album", {})
            images = album.get("images", [])
            
            t = TrackItem(
                track_id=item.get("uri", ""),
                source="local" if is_local else "spotify",
                name=item.get("name", ""),
                artist=artist_str,
                album=album.get("name", ""),
                duration_sec=item.get("duration_ms", 0) // 1000,
                is_local=is_local,
                image_url=images[-1].get("url") if images else None
            )
            queue_items.append(t)
        
        self.queue_state.current_track = current_track
        self.queue_state.up_next = queue_items
        self.queue_state.last_updated = now
        self._queue_cache_time = now
        
        return self.queue_state
    
    def set_active_playlist(self, playlist_id: str) -> bool:
        """Set a playlist as the active context and load its metadata."""
        # Get playlist details
        data = self._api_get(f"/playlists/{playlist_id}", {
            "fields": "id,name,public,collaborative,owner.id,snapshot_id,tracks.total,images"
        })
        
        if not data:
            return False
        
        images = data.get("images", [])
        img_60 = None
        img_300 = None
        for img in images:
            w = img.get("width") or 640
            url = img.get("url")
            if w <= 64:
                img_60 = url
            elif w <= 320:
                img_300 = url
        
        if not img_60 and images:
            img_60 = images[-1].get("url")
        if not img_300 and images:
            img_300 = images[0].get("url")
        
        # Download thumbnail
        thumb_b64 = self._download_thumbnail(img_60 or img_300, size=60) if (img_60 or img_300) else None
        
        self.queue_state.playlist_context = SpotifyPlaylistContext(
            playlist_id=data.get("id", playlist_id),
            name=data.get("name", "Playlist"),
            is_public=data.get("public", False) or False,
            is_collaborative=data.get("collaborative", False) or False,
            owner_id=data.get("owner", {}).get("id", ""),
            snapshot_id=data.get("snapshot_id", ""),
            total_tracks=data.get("tracks", {}).get("total", 0),
            image_url_60=img_60,
            image_url_300=img_300,
            thumb_b64=thumb_b64
        )
        
        return True
    
    # ========== PLAYBACK CONTROL ==========
    
    def play_track(self, track_uri: str, context_uri: Optional[str] = None) -> bool:
        """
        Start playback of a specific track.
        If context_uri is provided, plays within that playlist/album.
        """
        body = {}
        if context_uri:
            body["context_uri"] = context_uri
            body["offset"] = {"uri": track_uri}
        else:
            body["uris"] = [track_uri]
        
        return self._api_put("/me/player/play", json_body=body)
    
    def add_to_queue(self, track_uri: str) -> bool:
        """Add a track to the playback queue."""
        return self._api_post(f"/me/player/queue?uri={track_uri}") is not None
    
    def skip_to_next(self) -> bool:
        """Skip to next track."""
        return self._api_post("/me/player/next") is not None
    
    def skip_to_previous(self) -> bool:
        """Skip to previous track."""
        return self._api_post("/me/player/previous") is not None
    
    def set_shuffle(self, state: bool) -> bool:
        """Set shuffle mode on/off."""
        return self._api_put(f"/me/player/shuffle?state={'true' if state else 'false'}")
    
    def set_repeat(self, state: str) -> bool:
        """
        Set repeat mode.
        state: "track" (repeat one), "context" (repeat all), or "off"
        """
        if state not in ("track", "context", "off"):
            state = "off"
        return self._api_put(f"/me/player/repeat?state={state}")
    
    def check_saved_tracks(self, track_ids: list) -> list:
        """Check if tracks are saved in user's library. Returns list of booleans."""
        # Extract IDs if full URIs
        ids = []
        for tid in track_ids:
            if tid.startswith("spotify:track:"):
                ids.append(tid.split(":")[-1])
            else:
                ids.append(tid)
        
        if not ids:
            return []
        
        data = self._api_get("/me/tracks/contains", {"ids": ",".join(ids[:50])})  # Max 50
        return data if isinstance(data, list) else []
    
    # ========== PLAYLIST MODIFICATION ==========
    
    def remove_track_from_playlist(self, playlist_id: str, track_uri: str, snapshot_id: Optional[str] = None) -> Optional[str]:
        """
        Remove a track from a playlist.
        Returns new snapshot_id on success.
        """
        body = {
            "tracks": [{"uri": track_uri}]
        }
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        
        token = self._get_access_token()
        if not token:
            return None
        
        try:
            resp = requests.delete(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                new_snapshot = data.get("snapshot_id")
                if self.queue_state.playlist_context and self.queue_state.playlist_context.playlist_id == playlist_id:
                    self.queue_state.playlist_context.snapshot_id = new_snapshot
                return new_snapshot
            else:
                print(f"[QUEUE] Remove track error: {resp.status_code}")
                return None
        except Exception as e:
            print(f"[QUEUE] Remove track exception: {e}")
            return None
    
    def reorder_playlist_tracks(self, playlist_id: str, range_start: int, insert_before: int,
                                 range_length: int = 1, snapshot_id: Optional[str] = None) -> Optional[str]:
        """
        Reorder tracks in a playlist.
        Returns new snapshot_id on success.
        """
        body = {
            "range_start": range_start,
            "insert_before": insert_before,
            "range_length": range_length
        }
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        
        token = self._get_access_token()
        if not token:
            return None
        
        try:
            resp = requests.put(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                new_snapshot = data.get("snapshot_id")
                if self.queue_state.playlist_context and self.queue_state.playlist_context.playlist_id == playlist_id:
                    self.queue_state.playlist_context.snapshot_id = new_snapshot
                return new_snapshot
            else:
                print(f"[QUEUE] Reorder error: {resp.status_code}")
                return None
        except Exception as e:
            print(f"[QUEUE] Reorder exception: {e}")
            return None
    
    # ========== PLAYLIST FOLLOW/UNFOLLOW ==========
    
    def follow_playlist(self, playlist_id: str) -> bool:
        """Follow (save) a playlist."""
        return self._api_put(f"/playlists/{playlist_id}/followers")
    
    def unfollow_playlist(self, playlist_id: str) -> bool:
        """Unfollow a playlist."""
        return self._api_delete(f"/playlists/{playlist_id}/followers")
    
    # ========== LIBRARY (LIKES) ==========
    
    def save_track(self, track_id: str) -> bool:
        """Save a track to user's library (like it)."""
        # Track ID should be just the ID, not full URI
        if track_id.startswith("spotify:track:"):
            track_id = track_id.split(":")[-1]
        return self._api_put("/me/tracks", params={"ids": track_id})
    
    def remove_saved_track(self, track_id: str) -> bool:
        """Remove a track from user's library."""
        if track_id.startswith("spotify:track:"):
            track_id = track_id.split(":")[-1]
        return self._api_delete(f"/me/tracks?ids={track_id}")
    
    def add_to_playlist(self, playlist_id: str, track_uris: List[str]) -> bool:
        """Add tracks to a playlist.
        
        Args:
            playlist_id: Spotify playlist ID or URI
            track_uris: List of track URIs (e.g., ["spotify:track:xxx"])
        """
        # Extract playlist ID from URI if needed
        if playlist_id.startswith("spotify:playlist:"):
            playlist_id = playlist_id.split(":")[-1]
        
        # POST /playlists/{playlist_id}/tracks with body {"uris": [...]}
        return self._api_post(f"/playlists/{playlist_id}/tracks", {"uris": track_uris})
    
    # ========== RECENTLY PLAYED ==========
    
    def get_recently_played(self, limit: int = 10) -> List[TrackItem]:
        """Get recently played tracks."""
        data = self._api_get("/me/player/recently-played", {"limit": limit})
        if not data or "items" not in data:
            return []
        
        tracks = []
        for item in data["items"]:
            track = item.get("track", {})
            if not track:
                continue
            
            artists = track.get("artists", [])
            artist_str = ", ".join(a.get("name", "") for a in artists[:2]) if artists else ""
            album = track.get("album", {})
            images = album.get("images", [])
            
            t = TrackItem(
                track_id=track.get("uri", ""),
                source="spotify",
                name=track.get("name", ""),
                artist=artist_str,
                album=album.get("name", ""),
                duration_sec=track.get("duration_ms", 0) // 1000,
                image_url=images[-1].get("url") if images else None
            )
            tracks.append(t)
        
        return tracks
    
    def get_queue_for_esp(self, max_items: int = 10) -> Dict[str, Any]:
        """
        Get queue data formatted for ESP32.
        Fetches fresh queue if cache expired.
        """
        self.get_current_queue()
        return self.queue_state.to_esp_dict(max_queue_items=max_items)


# Global instance
_queue_manager: Optional[SpotifyQueueManager] = None


def get_queue_manager() -> SpotifyQueueManager:
    """Get or create the global queue manager instance."""
    global _queue_manager
    if _queue_manager is None:
        _queue_manager = SpotifyQueueManager()
    return _queue_manager


def get_all_required_scopes() -> str:
    """Get all required scopes as a space-separated string."""
    return " ".join(SpotifyQueueManager.REQUIRED_SCOPES + SpotifyQueueManager.OPTIONAL_SCOPES)
