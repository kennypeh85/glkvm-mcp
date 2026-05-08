#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]>=1.2",
#     "websockets>=12",
#     "httpx>=0.27",
# ]
# ///
"""
GLKVM MCP Server
================

Exposes a GL.iNet GLKVM (firmware 1.9.0+, PiKVM-fork) device's keyboard, mouse,
and screenshot capabilities as MCP tools.

The keyboard tools apply a fix for the well-known "stuck key / double-typing"
bug present in firmware <= 1.9.0 (gl-inet/glkvm issue #52, forum #64940):

  * every character is sent as keydown -> >=MIN_DOWN_UP_GAP ms -> keyup(finish=true)
  * modifiers in chords wrap strictly outside the main key (mods down -> key
    down -> key up -> mods up), matching the noVNC pattern
  * a watchdog releases any key still tracked as held after >STALE_MS ms

Scope: LAN only. TLS verification is disabled because the device ships a
self-signed certificate. Do not expose this server's stdio to a remote agent
without first confirming the target host is on a trusted network.

Run: `uv run glkvm_mcp.py`   (or `python glkvm_mcp.py` after `pip install` of
the dependencies above).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TypedDict
from urllib.parse import urlparse

import httpx
import websockets
from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_DOWN_UP_GAP_S = 0.025      # 25 ms minimum between keydown and keyup
INTER_CHAR_GAP_S = 0.010       # 10 ms between successive characters in send_text
MOD_GAP_S = 0.005              # 5 ms between modifier and main key
STALE_S = 0.250                # release a held key if not refreshed in 250 ms
WATCHDOG_PERIOD_S = 0.040
WS_PING_PERIOD_S = 1.0
HTTP_TIMEOUT_S = 10.0

LOG = logging.getLogger("glkvm_mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Key map: printable ASCII -> (W3C KeyboardEvent.code, needs_shift)
# US layout. Covers the characters most users actually type.
# ---------------------------------------------------------------------------
def _build_keymap() -> dict[str, tuple[str, bool]]:
    m: dict[str, tuple[str, bool]] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m[c] = (f"Key{c.upper()}", False)
        m[c.upper()] = (f"Key{c.upper()}", True)
    # Digits row
    digits = "0123456789"
    shift_digits = ")!@#$%^&*("
    for i, d in enumerate(digits):
        m[d] = (f"Digit{d}", False)
        m[shift_digits[i]] = (f"Digit{d}", True)
    # Symbols
    extras = {
        " ": ("Space", False),
        "\t": ("Tab", False),
        "\n": ("Enter", False),
        "\r": ("Enter", False),
        "-": ("Minus", False), "_": ("Minus", True),
        "=": ("Equal", False), "+": ("Equal", True),
        "[": ("BracketLeft", False), "{": ("BracketLeft", True),
        "]": ("BracketRight", False), "}": ("BracketRight", True),
        "\\": ("Backslash", False), "|": ("Backslash", True),
        ";": ("Semicolon", False), ":": ("Semicolon", True),
        "'": ("Quote", False), '"': ("Quote", True),
        ",": ("Comma", False), "<": ("Comma", True),
        ".": ("Period", False), ">": ("Period", True),
        "/": ("Slash", False), "?": ("Slash", True),
        "`": ("Backquote", False), "~": ("Backquote", True),
    }
    m.update(extras)
    return m


CHAR_TO_KEY = _build_keymap()

# Aliases accepted in send_keys() chord strings. Maps lowercase user input ->
# canonical W3C name used by GLKVM kvmd.
KEY_ALIASES: dict[str, str] = {
    # modifiers
    "ctrl": "ControlLeft", "control": "ControlLeft",
    "lctrl": "ControlLeft", "rctrl": "ControlRight",
    "shift": "ShiftLeft", "lshift": "ShiftLeft", "rshift": "ShiftRight",
    "alt": "AltLeft", "lalt": "AltLeft", "ralt": "AltRight",
    "altgr": "AltRight", "option": "AltLeft", "opt": "AltLeft",
    "meta": "MetaLeft", "lmeta": "MetaLeft", "rmeta": "MetaRight",
    "win": "MetaLeft", "windows": "MetaLeft",
    "cmd": "MetaLeft", "command": "MetaLeft",
    "super": "MetaLeft",
    # navigation
    "esc": "Escape", "escape": "Escape",
    "enter": "Enter", "return": "Enter",
    "tab": "Tab", "space": "Space", " ": "Space",
    "backspace": "Backspace", "bs": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown", "pgdown": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "capslock": "CapsLock", "caps": "CapsLock",
    "numlock": "NumLock", "scrolllock": "ScrollLock", "scroll": "ScrollLock",
    "printscreen": "PrintScreen", "prtsc": "PrintScreen", "prtscn": "PrintScreen",
    "pause": "Pause", "break": "Pause",
    "menu": "ContextMenu", "contextmenu": "ContextMenu",
}
for i in range(1, 13):
    KEY_ALIASES[f"f{i}"] = f"F{i}"

MOUSE_BUTTONS = {"left", "right", "middle", "up", "down"}
MODIFIER_KEYS = {
    "ControlLeft", "ControlRight",
    "ShiftLeft", "ShiftRight",
    "AltLeft", "AltRight",
    "MetaLeft", "MetaRight",
}


def resolve_key_name(name: str) -> str:
    """Accept user-friendly key names or W3C codes and return the canonical W3C code."""
    if not name:
        raise ValueError("empty key name")
    # Already a W3C-style code
    if name in CHAR_TO_KEY.values() or name.startswith(("Key", "Digit", "Numpad", "Arrow", "F")):
        return name
    return KEY_ALIASES.get(name.lower(), name)


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------
@dataclass
class Connection:
    base_url: str  # e.g. "https://192.168.8.55"
    http: httpx.AsyncClient
    ws: websockets.WebSocketClientProtocol
    held: dict[str, float] = field(default_factory=dict)  # key -> down_at (monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watchdog: Optional[asyncio.Task] = None
    pinger: Optional[asyncio.Task] = None


_conn: Optional[Connection] = None


def _require_conn() -> Connection:
    if _conn is None:
        raise RuntimeError("Not connected. Call kvm_connect(host, username, password) first.")
    return _conn


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _watchdog_loop(conn: Connection) -> None:
    """Force-release any key tracked as held but not refreshed within STALE_S."""
    try:
        while True:
            await asyncio.sleep(WATCHDOG_PERIOD_S)
            now = time.monotonic()
            stale = [k for k, t in conn.held.items() if now - t > STALE_S]
            for k in stale:
                LOG.warning("watchdog releasing stale key %s", k)
                try:
                    async with conn.send_lock:
                        await _ws_send_key(conn, k, state=False, finish=True)
                except Exception as e:  # noqa: BLE001
                    LOG.error("watchdog send failed for %s: %s", k, e)
                conn.held.pop(k, None)
    except asyncio.CancelledError:
        return


async def _pinger_loop(conn: Connection) -> None:
    """Send periodic ping bytes so kvmd doesn't drop us at 15 missed pings."""
    try:
        while True:
            await asyncio.sleep(WS_PING_PERIOD_S)
            try:
                await conn.ws.send(b"\x00")
            except Exception:  # noqa: BLE001
                return
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Low-level WS senders (assume caller holds send_lock when ordering matters)
# ---------------------------------------------------------------------------
async def _ws_send_key(conn: Connection, key: str, state: bool, finish: bool = False) -> None:
    import json
    payload = json.dumps({
        "event_type": "key",
        "event": {"key": key, "state": bool(state), "finish": bool(finish)},
    })
    await conn.ws.send(payload)
    if state:
        conn.held[key] = time.monotonic()
    else:
        conn.held.pop(key, None)


