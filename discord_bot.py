"""
Discord Voice Monitor Bot for ESP32 Display

A proper Discord Bot that monitors voice channel activity.
Uses official discord.py library with Bot token (not selfbot).

Setup:
1. Create app at https://discord.com/developers/applications
2. Create Bot, copy token
3. Enable SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT in Bot settings
4. Invite bot to server with OAuth2 URL Generator (bot scope + View Channels + Connect + Speak permissions)
5. Set DISCORD_TOKEN in .env file
6. Set DISCORD_USER_ID to your Discord user ID to auto-join when you join
"""

import asyncio
import threading
import time
import os
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field

try:
    import discord
    from discord import Client, Intents, VoiceClient
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False
    print("[DiscordBot] discord.py not installed. Run: pip install discord.py")


@dataclass
class VoiceUser:
    """User in a voice channel."""
    user_id: int
    name: str
    muted: bool = False
    deafened: bool = False
    speaking: bool = False


@dataclass 
class VoiceState:
    """Current voice channel state."""
    in_call: bool = False
    channel_id: int = 0
    channel_name: str = ""
    guild_name: str = ""
    users: List[VoiceUser] = field(default_factory=list)
    self_muted: bool = False
    self_deafened: bool = False
    last_update: float = 0.0


class DiscordVoiceBot:
    """
    Discord Bot that monitors voice channels and can join/control voice.
    
    Features:
    - Auto-join when authorized user joins a voice channel
    - Server mute/deafen for users (requires Mute Members and Deafen Members permissions)
    - Soundboard audio playback
    """
    
    MAX_USERS = 5
    
    # Soundboard configuration
    SOUNDBOARD_SOUNDS = [
        ("Airhorn", "airhorn.mp3"),
        ("Sad Trombone", "sad_trombone.mp3"),
        ("Cricket", "cricket.mp3"),
        ("Rimshot", "rimshot.mp3"),
        ("Golf Clap", "golf_clap.mp3"),
        ("Quack", "quack.mp3"),
        ("Fart", "fart.mp3"),
        ("Ba Dum Tss", "ba_dum_tss.mp3"),
    ]
    
    def __init__(self, token: Optional[str] = None, authorized_user_id: Optional[int] = None):
        self.token = token or os.getenv("DISCORD_TOKEN", "")
        # The user ID to follow - bot joins when this user joins voice
        self.authorized_user_id = authorized_user_id or int(os.getenv("DISCORD_USER_ID", "0"))
        
        # Sounds folder path (next to this file or in project root)
        self.sounds_folder = os.path.join(os.path.dirname(__file__), "sounds")
        
        self._client: Optional[Client] = None
        self._voice_client: Optional[VoiceClient] = None
        self._voice_state = VoiceState()
        self._lock = threading.Lock()
        self._running = False
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._update_callback: Optional[Callable[[VoiceState], None]] = None
        
        # Track current voice channel
        self._current_channel_id: Optional[int] = None
        
        # Self state (for UI)
        self._self_muted = False
        self._self_deafened = False
    
    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def voice_members(self) -> Dict[int, dict]:
        """Get current voice members as dict for external access."""
        with self._lock:
            return {u.user_id: {"name": u.name, "muted": u.muted, "deafened": u.deafened, "speaking": u.speaking} 
                    for u in self._voice_state.users}
    
    def set_callback(self, callback: Callable[[VoiceState], None]) -> None:
        self._update_callback = callback
    
    def get_voice_state(self) -> VoiceState:
        with self._lock:
            return self._voice_state
    
    def start(self, token: Optional[str] = None) -> bool:
        if not HAS_DISCORD:
            print("[DiscordBot] discord.py not available")
            return False
        
        if token:
            self.token = token
        
        if not self.token:
            print("[DiscordBot] No token! Set DISCORD_TOKEN in .env")
            return False
        
        if self._running:
            print("[DiscordBot] Already running")
            return True
        
        self._running = True
        self._thread = threading.Thread(target=self._run_bot, daemon=True)
        self._thread.start()
        
        if self._ready.wait(timeout=30):
            print("[DiscordBot] Bot started and ready!")
            if self.authorized_user_id:
                print(f"[DiscordBot] Will auto-join when user ID {self.authorized_user_id} joins voice")
            return True
        else:
            print("[DiscordBot] Timeout waiting for bot to be ready")
            self._running = False
            return False
    
    def stop(self) -> None:
        self._running = False
        if self._loop and self._client:
            future = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
            try:
                future.result(timeout=5)
            except:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._ready.clear()
        print("[DiscordBot] Stopped")
    
    # === Voice Control Commands (called from main.py) ===
    
    def server_mute_user(self, user_index: int) -> None:
        """Toggle server mute for a user by index. Requires Mute Members permission."""
        if self._loop and self._current_channel_id:
            asyncio.run_coroutine_threadsafe(self._async_server_mute_user(user_index), self._loop)
    
    def server_deafen_user(self, user_index: int) -> None:
        """Toggle server deafen for a user by index. Requires Deafen Members permission."""
        if self._loop and self._current_channel_id:
            asyncio.run_coroutine_threadsafe(self._async_server_deafen_user(user_index), self._loop)
    
    def play_soundboard(self, sound_index: int) -> None:
        """Play a soundboard sound."""
        if self._loop and self._voice_client and self._voice_client.is_connected():
            asyncio.run_coroutine_threadsafe(self._async_play_sound(sound_index), self._loop)
    
    async def _async_server_mute_user(self, user_index: int) -> None:
        """Toggle server mute for a user by their index in the voice channel."""
        if not self._current_channel_id or not self._client:
            return
        
        channel = self._client.get_channel(self._current_channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return
        
        # Get users (excluding bot)
        users = [m for m in channel.members if m.id != self._client.user.id]
        
        if 0 <= user_index < len(users):
            member = users[user_index]
            
            try:
                # Toggle server mute using member.edit()
                current_mute = member.voice.mute if member.voice else False
                await member.edit(mute=not current_mute)
                action = "unmuted" if current_mute else "muted"
                print(f"[DiscordBot] Server {action} {member.display_name}")
                await self._update_current_channel_state()
            except discord.Forbidden:
                print(f"[DiscordBot] No permission to mute {member.display_name}. Bot needs 'Mute Members' permission.")
            except Exception as e:
                print(f"[DiscordBot] Mute user error: {e}")
    
    async def _async_server_deafen_user(self, user_index: int) -> None:
        """Toggle server deafen for a user by their index in the voice channel."""
        if not self._current_channel_id or not self._client:
            return
        
        channel = self._client.get_channel(self._current_channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return
        
        # Get users (excluding bot)
        users = [m for m in channel.members if m.id != self._client.user.id]
        
        if 0 <= user_index < len(users):
            member = users[user_index]
            
            try:
                # Toggle server deafen using member.edit()
                current_deafen = member.voice.deaf if member.voice else False
                await member.edit(deafen=not current_deafen)
                action = "undeafened" if current_deafen else "deafened"
                print(f"[DiscordBot] Server {action} {member.display_name}")
                await self._update_current_channel_state()
            except discord.Forbidden:
                print(f"[DiscordBot] No permission to deafen {member.display_name}. Bot needs 'Deafen Members' permission.")
            except Exception as e:
                print(f"[DiscordBot] Deafen user error: {e}")
    
    async def _async_play_sound(self, sound_index: int) -> None:
        """Play a soundboard sound via FFmpeg."""
        if not self._voice_client or not self._voice_client.is_connected():
            print("[DiscordBot] Not in voice channel, can't play sound")
            return
        
        if sound_index < 0 or sound_index >= len(self.SOUNDBOARD_SOUNDS):
            print(f"[DiscordBot] Invalid sound index: {sound_index}")
            return
        
        sound_name, sound_file = self.SOUNDBOARD_SOUNDS[sound_index]
        sound_path = os.path.join(self.sounds_folder, sound_file)
        
        if not os.path.exists(sound_path):
            print(f"[DiscordBot] Sound file not found: {sound_path}")
            print(f"[DiscordBot] Create a 'sounds' folder with {sound_file}")
            return
        
        # Stop any currently playing audio
        if self._voice_client.is_playing():
            self._voice_client.stop()
        
        try:
            # Play audio using FFmpeg
            audio_source = discord.FFmpegPCMAudio(sound_path)
            self._voice_client.play(audio_source, after=lambda e: print(f"[DiscordBot] Finished playing {sound_name}") if not e else print(f"[DiscordBot] Playback error: {e}"))
            print(f"[DiscordBot] Playing soundboard: {sound_name}")
        except Exception as e:
            print(f"[DiscordBot] Error playing sound: {e}")
            print("[DiscordBot] Make sure FFmpeg is installed and in your PATH")
    
    # === Bot Core ===
    
    def _run_bot(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        intents = Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True
        
        self._client = Client(intents=intents)
        
        @self._client.event
        async def on_ready():
            print(f"[DiscordBot] Logged in as {self._client.user} (ID: {self._client.user.id})")
            print(f"[DiscordBot] Connected to {len(self._client.guilds)} guild(s)")
            
            # Check if authorized user is already in a voice channel
            if self.authorized_user_id:
                for guild in self._client.guilds:
                    for vc in guild.voice_channels:
                        for member in vc.members:
                            if member.id == self.authorized_user_id:
                                print(f"[DiscordBot] Found authorized user in #{vc.name}, joining...")
                                await self._join_voice_channel(vc)
                                break
            
            self._ready.set()
        
        @self._client.event
        async def on_voice_state_update(member, before, after):
            await self._handle_voice_update(member, before, after)
        
        try:
            self._loop.run_until_complete(self._client.start(self.token))
        except discord.LoginFailure as e:
            print(f"[DiscordBot] Login failed: {e}")
        except Exception as e:
            print(f"[DiscordBot] Error: {e}")
        finally:
            self._running = False
            self._ready.clear()
    
    async def _handle_voice_update(self, member, before, after) -> None:
        """Handle voice state changes - auto-join/leave based on authorized user."""
        
        # Check if authorized user joined a channel
        if self.authorized_user_id and member.id == self.authorized_user_id:
            if after.channel and (not before.channel or before.channel.id != after.channel.id):
                # Authorized user joined a voice channel
                print(f"[DiscordBot] Authorized user joined #{after.channel.name}")
                await self._join_voice_channel(after.channel)
            elif before.channel and not after.channel:
                # Authorized user left voice
                print(f"[DiscordBot] Authorized user left voice, disconnecting...")
                await self._leave_voice()
        
        # Update state for current channel
        await self._update_current_channel_state()
    
    async def _join_voice_channel(self, channel) -> None:
        """Join a voice channel."""
        try:
            # Leave current channel if in one
            if self._voice_client and self._voice_client.is_connected():
                await self._voice_client.disconnect()
            
            # Join new channel
            self._voice_client = await channel.connect()
            self._current_channel_id = channel.id
            print(f"[DiscordBot] Joined voice channel: #{channel.name}")
            
            # Update state
            await self._update_current_channel_state()
            
        except Exception as e:
            print(f"[DiscordBot] Error joining voice: {e}")
    
    async def _leave_voice(self) -> None:
        """Leave current voice channel."""
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect()
        self._voice_client = None
        self._current_channel_id = None
        self._self_muted = False
        self._self_deafened = False
        
        # Update state to show not in call
        with self._lock:
            self._voice_state = VoiceState(in_call=False, last_update=time.time())
        
        if self._update_callback:
            try:
                self._update_callback(self._voice_state)
            except Exception as e:
                print(f"[DiscordBot] Callback error: {e}")
    
    async def _update_current_channel_state(self) -> None:
        """Update voice state for current channel."""
        if not self._current_channel_id or not self._client:
            return
        
        channel = self._client.get_channel(self._current_channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            with self._lock:
                self._voice_state = VoiceState(in_call=False, last_update=time.time())
            return
        
        members = channel.members
        users = []
        
        for member in members[:self.MAX_USERS]:
            # Skip the bot itself from the user list
            if member.id == self._client.user.id:
                continue
                
            vs = member.voice
            muted = False
            deafened = False
            if vs:
                muted = getattr(vs, 'self_mute', False) or getattr(vs, 'mute', False)
                deafened = getattr(vs, 'self_deaf', False) or getattr(vs, 'deaf', False)
            
            user = VoiceUser(
                user_id=member.id,
                name=member.display_name[:16],
                muted=muted,
                deafened=deafened,
                speaking=not muted
            )
            users.append(user)
        
        in_call = len(members) > 0
        
        with self._lock:
            self._voice_state = VoiceState(
                in_call=in_call,
                channel_id=channel.id,
                channel_name=channel.name[:20],
                guild_name=channel.guild.name[:16] if channel.guild else "",
                users=users,
                self_muted=self._self_muted,
                self_deafened=self._self_deafened,
                last_update=time.time()
            )
        
        if self._update_callback:
            try:
                self._update_callback(self._voice_state)
            except Exception as e:
                print(f"[DiscordBot] Callback error: {e}")
            except Exception as e:
                print(f"[DiscordBot] Callback error: {e}")
    
    def to_esp32_json(self) -> Dict[str, Any]:
        """
        Get compact JSON for ESP32.
        Keys: c=in_call, ch=channel, sm=self_muted, sd=self_deafened, u=users
        User keys: n=name, m=muted, d=deafened, s=speaking
        """
        with self._lock:
            state = self._voice_state
        
        users = []
        for u in state.users:
            users.append({
                "n": u.name[:12],
                "m": u.muted,
                "d": u.deafened,
                "s": u.speaking
            })
        
        return {
            "c": 1 if state.in_call else 0,
            "ch": state.channel_name,
            "sm": self._self_muted,
            "sd": self._self_deafened,
            "u": users
        }
    
    def to_full_json(self) -> Dict[str, Any]:
        """Get full JSON with readable keys."""
        with self._lock:
            state = self._voice_state
        
        users = []
        for u in state.users:
            users.append({
                "id": u.user_id,
                "name": u.name,
                "muted": u.muted,
                "deafened": u.deafened,
                "speaking": u.speaking
            })
        
        return {
            "in_call": state.in_call,
            "channel": state.channel_name,
            "guild": state.guild_name,
            "self_muted": self._self_muted,
            "self_deafened": self._self_deafened,
            "users": users,
            "last_update": state.last_update
        }


# === Global instance management ===
_bot_instance: Optional[DiscordVoiceBot] = None


def get_discord_bot() -> Optional[DiscordVoiceBot]:
    """Get the global bot instance."""
    global _bot_instance
    return _bot_instance


def init_discord_bot(token: Optional[str] = None, user_id: Optional[int] = None) -> Optional[DiscordVoiceBot]:
    """Initialize the global bot instance."""
    global _bot_instance
    
    if not HAS_DISCORD:
        print("[DiscordBot] discord.py not available")
        return None
    
    token = token or os.getenv("DISCORD_TOKEN", "")
    if not token:
        print("[DiscordBot] No DISCORD_TOKEN set")
        return None
    
    # Get user ID to follow
    user_id = user_id or int(os.getenv("DISCORD_USER_ID", "0"))
    
    _bot_instance = DiscordVoiceBot(token=token, authorized_user_id=user_id)
    return _bot_instance


def stop_discord_bot() -> None:
    """Stop the global bot instance."""
    global _bot_instance
    if _bot_instance:
        _bot_instance.stop()
        _bot_instance = None


# === CLI for testing ===
if __name__ == "__main__":
    import json
    from dotenv import load_dotenv
    
    load_dotenv()
    
    print("=" * 50)
    print("Discord Voice Monitor Bot - Test Mode")
    print("=" * 50)
    
    token = os.getenv("DISCORD_TOKEN")
    user_id = os.getenv("DISCORD_USER_ID")
    
    if not token:
        print("\nNo DISCORD_TOKEN found in .env file!")
        print("\nTo set up:")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Create New Application -> name it -> Create")
        print("3. Go to Bot -> Add Bot -> Copy Token")
        print("4. Go to Bot -> Enable 'SERVER MEMBERS INTENT'")
        print("5. Go to OAuth2 -> URL Generator:")
        print("   - Check 'bot' scope")
        print("   - Check 'View Channels', 'Connect', 'Speak' permissions")
        print("   - Copy URL and open in browser to invite bot")
        print("6. Add to .env: DISCORD_TOKEN=your_bot_token_here")
        print("7. Add to .env: DISCORD_USER_ID=your_discord_user_id")
        exit(1)
    
    if not user_id:
        print("\nNo DISCORD_USER_ID found!")
        print("Add to .env: DISCORD_USER_ID=your_discord_user_id")
        print("(Right-click yourself in Discord with Developer Mode on -> Copy ID)")
    
    def on_voice_change(state: VoiceState):
        print(f"\n--- Voice State Update ---")
        print(f"In call: {state.in_call}")
        if state.in_call:
            print(f"Channel: {state.channel_name} ({state.guild_name})")
            print(f"Users ({len(state.users)}):")
            for u in state.users:
                status = []
                if u.muted: status.append("muted")
                if u.deafened: status.append("deaf")
                if u.speaking: status.append("speaking")
                print(f"  - {u.name}: {', '.join(status) or 'normal'}")
    
    bot = init_discord_bot(token, int(user_id) if user_id else None)
    if not bot:
        print("Failed to create bot")
        exit(1)
    
    bot.set_callback(on_voice_change)
    
    print("\nStarting bot...")
    if not bot.start():
        print("Failed to start bot")
        exit(1)
    
    print("\n" + "=" * 50)
    print("Bot is running! Commands:")
    print("  s               - Show current state")
    print("  j               - Show JSON output (ESP32 format)")
    print("  m               - Toggle mute")
    print("  d               - Toggle deafen")
    print("  q               - Quit")
    print("=" * 50)
    
    try:
        while True:
            cmd = input("\n> ").strip().lower()
            
            if cmd == "q":
                break
            elif cmd == "s":
                state = bot.get_voice_state()
                print(f"In call: {state.in_call}")
                print(f"Channel: {state.channel_name}")
                print(f"Users: {len(state.users)}")
                for u in state.users:
                    print(f"  - {u.name} (muted={u.muted}, deaf={u.deafened})")
            elif cmd == "j":
                print(json.dumps(bot.to_esp32_json(), indent=2))
            elif cmd == "m":
                bot.toggle_mute()
            elif cmd == "d":
                bot.toggle_deafen()
            else:
                print("Unknown command. Use: s, j, m, d, or q")
    
    except KeyboardInterrupt:
        print("\nInterrupted")
    
    finally:
        print("Stopping bot...")
        stop_discord_bot()
        print("Done!")
