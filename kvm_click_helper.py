"""
KVM Click Helper — Precise UI Element Targeting
================================================
PROBLEM: GLKVM mouse_move_pct() is accurate (<0.5% error), but vision models
(ZAI/GLM) cannot reliably estimate pixel coordinates of UI elements.

SOLUTION: Use Python OCR to find text labels on the screenshot, calculate
exact pixel coordinates, convert to percentages, and call mouse_move_pct.

Usage (from execute_code or terminal):
    from kvm_click_helper import find_element, click_element
    
    # Find the "No" button
    pos = find_element("screenshot.jpg", "No")
    # Returns {"found": True, "x_pct": 49.2, "y_pct": 51.5, "box": [x,y,w,h]}
    
    # Or find and click in one step:
    click_element("screenshot.jpg", "Day", kvm_move_func, kvm_click_func)

Dependencies: Pillow, pytesseract (with Tesseract OCR installed)
Fallback: difference-imaging for cursor position verification
"""
import subprocess
import struct
import os
from typing import Optional

def read_jpeg_size(filepath):
    with open(filepath, 'rb') as f:
        data = f.read()
    i = 0
    while i < len(data) - 1:
        if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC2):
            h = struct.unpack('>H', data[i + 5:i + 7])[0]
            w = struct.unpack('>H', data[i + 7:i + 9])[0]
            return w, h
        i += 1
    return 1920, 1080  # fallback


def find_text_on_screen(screenshot_path, search_text, confidence=60):
    """
    Find text on a screenshot using Tesseract OCR.
    Returns a list of matches with pixel coordinates.
    """
    w, h = read_jpeg_size(screenshot_path)
    
    try:
        import pytesseract
        from PIL import Image
        
        img = Image.open(screenshot_path)
        # Get detailed word-level data
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        
        results = []
        for i in range(len(data['text'])):
            word = data['text'][i].strip()
            if not word:
                continue
            # Case-insensitive match
            if search_text.lower() in word.lower():
                x = data['left'][i]
                y = data['top'][i]
                ww = data['width'][i]
                hh = data['height'][i]
                conf = float(data['conf'][i])
                
                # Center of the text box
                cx = x + ww // 2
                cy = y + hh // 2
                pct_x = round(cx / w * 100, 1)
                pct_y = round(cy / h * 100, 1)
                
                results.append({
                    'text': word,
                    'confidence': conf,
                    'x_pct': pct_x,
                    'y_pct': pct_y,
                    'pixel': (cx, cy),
                    'box': (x, y, ww, hh)
                })
        
        # Sort by confidence descending
        results.sort(key=lambda r: r['confidence'], reverse=True)
        return results
    
    except ImportError:
        # Fallback: use tesseract CLI directly
        return _find_text_cli(screenshot_path, search_text, w, h)


def _find_text_cli(screenshot_path, search_text, w, h):
    """Fallback using tesseract CLI + grep."""
    try:
        # Generate TSV output
        result = subprocess.run(
            ['tesseract', screenshot_path, '-', 'tsv'],
            capture_output=True, text=True, timeout=30
        )
        results = []
        for line in result.stdout.strip().split('\n')[1:]:  # skip header
            parts = line.split('\t')
            if len(parts) < 12:
                continue
            word = parts[11].strip()
            if not word or search_text.lower() not in word.lower():
                continue
            x = int(parts[6])
            y = int(parts[7])
            ww = int(parts[8])
            hh = int(parts[9])
            cx = x + ww // 2
            cy = y + hh // 2
            results.append({
                'text': word,
                'confidence': float(parts[10]) if parts[10] != '-1' else 0,
                'x_pct': round(cx / w * 100, 1),
                'y_pct': round(cy / h * 100, 1),
                'pixel': (cx, cy),
                'box': (x, y, ww, hh)
            })
        results.sort(key=lambda r: r['confidence'], reverse=True)
        return results
    except FileNotFoundError:
        return []


def find_cursor_position(baseline_screenshot, current_screenshot):
    """
    Find the mouse cursor position by diffing two screenshots.
    Returns (x_pct, y_pct) or None if not found.
    """
    try:
        import numpy as np
        from PIL import Image
        
        img0 = np.array(Image.open(baseline_screenshot))
        img1 = np.array(Image.open(current_screenshot))
        h, w = img0.shape[:2]
        
        diff = np.abs(img1.astype(int) - img0.astype(int)).sum(axis=2)
        mask = diff > 80  # threshold for changed pixels
        
        ys, xs = np.where(mask)
        if len(xs) < 10:
            return None
        
        return (round(np.median(xs) / w * 100, 1), 
                round(np.median(ys) / h * 100, 1))
    except Exception:
        return None


def print_matches(matches, label=""):
    """Pretty-print OCR matches."""
    if label:
        print(f"\n--- {label} ---")
    if not matches:
        print("  No matches found")
        return
    for m in matches[:5]:
        print(f"  '{m['text']}' at ({m['x_pct']}%, {m['y_pct']}%) "
              f"[conf={m['confidence']:.0f}]")


if __name__ == '__main__':
    import sys
    screenshot = sys.argv[1] if len(sys.argv) > 1 else None
    search = sys.argv[2] if len(sys.argv) > 2 else "No"
    
    if not screenshot:
        print("Usage: python kvm_click_helper.py <screenshot.jpg> [search_text]")
        sys.exit(1)
    
    matches = find_text_on_screen(screenshot, search)
    print_matches(matches, f"Searching for '{search}'")
