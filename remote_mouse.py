#!/usr/bin/env python3
"""Remote mouse tool: turns a phone on the LAN into a modern touchpad.

Creates a virtual mouse and keyboard device via /dev/uinput (evdev) and serves
a web app tuned for usability and ergonomics.
Python standard library + python-evdev only, no root required.

    python3 remote_mouse.py [--port 8000] [--token SECRET]
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from evdev import UInput, ecodes as e

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------- virtual device

class VirtualInputDevice:
    """Serialized access to the uinput device for mouse & keyboard."""

    BUTTONS = {
        "l": e.BTN_LEFT,
        "r": e.BTN_RIGHT,
        "m": e.BTN_MIDDLE,
    }

    KEY_MAP = {
        # Navigation & function
        "enter": e.KEY_ENTER,
        "return": e.KEY_ENTER,
        "backspace": e.KEY_BACKSPACE,
        "space": e.KEY_SPACE,
        "tab": e.KEY_TAB,
        "esc": e.KEY_ESC,
        "escape": e.KEY_ESC,
        "delete": e.KEY_DELETE,
        "del": e.KEY_DELETE,
        "insert": e.KEY_INSERT,
        "home": e.KEY_HOME,
        "end": e.KEY_END,
        "pageup": e.KEY_PAGEUP,
        "pagedown": e.KEY_PAGEDOWN,
        "up": e.KEY_UP,
        "down": e.KEY_DOWN,
        "left": e.KEY_LEFT,
        "right": e.KEY_RIGHT,
        # Modifiers
        "ctrl": e.KEY_LEFTCTRL,
        "alt": e.KEY_LEFTALT,
        "shift": e.KEY_LEFTSHIFT,
        "super": e.KEY_LEFTMETA,
        "win": e.KEY_LEFTMETA,
        "meta": e.KEY_LEFTMETA,
        # F keys
        "f1": e.KEY_F1, "f2": e.KEY_F2, "f3": e.KEY_F3, "f4": e.KEY_F4,
        "f5": e.KEY_F5, "f6": e.KEY_F6, "f7": e.KEY_F7, "f8": e.KEY_F8,
        "f9": e.KEY_F9, "f10": e.KEY_F10, "f11": e.KEY_F11, "f12": e.KEY_F12,
        # Media & volume
        "playpause": e.KEY_PLAYPAUSE,
        "volup": e.KEY_VOLUMEUP,
        "voldown": e.KEY_VOLUMEDOWN,
        "mute": e.KEY_MUTE,
        "next": e.KEY_NEXTSONG,
        "prev": e.KEY_PREVIOUSSONG,
        "stop": e.KEY_STOPCD,
    }

    # ASCII character -> (keycode, shift state)
    CHAR_MAP = {
        **{c: (getattr(e, f"KEY_{c.upper()}"), False) for c in "abcdefghijklmnopqrstuvwxyz"},
        **{c: (getattr(e, f"KEY_{c}"), True) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: (getattr(e, f"KEY_{c}"), False) for c in "0123456789"},
        " ": (e.KEY_SPACE, False), "\n": (e.KEY_ENTER, False), "\t": (e.KEY_TAB, False),
        ".": (e.KEY_DOT, False), ",": (e.KEY_COMMA, False), "/": (e.KEY_SLASH, False),
        "-": (e.KEY_MINUS, False), "=": (e.KEY_EQUAL, False), ";": (e.KEY_SEMICOLON, False),
        "'": (e.KEY_APOSTROPHE, False), "[": (e.KEY_LEFTBRACE, False), "]": (e.KEY_RIGHTBRACE, False),
        "\\": (e.KEY_BACKSLASH, False), "`": (e.KEY_GRAVE, False),
        ":": (e.KEY_SEMICOLON, True), "_": (e.KEY_MINUS, True), "+": (e.KEY_EQUAL, True),
        "!": (e.KEY_1, True), "@": (e.KEY_2, True), "#": (e.KEY_3, True), "$": (e.KEY_4, True),
        "%": (e.KEY_5, True), "^": (e.KEY_6, True), "&": (e.KEY_7, True), "*": (e.KEY_8, True),
        "(": (e.KEY_9, True), ")": (e.KEY_0, True), "?": (e.KEY_SLASH, True), "<": (e.KEY_COMMA, True),
        ">": (e.KEY_DOT, True), "\"": (e.KEY_APOSTROPHE, True), "~": (e.KEY_GRAVE, True),
        "{": (e.KEY_LEFTBRACE, True), "}": (e.KEY_RIGHTBRACE, True), "|": (e.KEY_BACKSLASH, True),
    }

    HOLD_TIMEOUT = 5.0  # Seconds without a sign of life before held keys are released

    def __init__(self):
        # Register only KEY_* plus the three mouse buttons. Registering every
        # BTN_* would drag in joystick, gamepad and tablet buttons; udev then
        # stops classifying the device as a mouse (ID_INPUT_MOUSE is missing)
        # and libinput never grants it pointer capabilities.
        all_keys = [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
        for name in dir(e):
            if name.startswith("KEY_") and not name.startswith("KEY_MAX"):
                val = getattr(e, name)
                if isinstance(val, int) and val < 0x2FF:
                    all_keys.append(val)

        caps = {
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
            e.EV_KEY: list(set(all_keys)),
        }
        self.ui = UInput(caps, name="remote-mouse", version=1)
        self.lock = threading.Lock()
        self.down = set()
        self.last_input = time.monotonic()
        time.sleep(0.3)  # Give the compositor time to register the device
        threading.Thread(target=self._watchdog, daemon=True).start()

    def touch(self):
        self.last_input = time.monotonic()

    def _watchdog(self):
        """Releases held keys after a timeout in case the client drops out."""
        while True:
            time.sleep(0.5)
            if self.down and time.monotonic() - self.last_input > self.HOLD_TIMEOUT:
                print(f"[!] {self.HOLD_TIMEOUT}s without input while a key was held "
                      f"-> forcing release", flush=True)
                self.release_all()

    def move(self, dx, dy):
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return
        with self.lock:
            if dx:
                self.ui.write(e.EV_REL, e.REL_X, dx)
            if dy:
                self.ui.write(e.EV_REL, e.REL_Y, dy)
            self.ui.syn()

    def scroll(self, dy, dx=0):
        dy, dx = int(round(dy)), int(round(dx))
        if not dy and not dx:
            return
        with self.lock:
            if dy:
                self.ui.write(e.EV_REL, e.REL_WHEEL, dy)
            if dx:
                self.ui.write(e.EV_REL, e.REL_HWHEEL, dx)
            self.ui.syn()

    def _code_for(self, name):
        """Key name -> keycode. Knows both KEY_MAP and single characters."""
        name = str(name).lower()
        code = self.KEY_MAP.get(name)
        if code is None and len(name) == 1:
            entry = self.CHAR_MAP.get(name)
            if entry is not None:
                code = entry[0]
        return code

    def _write_key(self, code, value):
        """The only way a key event gets written.

        Keeps self.down current so release_all() and the watchdog catch every
        pressed key -- including modifiers from combos and from text typing,
        which do not go through button().
        """
        with self.lock:
            self.ui.write(e.EV_KEY, code, value)
            self.ui.syn()
        if value:
            self.down.add(code)
        else:
            self.down.discard(code)

    def button(self, name, pressed):
        code = self.BUTTONS.get(name)
        if code is None:
            return
        self._write_key(code, 1 if pressed else 0)

    def click(self, name):
        if name == "double":
            self.click("l")
            time.sleep(0.06)
            self.click("l")
            return
        self.button(name, True)
        time.sleep(0.02)
        self.button(name, False)

    def key(self, name, pressed=None):
        code = self._code_for(name)
        if code is None:
            return
        if pressed is None:
            # Plain key tap
            self._write_key(code, 1)
            time.sleep(0.02)
            self._write_key(code, 0)
        else:
            # Hold / release the key
            self._write_key(code, 1 if pressed else 0)

    def key_combo(self, keys):
        """Presses a key combination (e.g. ['ctrl', 'c']) and releases it again."""
        codes = [c for c in (self._code_for(k) for k in keys) if c is not None]
        if not codes:
            return
        for c in codes:
            self._write_key(c, 1)
        time.sleep(0.03)
        for c in reversed(codes):
            self._write_key(c, 0)

    def type_text(self, text):
        """Types a string one character at a time.

        CHAR_MAP covers the US layout; anything missing from it (umlauts, ss,
        euro) is skipped and reported at the end instead of being swallowed
        silently.
        """
        if not text:
            return
        skipped = []
        for ch in text:
            entry = self.CHAR_MAP.get(ch)
            if entry is None:
                skipped.append(ch)
                continue
            code, shift = entry
            # Longer texts take a while; without this the watchdog would release
            # a mouse button held in parallel (drag lock) mid-typing after 5 s.
            self.touch()
            if shift:
                self._write_key(e.KEY_LEFTSHIFT, 1)
            self._write_key(code, 1)
            time.sleep(0.012)
            self._write_key(code, 0)
            if shift:
                self._write_key(e.KEY_LEFTSHIFT, 0)
            time.sleep(0.012)
        if skipped:
            print(f"[!] {len(skipped)} characters not in the layout mapping, skipped: "
                  f"{''.join(dict.fromkeys(skipped))}", flush=True)

    def release_all(self):
        for code in list(self.down):
            with self.lock:
                self.ui.write(e.EV_KEY, code, 0)
                self.ui.syn()
            self.down.discard(code)


# ------------------------------------------------------------------- websocket

def ws_accept(key):
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def ws_read_frame(rfile):
    """Reads one frame. Returns (opcode, payload), or None on EOF/close."""
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    if length > 1 << 20:
        return None
    mask = rfile.read(4) if masked else b""
    data = rfile.read(length)
    if len(data) < length:
        return None
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


def ws_send(sock, payload, opcode=0x1):
    data = payload.encode() if isinstance(payload, str) else payload
    n = len(data)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    sock.sendall(head + data)


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "remote-mouse"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            print(f"    [http] {self.client_address[0]} {self.requestline} -> {args[1]}", flush=True)

    # -- helpers

    def _token_ok(self, query):
        return secrets.compare_digest(query.get("t", [""])[0], self.server.token)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # -- routes

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/ws":
            if not self._token_ok(query):
                self._send(403, b"forbidden")
                return
            self.handle_ws()
            return

        if url.path != "/":
            self._send(404, b"not found")
            return

        if not self._token_ok(query):
            self._send(403, b"Wrong or missing token. Please scan the QR code again.")
            return

        self._send(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/i" or not self._token_ok(parse_qs(url.query)):
            self._send(403, b"forbidden")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > 65536:
            self._send(413, b"too large")
            return
        body = self.rfile.read(n).decode("utf-8", "replace")
        if not self.server.saw_http_fallback:
            self.server.saw_http_fallback = True
            print(f"[i] {self.client_address[0]} using HTTP fallback (no WebSocket)", flush=True)
        for line in body.splitlines():
            if line.strip():
                try:
                    self.dispatch(self.server.mouse, json.loads(line))
                except (ValueError, TypeError):
                    pass
        self._send(204)

    def handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, b"no websocket key")
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()

        mouse = self.server.mouse
        sock = self.connection
        sock.settimeout(300)
        print(f"[+] Client connected: {self.client_address[0]}", flush=True)
        if self.server.verbose:
            for h in ("User-Agent", "Origin", "Sec-WebSocket-Version", "Sec-WebSocket-Extensions"):
                print(f"    [ws] {h}: {self.headers.get(h)}", flush=True)
        reason, frames = "EOF (peer closed the connection)", 0
        try:
            while True:
                frame = ws_read_frame(self.rfile)
                if frame is None:
                    break
                opcode, data = frame
                frames += 1
                if opcode == 0x8:  # close
                    code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
                    reason = f"close frame from client (code={code})"
                    break
                if opcode == 0x9:  # ping
                    ws_send(sock, data, opcode=0xA)
                    continue
                if opcode != 0x1:
                    continue
                for line in data.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            if msg.get("t") == "ping":
                                # Immediate pong reply for latency measurement
                                ws_send(sock, json.dumps({"t": "pong", "id": msg.get("id", 0)}))
                            else:
                                self.dispatch(mouse, msg)
                        except Exception:
                            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        finally:
            mouse.release_all()
            print(f"[-] Client disconnected: {self.client_address[0]} "
                  f"| {frames} frames received | reason: {reason}", flush=True)

    @staticmethod
    def dispatch(mouse, msg):
        kind = msg.get("t")
        mouse.touch()
        if kind == "hb":       # heartbeat
            return
        if kind == "m":        # pointer movement
            mouse.move(msg.get("dx", 0), msg.get("dy", 0))
        elif kind == "s":      # scroll
            mouse.scroll(msg.get("dy", 0), msg.get("dx", 0))
        elif kind == "b":      # mouse button down / up
            mouse.button(msg.get("b", "l"), bool(msg.get("d")))
        elif kind == "c":      # mouse click
            mouse.click(msg.get("b", "l"))
        elif kind == "k":      # keyboard key
            k_name = msg.get("k")
            d_state = msg.get("d")  # None = tap, 1 = down, 0 = up
            mouse.key(k_name, pressed=d_state)
        elif kind == "combo":  # key combination (e.g. ["ctrl", "c"])
            keys = msg.get("keys", [])
            if keys:
                mouse.key_combo(keys)
        elif kind == "text":   # type a block of text
            text = msg.get("text", "")
            if text:
                mouse.type_text(text)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False
    saw_http_fallback = False

    BENIGN = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self.BENIGN):
            if self.verbose:
                print(f"    [net] {client_address[0]}: {type(exc).__name__} "
                      f"(connection dropped, harmless)", flush=True)
            return
        super().handle_error(request, client_address)


# ------------------------------------------------------------------ client app

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0f17">
<title>Remote Mouse</title>
<style>
  :root {
    --bg: #0b0f17;
    --surface: rgba(22, 28, 41, 0.85);
    --surface-solid: #161c29;
    --surface-hover: #1f2738;
    --surface-active: #2b364d;
    --border: rgba(255, 255, 255, 0.08);
    --border-subtle: rgba(255, 255, 255, 0.04);
    --accent: #6366f1;
    --accent-glow: rgba(99, 102, 241, 0.35);
    --accent-light: #818cf8;
    --cyan: #06b6d4;
    --cyan-glow: rgba(6, 182, 212, 0.3);
    --fg: #f1f5f9;
    --dim: #94a3b8;
    --muted: #64748b;
    --pad-bg: #111622;
    --pad-active: #182030;
    --danger: #ef4444;
    --success: #10b981;
    --warning: #f59e0b;
    --safe-bottom: env(safe-area-inset-bottom, 0px);
    --safe-top: env(safe-area-inset-top, 0px);
    --radius-lg: 20px;
    --radius-md: 14px;
    --radius-sm: 10px;
  }

  /* Themes */
  body.theme-cyber {
    --bg: #070913;
    --surface: rgba(16, 23, 44, 0.85);
    --surface-solid: #10172c;
    --surface-hover: #1b2649;
    --pad-bg: #0b1021;
    --pad-active: #131c38;
    --accent: #06b6d4;
    --accent-glow: rgba(6, 182, 212, 0.4);
    --accent-light: #38bdf8;
  }
  body.theme-oled {
    --bg: #000000;
    --surface: rgba(18, 18, 18, 0.95);
    --surface-solid: #121212;
    --surface-hover: #222222;
    --pad-bg: #050505;
    --pad-active: #1a1a1a;
    --border: rgba(255, 255, 255, 0.12);
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; outline: none; }
  
  html, body {
    height: 100%;
    height: 100dvh;
    margin: 0;
    padding: 0;
    overscroll-behavior: none;
    touch-action: none;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
    font-size: 14px;
    user-select: none;
    -webkit-user-select: none;
    overflow: hidden;
  }

  /* Layout */
  #app {
    height: 100%;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    padding: 8px 10px;
    padding-top: max(8px, var(--safe-top));
    padding-bottom: max(8px, var(--safe-bottom));
    gap: 8px;
    position: relative;
  }

  /* Header Bar */
  #header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 42px;
    padding: 0 4px;
    flex-shrink: 0;
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    background: var(--surface);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 30px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }
  .status-pill:active { transform: scale(0.96); }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--danger);
    box-shadow: 0 0 8px var(--danger);
    transition: all 0.3s;
  }
  .dot.connected {
    background: var(--success);
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.7);
  }
  .dot.connecting {
    background: var(--warning);
    box-shadow: 0 0 8px rgba(245, 158, 11, 0.7);
    animation: pulse 1.2s infinite;
  }
  .dot.http {
    background: var(--cyan);
    box-shadow: 0 0 8px var(--cyan-glow);
  }

  @keyframes pulse {
    0% { opacity: 0.4; transform: scale(0.85); }
    50% { opacity: 1; transform: scale(1.15); }
    100% { opacity: 0.4; transform: scale(0.85); }
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .icon-btn {
    width: 36px;
    height: 36px;
    border-radius: var(--radius-sm);
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--dim);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .icon-btn:active {
    transform: scale(0.92);
    background: var(--surface-active);
    color: var(--fg);
  }
  .icon-btn.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent-light);
    box-shadow: 0 0 12px var(--accent-glow);
  }
  .icon-btn svg { width: 18px; height: 18px; fill: currentColor; }

  /* Main Stage (always visible: touchpad + mouse deck) */
  #stage {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: clamp(170px, 40dvh, 320px);
  }

  /* Optional Layers (Keyboard / Media panels) */
  #panels {
    flex: 0 1 auto;
    min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overscroll-behavior: contain;
  }
  #panels.empty { display: none; }
  .panel {
    display: none;
    flex-direction: column;
    flex-shrink: 0;
    gap: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 8px;
  }
  .panel.open {
    display: flex;
    scroll-margin: 8px;
    animation: panelIn 0.18s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes panelIn {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Layer Toggle Bar */
  #layer-bar {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
  .layer-btn {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    height: 44px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--dim);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .layer-btn:active { transform: scale(0.96); }
  .layer-btn.active {
    background: var(--accent);
    border-color: var(--accent-light);
    color: #ffffff;
    box-shadow: 0 2px 12px var(--accent-glow);
  }
  .layer-btn svg { width: 16px; height: 16px; fill: currentColor; }
  .layer-btn .chevron {
    width: 12px;
    height: 12px;
    opacity: 0.7;
    transition: transform 0.2s ease;
  }
  .layer-btn.active .chevron { transform: rotate(180deg); }
  body.left-handed #layer-bar { flex-direction: row-reverse; }

  /* Trackpad View */
  #trackpad-wrap {
    flex: 1;
    display: flex;
    position: relative;
    gap: 8px;
    min-height: 0;
  }
  #pad {
    flex: 1;
    border-radius: var(--radius-lg);
    background: var(--pad-bg);
    border: 1px solid var(--border);
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    box-shadow: inset 0 0 30px rgba(0, 0, 0, 0.4);
    transition: background 0.15s, border-color 0.15s;
  }
  #pad.active {
    background: var(--pad-active);
    border-color: rgba(99, 102, 241, 0.35);
  }
  #pad.drag-active {
    border-color: var(--warning);
    box-shadow: inset 0 0 30px rgba(245, 158, 11, 0.15);
  }

  /* Ripple & pointer indicators */
  .touch-ripple {
    position: absolute;
    width: 60px;
    height: 60px;
    margin-left: -30px;
    margin-top: -30px;
    border-radius: 50%;
    background: radial-gradient(circle, var(--accent-light) 0%, rgba(99, 102, 241, 0) 70%);
    opacity: 0.5;
    pointer-events: none;
    transform: scale(0.6);
    transition: opacity 0.3s, transform 0.3s;
  }

  .pad-hint {
    color: var(--muted);
    font-size: 12px;
    text-align: center;
    line-height: 1.6;
    pointer-events: none;
    opacity: 0.75;
    transition: opacity 0.3s;
    padding: 20px;
  }
  .pad-hint svg { vertical-align: middle; margin-right: 4px; }
  #pad.active .pad-hint { opacity: 0.15; }
  body.panels-open .pad-hint { display: none; }

  /* Drag mode toast overlay */
  #drag-toast {
    position: absolute;
    top: 14px;
    background: var(--warning);
    color: #000;
    font-weight: 700;
    font-size: 11.5px;
    padding: 5px 14px;
    border-radius: 20px;
    box-shadow: 0 4px 15px rgba(245, 158, 11, 0.4);
    display: none;
    align-items: center;
    gap: 6px;
    z-index: 5;
    pointer-events: auto;
    cursor: pointer;
  }
  #drag-toast.visible { display: flex; animation: slideDown 0.2s ease; }
  @keyframes slideDown {
    from { transform: translateY(-10px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }

  /* Dedicated Edge Scroll Strip */
  #edge-scroll {
    width: 44px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: space-between;
    padding: 12px 0;
    position: relative;
    overflow: hidden;
    cursor: pointer;
    flex-shrink: 0;
  }
  #edge-scroll.active {
    background: var(--surface-hover);
    border-color: var(--accent);
  }
  .edge-indicator {
    color: var(--muted);
    font-size: 11px;
    font-weight: bold;
    pointer-events: none;
  }
  .edge-thumb {
    width: 6px;
    height: 38px;
    background: var(--accent);
    border-radius: 6px;
    box-shadow: 0 0 10px var(--accent-glow);
    pointer-events: none;
    transition: transform 0.05s ease-out;
  }

  /* Mouse Buttons Deck */
  #mouse-deck {
    display: flex;
    gap: 8px;
    height: 72px;
    margin-top: 8px;
    flex-shrink: 0;
  }
  .mouse-btn {
    border-radius: var(--radius-md);
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--fg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    transition: all 0.12s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
  }
  .mouse-btn svg { width: 19px; height: 19px; fill: currentColor; opacity: 0.85; }
  .mouse-btn:active, .mouse-btn.down {
    background: var(--accent);
    border-color: var(--accent-light);
    color: #ffffff;
    box-shadow: 0 0 16px var(--accent-glow);
    transform: scale(0.96);
  }
  .mouse-btn.locked {
    background: var(--warning);
    border-color: #fbbf24;
    color: #000000;
    box-shadow: 0 0 16px rgba(245, 158, 11, 0.45);
  }
  .mouse-btn.locked svg { fill: #000000; }

  #btn-left { flex: 1.4; }
  #btn-drag { flex: 0.85; }
  #btn-mid { flex: 0.85; }
  #btn-right { flex: 1.4; }

  /* Left-Handed Mode */
  body.left-handed #trackpad-wrap { flex-direction: row-reverse; }
  body.left-handed #mouse-deck { flex-direction: row-reverse; }

  /* Keyboard Panel */
  .text-input-box {
    display: flex;
    gap: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 6px;
  }
  .text-input-box input {
    flex: 1;
    background: transparent;
    border: none;
    color: var(--fg);
    font-size: 15px;
    padding: 6px 8px;
  }
  .text-input-box input::placeholder { color: var(--muted); }
  .btn-primary {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 10px;
    padding: 0 14px;
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 4px;
    transition: all 0.15s;
  }
  .btn-primary:active { transform: scale(0.95); opacity: 0.9; }

  .kb-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
  }
  .kb-btn {
    height: 48px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--fg);
    font-size: 13px;
    font-weight: 600;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.12s;
  }
  .kb-btn:active {
    background: var(--accent);
    border-color: var(--accent-light);
    color: #fff;
    transform: scale(0.94);
  }
  .kb-btn.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent-light);
  }
  .kb-btn sub { font-size: 9px; color: var(--dim); margin-top: -2px; }

  /* D-Pad Nav */
  .dpad-container {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 12px;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    grid-template-rows: 1fr 1fr 1fr;
    gap: 6px;
    max-width: 250px;
    margin: 0 auto;
    width: 100%;
  }
  .dpad-btn {
    height: 42px;
    background: var(--surface-hover);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--fg);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: bold;
    font-size: 16px;
    cursor: pointer;
    transition: all 0.12s;
  }
  .dpad-btn:active {
    background: var(--accent);
    transform: scale(0.93);
    color: #fff;
  }
  .dpad-center {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent-light);
  }

  /* Media Panel */
  .media-circle-btn {
    width: 66px;
    height: 66px;
    border-radius: 50%;
    background: var(--accent);
    border: 3px solid var(--accent-light);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    box-shadow: 0 0 24px var(--accent-glow);
    transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .media-circle-btn:active { transform: scale(0.92); }
  .media-circle-btn svg { width: 30px; height: 30px; fill: currentColor; }

  .media-row {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 8px;
    width: 100%;
  }
  .media-card-btn {
    flex: 1;
    max-width: 110px;
    height: 54px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--fg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    font-size: 11.5px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.12s;
  }
  .media-card-btn:active {
    background: var(--surface-active);
    border-color: var(--accent);
    transform: scale(0.94);
  }
  .media-card-btn svg { width: 20px; height: 20px; fill: currentColor; }

  /* Settings Modal / Bottom Sheet */
  #settings-sheet {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    z-index: 99;
    display: none;
    flex-direction: column;
    justify-content: flex-end;
  }
  #settings-sheet.open { display: flex; animation: fadeIn 0.2s ease; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

  .sheet-panel {
    background: var(--surface-solid);
    border-top: 1px solid var(--border);
    border-radius: 24px 24px 0 0;
    padding: 16px 20px;
    padding-bottom: max(20px, var(--safe-bottom));
    max-height: 85vh;
    overflow-y: auto;
    animation: sheetUp 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes sheetUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }

  .sheet-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .sheet-header h3 { margin: 0; font-size: 16px; font-weight: 700; }
  .sheet-close {
    background: var(--surface-hover);
    border: none;
    color: var(--dim);
    width: 30px;
    height: 30px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 11px 0;
    border-bottom: 1px solid var(--border-subtle);
  }
  .setting-info { display: flex; flex-direction: column; gap: 2px; }
  .setting-title { font-weight: 600; font-size: 13.5px; }
  .setting-desc { font-size: 11.5px; color: var(--muted); }

  .toggle {
    position: relative;
    width: 48px;
    height: 28px;
    flex-shrink: 0;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background-color: #334155;
    border-radius: 28px;
    transition: 0.25s;
  }
  .toggle-slider:before {
    position: absolute;
    content: "";
    height: 22px;
    width: 22px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    border-radius: 50%;
    transition: 0.25s;
  }
  .toggle input:checked + .toggle-slider { background-color: var(--accent); }
  .toggle input:checked + .toggle-slider:before { transform: translateX(20px); }

  .range-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 150px;
  }
  .range-wrap input[type=range] {
    flex: 1;
    accent-color: var(--accent);
  }
  .range-val {
    font-size: 12px;
    font-weight: bold;
    min-width: 32px;
    text-align: right;
    color: var(--accent-light);
  }

  .theme-options {
    display: flex;
    gap: 8px;
  }
  .theme-btn {
    padding: 6px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--fg);
    font-size: 12px;
    cursor: pointer;
  }
  .theme-btn.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent-light);
  }
</style>
</head>
<body>
<div id="app">

  <!-- Header Bar -->
  <div id="header">
    <div class="status-pill" id="status-pill" title="Tap to reconnect">
      <span class="dot connecting" id="status-dot"></span>
      <span id="status-text">connecting…</span>
      <span id="ping-text" style="font-size: 10px; color: var(--muted); margin-left: 2px;"></span>
    </div>

    <div class="header-actions">
      <button class="icon-btn" id="btn-wakelock" title="Keep screen awake" aria-label="Wake Lock">
        <svg viewBox="0 0 24 24"><path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zm0 8c-1.65 0-3-1.35-3-3s1.35-3 3-3 3 1.35 3 3-1.35 3-3 3zm0-13C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg>
      </button>
      <button class="icon-btn" id="btn-fullscreen" title="Fullscreen" aria-label="Fullscreen">
        <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
      </button>
      <button class="icon-btn" id="btn-settings" title="Settings" aria-label="Settings">
        <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
      </button>
    </div>
  </div>

  <!-- MAIN STAGE: always visible -->
  <div id="stage">
    <div id="trackpad-wrap">
      <div id="pad">
        <div id="drag-toast">🔒 Drag mode active &bull; tap to release</div>
        <div class="pad-hint">
          <strong>Gesture controls</strong><br>
          1 finger: move pointer &middot; tap: click<br>
          2 fingers: scroll &middot; two-finger tap: right click<br>
          Double-tap + hold: drag &amp; drop
        </div>
      </div>
      <div id="edge-scroll" title="One-finger scroll strip">
        <div class="edge-indicator">▲</div>
        <div class="edge-thumb" id="edge-thumb"></div>
        <div class="edge-indicator">▼</div>
      </div>
    </div>

    <!-- Bottom Mouse Deck -->
    <div id="mouse-deck">
      <button class="mouse-btn" id="btn-left">
        <svg viewBox="0 0 24 24"><path d="M13 1.07V9h7c0-4.08-3.05-7.44-7-7.93zM4 9h7V1.07C7.05 1.56 4 4.92 4 9zm0 6c0 4.42 3.58 8 8 8s8-3.58 8-8v-4H4v4z"/></svg>
        <span>Left</span>
      </button>
      <button class="mouse-btn" id="btn-drag" title="Hold drag &amp; drop">
        <svg viewBox="0 0 24 24"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>
        <span>Drag</span>
      </button>
      <button class="mouse-btn" id="btn-mid" title="Middle click (wheel)">
        <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 14h-2V8h2v8z"/></svg>
        <span>Middle</span>
      </button>
      <button class="mouse-btn" id="btn-right">
        <svg viewBox="0 0 24 24"><path d="M11 1.07C7.05 1.56 4 4.92 4 9h7V1.07zm2 0V9h7c0-4.08-3.05-7.44-7-7.93zM4 15c0 4.42 3.58 8 8 8s8-3.58 8-8v-4H4v4z"/></svg>
        <span>Right</span>
      </button>
    </div>
  </div>

  <!-- OPTIONAL LAYERS: toggled on top of the stage -->
  <div id="panels" class="empty">

    <!-- LAYER: Media & Shortcuts -->
    <section class="panel" id="panel-media" aria-label="Media and shortcuts">
      <div class="media-row">
        <button class="media-card-btn" data-key="prev" title="Previous track">
          <svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>
          <span>Previous</span>
        </button>

        <button class="media-circle-btn" data-key="playpause" id="btn-playpause" title="Play / Pause">
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </button>

        <button class="media-card-btn" data-key="next" title="Next track">
          <svg viewBox="0 0 24 24"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>
          <span>Next</span>
        </button>
      </div>

      <!-- Volume Deck -->
      <div class="media-row">
        <button class="media-card-btn" data-key="voldown" title="Volume down">
          <svg viewBox="0 0 24 24"><path d="M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z"/></svg>
          <span>Vol &minus;</span>
        </button>
        <button class="media-card-btn" data-key="mute" title="Mute">
          <svg viewBox="0 0 24 24"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>
          <span>Mute</span>
        </button>
        <button class="media-card-btn" data-key="volup" title="Volume up">
          <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>
          <span>Vol +</span>
        </button>
      </div>

      <!-- Presentation & Quick Actions -->
      <div class="media-row">
        <button class="media-card-btn" data-key="pageup" title="Previous slide">
          <svg viewBox="0 0 24 24"><path d="M12 8l-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z"/></svg>
          <span>Page ▲</span>
        </button>
        <button class="media-card-btn" data-key="f11" title="Fullscreen">
          <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
          <span>Fullscreen (F11)</span>
        </button>
        <button class="media-card-btn" data-key="pagedown" title="Next slide">
          <svg viewBox="0 0 24 24"><path d="M16.59 8.59L12 13.17 7.41 8.59 6 10l6 6 6-6z"/></svg>
          <span>Page ▼</span>
        </button>
      </div>
    </section>

    <!-- LAYER: Keyboard -->
    <section class="panel" id="panel-keyboard" aria-label="Keyboard">
      <div class="text-input-box">
        <input type="text" id="text-input" placeholder="Type or dictate text / URL…" autocomplete="off">
        <button class="btn-primary" id="btn-send-text">Send</button>
      </div>

      <!-- Essential Keys Grid -->
      <div class="kb-grid">
        <button class="kb-btn" data-key="esc">Esc</button>
        <button class="kb-btn" data-key="tab">Tab</button>
        <button class="kb-btn" data-key="backspace">⌫ Backspace</button>
        <button class="kb-btn" data-key="enter" style="background: var(--surface-active); color: var(--accent-light);">↵ Enter</button>

        <button class="kb-btn" data-combo="ctrl,c">Ctrl+C<sub>Copy</sub></button>
        <button class="kb-btn" data-combo="ctrl,v">Ctrl+V<sub>Paste</sub></button>
        <button class="kb-btn" data-combo="ctrl,z">Ctrl+Z<sub>Undo</sub></button>
        <button class="kb-btn" data-combo="ctrl,a">Ctrl+A<sub>Select all</sub></button>

        <button class="kb-btn" data-combo="alt,tab">Alt+Tab<sub>Switch</sub></button>
        <button class="kb-btn" data-key="super">Win / Super</button>
        <button class="kb-btn" data-combo="ctrl,w">Ctrl+W<sub>Close tab</sub></button>
        <button class="kb-btn" data-key="f5">F5<sub>Reload</sub></button>
      </div>

      <!-- D-Pad Navigation -->
      <div class="dpad-container">
        <div></div>
        <button class="dpad-btn" data-key="up">▲</button>
        <div></div>

        <button class="dpad-btn" data-key="left">◄</button>
        <button class="dpad-btn dpad-center" data-key="enter">OK</button>
        <button class="dpad-btn" data-key="right">►</button>

        <div></div>
        <button class="dpad-btn" data-key="down">▼</button>
        <div></div>
      </div>

      <!-- Space & Delete Bar -->
      <div style="display: flex; gap: 6px;">
        <button class="kb-btn" data-key="space" style="flex: 2;">Space</button>
        <button class="kb-btn" data-key="delete" style="flex: 1;">Del</button>
      </div>
    </section>

  </div>

  <!-- LAYER TOGGLES -->
  <div id="layer-bar">
    <button class="layer-btn" data-panel="keyboard" aria-pressed="false">
      <svg viewBox="0 0 24 24"><path d="M20 5H4c-1.1 0-1.99.9-1.99 2L2 17c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm-9 3h2v2h-2V8zm0 3h2v2h-2v-2zM8 8h2v2H8V8zm0 3h2v2H8v-2zm-1 2H5v-2h2v2zm0-3H5V8h2v2zm9 7H8v-2h8v2zm0-4h-2v-2h2v2zm0-3h-2V8h2v2zm3 3h-2v-2h2v2zm0-3h-2V8h2v2z"/></svg>
      Keyboard
      <svg class="chevron" viewBox="0 0 24 24"><path d="M12 8l-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z"/></svg>
    </button>
    <button class="layer-btn" data-panel="media" aria-pressed="false">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z"/></svg>
      Media &amp; SC
      <svg class="chevron" viewBox="0 0 24 24"><path d="M12 8l-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z"/></svg>
    </button>
  </div>

</div>

<!-- Settings Sheet Modal -->
<div id="settings-sheet">
  <div class="sheet-panel">
    <div class="sheet-header">
      <h3>Settings &amp; usability</h3>
      <button class="sheet-close" id="btn-close-settings">&times;</button>
    </div>

    <!-- Speed Slider -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Pointer speed</span>
        <span class="setting-desc">How fast the pointer follows your finger</span>
      </div>
      <div class="range-wrap">
        <input type="range" id="cfg-speed" min="0.5" max="4.0" step="0.1" value="1.6">
        <span class="range-val" id="val-speed">1.6x</span>
      </div>
    </div>

    <!-- Acceleration Toggle -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Dynamic acceleration</span>
        <span class="setting-desc">Precise on slow moves, fast on wide swipes</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-accel" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Scroll Speed Slider -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Scroll sensitivity</span>
        <span class="setting-desc">Speed of two-finger scrolling</span>
      </div>
      <div class="range-wrap">
        <input type="range" id="cfg-scroll-speed" min="0.5" max="3.0" step="0.1" value="1.2">
        <span class="range-val" id="val-scroll-speed">1.2x</span>
      </div>
    </div>

    <!-- Invert Scroll Toggle -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Natural scrolling</span>
        <span class="setting-desc">Invert scroll direction (like a touchscreen)</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-invert-scroll">
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Edge Scrollbar Toggle -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Edge scroll strip</span>
        <span class="setting-desc">One-finger scrolling along the edge</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-edge-scroll" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Haptic Feedback Toggle -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Haptic feedback</span>
        <span class="setting-desc">Vibrate on clicks and gestures</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-haptics" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Tap to Click Toggle -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Tap to click</span>
        <span class="setting-desc">A short tap triggers a left click</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-tap-click" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Left Handed Mode -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Left-handed mode</span>
        <span class="setting-desc">Mirror the buttons and the scroll strip</span>
      </div>
      <label class="toggle">
        <input type="checkbox" id="cfg-left-handed">
        <span class="toggle-slider"></span>
      </label>
    </div>

    <!-- Theme Selection -->
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-title">Colour theme</span>
        <span class="setting-desc">Visual style of the interface</span>
      </div>
      <div class="theme-options">
        <button class="theme-btn active" data-theme="default">Slate</button>
        <button class="theme-btn" data-theme="cyber">Cyber</button>
        <button class="theme-btn" data-theme="oled">OLED</button>
      </div>
    </div>
  </div>
</div>

<script>
(() => {
  "use strict";

  // --- Parameter & State
  const token = new URLSearchParams(location.search).get("t") || "";
  let mode = new URLSearchParams(location.search).get("transport") === "http" ? "http" : "ws";

  // DOM Elements
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const pingText = document.getElementById("ping-text");
  const statusPill = document.getElementById("status-pill");
  const pad = document.getElementById("pad");
  const edgeScroll = document.getElementById("edge-scroll");
  const edgeThumb = document.getElementById("edge-thumb");
  const dragToast = document.getElementById("drag-toast");
  const btnWakeLock = document.getElementById("btn-wakelock");
  const btnFullscreen = document.getElementById("btn-fullscreen");
  const btnSettings = document.getElementById("btn-settings");
  const btnCloseSettings = document.getElementById("btn-close-settings");
  const settingsSheet = document.getElementById("settings-sheet");

  // Config with localStorage persistence
  const config = {
    speed: parseFloat(localStorage.getItem("rm_speed") || "1.6"),
    accel: localStorage.getItem("rm_accel") !== "false",
    scrollSpeed: parseFloat(localStorage.getItem("rm_scroll_speed") || "1.2"),
    invertScroll: localStorage.getItem("rm_invert_scroll") === "true",
    edgeScroll: localStorage.getItem("rm_edge_scroll") !== "false",
    haptics: localStorage.getItem("rm_haptics") !== "false",
    tapClick: localStorage.getItem("rm_tap_click") !== "false",
    leftHanded: localStorage.getItem("rm_left_handed") === "true",
    theme: localStorage.getItem("rm_theme") || "default",
  };

  // Apply saved config to DOM
  const applyConfigToUI = () => {
    document.getElementById("cfg-speed").value = config.speed;
    document.getElementById("val-speed").textContent = config.speed.toFixed(1) + "x";
    document.getElementById("cfg-accel").checked = config.accel;
    document.getElementById("cfg-scroll-speed").value = config.scrollSpeed;
    document.getElementById("val-scroll-speed").textContent = config.scrollSpeed.toFixed(1) + "x";
    document.getElementById("cfg-invert-scroll").checked = config.invertScroll;
    document.getElementById("cfg-edge-scroll").checked = config.edgeScroll;
    document.getElementById("cfg-haptics").checked = config.haptics;
    document.getElementById("cfg-tap-click").checked = config.tapClick;
    document.getElementById("cfg-left-handed").checked = config.leftHanded;

    edgeScroll.style.display = config.edgeScroll ? "flex" : "none";
    document.body.classList.toggle("left-handed", config.leftHanded);
    document.body.className = `theme-${config.theme} ${config.leftHanded ? "left-handed" : ""}`.trim();

    document.querySelectorAll(".theme-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.theme === config.theme);
    });
  };
  applyConfigToUI();

  // Haptic feedback helper
  const vibrate = (ms = 12) => {
    if (config.haptics && navigator.vibrate) {
      try { navigator.vibrate(ms); } catch (e) {}
    }
  };

  // --- Network Transport & Latency Tracking
  let ws = null, queue = [], inflight = false;
  let wsFails = 0, openedAt = 0;
  let pingStart = 0, pingInterval = null;

  const setState = (txt, stateClass) => {
    statusText.textContent = txt;
    statusDot.className = `dot ${stateClass}`;
    if (stateClass !== "connected") pingText.textContent = "";
  };

  const CONNECT_TIMEOUT = 2500;

  function connect() {
    if (mode !== "ws") return;
    let sock;
    try {
      sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?t=${encodeURIComponent(token)}`);
    } catch (e) {
      toHttp("WebSocket unavailable");
      return;
    }
    ws = sock;
    setState("connecting…", "connecting");

    let retired = false, opened = false;

    // Retires this attempt exactly once, so a late onclose after an onerror or
    // a timeout cannot start a second retry chain.
    function retire(why) {
      if (retired) return;
      retired = true;
      clearTimeout(timer);
      clearInterval(pingInterval);
      try { sock.close(); } catch (e) {}
      // A handshake that never opened, or a connection that dropped right
      // after opening, counts towards the HTTP fallback.
      const shortLived = !opened || Date.now() - openedAt < 2000;
      if (shortLived && ++wsFails >= 2) {
        toHttp(why);
        return;
      }
      setState(`${why} – retrying…`, "connecting");
      setTimeout(connect, 600);
    }

    const timer = setTimeout(() => retire("Timeout"), CONNECT_TIMEOUT);

    sock.onopen = () => {
      if (retired) return;
      opened = true;
      clearTimeout(timer);
      openedAt = Date.now();
      wsFails = 0;
      setState("connected (WS)", "connected");

      // Ping interval for live latency
      clearInterval(pingInterval);
      pingInterval = setInterval(() => {
        if (sock.readyState === 1) {
          pingStart = performance.now();
          sock.send(JSON.stringify({ t: "ping", id: Math.floor(pingStart) }));
        }
      }, 3000);
    };

    sock.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.t === "pong") {
          const lat = Math.round(performance.now() - pingStart);
          pingText.textContent = `${lat}ms`;
        }
      } catch (e) {}
    };

    sock.onerror = () => retire("WS error");
    sock.onclose = (ev) => retire(opened ? "disconnected" : `WS refused (${ev.code})`);
  }

  function toHttp(why) {
    mode = "http";
    ws = null;
    clearInterval(pingInterval);
    setState("connected (HTTP)", "http");
    console.log("Falling back to HTTP:", why);
  }

  let btnDownCount = 0, lastSentAt = 0;

  function send(obj) {
    if (obj.t === "b") btnDownCount = Math.max(0, btnDownCount + (obj.d ? 1 : -1));
    queue.push(JSON.stringify(obj));
    if (queue.length > 240) queue.splice(0, queue.length - 240);
  }

  function flush() {
    requestAnimationFrame(flush);
    if (btnDownCount > 0 && Date.now() - lastSentAt > 1000) send({ t: "hb" });
    if (!queue.length) return;
    lastSentAt = Date.now();

    if (mode === "ws") {
      if (ws && ws.readyState === 1) {
        ws.send(queue.join("\n"));
        queue = [];
      }
      return;
    }
    if (inflight) return;
    const body = queue.join("\n");
    queue = [];
    inflight = true;
    fetch(`/i?t=${encodeURIComponent(token)}`, {
      method: "POST", body, keepalive: true, headers: { "Content-Type": "text/plain" }
    }).catch(() => setState("HTTP error", "danger")).finally(() => { inflight = false; });
  }

  if (mode === "http") setState("connected (HTTP)", "http"); else connect();
  requestAnimationFrame(flush);

  statusPill.addEventListener("click", () => {
    vibrate(20);
    if (mode === "ws") {
      try { ws && ws.close(); } catch (e) {}
      wsFails = 0;
      connect();
    } else {
      mode = "ws";
      wsFails = 0;
      connect();
    }
  });

  // --- Gestures & trackpad control
  const TAP_MS = 240, TAP_SLOP = 10;
  const pts = new Map();
  let moved = 0, startTime = 0, maxPts = 0;
  let dragging = false, dragLocked = false, lastTapEnd = 0, scrollAcc = 0;

  // Acceleration curve
  const accel = (d) => {
    const s = config.speed;
    if (!config.accel) return d * s;
    const sign = Math.sign(d);
    const mag = Math.abs(d);
    const multiplier = 1 + Math.min(2.5, mag / 7);
    return sign * mag * s * multiplier;
  };

  const setDragVisual = (active) => {
    pad.classList.toggle("drag-active", active);
    dragToast.classList.toggle("visible", active);
    document.getElementById("btn-drag").classList.toggle("locked", dragLocked);
  };

  pad.addEventListener("pointerdown", (ev) => {
    pad.setPointerCapture(ev.pointerId);
    pts.set(ev.pointerId, { x: ev.clientX, y: ev.clientY, sx: ev.clientX, sy: ev.clientY });

    // Touch Indicator Ripple
    const rect = pad.getBoundingClientRect();
    const ripple = document.createElement("div");
    ripple.className = "touch-ripple";
    ripple.style.left = `${ev.clientX - rect.left}px`;
    ripple.style.top = `${ev.clientY - rect.top}px`;
    pad.appendChild(ripple);
    setTimeout(() => ripple.remove(), 400);

    if (pts.size === 1) {
      moved = 0;
      startTime = performance.now();
      maxPts = 1;
      pad.classList.add("active");
    }
    maxPts = Math.max(maxPts, pts.size);

    // Double-tap + hold = drag & drop
    if (pts.size === 1 && performance.now() - lastTapEnd < 320) {
      dragging = true;
      vibrate(30);
      send({ t: "b", b: "l", d: 1 });
      setDragVisual(true);
    }
  });

  pad.addEventListener("pointermove", (ev) => {
    const p = pts.get(ev.pointerId);
    if (!p) return;
    const dx = ev.clientX - p.x, dy = ev.clientY - p.y;
    p.x = ev.clientX; p.y = ev.clientY;
    moved = Math.max(moved, Math.hypot(ev.clientX - p.sx, ev.clientY - p.sy));

    if (pts.size >= 2) {
      // 2 fingers = scroll
      if (ev.pointerId !== pts.keys().next().value) return;
      const factor = config.invertScroll ? -1 : 1;
      scrollAcc += (dy * factor * config.scrollSpeed) / 12;
      const ticks = Math.trunc(scrollAcc);
      if (ticks) {
        scrollAcc -= ticks;
        send({ t: "s", dy: ticks });
      }
    } else {
      // 1 finger = pointer movement
      send({ t: "m", dx: accel(dx), dy: accel(dy) });
    }
  });

  const endPointer = (ev) => {
    if (!pts.has(ev.pointerId)) return;
    pts.delete(ev.pointerId);
    if (pts.size) return;

    pad.classList.remove("active");
    const dt = performance.now() - startTime;

    if (dragging) {
      if (!dragLocked) {
        dragging = false;
        send({ t: "b", b: "l", d: 0 });
        setDragVisual(false);
      }
    } else if (config.tapClick && dt < TAP_MS && moved < TAP_SLOP) {
      if (maxPts >= 2) {
        // Two-finger tap = right click
        vibrate([10, 40, 15]);
        send({ t: "c", b: "r" });
      } else {
        // One-finger tap = left click
        vibrate(12);
        send({ t: "c", b: "l" });
        lastTapEnd = performance.now();
      }
    }
    scrollAcc = 0;
  };

  pad.addEventListener("pointerup", endPointer);
  pad.addEventListener("pointercancel", endPointer);

  dragToast.addEventListener("click", () => {
    dragLocked = false;
    dragging = false;
    send({ t: "b", b: "l", d: 0 });
    setDragVisual(false);
    vibrate(20);
  });

  // --- Edge Scroll Strip
  let edgeStartY = 0, edgeLastY = 0, edgeAccum = 0;
  edgeScroll.addEventListener("pointerdown", (ev) => {
    edgeScroll.setPointerCapture(ev.pointerId);
    edgeScroll.classList.add("active");
    edgeStartY = ev.clientY;
    edgeLastY = ev.clientY;
    edgeAccum = 0;
    vibrate(10);
  });

  edgeScroll.addEventListener("pointermove", (ev) => {
    if (!edgeScroll.hasPointerCapture(ev.pointerId)) return;
    const dy = ev.clientY - edgeLastY;
    edgeLastY = ev.clientY;

    const rect = edgeScroll.getBoundingClientRect();
    const relY = Math.max(0, Math.min(rect.height - 38, ev.clientY - rect.top - 19));
    edgeThumb.style.transform = `translateY(${relY}px)`;

    const factor = config.invertScroll ? -1 : 1;
    edgeAccum += (dy * factor * config.scrollSpeed) / 8;
    const ticks = Math.trunc(edgeAccum);
    if (ticks) {
      edgeAccum -= ticks;
      send({ t: "s", dy: ticks });
      vibrate(6);
    }
  });

  const endEdgeScroll = (ev) => {
    edgeScroll.classList.remove("active");
    edgeThumb.style.transform = `translateY(0)`;
    edgeAccum = 0;
  };
  edgeScroll.addEventListener("pointerup", endEdgeScroll);
  edgeScroll.addEventListener("pointercancel", endEdgeScroll);

  // --- Mouse Buttons Deck
  const bindMouseBtn = (id, btnCode) => {
    const el = document.getElementById(id);
    el.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      el.setPointerCapture(ev.pointerId);
      el.classList.add("down");
      vibrate(15);
      send({ t: "b", b: btnCode, d: 1 });
    });
    const up = () => {
      el.classList.remove("down");
      send({ t: "b", b: btnCode, d: 0 });
    };
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };

  bindMouseBtn("btn-left", "l");
  bindMouseBtn("btn-mid", "m");
  bindMouseBtn("btn-right", "r");

  // Drag Lock Button
  const btnDrag = document.getElementById("btn-drag");
  btnDrag.addEventListener("click", () => {
    dragLocked = !dragLocked;
    dragging = dragLocked;
    vibrate(dragLocked ? [20, 40, 20] : 15);
    send({ t: "b", b: "l", d: dragLocked ? 1 : 0 });
    setDragVisual(dragLocked);
  });

  // --- Layer Panels (touchpad stays visible, panels stack below it)
  const panelsWrap = document.getElementById("panels");

  const setPanel = (name, on, persist = true) => {
    const panel = document.getElementById(`panel-${name}`);
    const btn = document.querySelector(`.layer-btn[data-panel="${name}"]`);
    panel.classList.toggle("open", on);
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    const anyOpen = !!panelsWrap.querySelector(".panel.open");
    panelsWrap.classList.toggle("empty", !anyOpen);
    document.body.classList.toggle("panels-open", anyOpen);
    if (persist) localStorage.setItem(`rm_panel_${name}`, on);
  };

  document.querySelectorAll(".layer-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.panel;
      const on = !document.getElementById(`panel-${name}`).classList.contains("open");
      vibrate(10);
      setPanel(name, on);
      if (on) {
        requestAnimationFrame(() => {
          document.getElementById(`panel-${name}`).scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
      }
    });
  });

  // Media is on by default so the extra layers are discoverable; keyboard is opt-in.
  setPanel("media", localStorage.getItem("rm_panel_media") !== "false", false);
  setPanel("keyboard", localStorage.getItem("rm_panel_keyboard") === "true", false);

  // --- Keyboard & Hotkeys
  const sendText = () => {
    const input = document.getElementById("text-input");
    const val = input.value;
    if (!val) return;
    vibrate(15);
    send({ t: "text", text: val });
    input.value = "";
    input.blur();
  };

  document.getElementById("btn-send-text").addEventListener("click", sendText);
  document.getElementById("text-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      sendText();
    }
  });

  // Key and Combo buttons
  document.querySelectorAll("[data-key]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.key;
      vibrate(14);
      send({ t: "k", k: key });
    });
  });

  document.querySelectorAll("[data-combo]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const combo = btn.dataset.combo.split(",");
      vibrate(18);
      send({ t: "combo", keys: combo });
    });
  });

  // --- Wake Lock API
  let wakeLock = null;
  const toggleWakeLock = async () => {
    if (!navigator.wakeLock) {
      alert("This browser does not support the Wake Lock API.");
      return;
    }
    try {
      if (wakeLock) {
        await wakeLock.release();
        wakeLock = null;
        btnWakeLock.classList.remove("active");
        vibrate(10);
      } else {
        wakeLock = await navigator.wakeLock.request("screen");
        btnWakeLock.classList.add("active");
        vibrate([15, 30, 15]);
        wakeLock.addEventListener("release", () => {
          btnWakeLock.classList.remove("active");
          wakeLock = null;
        });
      }
    } catch (err) {
      console.warn("WakeLock Error:", err);
    }
  };
  btnWakeLock.addEventListener("click", toggleWakeLock);

  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState === "visible") {
      if (btnWakeLock.classList.contains("active") && !wakeLock) {
        try {
          wakeLock = await navigator.wakeLock.request("screen");
        } catch (e) {}
      }
      if (mode === "ws" && (!ws || ws.readyState > 1)) {
        connect();
      }
    }
  });

  // --- Fullscreen Toggle
  btnFullscreen.addEventListener("click", () => {
    vibrate(12);
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
      btnFullscreen.classList.add("active");
    } else {
      document.exitFullscreen().catch(() => {});
      btnFullscreen.classList.remove("active");
    }
  });

  // --- Settings Sheet
  btnSettings.addEventListener("click", () => {
    vibrate(12);
    settingsSheet.classList.add("open");
  });
  btnCloseSettings.addEventListener("click", () => {
    vibrate(10);
    settingsSheet.classList.remove("open");
  });
  settingsSheet.addEventListener("click", (ev) => {
    if (ev.target === settingsSheet) settingsSheet.classList.remove("open");
  });

  // Settings Controls Listeners
  document.getElementById("cfg-speed").addEventListener("input", (ev) => {
    config.speed = parseFloat(ev.target.value);
    document.getElementById("val-speed").textContent = config.speed.toFixed(1) + "x";
    localStorage.setItem("rm_speed", config.speed);
  });

  document.getElementById("cfg-accel").addEventListener("change", (ev) => {
    config.accel = ev.target.checked;
    localStorage.setItem("rm_accel", config.accel);
  });

  document.getElementById("cfg-scroll-speed").addEventListener("input", (ev) => {
    config.scrollSpeed = parseFloat(ev.target.value);
    document.getElementById("val-scroll-speed").textContent = config.scrollSpeed.toFixed(1) + "x";
    localStorage.setItem("rm_scroll_speed", config.scrollSpeed);
  });

  document.getElementById("cfg-invert-scroll").addEventListener("change", (ev) => {
    config.invertScroll = ev.target.checked;
    localStorage.setItem("rm_invert_scroll", config.invertScroll);
  });

  document.getElementById("cfg-edge-scroll").addEventListener("change", (ev) => {
    config.edgeScroll = ev.target.checked;
    edgeScroll.style.display = config.edgeScroll ? "flex" : "none";
    localStorage.setItem("rm_edge_scroll", config.edgeScroll);
  });

  document.getElementById("cfg-haptics").addEventListener("change", (ev) => {
    config.haptics = ev.target.checked;
    localStorage.setItem("rm_haptics", config.haptics);
  });

  document.getElementById("cfg-tap-click").addEventListener("change", (ev) => {
    config.tapClick = ev.target.checked;
    localStorage.setItem("rm_tap_click", config.tapClick);
  });

  document.getElementById("cfg-left-handed").addEventListener("change", (ev) => {
    config.leftHanded = ev.target.checked;
    document.body.classList.toggle("left-handed", config.leftHanded);
    localStorage.setItem("rm_left_handed", config.leftHanded);
  });

  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      config.theme = btn.dataset.theme;
      localStorage.setItem("rm_theme", config.theme);
      applyConfigToUI();
      vibrate(12);
    });
  });

  // Prevent default context menus and zoom gestures
  document.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("gesturestart", (e) => e.preventDefault());
})();
</script>
</body>
</html>
"""


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # Picks the interface that would route out
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Turn a phone into a modern touchpad over the LAN")
    ap.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    ap.add_argument("--token", default=os.environ.get("REMOTE_MOUSE_TOKEN"), help="Secret access token")
    ap.add_argument("--ip", help="Force the IP used in the URL/QR code (for hosts with several NICs)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Log every request")
    args = ap.parse_args()

    token = args.token or secrets.token_urlsafe(6)
    try:
        mouse = VirtualInputDevice()
    except PermissionError:
        raise SystemExit(
            "No access to /dev/uinput. Fix: sudo usermod -aG input $USER plus "
            "a udev rule KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\"."
        )

    httpd = Server((args.host, args.port), Handler)
    httpd.mouse = mouse
    httpd.token = token
    httpd.verbose = args.verbose
    url = f"http://{args.ip or lan_ip()}:{args.port}/?t={token}"

    print("\n  ========================================================")
    print("  🚀 Remote Mouse is running!")
    print(f"  📱 Open this on your phone:\n\n     {url}\n")
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
    print("  ⚙️  Stop with Ctrl+C.")
    print("  ========================================================\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        mouse.release_all()


if __name__ == "__main__":
    main()
