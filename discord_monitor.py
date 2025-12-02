"""
Discord Voice Call Monitor for ESP32 Display

Monitors Discord voice channel activity and provides data for ESP32 display.
Uses discord.py-self for user account monitoring.

WARNING: Using a user token (self-bot) is against Discord's Terms of Service.
Use at your own risk.
"""

import asyncio
import base64
import threading
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
import os

# Discord.py-self for user account (self-bot)
try:
    import discord
    from discord import Client, Intents
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False
    print("[Discord] discord.py not installed. Install with: pip install discord.py-self")

# For avatar downloading
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


@dataclass
class DiscordUser:
    """Discord user in voice channel."""
    user_id: int
    name: str
    discriminator: str = ""
    muted: bool = False         # Self-muted OR server-muted
    deafened: bool = False      # Self-deafened OR server-deafened
    self_muted: bool = False
    server_muted: bool = False
    self_deafened: bool = False
    server_deafened: bool = False
    speaking: bool = False      # Simulated - True if not muted
    avatar_b64: Optional[str] = None  # Base64 encoded avatar (small)
    avatar_hash: str = ""


@dataclass
class DiscordVoiceState:
    """Current Discord voice call state."""
    in_call: bool = False
    channel_id: int = 0
    channel_name: str = ""
    guild_id: int = 0
    guild_name: str = ""
    users: List[DiscordUser] = field(default_factory=list)
    self_user_id: int = 0
    self_muted: bool = False
    self_deafened: bool = False
    last_update: float = 0.0


