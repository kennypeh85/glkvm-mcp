"""
KVM Screenshot Calibration Tool
================================
The GLKVM mouse_move_pct() is perfectly accurate, but vision models
(ZAI/GLM) cannot reliably estimate pixel coordinates from raw screenshots.

FIX: Overlay a coordinate grid (x% and y% labels) onto the screenshot.
The vision model reads the nearest grid label to the target element,
turning "estimate coordinates" into "read a number" — far more reliable.

Usage:
    python kvm_calibrate.py <input.jpg> <output.jpg>
    python kvm_calibrate.py <input.jpg>   # writes to <input>_grid.jpg
"""
import sys
import struct
from io import BytesIO

def read_jpeg_size(filepath):
    """Read JPEG dimensions without PIL."""
    with open(filepath, 'rb') as f:
        data = f.read()
    i = 0
    while i < len(data) - 1:
        if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC2):
            h = struct.unpack('>H', data[i + 5:i + 7])[0]
            w = struct.unpack('>H', data[i + 7:i + 9])[0]
            return w, h
        i += 1
    raise ValueError("Could not read JPEG dimensions")

def overlay_grid(input_path, output_path):
    """Overlay a 10% coordinate grid with labels onto a JPEG image."""
    # Read original dimensions
    w, h = read_jpeg_size(input_path)

    # Build SVG overlay
    svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    svg_parts.append(f'<image href="file:///{input_path}" width="{w}" height="{h}"/>')

    # Grid lines every 10%
    for pct in range(0, 101, 10):
        x_px = int(w * pct / 100)
        y_px = int(h * pct / 100)

        # Vertical line
        opacity_v = 0.7 if pct in (0, 50, 100) else 0.35
        svg_parts.append(
            f'<line x1="{x_px}" y1="0" x2="{x_px}" y2="{h}" '
            f'stroke="{"#FF0000" if pct in (0,50,100) else "#FF6600"}" '
            f'stroke-width="2" opacity="{opacity_v}"/>'
        )
        # X label at top
        svg_parts.append(
            f'<text x="{x_px}" y="18" fill="#FFFF00" font-size="20" '
            f'font-family="Consolas" font-weight="bold" text-anchor="middle" '
            f'stroke="black" stroke-width="1.5" paint-order="stroke">{pct}%</text>'
        )

        # Horizontal line
        opacity_h = 0.7 if pct in (0, 50, 100) else 0.35
        svg_parts.append(
            f'<line x1="0" y1="{y_px}" x2="{w}" y2="{y_px}" '
            f'stroke="{"#00FF00" if pct in (0,50,100) else "#00CC44"}" '
            f'stroke-width="2" opacity="{opacity_h}"/>'
        )
        # Y label at left
        svg_parts.append(
            f'<text x="5" y="{y_px + 6}" fill="#00FFFF" font-size="20" '
            f'font-family="Consolas" font-weight="bold" '
            f'stroke="black" stroke-width="1.5" paint-order="stroke">{pct}%</text>'
        )

    svg_parts.append('</svg>')
    svg = '\n'.join(svg_parts)

    # Use Inkscape/Edge/soffice to rasterize... or just use Pillow if available
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(input_path)
        draw = ImageDraw.Draw(img)

        for pct in range(0, 101, 10):
            x_px = int(w * pct / 100)
            y_px = int(h * pct / 100)

            color_v = (255, 0, 0) if pct in (0, 50, 100) else (255, 100, 0)
            color_h = (0, 255, 0) if pct in (0, 50, 100) else (0, 200, 80)

            # Vertical line
            draw.line([(x_px, 0), (x_px, h)], fill=color_v, width=2)
            # Horizontal line
            draw.line([(0, y_px), (w, y_px)], fill=color_h, width=2)

            # Labels with outline (draw black shadow first)
            for dx, dy in [(-1,-1),(-1,1),(1,-1),(1,1),(0,-1),(0,1),(-1,0),(1,0)]:
                draw.text((x_px + dx, 2 + dy), f"{pct}%", fill=(0,0,0), font=ImageFont.load_default())
                draw.text((3 + dx, y_px + dy), f"{pct}%", fill=(0,0,0), font=ImageFont.load_default())

            draw.text((x_px, 2), f"{pct}%", fill=(255,255,0), font=ImageFont.load_default(), anchor="mt")
            draw.text((3, y_px), f"{pct}%", fill=(0,255,255), font=ImageFont.load_default())

        img.save(output_path, quality=90)
        return True
    except ImportError:
        # No PIL — write SVG file instead
        svg_path = output_path.rsplit('.', 1)[0] + '.svg'
        with open(svg_path, 'w') as f:
            f.write(svg)
        print(f"PIL not available — wrote SVG overlay to {svg_path}")
        print("Install PIL for JPEG output: pip install Pillow")
        return False

if __name__ == '__main__':
    inp = sys.argv[1]
    if len(sys.argv) > 2:
        outp = sys.argv[2]
    else:
        outp = inp.rsplit('.', 1)[0] + '_grid.jpg'

    w, h = read_jpeg_size(inp)
    print(f"Input: {inp} ({w}x{h})")
    result = overlay_grid(inp, outp)
    if result:
        print(f"Grid overlay written to: {outp}")
    print(f"\nNow send the grid image to vision model with prompt:")
    print(f"'Look at the grid overlay. What x% and y% label is nearest to <target element>?'")