async def _ws_send_mouse_button(conn: Connection, button: str, state: bool) -> None:
    import json
    if button not in MOUSE_BUTTONS:
        raise ValueError(f"unknown mouse button: {button}")
    await conn.ws.send(json.dumps({
        "event_type": "mouse_button",
        "event": {"button": button, "state": bool(state)},
    }))


async def _ws_send_mouse_move(conn: Connection, x: int, y: int) -> None:
    """Absolute move. x, y must be in PiKVM-normalized int16 range -32768..32767."""
    import json
    x = max(-32768, min(32767, int(x)))
    y = max(-32768, min(32767, int(y)))
    await conn.ws.send(json.dumps({
        "event_type": "mouse_move",
        "event": {"to": {"x": x, "y": y}},
    }))


async def _ws_send_mouse_wheel(conn: Connection, dx: int, dy: int) -> None:
    import json
    dx = max(-127, min(127, int(dx)))
    dy = max(-127, min(127, int(dy)))
    await conn.ws.send(json.dumps({
        "event_type": "mouse_wheel",
        "event": {"delta": {"x": dx, "y": dy}, "squash": False},
    }))


# ---------------------------------------------------------------------------
# Higher-level keyboard helpers (with fix logic)
# ---------------------------------------------------------------------------
async def _atomic_press(conn: Connection, key: str, hold_s: float = MIN_DOWN_UP_GAP_S) -> None:
    """Send keydown -> sleep -> keyup(finish=True). Caller holds send_lock."""
    await _ws_send_key(conn, key, state=True, finish=False)
    await asyncio.sleep(max(hold_s, MIN_DOWN_UP_GAP_S))
    await _ws_send_key(conn, key, state=False, finish=True)


