# GLKVM Stuck-Key / Double-Typing Fix

A userscript that patches the GLKVM web UI in the browser to mitigate the "keys not released immediately → double typing" bug present in firmware 1.9.0 and earlier.

## Why a userscript and not an .exe

The GLKVM desktop app is a Chromium wrapper around the same web UI hosted on the device, so the bug lives in JavaScript on the device. A userscript injected into a regular browser session reaches the same JS without touching the device firmware. Quickest, lowest-risk fix.

## Install (5 minutes)

1. Install the **Tampermonkey** browser extension in Chrome / Edge / Firefox.
   - Chrome: <https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo>
2. Click the Tampermonkey icon → **Create a new script**.
3. Delete the template, paste the entire contents of `glkvm-stuck-key-fix.user.js`, save (Ctrl+S).
4. Open the GLKVM web UI in a tab — `https://<your-KVM-IP>/` on LAN, or your GoodCloud URL.
5. Take control of the remote keyboard and type. The fix loads silently. Set `DEBUG = true` near the top of the script if you want to see the mitigations log to DevTools console.

## What it does

Three mitigations, all at the WebSocket layer:

| # | Mitigation | Targets |
|---|------------|---------|
| 1 | Inserts ≥ 25 ms gap between matching keydown and keyup before sending | HID frame coalescing on the device side |
| 2 | If a keydown is sent for a key still tracked as held, synthesizes a keyup first | Lost / out-of-order keyup events |
| 3 | Watchdog: every 40 ms, releases any key the browser no longer sees activity for after 250 ms | Browser-level missing keyup events |

## What it does NOT do

- **It does not disable key auto-repeat.** Holding Backspace, holding an arrow key, etc. still works — real OS-level repeats fire `keydown` events with `event.repeat=true` that keep refreshing the watchdog's "this key is genuinely held" signal.
- It doesn't touch mouse, video, or any other channel.
- It does nothing on pages that aren't the GLKVM UI (no key event WS messages → no interception triggers).

## Tuning

Edit the constants at the top of the script:

| Constant | Default | Effect of increasing |
|---|---|---|
| `MIN_DOWN_UP_GAP` | 25 | More reliable on high-latency links, slightly slower typing |
| `STALE_MS` | 250 | Less aggressive watchdog (fewer false releases) but slower stuck-key recovery |
| `POLL_MS` | 40 | Lower CPU, slower watchdog reaction |

## If it doesn't help

Send me a console log with `DEBUG = true` enabled. The most useful output is the count of "delayed keyup" / "synthetic keyup" / "watchdog released" entries while reproducing the bug — that tells us which failure mode is actually firing and we can tune from there.

If the desktop app is your preferred entry point and not a browser, two follow-ups are possible:
1. A small Python WebSocket proxy that does the same patches in a process you run alongside the app.
2. An on-device patch via SSH that modifies `/usr/share/kvmd/web/share/js/keypad.js` directly — permanent, persists across reboots, but reverts on firmware update.

## References

- Source repo: <https://github.com/gl-inet/glkvm>
- Issue #52 (open): Mac copy/paste bug — multiple pastes
- Issue #22 (closed in 1.3.0): Cmd-key combos
- Forum thread: "Repeated keypress" — `https://forum.gl-inet.com/t/repeated-keypress/64940`
