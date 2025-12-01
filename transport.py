"""
Transport abstraction for ESP32 communication.
Supports Serial (USB) and TCP (WiFi) transports.
"""

import socket
import threading
import queue
import time
from abc import ABC, abstractmethod
from typing import Optional, Callable

import serial
from serial import SerialException


class Transport(ABC):
    """Abstract base class for ESP32 transport."""
    
    @abstractmethod
    def send_line(self, data: bytes) -> bool:
        """Send data to ESP32. Returns True on success."""
        raise NotImplementedError
    
    @abstractmethod
    def recv_line(self, timeout: float = 0.1) -> Optional[str]:
        """Receive a line from ESP32. Returns None if no data."""
        raise NotImplementedError
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        raise NotImplementedError
    
    @abstractmethod
    def close(self) -> None:
        """Close the transport."""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Transport name for logging."""
        raise NotImplementedError


class SerialTransport(Transport):
    """Serial (USB) transport for ESP32."""
    
    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
    
    def connect(self) -> bool:
        """Open the serial port."""
        try:
            with self._lock:
                if self._serial is not None:
                    self._serial.close()
                self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout)
                print(f"[SerialTransport] Connected to {self.port} at {self.baud}")
                return True
        except SerialException as e:
            print(f"[SerialTransport] Failed to open {self.port}: {e}")
            return False
    
    def send_line(self, data: bytes) -> bool:
        with self._lock:
            if self._serial is None:
                return False
            try:
                self._serial.write(data)
                return True
            except Exception as e:
                print(f"[SerialTransport] Write error: {e}")
                return False
    
    def recv_line(self, timeout: float = 0.1) -> Optional[str]:
        with self._lock:
            if self._serial is None:
                return None
            try:
                if self._serial.in_waiting:
                    line = self._serial.readline().decode("utf-8", errors="ignore").strip()
                    return line if line else None
            except Exception as e:
                print(f"[SerialTransport] Read error: {e}")
        return None
    
    def is_connected(self) -> bool:
        with self._lock:
            return self._serial is not None and self._serial.is_open
    
    def close(self) -> None:
        with self._lock:
            if self._serial:
                self._serial.close()
                self._serial = None
    
    @property
    def name(self) -> str:
        return f"Serial({self.port})"
    
    @property
    def in_waiting(self) -> int:
        """Check bytes waiting (for compatibility)."""
        with self._lock:
            if self._serial:
                return self._serial.in_waiting
        return 0


class TcpServerTransport(Transport):
    """TCP Server transport - ESP32 connects to this server."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 5555):
        self.host = host
        self.port = port
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
        self._client_addr = None
        self._lock = threading.Lock()
        self._running = False
        self._accept_thread: Optional[threading.Thread] = None
        self._recv_buffer = ""
    
    def start_server(self) -> bool:
        """Start the TCP server and wait for connections."""
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind((self.host, self.port))
            self._server_socket.listen(1)
            self._server_socket.settimeout(1.0)  # Non-blocking accept
            self._running = True
            
            # Start accept thread
            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._accept_thread.start()
            
            print(f"[TcpServerTransport] Listening on {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"[TcpServerTransport] Failed to start server: {e}")
            return False
    
    def _accept_loop(self):
        """Background thread to accept connections."""
        while self._running:
            try:
                if self._client_socket is not None:
                    # Already have a client, wait
                    time.sleep(0.5)
                    continue
                
                client, addr = self._server_socket.accept()
                with self._lock:
                    self._client_socket = client
                    self._client_socket.settimeout(0.1)
                    self._client_addr = addr
                    self._recv_buffer = ""
                print(f"[TcpServerTransport] ESP32 connected from {addr}")
                
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"[TcpServerTransport] Accept error: {e}")
                time.sleep(1)
    
    def send_line(self, data: bytes) -> bool:
        with self._lock:
            if self._client_socket is None:
                return False
            try:
                self._client_socket.sendall(data)
                return True
            except Exception as e:
                print(f"[TcpServerTransport] Send error: {e}")
                self._disconnect_client()
                return False
    
    def recv_line(self, timeout: float = 0.1) -> Optional[str]:
        with self._lock:
            if self._client_socket is None:
                return None
            try:
                # Check for complete line in buffer
                if '\n' in self._recv_buffer:
                    line, self._recv_buffer = self._recv_buffer.split('\n', 1)
                    return line.strip()
                
                # Try to receive more data
                try:
                    data = self._client_socket.recv(4096)
                    if data:
                        self._recv_buffer += data.decode("utf-8", errors="ignore")
                        # Check again for complete line
                        if '\n' in self._recv_buffer:
                            line, self._recv_buffer = self._recv_buffer.split('\n', 1)
                            return line.strip()
                    else:
                        # Connection closed
                        print("[TcpServerTransport] Client disconnected (recv returned empty)")
                        self._disconnect_client()
                except socket.timeout:
                    pass
            except Exception as e:
                print(f"[TcpServerTransport] Recv error: {e}")
                self._disconnect_client()
        return None
    
    def _disconnect_client(self):
        """Disconnect current client (must be called with lock held)."""
        if self._client_socket:
            try:
                self._client_socket.close()
            except:
                pass
            self._client_socket = None
            self._client_addr = None
            self._recv_buffer = ""
            print("[TcpServerTransport] Client disconnected, waiting for reconnect...")
    
    def is_connected(self) -> bool:
        with self._lock:
            return self._client_socket is not None
    
    def close(self) -> None:
        self._running = False
        with self._lock:
            if self._client_socket:
                self._client_socket.close()
                self._client_socket = None
            if self._server_socket:
                self._server_socket.close()
                self._server_socket = None
    
    @property
    def name(self) -> str:
        addr = self._client_addr if self._client_addr else "waiting"
        return f"TCP({self.host}:{self.port}, client={addr})"