async def _press_with_modifiers(conn: Connection, key: str, modifiers: list[str]) -> None:
    """Mods down -> key atomic-press -> mods up. Caller holds send_lock."""
    for m in modifiers:
        await _ws_send_key(conn, m, state=True, finish=False)
        await asyncio.sleep(MOD_GAP_S)
    await _atomic_press(conn, key)
    for m in reversed(modifiers):
        await _ws_send_key(conn, m, state=False, finish=True)
        await asyncio.sleep(MOD_GAP_S)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("glkvm")


# ---- Connection management ------------------------------------------------
class ConnectResult(TypedDict):
    connected: bool
    host: str
    message: str


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_connect(host: str, password: str, username: str = "admin") -> ConnectResult:
    """
    Connect to a GLKVM device on the LAN and authenticate.

    Args:
        host: device hostname or IP (e.g. "192.168.8.55" or "glkvm.local"). HTTPS assumed.
        password: KVM web admin password (no default — must be supplied by the caller).
        username: KVM web admin username (default "admin").

    Returns:
        connected, host, message.
    """
    global _conn
    if _conn is not None:
        await kvm_disconnect()  # type: ignore[func-returns-value]

    # Normalize host into a base URL
    if "://" in host:
        base_url = host.rstrip("/")
    else:
        base_url = f"https://{host}"
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise ValueError(f"invalid host: {host}")

    # Self-signed cert is expected on a LAN-only device.
    http = httpx.AsyncClient(verify=False, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
    try:
        login = await http.post(
            f"{base_url}/api/auth/login",
            data={"user": username, "passwd": password, "expire": "0"},
        )
        login.raise_for_status()
        if "auth_token" not in http.cookies and login.json().get("two_step_required"):
            raise RuntimeError("Two-step login required; not implemented in this build.")
        token = http.cookies.get("auth_token")
        if not token:
            raise RuntimeError("Login succeeded but no auth_token cookie returned.")
    except Exception as e:  # noqa: BLE001
        await http.aclose()
        raise RuntimeError(f"login failed against {base_url}: {e}") from e

    # Open the control WebSocket. Pass the token as a query param so we don't
    # have to fight the websockets lib's cookie handling.
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/api/ws?auth_token={token}&stream=false"
    import ssl as _ssl
    sslctx = _ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = _ssl.CERT_NONE
    ws = await websockets.connect(
        ws_url,
        ssl=sslctx if ws_scheme == "wss" else None,
        max_size=8 * 1024 * 1024,
        open_timeout=HTTP_TIMEOUT_S,
        ping_interval=None,  # we manage our own pings
    )

    conn = Connection(base_url=base_url, http=http, ws=ws)
    conn.watchdog = asyncio.create_task(_watchdog_loop(conn), name="glkvm-watchdog")
    conn.pinger = asyncio.create_task(_pinger_loop(conn), name="glkvm-pinger")
    _conn = conn

    LOG.info("connected to %s", base_url)
    return ConnectResult(connected=True, host=base_url, message="ok")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def kvm_disconnect() -> dict:
    """Close the WebSocket and HTTP session. Releases any held keys first."""
    global _conn
    if _conn is None:
        return {"connected": False, "message": "already disconnected"}
    conn = _conn

    # Release everything we know is held
    try:
        async with conn.send_lock:
            for k in list(conn.held.keys()):
                try:
                    await _ws_send_key(conn, k, state=False, finish=True)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    for task in (conn.watchdog, conn.pinger):
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    try:
        await conn.ws.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await conn.http.aclose()
    except Exception:  # noqa: BLE001
        pass
    _conn = None
    return {"connected": False, "message": "disconnected"}


# ---- Keyboard tools -------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_text(text: str, wpm: int = 200) -> dict:
    """
    Type a string on the remote machine using the bug-fix-aware atomic press
    pattern (each char is sent as keydown -> 25 ms gap -> keyup(finish=True)).

    Args:
        text: the literal text to type. Newlines are sent as Enter.
        wpm: approximate words-per-minute (used to pace inter-character gap).
            Use 0 for as-fast-as-possible.

    Returns:
        {"chars": <count>, "skipped": [<unsupported chars>], "elapsed_s": ...}
    """
    conn = _require_conn()
    inter = max(0.0, 60.0 / max(1, wpm) / 5.0) if wpm > 0 else 0.0
    start = time.monotonic()
    sent = 0
    skipped: list[str] = []
    async with conn.send_lock:
        for ch in text:
            mapping = CHAR_TO_KEY.get(ch)
            if mapping is None:
                skipped.append(ch)
                continue
            key, needs_shift = mapping
            mods = ["ShiftLeft"] if needs_shift else []
            await _press_with_modifiers(conn, key, mods)
            sent += 1
            if inter > 0:
                await asyncio.sleep(inter + INTER_CHAR_GAP_S)
            else:
                await asyncio.sleep(INTER_CHAR_GAP_S)
    return {"chars": sent, "skipped": skipped, "elapsed_s": round(time.monotonic() - start, 3)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_send_keys(combo: str) -> dict:
    """
    Send a single key chord, e.g. "Ctrl+Alt+Delete", "Cmd+Tab", "Win+L", "F11".

    Modifiers are pressed first, then the main key is sent atomically
    (down -> 25 ms -> up), then modifiers are released in reverse order. This
    is the noVNC pattern — it avoids the "Cmd+key release order" bug
    (gl-inet/glkvm issue #22) and the more general dropped-keyup bug.

    Args:
        combo: '+'-separated key names. Aliases accepted: ctrl, shift, alt, cmd,
            win, meta, opt, esc, enter, tab, space, backspace, del, home, end,
            pgup, pgdn, up/down/left/right, f1..f12, etc. The last token is the
            main key; everything before it is treated as a modifier.

    Returns:
        {"sent": <combo>, "modifiers": [...], "key": "..."}
    """
    conn = _require_conn()
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty combo")

    if len(parts) == 1:
        key = resolve_key_name(parts[0])
        async with conn.send_lock:
            await _atomic_press(conn, key)
        return {"sent": combo, "modifiers": [], "key": key}

    *mod_tokens, main_token = parts
    modifiers = [resolve_key_name(m) for m in mod_tokens]
    for m in modifiers:
        if m not in MODIFIER_KEYS:
            # tolerate non-modifier in mod position by treating it as a chained press
            pass
    key = resolve_key_name(main_token)
    async with conn.send_lock:
        await _press_with_modifiers(conn, key, modifiers)
    return {"sent": combo, "modifiers": modifiers, "key": key}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_hold_key(key: str, duration_ms: int) -> dict:
    """
    Press and hold a single key for an explicit duration, then release.

    Use when you specifically want OS-level auto-repeat (e.g. holding ArrowDown
    to scroll) — kvm_send_keys is not appropriate for that because it releases
    after 25 ms.

    Args:
        key: key name (W3C code or alias, e.g. "ArrowDown", "Backspace", "F5").
        duration_ms: how long to hold, in milliseconds. Capped at 5000.
    """
    conn = _require_conn()
    duration_ms = max(1, min(5000, int(duration_ms)))
    canonical = resolve_key_name(key)
    async with conn.send_lock:
        await _ws_send_key(conn, canonical, state=True, finish=False)
    try:
        await asyncio.sleep(duration_ms / 1000.0)
    finally:
        async with conn.send_lock:
            await _ws_send_key(conn, canonical, state=False, finish=True)
    return {"key": canonical, "duration_ms": duration_ms}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def kvm_release_all() -> dict:
    """Force-release every key the server thinks is currently held. Recovery tool."""
    conn = _require_conn()
    released: list[str] = []
    async with conn.send_lock:
        for k in list(conn.held.keys()):
            try:
                await _ws_send_key(conn, k, state=False, finish=True)
                released.append(k)
            except Exception as e:  # noqa: BLE001
                LOG.error("release_all failed for %s: %s", k, e)
    return {"released": released}


# ---- Mouse tools ----------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move(x: int, y: int) -> dict:
    """
    Move the remote cursor to absolute coordinates.

    Coordinates are in PiKVM-normalized int16 space: -32768..32767 spans the
    full screen on each axis. So (0, 0) is screen center, (-32768, -32768) is
    top-left, (32767, 32767) is bottom-right.

    Tip: most callers find it easier to use kvm_mouse_move_pct(x_pct, y_pct).
    """
    conn = _require_conn()
    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x, y)
    return {"x": x, "y": y}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_move_pct(x_pct: float, y_pct: float) -> dict:
    """Move the cursor to a percentage of the screen. (0,0) = top-left, (100,100) = bottom-right."""
    conn = _require_conn()
    x = int(round((max(0.0, min(100.0, x_pct)) / 100.0) * 65535 - 32768))
    y = int(round((max(0.0, min(100.0, y_pct)) / 100.0) * 65535 - 32768))
    async with conn.send_lock:
        await _ws_send_mouse_move(conn, x, y)
    return {"x": x, "y": y, "x_pct": x_pct, "y_pct": y_pct}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_click(button: str = "left", count: int = 1) -> dict:
    """
    Click the named mouse button at the current cursor position.

    Args:
        button: "left", "right", "middle", "up" (X1), or "down" (X2).
        count: number of clicks (1 for single, 2 for double, etc.). Capped at 5.
    """
    conn = _require_conn()
    count = max(1, min(5, int(count)))
    async with conn.send_lock:
        for _ in range(count):
            await _ws_send_mouse_button(conn, button, True)
            await asyncio.sleep(MIN_DOWN_UP_GAP_S)
            await _ws_send_mouse_button(conn, button, False)
            await asyncio.sleep(0.030)
    return {"button": button, "count": count}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
async def kvm_mouse_scroll(dx: int = 0, dy: int = 0) -> dict:
    """
    Scroll the remote mouse wheel.

    Args:
        dx: horizontal wheel delta (-127..127). Positive = right.
        dy: vertical wheel delta   (-127..127). Positive = down.
    """
    conn = _require_conn()
    async with conn.send_lock:
        await _ws_send_mouse_wheel(conn, dx, dy)
    return {"dx": dx, "dy": dy}


# ---- Screenshot -----------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot(preview: bool = True, max_width: int = 1024, quality: int = 60) -> Image:
    """
    Capture a single JPEG frame from the remote video output and return it
    as MCP image content (so the host displays it directly and Claude reads
    it via vision tokens, not as base64 text).

    Args:
        preview: if True (default), request a downsized JPEG from the device.
            Set False to retrieve the full-resolution frame -- be aware large
            frames may still exceed the host's tool-output budget.
        max_width: when preview=True, max width in pixels (default 1024).
            Lower this if responses are still too large for your host.
        quality: when preview=True, JPEG quality 1-100 (default 60).

    Returns:
        MCP image content (image/jpeg).
    """
    conn = _require_conn()
    params: dict[str, str] = {"allow_offline": "true"}
    if preview:
        params["preview"] = "true"
        params["preview_max_width"] = str(int(max_width))
        params["preview_quality"] = str(max(1, min(100, int(quality))))
    r = await conn.http.get(f"{conn.base_url}/api/streamer/snapshot", params=params)
    r.raise_for_status()
    return Image(data=r.content, format="jpeg")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_screenshot_to_file(path: str, preview: bool = False, max_width: int = 1920, quality: int = 80) -> dict:
    """
    Capture a JPEG frame and save it to a file on disk. Returns the path and
    byte size. Useful when you want the full-resolution image without sending
    it through the MCP channel at all (e.g. for archival or offline review).

    Args:
        path: absolute file path to write to. Parent directory must exist.
        preview: if True, downsize before saving (faster, smaller file).
        max_width: when preview=True, max width in pixels.
        quality: when preview=True, JPEG quality 1-100.
    """
    conn = _require_conn()
    params: dict[str, str] = {"allow_offline": "true"}
    if preview:
        params["preview"] = "true"
        params["preview_max_width"] = str(int(max_width))
        params["preview_quality"] = str(max(1, min(100, int(quality))))
    r = await conn.http.get(f"{conn.base_url}/api/streamer/snapshot", params=params)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return {"path": path, "bytes": len(r.content), "mime_type": r.headers.get("content-type", "image/jpeg")}


# ---- Status ---------------------------------------------------------------
class StatusResult(TypedDict):
    connected: bool
    host: str
    held_keys: list[str]
    ws_open: bool


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def kvm_status() -> StatusResult:
    """Report current connection state and any keys the server believes are still held."""
    if _conn is None:
        return StatusResult(connected=False, host="", held_keys=[], ws_open=False)
    return StatusResult(
        connected=True,
        host=_conn.base_url,
        held_keys=sorted(_conn.held.keys()),
        ws_open=_conn.ws.state.name == "OPEN" if hasattr(_conn.ws, "state") else not _conn.ws.closed,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Suppress noisy InsecureRequestWarning from urllib3 (we know — self-signed cert).
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    mcp.run()  # stdio
