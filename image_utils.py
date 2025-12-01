"""
Image utilities for downloading and converting artwork to RGB565 for ESP32 display.
"""

import io
import base64
import requests
from PIL import Image

# Target size for ESP32 display (80x80 pixels)
TARGET_WIDTH = 80
TARGET_HEIGHT = 80

# Cache to avoid re-downloading the same image
_last_url = None
_last_rgb565 = None


def url_to_rgb565(url: str) -> bytes:
    """
    Download image from URL, resize to 80x80, convert to RGB565 bytes.
    Returns raw RGB565 bytes (12800 bytes for 80x80).
    """
    global _last_url, _last_rgb565
    
    # Return cached if same URL
    if url == _last_url and _last_rgb565 is not None:
        return _last_rgb565
    
    try:
        # Download image
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        
        # Open with PIL
        img = Image.open(io.BytesIO(resp.content))
        
        # Convert to RGB if needed
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize to target size (use LANCZOS for quality)
        img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)
        
        # Convert to RGB565
        rgb565_data = rgb_to_rgb565(img)
        
        # Cache it
        _last_url = url
        _last_rgb565 = rgb565_data
        
        return rgb565_data
        
    except Exception as e:
        print(f"[IMAGE] Error downloading/converting: {e}")
        return None


def rgb_to_rgb565(img: Image.Image) -> bytes:
    """
    Convert PIL Image to RGB565 bytes (little-endian for ESP32).
    """
    pixels = img.load()
    width, height = img.size
    
    result = bytearray(width * height * 2)
    idx = 0
    
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            
            # RGB565: RRRRRGGG GGGBBBBB
            # 5 bits red, 6 bits green, 5 bits blue
            r5 = (r >> 3) & 0x1F
            g6 = (g >> 2) & 0x3F
            b5 = (b >> 3) & 0x1F
            
            rgb565 = (r5 << 11) | (g6 << 5) | b5
            
            # Little-endian for ESP32
            result[idx] = rgb565 & 0xFF
            result[idx + 1] = (rgb565 >> 8) & 0xFF
            idx += 2
    
    return bytes(result)


def rgb565_to_base64(rgb565_data: bytes) -> str:
    """
    Convert RGB565 bytes to base64 string for serial transmission.
    """
    return base64.b64encode(rgb565_data).decode('ascii')


def get_artwork_rgb565_base64(url: str) -> str:
    """
    Main function: URL -> RGB565 -> base64 string.
    Returns None if failed.
    """
    if not url:
        return None
        
    rgb565_data = url_to_rgb565(url)
    if rgb565_data is None:
        return None
    
    return rgb565_to_base64(rgb565_data)


def clear_cache():
    """Clear the image cache."""
    global _last_url, _last_rgb565
    _last_url = None
    _last_rgb565 = None