class TransportManager:
    """
    Manages transport selection and provides unified interface.
    Can run both Serial and TCP simultaneously.
    """
    
    def __init__(self, debug: bool = True):
        self.debug = debug
        self._transports: list[Transport] = []
        self._active_transport: Optional[Transport] = None
        self._send_queue = queue.Queue()
        self._priority_queue = queue.Queue(maxsize=2)
        self._running = False
        self._writer_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._command_callback: Optional[Callable[[dict], None]] = None
        self._lock = threading.Lock()
    
    def add_serial(self, port: str, baud: int = 115200) -> Optional[SerialTransport]:
        """Add a serial transport."""
        transport = SerialTransport(port, baud)
        if transport.connect():
            self._transports.append(transport)
            if self._active_transport is None:
                self._active_transport = transport
            return transport
        return None
    
    def add_tcp_server(self, host: str = "0.0.0.0", port: int = 5555) -> Optional[TcpServerTransport]:
        """Add a TCP server transport."""
        transport = TcpServerTransport(host, port)
        if transport.start_server():
            self._transports.append(transport)
            # TCP becomes active when it has a connected client
            return transport
        return None
    
    def set_command_callback(self, callback: Callable[[dict], None]):
        """Set callback for received commands from ESP32."""
        self._command_callback = callback
    
    def queue_send(self, data: bytes, priority: bool = False, metadata: dict = None):
        """Queue data to be sent to ESP32."""
        item = {"payload": data, "metadata": metadata or {}}
        if priority:
            try:
                self._priority_queue.put_nowait(item)
            except queue.Full:
                pass  # Drop if priority queue is full
        else:
            # Limit queue size
            if self._send_queue.qsize() < 8:
                self._send_queue.put(item)
    
    def start(self):
        """Start writer and reader threads."""
        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._writer_thread.start()
        self._reader_thread.start()
        print("[TransportManager] Started writer and reader threads")
    
    def stop(self):
        """Stop all threads and close transports."""
        self._running = False
        for t in self._transports:
            t.close()
        self._transports.clear()
    
    def _get_active_transport(self) -> Optional[Transport]:
        """Get the best active transport (prefer TCP if connected)."""
        # Check for connected TCP transport first
        for t in self._transports:
            if isinstance(t, TcpServerTransport) and t.is_connected():
                return t
        # Fall back to serial
        for t in self._transports:
            if isinstance(t, SerialTransport) and t.is_connected():
                return t
        return None
    
    def _writer_loop(self):
        """Background thread to send data."""
        last_log_time = 0
        while self._running:
            try:
                transport = self._get_active_transport()
                if transport is None:
                    time.sleep(0.1)
                    continue
                
                # Handle priority queue first
                try:
                    item = self._priority_queue.get_nowait()
                    if item and transport.send_line(item["payload"]):
                        if self.debug:
                            meta = item.get("metadata", {})
                            if meta.get("type") == "artwork":
                                print(f"[TransportManager] Sent artwork via {transport.name}")
                    self._priority_queue.task_done()
                    continue
                except queue.Empty:
                    pass
                
                # Handle normal queue
                try:
                    item = self._send_queue.get(timeout=0.1)
                    if item and transport.send_line(item["payload"]):
                        pass  # Sent successfully
                    self._send_queue.task_done()
                except queue.Empty:
                    pass
                
                # Log queue size periodically
                now = time.time()
                if self.debug and (now - last_log_time) > 2:
                    qsize = self._send_queue.qsize()
                    if qsize > 0:
                        print(f"[TransportManager] Queue size: {qsize}")
                    last_log_time = now
                    
            except Exception as e:
                if self.debug:
                    print(f"[TransportManager] Writer error: {e}")
                time.sleep(0.1)
    
    def _reader_loop(self):
        """Background thread to receive commands."""
        while self._running:
            try:
                # Try to read from all transports
                for transport in self._transports:
                    if not transport.is_connected():
                        continue
                    
                    line = transport.recv_line()
                    if line:
                        # Try to parse as JSON command
                        try:
                            import json
                            cmd = json.loads(line)
                            if isinstance(cmd, dict):
                                if self.debug:
                                    cmd_type = cmd.get("cmd") or cmd.get("type")
                                    print(f"[TransportManager] Received command: {cmd_type}")
                                if self._command_callback:
                                    self._command_callback(cmd)
                        except:
                            # Not JSON, just debug output
                            if self.debug and line:
                                print(f"[SERIAL IN] {line}")
                
                time.sleep(0.01)
            except Exception as e:
                if self.debug:
                    print(f"[TransportManager] Reader error: {e}")
                time.sleep(0.1)
    
    @property
    def is_connected(self) -> bool:
        """Check if any transport is connected."""
        return self._get_active_transport() is not None


# Global instance
_transport_manager: Optional[TransportManager] = None


def get_transport_manager() -> TransportManager:
    """Get the global transport manager instance."""
    global _transport_manager
    if _transport_manager is None:
        _transport_manager = TransportManager()
    return _transport_manager