class DiscordMonitor:
    """
    Monitors Discord voice calls and provides state updates.
    
    Usage:
        monitor = DiscordMonitor(token="YOUR_USER_TOKEN")
        monitor.set_update_callback(my_callback)
        monitor.start()
        
        # Later...
        state = monitor.get_voice_state()
        monitor.stop()
    """
    
    MAX_USERS = 5  # Maximum users to track (for ESP32 memory)
    AVATAR_SIZE = 64  # Avatar size in pixels
    
    def __init__(self, token: Optional[str] = None):
        """
        Initialize Discord monitor.
        
        Args:
            token: Discord user token. If None, reads from DISCORD_TOKEN env var.
        """
        self.token = token or os.getenv("DISCORD_TOKEN", "")
        self._client: Optional[Client] = None
        self._voice_state = DiscordVoiceState()
        self._avatar_cache: Dict[int, tuple] = {}  # {user_id: (avatar_hash, bytes)}
        self._update_callback: Optional[Callable[[DiscordVoiceState], None]] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        
    def set_update_callback(self, callback: Callable[[DiscordVoiceState], None]) -> None:
        """Set callback function to be called when voice state changes."""
        self._update_callback = callback
    
    def get_voice_state(self) -> DiscordVoiceState:
        """Get current voice state (thread-safe)."""
        with self._lock:
            return self._voice_state
    
    def is_ready(self) -> bool:
        """Check if Discord client is ready."""
        return self._ready.is_set()
    
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running
    
    def start(self) -> bool:
        """
        Start the Discord monitor in a background thread.
        Returns True if started successfully.
        """
        if not HAS_DISCORD:
            print("[Discord] discord.py not available")
            return False
        
        if not self.token:
            print("[Discord] No token provided. Set DISCORD_TOKEN env var or pass token to constructor.")
            return False
        
        if self._running:
            print("[Discord] Already running")
            return True
        
        self._running = True
        self._thread = threading.Thread(target=self._run_client, daemon=True)
        self._thread.start()
        
        # Wait for client to be ready (with timeout)
        if self._ready.wait(timeout=30):
            print("[Discord] Monitor started successfully")
            return True
        else:
            print("[Discord] Timeout waiting for client to be ready")
            return False
    
    def stop(self) -> None:
        """Stop the Discord monitor."""
        self._running = False
        if self._loop and self._client:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        self._ready.clear()
        print("[Discord] Monitor stopped")
    
    def _run_client(self) -> None:
        """Run the Discord client in a dedicated thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        # Create client with required intents
        intents = Intents.default()
        intents.voice_states = True
        intents.presences = True
        intents.members = True
        intents.guilds = True
        
        self._client = Client(intents=intents)
        
        @self._client.event
        async def on_ready():
            print(f"[Discord] Logged in as {self._client.user} (ID: {self._client.user.id})")
            self._voice_state.self_user_id = self._client.user.id
            
            # Check if already in a voice channel
            for guild in self._client.guilds:
                me = guild.me
                if me and me.voice and me.voice.channel:
                    await self._update_voice_state(me.voice.channel)
                    break
            
            self._ready.set()
        
        @self._client.event
        async def on_voice_state_update(member, before, after):
            await self._handle_voice_state_update(member, before, after)
        
        try:
            self._loop.run_until_complete(self._client.start(self.token))
        except Exception as e:
            print(f"[Discord] Client error: {e}")
        finally:
            self._running = False
            self._ready.clear()
    
    async def _handle_voice_state_update(self, member, before, after) -> None:
        """Handle voice state changes."""
        if not self._client or not self._client.user:
            return
        
        current_channel = None
        
        # Get current voice channel we're tracking
        with self._lock:
            if self._voice_state.channel_id:
                for guild in self._client.guilds:
                    channel = guild.get_channel(self._voice_state.channel_id)
                    if channel:
                        current_channel = channel
                        break
        
        # Self user voice state changed
        if member.id == self._client.user.id:
            if before.channel is None and after.channel is not None:
                # Joined a voice channel
                print(f"[Discord] Joined voice channel: {after.channel.name}")
                await self._update_voice_state(after.channel)
            elif before.channel is not None and after.channel is None:
                # Left voice channel
                print("[Discord] Left voice channel")
                await self._clear_voice_state()
            elif after.channel != before.channel and after.channel is not None:
                # Moved to different channel
                print(f"[Discord] Moved to voice channel: {after.channel.name}")
                await self._update_voice_state(after.channel)
            else:
                # Self mute/deaf changed
                await self._update_voice_state(after.channel if after.channel else current_channel)
            return
        
        # Other member's voice state changed
        if current_channel:
            chan_before = before.channel
            chan_after = after.channel
            
            # Check if the change affects our channel
            if ((chan_before and chan_before.id == current_channel.id) or
                (chan_after and chan_after.id == current_channel.id)):
                await self._update_voice_state(current_channel)
    
    async def _update_voice_state(self, channel) -> None:
        """Update voice state for the given channel."""
        if not channel:
            await self._clear_voice_state()
            return
        
        users = []
        
        # Get members in the channel
        members = channel.members if hasattr(channel, 'members') else []
        
        # Filter out self and limit to MAX_USERS
        other_members = [m for m in members if m.id != self._client.user.id][:self.MAX_USERS]
        
        for member in other_members:
            vs = member.voice
            if not vs:
                continue
            
            # Get mute/deaf status
            self_muted = vs.self_mute if hasattr(vs, 'self_mute') else False
            server_muted = vs.mute if hasattr(vs, 'mute') else False
            self_deafened = vs.self_deaf if hasattr(vs, 'self_deaf') else False
            server_deafened = vs.deaf if hasattr(vs, 'deaf') else False
            
            muted = self_muted or server_muted
            deafened = self_deafened or server_deafened
            
            # Get avatar
            avatar_b64 = await self._get_avatar_b64(member)
            avatar_hash = str(member.avatar.key) if member.avatar else ""
            
            user = DiscordUser(
                user_id=member.id,
                name=member.display_name[:16],  # Truncate for ESP32
                discriminator=str(member.discriminator) if hasattr(member, 'discriminator') else "",
                muted=muted,
                deafened=deafened,
                self_muted=self_muted,
                server_muted=server_muted,
                self_deafened=self_deafened,
                server_deafened=server_deafened,
                speaking=not muted,  # Simulated speaking indicator
                avatar_b64=avatar_b64,
                avatar_hash=avatar_hash
            )
            users.append(user)
        
        # Get self user's mute/deaf status
        self_vs = None
        for guild in self._client.guilds:
            me = guild.me
            if me and me.voice and me.voice.channel and me.voice.channel.id == channel.id:
                self_vs = me.voice
                break
        
        self_muted = False
        self_deafened = False
        if self_vs:
            self_muted = (self_vs.self_mute if hasattr(self_vs, 'self_mute') else False) or \
                         (self_vs.mute if hasattr(self_vs, 'mute') else False)
            self_deafened = (self_vs.self_deaf if hasattr(self_vs, 'self_deaf') else False) or \
                           (self_vs.deaf if hasattr(self_vs, 'deaf') else False)
        
        # Update state
        with self._lock:
            self._voice_state = DiscordVoiceState(
                in_call=True,
                channel_id=channel.id,
                channel_name=channel.name[:24],  # Truncate for ESP32
                guild_id=channel.guild.id if channel.guild else 0,
                guild_name=channel.guild.name[:16] if channel.guild else "",
                users=users,
                self_user_id=self._client.user.id,
                self_muted=self_muted,
                self_deafened=self_deafened,
                last_update=time.time()
            )
        
        # Notify callback
        if self._update_callback:
            try:
                self._update_callback(self._voice_state)
            except Exception as e:
                print(f"[Discord] Callback error: {e}")
    
    async def _clear_voice_state(self) -> None:
        """Clear voice state (left channel)."""
        with self._lock:
            self._voice_state = DiscordVoiceState(
                in_call=False,
                self_user_id=self._client.user.id if self._client and self._client.user else 0,
                last_update=time.time()
            )
        
        if self._update_callback:
            try:
                self._update_callback(self._voice_state)
            except Exception as e:
                print(f"[Discord] Callback error: {e}")
    
    async def _get_avatar_b64(self, member) -> Optional[str]:
        """Get member's avatar as base64 encoded string."""
        try:
            # Check cache first
            avatar_hash = str(member.avatar.key) if member.avatar else "default"
            if member.id in self._avatar_cache:
                cached_hash, cached_b64 = self._avatar_cache[member.id]
                if cached_hash == avatar_hash:
                    return cached_b64
            
            # Get avatar asset
            avatar_asset = member.display_avatar
            
            # Request specific size and format
            if hasattr(avatar_asset, 'with_size'):
                avatar_asset = avatar_asset.with_size(self.AVATAR_SIZE)
            if hasattr(avatar_asset, 'with_format'):
                # Use PNG for static, GIF for animated
                fmt = "gif" if (hasattr(avatar_asset, 'is_animated') and avatar_asset.is_animated()) else "png"
                avatar_asset = avatar_asset.with_format(fmt)
            
            # Download avatar bytes
            avatar_bytes = await avatar_asset.read()
            
            # Convert to base64
            avatar_b64 = base64.b64encode(avatar_bytes).decode('ascii')
            
            # Cache it
            self._avatar_cache[member.id] = (avatar_hash, avatar_b64)
            
            return avatar_b64
        except Exception as e:
            print(f"[Discord] Failed to get avatar for {member.name}: {e}")
            return None
    
    def to_json_dict(self) -> Dict[str, Any]:
        """Convert current voice state to JSON-serializable dict for ESP32."""
        with self._lock:
            state = self._voice_state
        
        users_list = []
        for user in state.users:
            users_list.append({
                "id": user.user_id,
                "name": user.name,
                "muted": user.muted,
                "deafened": user.deafened,
                "speaking": user.speaking,
                "avatar": user.avatar_b64  # Can be None or large - consider omitting for bandwidth
            })
        
        return {
            "discord": {
                "in_call": state.in_call,
                "channel": state.channel_name,
                "guild": state.guild_name,
                "self_muted": state.self_muted,
                "self_deafened": state.self_deafened,
                "users": users_list
            }
        }
    
    def to_esp32_json(self, include_avatars: bool = False) -> Dict[str, Any]:
        """
        Convert to compact JSON for ESP32 (without avatars by default).
        Avatars can be sent separately if needed.
        """
        with self._lock:
            state = self._voice_state
        
        users_list = []
        for user in state.users:
            u = {
                "n": user.name[:12],  # Short name
                "m": user.muted,
                "d": user.deafened,
                "s": user.speaking
            }
            if include_avatars and user.avatar_b64:
                u["a"] = user.avatar_b64
            users_list.append(u)
        
        return {
            "discord": {
                "c": 1 if state.in_call else 0,  # in_call
                "ch": state.channel_name[:16] if state.in_call else "",
                "sm": state.self_muted,
                "sd": state.self_deafened,
                "u": users_list
            }
        }


