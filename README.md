# glkvm-mcp

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes a
[GL.iNet GLKVM](https://www.gl-inet.com/products/gl-kvm/) (RM1 / RM10 / Comet) device's
keyboard, mouse, and screenshot capabilities to MCP-capable hosts (Claude Desktop,
Cowork, etc.) -- with a built-in mitigation for the firmware <= 1.9.0 "stuck key /
double typing" bug.

## Why this exists

The official GLKVM web UI sometimes drops or delays keyup events under WebSocket
latency, causing the target machine's OS-level auto-repeat to fire and produce
"Heeeellooooo" instead of "Hello" (see
[gl-inet/glkvm#52](https://github.com/gl-inet/glkvm/issues/52) and
[forum thread #64940](https://forum.gl-inet.com/t/repeated-keypress/64940)).
This server applies three layered mitigations on the protocol path so an LLM
can drive the KVM cleanly:

1. **Atomic press.** Every character / chord is sent as `keydown -> >=25 ms gap
   -> keyup(finish=True)`. The 25 ms gap exceeds the device's USB HID poll
   period (~8 ms) so press and release cannot coalesce into one HID frame.
2. **Modifier wrapping.** Chords use the noVNC pattern: `mods down -> key down
   -> key up -> mods up`, the same fix that landed for issue #22 in firmware
   1.3.0 but applied uniformly.
3. **Watchdog.** Every 40 ms, any key still tracked as "down" without recent
   activity is force-released. `kvm_release_all` is the manual escape hatch.

This server is the _agent-driven_ fix. For your own typing in the browser
there's a complementary Tampermonkey userscript under [extras/](extras/).

## Tools

| Tool | Purpose |
|---|---|
| `kvm_connect(host, password, username="admin")` | HTTPS login + open kvmd WebSocket. |
| `kvm_disconnect()` | Close WS, release any held keys. |
| `kvm_send_text(text, wpm=200)` | Type a string with the bug-fix logic. |
| `kvm_send_keys(combo)` | Single chord, e.g. `"Ctrl+Alt+Delete"`. |
| `kvm_hold_key(key, duration_ms)` | Explicit hold for genuine auto-repeat. |
| `kvm_release_all()` | Recovery -- force-release everything held. |
| `kvm_mouse_move(x, y)` | Absolute move (PiKVM int16 space). |
| `kvm_mouse_move_pct(x_pct, y_pct)` | Absolute move in 0..100 % screen units. |
| `kvm_mouse_click(button, count)` | Click left/right/middle/X1/X2. |
| `kvm_mouse_scroll(dx, dy)` | Wheel scroll. |
| `kvm_screenshot(preview, max_width, quality)` | Single JPEG returned as MCP image content. |
| `kvm_screenshot_to_file(path, ...)` | Same, but saved to disk. |
| `kvm_status()` | Connection state + keys currently held. |

## Install

### With `uv` (recommended)

```bash
# install uv if you don't have it
winget install --id=astral-sh.uv -e        # Windows
# or: curl -LsSf https://astral.sh/uv/install.sh | sh   # mac/linux

# clone and run
git clone https://github.com/kennypeh85/glkvm-mcp.git
uv run glkvm-mcp/glkvm_mcp.py
```

The script declares its dependencies inline (PEP 723), so `uv` resolves
`mcp[cli]`, `websockets`, and `httpx` automatically on first launch and
caches them. No virtualenv to manage.

### With pip

```bash
git clone https://github.com/kennypeh85/glkvm-mcp.git
cd glkvm-mcp
python -m venv .venv && . .venv/Scripts/activate
pip install "mcp[cli]>=1.2" "websockets>=12" "httpx>=0.27"
python glkvm_mcp.py
```

### Wire it into Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "glkvm": {
      "command": "uv",
      "args": ["run", "--script", "C:\\path\\to\\glkvm-mcp\\glkvm_mcp.py"]
    }
  }
}
```

Restart Claude Desktop. In a new chat:
*"Use the glkvm server. Connect to my KVM at 192.168.8.55, then take a preview screenshot."*

## Typical usage from an agent

```
kvm_connect(host="192.168.8.55", password="<your-password>")
kvm_screenshot(preview=true, max_width=1024)
kvm_send_keys("Ctrl+Alt+Delete")
kvm_send_text("hello world")
kvm_send_keys("Enter")
kvm_disconnect()
```

## How it talks to the device

This server is a thin client over the kvmd HTTP/WebSocket API exposed by the
GLKVM firmware. Specifically:

* **Login** -- `POST /api/auth/login` with form-encoded `user`+`passwd`,
  receives an `auth_token` cookie.
* **Control WebSocket** -- `wss://<host>/api/ws?auth_token=<token>`, JSON
  messages of the form `{"event_type": "key", "event": {"key": "KeyA",
  "state": true, "finish": false}}` for keys, with parallel shapes for
  mouse_button / mouse_move / mouse_wheel.
* **Snapshot** -- `GET /api/streamer/snapshot[?preview=true&...]` returns a
  single JPEG.
* **Keepalive** -- a `\x00` byte every second; the server drops the WS after
  15 missed pings.

All key names are W3C `KeyboardEvent.code` style (`KeyA`, `Enter`,
`ShiftLeft`, etc.) per the upstream `keymap.csv`. The high-level tools accept
friendlier aliases (`ctrl`, `cmd`, `f5`, ...) which they normalize before
sending.

## Caveats

* **LAN only by design.** TLS verification is disabled to accommodate the
  device's self-signed cert. Don't expose this server's stdio to anything
  outside your local network.
* **US layout only.** `kvm_send_text` maps printable ASCII via a US-layout
  table. Non-ASCII characters end up in the `skipped` list of the response.
  International layouts can be added by extending `_build_keymap()`.
* **Single connection.** One active KVM connection per server process.
  Calling `kvm_connect` twice closes the previous one.
* **No 2FA.** Two-step login is detected and refused -- disable on the KVM
  admin page or extend `kvm_connect` (PRs welcome).
* **Agent typing only.** This server fixes typing performed _by an LLM_. For
  fixing your own at-the-keyboard typing in the browser, install the
  userscript in [extras/](extras/).

## License

MIT -- see [LICENSE](LICENSE).

## Acknowledgements / upstream

* GL.iNet's GLKVM firmware: <https://github.com/gl-inet/glkvm> (GPL-3.0 fork
  of [PiKVM](https://github.com/pikvm/pikvm))
* MCP / FastMCP: <https://modelcontextprotocol.io>

This project is **not** affiliated with GL.iNet or Anthropic.