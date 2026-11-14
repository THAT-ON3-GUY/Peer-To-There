#!/usr/bin/env python3
"""
filetransfer.py - send a file from one computer to another on your local network over TCP.

Uses only Python's standard library (socket, hashlib, json, struct), so nothing extra to install.

On the RECEIVING computer (start this first):
    python filetransfer.py receive
    python filetransfer.py receive --port 5001 --dest ./received

On the SENDING computer:
    python filetransfer.py ping 192.168.1.42          # test the connection first
    python filetransfer.py send myfile.zip 192.168.1.42

How it works (the "protocol"):
    1. Sender connects and sends a small JSON header: file name, size, SHA-256 checksum.
       Each JSON message is prefixed with a 4-byte length so the receiver knows where it ends.
    2. Receiver checks the header (safe name, enough disk space) and replies "ready".
    3. Sender streams the raw file bytes.
    4. Receiver writes them to "<name>.part", hashing as it goes. When all bytes are in,
       it compares the checksum. If it matches, the .part file is renamed to the real name
       and the receiver replies "ok"; otherwise the partial file is deleted and it replies "error".
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import time

DEFAULT_PORT = 5001
CHUNK_SIZE = 64 * 1024          # 64 KB per read/send
MAX_HEADER_BYTES = 64 * 1024    # refuse absurdly large headers
TIMEOUT_SECONDS = 30            # give up if the connection stalls this long


# ---------------------------------------------------------------- helpers

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
    return json.loads(recv_exact(sock, length).decode("utf-8"))


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
        elapsed = max(now - self.start, 1e-6)
        speed = done / elapsed
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "#" * filled + "-" * (bar_len - filled)
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


# ---------------------------------------------------------------- receiver

def handle_connection(conn, dest):
    header = recv_msg(conn)

    if header.get("type") == "ping":
        send_msg(conn, {"status": "pong", "host": socket.gethostname()})
        print("  Ping received - connection works.")
        return

    try:
        name = safe_filename(header["name"])
        size = int(header["size"])
        expected_hash = str(header["sha256"]).lower()
        if size < 0:
            raise ValueError("negative file size")
    except (KeyError, ValueError, TypeError) as e:
        send_msg(conn, {"status": "error", "error": f"bad header: {e}"})
        print(f"  Rejected: bad header ({e})")
        return

    free = shutil.disk_usage(dest).free
    if size > free:
        msg = f"not enough disk space (need {human_size(size)}, have {human_size(free)})"
        send_msg(conn, {"status": "error", "error": msg})
        print(f"  Rejected: {msg}")
        return

    final_path = unique_path(dest, name)
    part_path = final_path + ".part"
    print(f"  Incoming: {name} ({human_size(size)})")
    send_msg(conn, {"status": "ready"})

    hasher = hashlib.sha256()
    received = 0
    progress = Progress(size, "  Receiving")
    try:
        with open(part_path, "wb") as f:
            while received < size:
                chunk = conn.recv(min(CHUNK_SIZE, size - received))
                if not chunk:
                    raise ConnectionError(
                        f"sender disconnected after {human_size(received)} of {human_size(size)}"
                    )
                f.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
                progress.update(received)
        progress.finish()
    except BaseException:
        sys.stdout.write("\n")
        if os.path.exists(part_path):
            os.remove(part_path)        # don't leave half-written files behind
        raise

    actual_hash = hasher.hexdigest()
    if actual_hash != expected_hash:
        os.remove(part_path)
        send_msg(conn, {"status": "error", "error": "checksum mismatch - file was corrupted in transit"})
        print("  FAILED: checksum mismatch, partial file deleted.")
        return

    os.replace(part_path, final_path)
    send_msg(conn, {"status": "ok", "saved_as": os.path.basename(final_path)})
    print(f"  Saved and verified: {final_path}")


def run_receiver(port, dest, once):
    os.makedirs(dest, exist_ok=True)
    dest = os.path.abspath(dest)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))      # listen on all network interfaces
        server.listen(1)
        print(f"Receiver listening on {local_ip()}:{port}")
        print(f"Saving files to: {dest}")
        print("Press Ctrl+C to stop.\n")

        while True:
            conn, addr = server.accept()
            with conn:
                conn.settimeout(TIMEOUT_SECONDS)
                print(f"Connection from {addr[0]}")
                try:
                    handle_connection(conn, dest)
                except socket.timeout:
                    print("  ERROR: connection stalled (timed out). Partial file deleted.")
                except (ConnectionError, ValueError, OSError) as e:
                    print(f"  ERROR: {e}")
            if once:
                break


# ---------------------------------------------------------------- sender

def connect(host, port):
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT_SECONDS)
    except ConnectionRefusedError:
        sys.exit(
            f"Could not connect to {host}:{port} - connection refused.\n"
            "  Is the receiver running on that computer, on that port?"
        )
    except socket.timeout:
        sys.exit(
            f"Could not connect to {host}:{port} - timed out.\n"
            "  Check the IP address, that both devices are on the same network,\n"
            "  and that the receiver's firewall allows incoming connections on this port."
        )
    except OSError as e:
        sys.exit(f"Could not connect to {host}:{port} - {e}")
    sock.settimeout(TIMEOUT_SECONDS)
    return sock


def run_ping(host, port):
    start = time.monotonic()
    with connect(host, port) as sock:
        send_msg(sock, {"type": "ping"})
        reply = recv_msg(sock)
    ms = (time.monotonic() - start) * 1000
    print(f"Connected to '{reply.get('host', host)}' at {host}:{port} ({ms:.0f} ms). Ready to send files.")


def run_sender(path, host, port):
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")

    size = os.path.getsize(path)
    name = os.path.basename(path)
    print(f"Preparing {name} ({human_size(size)}) - computing checksum...")
    digest = sha256_of_file(path)

    with connect(host, port) as sock:
        try:
            send_msg(sock, {"type": "file", "name": name, "size": size, "sha256": digest})
            reply = recv_msg(sock)
            if reply.get("status") != "ready":
                sys.exit(f"Receiver refused the file: {reply.get('error', reply)}")

            progress = Progress(size, "Sending")
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
                    progress.update(sent)
            progress.finish()

            result = recv_msg(sock)
        except socket.timeout:
            sys.exit("\nTransfer failed: the connection stalled (timed out).")
        except (ConnectionError, OSError) as e:
            sys.exit(f"\nTransfer failed: {e}")

    if result.get("status") == "ok":
        print(f"Done. Receiver verified the checksum and saved it as '{result['saved_as']}'.")
    else:
        sys.exit(f"Transfer failed: {result.get('error', result)}")


# ---------------------------------------------------------------- command line

def main():
    parser = argparse.ArgumentParser(description="Send files between computers on your LAN over TCP.")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("receive", help="wait for incoming files")
    r.add_argument("--port", type=int, default=DEFAULT_PORT)
    r.add_argument("--dest", default="received", help="folder to save files in (default: ./received)")
    r.add_argument("--once", action="store_true", help="exit after one connection")

    p = sub.add_parser("ping", help="test the connection to a receiver")
    p.add_argument("host", help="receiver's IP address, e.g. 192.168.1.42")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)

    s = sub.add_parser("send", help="send a file to a receiver")
    s.add_argument("file")
    s.add_argument("host", help="receiver's IP address, e.g. 192.168.1.42")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)

    args = parser.parse_args()
    try:
        if args.command == "receive":
            run_receiver(args.port, args.dest, args.once)
        elif args.command == "ping":
            run_ping(args.host, args.port)
        else:
            run_sender(args.file, args.host, args.port)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
