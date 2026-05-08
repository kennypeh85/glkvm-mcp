// ==UserScript==
// @name         GLKVM Stuck-Key / Double-Typing Fix
// @namespace    https://github.com/gl-inet/glkvm
// @version      1.0.0
// @description  Mitigate the "double typing" / stuck-key bug in GLKVM (RM1 / RM10 / Comet) web UI on firmware 1.9.0 and earlier. Inserts down/up spacing, refires missing keyups, and runs a watchdog. Does NOT break key auto-repeat (holding Backspace still works).
// @author       community patch
// @match        *://*/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    // ---- CONFIG -------------------------------------------------------------
    const STALE_MS         = 250; // force keyup if browser stops reporting activity
    const MIN_DOWN_UP_GAP  = 25;  // minimum ms between keydown and keyup at WS layer
    const POLL_MS          = 40;
    const DEBUG            = false; // set true to see [GLKVM-Fix] logs in DevTools

    const log = DEBUG ? function () { console.log.apply(console, ['[GLKVM-Fix]'].concat([].slice.call(arguments))); }
                      : function () {};

    // ---- BROWSER-LEVEL KEY STATE -------------------------------------------
    // Refreshed by every native keydown (including OS auto-repeat keydowns).
    const browserHeld = new Map(); // ev.code -> last activity timestamp

    document.addEventListener('keydown', function (ev) {
        if (ev && ev.code) browserHeld.set(ev.code, Date.now());
    }, true);

    document.addEventListener('keyup', function (ev) {
        if (ev && ev.code) browserHeld.delete(ev.code);
    }, true);

    window.addEventListener('blur', function () {
        // Window lost focus — clear our shadow state. The GLKVM keypad already
        // releases keys on blur over the WS, so we just stop tracking.
        browserHeld.clear();
        wsHeld.clear();
    }, true);

    // ---- WS-LEVEL KEY STATE ------------------------------------------------
    // Tracks what we've actually sent down/up of, keyed by the GLKVM key name.
    const wsHeld = new Map();   // key string -> { downAt, ws }
    let trackedWs = null;

    function buildKeyMsg(key, isDown) {
        return JSON.stringify({
            event_type: 'key',
            event: { key: key, state: !!isDown, finish: false }
        });
    }

    // GLKVM uses PiKVM's key names (mostly identical to ev.code: "KeyA",
    // "ShiftLeft", "Enter", etc.). The watchdog is conservative — only
    // fires if browser shows NO held key with that exact name.
    function browserStillHolds(glkvmKey) {
        if (browserHeld.has(glkvmKey)) return true;
        // A few PiKVM names differ slightly — best-effort fallbacks:
        if (browserHeld.has('Key' + glkvmKey.toUpperCase())) return true;
        return false;
    }

    // ---- WEBSOCKET HOOK ----------------------------------------------------
    const NativeWS = window.WebSocket;

    function HookedWS(url, protocols) {
        const ws = (protocols !== undefined)
            ? new NativeWS(url, protocols)
            : new NativeWS(url);

        const origSend = ws.send.bind(ws);

        ws.send = function (data) {
            try {
                if (typeof data === 'string' && data.indexOf('"event_type"') !== -1) {
                    const msg = JSON.parse(data);
                    if (msg && msg.event_type === 'key' && msg.event && msg.event.key !== undefined) {
                        trackedWs = ws; // remember the kvmd control WS
                        const k      = msg.event.key;
                        const isDown = (msg.event.state === true);

                        if (isDown) {
                            // Stuck-state guard: if we still think this key is down,
                            // synthesize a keyup first so device sees a clean cycle.
                            if (wsHeld.has(k)) {
                                origSend(buildKeyMsg(k, false));
                                log('inserted synthetic keyup before duplicate keydown:', k);
                            }
                            wsHeld.set(k, { downAt: Date.now(), ws: ws });
                        } else {
                            // keyup: enforce minimum down→up gap so the HID frame
                            // (USB poll ~8 ms) doesn't merge press+release.
                            const info = wsHeld.get(k);
                            if (info) {
                                const gap = Date.now() - info.downAt;
                                if (gap < MIN_DOWN_UP_GAP) {
                                    const wait = MIN_DOWN_UP_GAP - gap;
                                    const payload = data;
                                    setTimeout(function () {
                                        try { origSend(payload); } catch (e) {}
                                        wsHeld.delete(k);
                                    }, wait);
                                    log('delayed keyup for', k, 'by', wait, 'ms');
                                    return; // swallow original — replaced by setTimeout
                                }
                            }
                            wsHeld.delete(k);
                        }
                    }
                }
            } catch (e) {
                // Not JSON or not a key event — pass through unmodified.
            }
            return origSend(data);
        };

        return ws;
    }

    HookedWS.prototype  = NativeWS.prototype;
    HookedWS.CONNECTING = 0;
    HookedWS.OPEN       = 1;
    HookedWS.CLOSING    = 2;
    HookedWS.CLOSED     = 3;
    window.WebSocket    = HookedWS;

    // ---- WATCHDOG ----------------------------------------------------------
    // If a key is held at the WS layer but the browser hasn't seen any native
    // activity for it (initial keydown OR repeat keydown) within STALE_MS,
    // force a keyup. Real holds keep firing keydown with ev.repeat=true so
    // they keep refreshing browserHeld — they will NOT be released.
    setInterval(function () {
        if (!trackedWs || trackedWs.readyState !== 1) return;
        const now = Date.now();
        for (const entry of Array.from(wsHeld.entries())) {
            const k = entry[0], info = entry[1];
            if (browserStillHolds(k)) continue;
            if (now - info.downAt > STALE_MS) {
                try {
                    trackedWs.send(buildKeyMsg(k, false));
                    log('watchdog released stale key:', k, 'after', now - info.downAt, 'ms');
                } catch (e) {}
                wsHeld.delete(k);
            }
        }
    }, POLL_MS);

    log('Loaded — stuck-key mitigations active');
})();