# Global instance
_discord_monitor: Optional[DiscordMonitor] = None


def get_discord_monitor() -> Optional[DiscordMonitor]:
    """Get the global Discord monitor instance."""
    global _discord_monitor
    return _discord_monitor


def init_discord_monitor(token: Optional[str] = None) -> Optional[DiscordMonitor]:
    """Initialize and return the global Discord monitor."""
    global _discord_monitor
    
    if not HAS_DISCORD:
        print("[Discord] discord.py not available - Discord monitoring disabled")
        return None
    
    token = token or os.getenv("DISCORD_TOKEN", "")
    if not token:
        print("[Discord] No DISCORD_TOKEN found - Discord monitoring disabled")
        return None
    
    _discord_monitor = DiscordMonitor(token=token)
    return _discord_monitor


def stop_discord_monitor() -> None:
    """Stop the global Discord monitor."""
    global _discord_monitor
    if _discord_monitor:
        _discord_monitor.stop()
        _discord_monitor = None


# Test/debug code
if __name__ == "__main__":
    import json
    
    def on_update(state: DiscordVoiceState):
        print(f"\n=== Voice State Update ===")
        print(f"In call: {state.in_call}")
        if state.in_call:
            print(f"Channel: {state.channel_name} ({state.guild_name})")
            print(f"Self: muted={state.self_muted}, deafened={state.self_deafened}")
            print(f"Users ({len(state.users)}):")
            for u in state.users:
                print(f"  - {u.name}: muted={u.muted}, deaf={u.deafened}, speaking={u.speaking}")
    
    monitor = init_discord_monitor()
    if monitor:
        monitor.set_update_callback(on_update)
        if monitor.start():
            print("Discord monitor running. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                stop_discord_monitor()
        else:
            print("Failed to start Discord monitor")
    else:
        print("Failed to initialize Discord monitor")
