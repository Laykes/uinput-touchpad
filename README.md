# uinput-touchpad

Turn a phone into a trackpad and remote control for a Linux machine. The server
creates a virtual mouse and keyboard through `/dev/uinput` and serves a touch UI
that you open in the phone's browser. Nothing to install on the phone.

Everything lives in one file, `uinput-touchpad.py`. It needs the Python standard
library and `python-evdev`, nothing else. There is no build step, no bundler and
no daemon to register: copy the file to a machine and run it. That is the main
reason to pick this over the more featureful alternatives listed at the bottom.

## Requirements

- The phone and the machine on the same local network
- Linux with `uinput` available
- Python 3.8 or newer
- [`python-evdev`](https://pypi.org/project/evdev/) (`pip install -r requirements.txt`)
- Write access to `/dev/uinput`
- Optional: `qrencode`, to get a scannable QR code in the terminal

Running as root is not required and not recommended. To grant access to
`/dev/uinput` permanently:

```bash
sudo usermod -aG input $USER
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
```

Log out and back in for the group change to apply.

## Usage

```bash
python3 uinput-touchpad.py
```

The server prints a URL containing a freshly generated token, plus a QR code if
`qrencode` is installed:

```
http://192.168.2.129:8000/?t=aB3xY1kQ
```

Open it on the phone. Both devices have to be on the same network. Stop the
server with Ctrl-C.

If the page does not load, a firewall on the host is the usual reason. See
[Troubleshooting](#troubleshooting).

### Options

| Flag | Description |
| --- | --- |
| `--port PORT` | Listening port (default `8000`) |
| `--host ADDR` | Bind address (default `0.0.0.0`) |
| `--token STR` | Use a fixed token instead of a random one |
| `--ip ADDR` | Force the IP used in the printed URL and QR code |
| `-v`, `--verbose` | Log every request and WebSocket event |

A token can also come from the `UINPUT_TOUCHPAD_TOKEN` environment variable, which
keeps it out of the process list.

`--ip` is useful on hosts with several interfaces, where the address picked
automatically is not the one the phone can reach.

## Controls

The trackpad and its mouse buttons are always on screen. The keyboard and the
media panel are two extra layers that you fold in and out with the two toggle
buttons at the bottom edge; both can be open at the same time, and the trackpad
just gets smaller. Which layers are open is remembered in the browser.

### Trackpad

| Gesture | Action |
| --- | --- |
| One finger drag | Move the pointer |
| Tap | Left click |
| Two-finger tap | Right click |
| Two-finger drag | Scroll vertically and horizontally |
| Double tap, hold, drag | Drag and drop |
| Drag along the right edge | Scroll one-handed |
| Buttons at the bottom | Left, middle, right; hold to keep pressed |

The lock button next to the mouse buttons holds the left button down until you
press it again, which is easier than a long double-tap-hold for selecting text
or dragging across a large window.

### Keyboard

A text field sends whole strings at once, so you can type or dictate a URL on
the phone and have it appear on the PC. There are buttons for Esc, Tab,
Backspace, Enter, Space, Delete, arrow keys, and for Ctrl+C, Ctrl+V, Ctrl+Z,
Ctrl+A, Ctrl+W, Alt+Tab, Super and F5.

Text input assumes a **US keyboard layout on the host**. See Limitations.

### Media and presentation

Play/pause, previous and next track, volume up, volume down and mute. Page Up
and Page Down for slides, F11 for fullscreen.

### Settings

Pointer speed, acceleration curve, scroll speed, inverted scrolling, edge
scrolling, haptic feedback, tap-to-click and a left-handed mode that mirrors the
buttons and the edge scroll strip. Three colour schemes, including a black one
for OLED displays. Settings are stored in the browser's local storage, so they
survive a reload but are per-device.

The status pill at the top shows the transport and the current round-trip
latency. Tapping it forces a reconnect. There is also a wake lock toggle, which
keeps the phone's screen from turning off, and a fullscreen toggle.

## Transport

The page connects over a WebSocket first. If the handshake times out or the
connection drops twice shortly after opening, it falls back to HTTP POST. The
status pill shows which one is active. In a local network the difference is
barely noticeable.

Appending `&transport=http` to the URL skips the WebSocket attempt and its
timeout entirely.

## Security

Anyone who has the URL and its token controls the mouse and keyboard of the
machine running the server. Treat the token as a password.

- The token is regenerated on every start. A restart invalidates old links.
- Traffic is plain HTTP. It is neither encrypted nor authenticated beyond the
  token, and the token itself travels in the query string.
- Run this on a network you trust. Do not forward the port through a router and
  do not expose it to the internet.
- `--token` puts the token into the process list, where other local users can
  read it. Prefer `UINPUT_TOUCHPAD_TOKEN`.

If a connection dies while a button is held, for example because the phone's
screen turned off mid-drag, a watchdog releases every pressed key and button
after five seconds without a sign of life. Without it a held button would stay
down at the kernel level and leave the desktop unusable. While you hold a
button, the page sends a heartbeat once per second so the watchdog does not
interfere.

## Limitations

- **Text input is bound to the US layout.** `uinput` transmits key positions,
  not characters, so what arrives depends on the layout configured on the host.
  On a German layout `y` and `z` are swapped and most symbols land wrong. There
  is no layout option yet.
- **Characters outside that mapping are skipped.** Umlauts, `ß` and `€` cannot
  be produced. They are dropped and reported in the server log rather than
  silently swallowed.
- **Long text blocks the connection.** Typing runs at roughly 40 characters per
  second in the connection's thread, so pointer input pauses while a long string
  is being typed.
- Linux only. The virtual device is created through `evdev`, which has no
  equivalent on macOS or Windows.
- One client at a time is the intended use. Nothing enforces it; two connected
  phones will fight over the pointer.

## Troubleshooting

If the page does not load at all, the firewall is the usual cause:

```bash
# ufw
sudo ufw allow from 192.168.2.0/24 to any port 8000 proto tcp

# firewalld
sudo firewall-cmd --add-port=8000/tcp
```

Adjust the subnet to your own. The `firewalld` rule applies to the default
zone and is dropped on the next reload; add `--permanent` to keep it.

Run with `-v` to log every request, plus the reason a client disconnected and
how many frames it sent before doing so.

`PermissionError` on startup means `/dev/uinput` is not writable; see
Requirements.

If the pointer does not move but keyboard input works, check that the virtual
device is classified as a mouse:

```bash
udevadm info --query=property /dev/input/eventN | grep ID_INPUT
```

`ID_INPUT_MOUSE=1` should be present. Its absence means `libinput` did not give
the device pointer capabilities.

## Alternatives

[Unrud/remote-touchpad](https://github.com/Unrud/remote-touchpad) is the most
complete implementation of this idea. It supports Wayland through the
RemoteDesktop portal as well as X11 and Windows, ships as a Flatpak and handles
keyboard input properly across layouts. If you want something finished rather
than something small, use that one.

[KDE Connect](https://kdeconnect.kde.org/) has a virtual trackpad in its Remote
Input plugin, at the cost of installing its app on the phone.

## Notes on the implementation

The client is embedded in the Python file as a string rather than split into
separate assets. That keeps deployment to a single `scp`, which is the point of
the project, at the cost of a large source file.

The WebSocket handshake and framing are implemented directly against the
standard library instead of pulling in a dependency. Only text frames, ping and
close are handled, which is all the client sends.

## License

MIT
