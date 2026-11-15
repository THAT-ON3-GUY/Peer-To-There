"""
common.py - the shared toolbox. Every other file imports from here.

Contains:
  * settings (ports, sizes, timeouts)
  * TransferError - the one error type the program reports to you
  * message framing - how JSON messages are sent over a TCP stream
  * password helpers - signing and checking without sending the password
  * small helpers - checksums, progress bar, finding your IP, safe file names

Nothing in this file opens connections or makes decisions on its own;
it only provides building blocks for transfer.py, discovery.py and filetransfer.py.
"""

import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import time

# -------------------- settings

DEFAULT_PORT = 5001             # TCP port used for file transfers (change with --port)
DISCOVERY_PORT = 5002           # UDP port used to find other computers
CHUNK_SIZE = 64 * 1024          # how many bytes we read/send at a time (64 KB)
MAX_HEADER_BYTES = 64 * 1024    # largest JSON message we'll accept
TIMEOUT_SECONDS = 30            # give up if a connection is silent this long
BEACON_INTERVAL = 3             # seconds between discovery "hello" broadcasts
PEER_EXPIRY = 12                # forget a computer we haven't heard from in this long
APP_ID = "filetransfer-v3"      # copies with a different ID ignore each other


# -------------------- errors

class TransferError(Exception):
    """A send/ping failed. Carries a friendly message for the user."""


# -------------------- message framing

def recv_exact(sock, n):
    """Read exactly n bytes from the socket (recv() can return fewer than asked)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock, obj):
    """Send a dict as length-prefixed JSON."""
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_msg(sock):
    """Receive a length-prefixed JSON message and return it as a dict."""
    (length,) = struct.unpack("!I", recv_exact(sock, 4))
    if length > MAX_HEADER_BYTES:
        raise ValueError(f"header too large ({length} bytes)")
    msg = json.loads(recv_exact(sock, length).decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("malformed message")
    return msg


# -------------------- password helpers

def sign(password, text):
    """HMAC-SHA256 of text using the password as the key."""
    return hmac.new(password.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def same_secret(a, b):
    """Constant-time comparison (doesn't leak how many characters matched)."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


# -------------------- file and display helpers

def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


class Progress:
    """Prints a one-line progress bar that updates in place."""

    def __init__(self, total, label):
        self.total = total
        self.label = label
        self.start = time.monotonic()
        self.last_draw = 0.0

    def update(self, done, force=False):
        now = time.monotonic()
        if not force and now - self.last_draw < 0.1:   # redraw at most 10x/second
            return
        self.last_draw = now
        pct = 100.0 if self.total == 0 else done * 100 / self.total
        speed = done / max(now - self.start, 1e-6)
        filled = int(30 * pct / 100)
        bar = "#" * filled + "-" * (30 - filled)
        sys.stdout.write(
            f"\r{self.label} [{bar}] {pct:5.1f}%  "
            f"{human_size(done)}/{human_size(self.total)}  {human_size(speed)}/s   "
        )
        sys.stdout.flush()

    def finish(self):
        self.update(self.total, force=True)
        sys.stdout.write("\n")


def local_ip():
    """Best guess at this machine's LAN IP address (no traffic is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def safe_filename(name):
    """Strip any directory parts so a sender can't write outside the destination folder."""
    name = os.path.basename(str(name).replace("\\", "/"))
    if name in ("", ".", ".."):
        raise ValueError("invalid file name")
    return name


def unique_path(folder, name):
    """Avoid overwriting: 'report.pdf' -> 'report (1).pdf' if it already exists."""
    path = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(path) or os.path.exists(path + ".part"):
        path = os.path.join(folder, f"{stem} ({i}){ext}")
        i += 1
    return path
