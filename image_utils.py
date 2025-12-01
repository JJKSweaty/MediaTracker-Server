"""
Image utilities for downloading and converting artwork to RGB565 for ESP32 display.
"""

import io
import base64
import requests
from PIL import Image

# Enable AVIF support (YouTube often serves AVIF thumbnails)
try:
    import pillow_avif
except ImportError:
    pass  # AVIF support not available

# Target size for ESP32 display (80x80 pixels)
TARGET_WIDTH = 80
TARGET_HEIGHT = 80

# Cache to avoid re-downloading the same image
_last_url = None
_last_rgb565 = None
_last_png_b64 = None


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


def get_artwork_png_b64(artwork_url: str) -> str:
    """
    Download artwork, resize to 80x80, convert to RGB565, and return as base64 string.
    RGB565 is used because LVGL on ESP32 can display it directly without a PNG decoder.
    """
    global _last_url, _last_png_b64
    
    if not artwork_url:
        return None
    
    # Handle dict format {'src': 'url'}
    if isinstance(artwork_url, dict):
        artwork_url = artwork_url.get('src', '')
    
    if not artwork_url or not artwork_url.startswith('http'):
        return None
    
    # Return cached if same URL
    if artwork_url == _last_url and _last_png_b64 is not None:
        return _last_png_b64
    
    try:
        # For YouTube thumbnails, try to get a guaranteed JPEG URL
        # YouTube serves AVIF by default which PIL can't always handle
        url_to_fetch = artwork_url
        if 'ytimg.com' in artwork_url or 'youtube.com' in artwork_url:
            # YouTube thumbnail - convert to sddefault.jpg for reliable JPEG
            # Common patterns: vi/VIDEO_ID/xxx.jpg or vi_webp/VIDEO_ID/xxx.webp
            import re
            match = re.search(r'(?:vi|vi_webp)/([a-zA-Z0-9_-]{11})/', artwork_url)
            if match:
                video_id = match.group(1)
                # Use sddefault for good quality without AVIF
                url_to_fetch = f"https://img.youtube.com/vi/{video_id}/sddefault.jpg"
                print(f"[IMAGE] YouTube: Using JPEG thumbnail for {video_id}")
        
        # Request JPEG/PNG explicitly to avoid AVIF
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/jpeg,image/png,image/webp,image/*;q=0.8',
        }
        resp = requests.get(url_to_fetch, timeout=5, headers=headers)
        resp.raise_for_status()
        
        # Check if we got actual image data
        content_type = resp.headers.get('Content-Type', '')
        if resp.content[:4] == b'<htm' or resp.content[:5] == b'<!DOC':
            print(f"[IMAGE] Received HTML instead of image from {artwork_url[:50]}...")
            return None
        
        # Check we have enough data
        if len(resp.content) < 100:
            print(f"[IMAGE] Response too small ({len(resp.content)} bytes)")
            return None
        
        # Check for AVIF format (PIL doesn't support it natively)
        if b'ftypavif' in resp.content[:32] or 'avif' in content_type.lower():
            print(f"[IMAGE] Received AVIF format - trying alternate URL...")
            # For YouTube, try replacing hqdefault with mqdefault or sddefault
            # Or try adding a format parameter
            alt_url = artwork_url.replace('hqdefault', 'mqdefault')
            if alt_url != artwork_url:
                resp = requests.get(alt_url, timeout=5, headers=headers)
                resp.raise_for_status()
                if b'ftypavif' in resp.content[:32]:
                    print(f"[IMAGE] Still AVIF, skipping artwork")
                    return None
        
        # Load image from bytes
        img_bytes = io.BytesIO(resp.content)
        try:
            img = Image.open(img_bytes)
            img.load()  # Force load to catch any decoding errors
        except Exception as img_err:
            # Try to identify what we received
            preview = resp.content[:50]
            print(f"[IMAGE] Cannot open image ({len(resp.content)} bytes, type={content_type})")
            print(f"[IMAGE] First bytes: {preview}")
            return None
        
        # Convert to RGB if needed (handles RGBA, P, etc.)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize to target size
        img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)
        
        # Convert to RGB565 bytes
        rgb565_bytes = rgb_to_rgb565(img)
        
        # Base64 encode
        b64 = base64.b64encode(rgb565_bytes).decode("ascii")
        
        # Cache it
        _last_url = artwork_url
        _last_png_b64 = b64
        
        print(f"[IMAGE] RGB565 b64 size: {len(b64)} chars ({len(rgb565_bytes)} bytes)")
        return b64
        
    except requests.RequestException as e:
        print(f"[IMAGE] Network error fetching artwork: {e}")
        return None
    except Exception as e:
        print(f"[IMAGE] Error getting artwork: {e}")
        import traceback
        traceback.print_exc()
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
    global _last_url, _last_rgb565, _last_png_b64
    _last_url = None
    _last_rgb565 = None
    _last_png_b64 = None
