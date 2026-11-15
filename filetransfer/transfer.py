"""
transfer.py - everything that happens over a TCP connection between two copies of the program.

The CLIENT (the computer making a request) and the SERVER (the computer answering it) are in
one file because they are two halves of the same conversation. In two-way mode, every
computer is both: its background server answers requests while you make your own.

Every conversation starts the same way:

    CLIENT                                    SERVER
    opens TCP connection          ------->    accepts it
                                  <-------    hello: {nonce, password needed?}
    request: {type, proof, ...}   ------->    checks the password proof, then
                                              handles the request based on its "type"

The server supports FOUR kinds of request:

  ping  "Are you there?"
        client -> {"type": "ping"}
        server -> {"status": "pong"}

  list  "What files do you have?"
        client -> {"type": "list"}
        server -> {"status": "ok", "files": [{"name": ..., "size": ...}, ...]}

  file  "Here's a file for you."  (upload / push)
        client -> {"type": "file", "name", "size", "sha256"}
        server -> {"status": "ready"}
        client -> <raw file bytes>
        server -> {"status": "ok"}  or an error if the checksum didn't match

  get   "Send me one of your files."  (download / pull)
        client -> {"type": "get", "name"}
        server -> {"status": "sending", "name", "size", "sha256"}
        server -> <raw file bytes>
        client -> {"status": "ok"}  or an error if the checksum didn't match

Any request can instead be answered with {"status": "error", "error": "<reason>"}.
The files a server shares (for list and get) are the ones in its received folder.
"""

import hashlib
import os
import secrets
import shutil
import socket
import sys
import time

from common import (
    CHUNK_SIZE, TIMEOUT_SECONDS, TransferError, Progress,
    send_msg, recv_msg, sign, same_secret,
    sha256_of_file, human_size, local_ip, safe_filename, unique_path,
)


# -------------------- moving file bytes (used by both sides)

def send_file_data(sock, path, size, label):
    """Stream a file's raw bytes over the socket, showing a progress bar."""
    progress = Progress(size, label)
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


def receive_file_data(sock, dest, name, size, expected_hash, label):
    """
    Read exactly `size` bytes from the socket into dest/name and verify the checksum.

    Returns the saved file's path, or None if the checksum didn't match.
    If the connection fails partway, raises the error. In every failure case the
    partial file is deleted, so a half-finished file is never left behind.
    """
    final_path = unique_path(dest, name)
    part_path = final_path + ".part"
    hasher = hashlib.sha256()
    received = 0
    progress = Progress(size, label)
    try:
        with open(part_path, "wb") as f:
            while received < size:
                chunk = sock.recv(min(CHUNK_SIZE, size - received))
                if not chunk:
                    raise ConnectionError(
                        f"connection lost after {human_size(received)} of {human_size(size)}"
                    )
                f.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
                progress.update(received)
        progress.finish()
    except BaseException:
        sys.stdout.write("\n")
        if os.path.exists(part_path):
            os.remove(part_path)
        raise

    if hasher.hexdigest() != str(expected_hash).lower():
        os.remove(part_path)
        return None
    os.replace(part_path, final_path)
    return final_path


def shareable_files(dest):
    """The files this computer offers for 'list' and 'get': regular files in its received folder."""
    files = []
    for name in sorted(os.listdir(dest), key=str.lower):
        path = os.path.join(dest, name)
        if name.startswith(".") or name.endswith(".part") or not os.path.isfile(path):
            continue        # skip hidden files (like .gitkeep), unfinished downloads and folders
        files.append({"name": name, "size": os.path.getsize(path)})
    return files


# -------------------- CLIENT SIDE (making requests)

def connect(host, port, password):
    """Connect to a server and do the greeting. Returns (socket, proof)."""
    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT_SECONDS)
    except ConnectionRefusedError:
        raise TransferError(
            f"Could not connect to {host}:{port} - connection refused.\n"
            "  Is the program running on that computer, on that port?"
        )
    except socket.timeout:
        raise TransferError(
            f"Could not connect to {host}:{port} - timed out.\n"
            "  Check the IP address, that both devices are on the same network,\n"
            "  and that the other computer's firewall allows this port."
        )
    except OSError as e:
        raise TransferError(f"Could not connect to {host}:{port} - {e}")

    sock.settimeout(TIMEOUT_SECONDS)
    try:
        hello = recv_msg(sock)
        if hello.get("type") != "hello":
            raise ValueError("unexpected greeting")
    except (ConnectionError, OSError, ValueError):
        sock.close()
        raise TransferError(
            f"{host}:{port} answered, but not like this program does.\n"
            "  Make sure both computers have the same, latest version of the program."
        )

    if hello.get("auth") and not password:
        sock.close()
        raise TransferError("That computer requires a password. Start this program with --password.")
    proof = sign(password, str(hello.get("nonce", ""))) if password else ""
    return sock, proof


def exchange(sock, message):
    """Send one request and wait for the reply, turning network problems into TransferError."""
    try:
        send_msg(sock, message)
        return recv_msg(sock)
    except socket.timeout:
        raise TransferError("No answer - the connection timed out.")
    except (ConnectionError, OSError, ValueError) as e:
        raise TransferError(f"Connection problem: {e}")


def run_ping(host, port, password):
    """Request type 'ping': check that the other computer is reachable and answering."""
    start = time.monotonic()
    sock, proof = connect(host, port, password)
    with sock:
        reply = exchange(sock, {"type": "ping", "proof": proof})
    if reply.get("status") != "pong":
        raise TransferError(f"Ping failed: {reply.get('error', reply)}")
    ms = (time.monotonic() - start) * 1000
    print(f"Connected to '{reply.get('host', host)}' at {host}:{port} ({ms:.0f} ms). Ready to send files.")


def run_list(host, port, password):
    """Request type 'list': show the files the other computer is sharing."""
    sock, proof = connect(host, port, password)
    with sock:
        reply = exchange(sock, {"type": "list", "proof": proof})
    if reply.get("status") != "ok":
        raise TransferError(f"List failed: {reply.get('error', reply)}")

    who = reply.get("host", host)
    files = reply.get("files", [])
    if not files:
        print(f"'{who}' has no files to share yet.")
        return
    print(f"Files on '{who}':")
    for f in files:
        print(f"  {f.get('name', '?')}  ({human_size(int(f.get('size', 0)))})")
    print("Type 'get <name>' to download one.")


def run_sender(path, host, port, password):
    """Request type 'file': upload a file to the other computer."""
    if not os.path.isfile(path):
        raise TransferError(f"File not found: {path}")

    size = os.path.getsize(path)
    name = os.path.basename(path)
    print(f"Preparing {name} ({human_size(size)}) - computing checksum...")
    digest = sha256_of_file(path)

    sock, proof = connect(host, port, password)
    with sock:
        reply = exchange(sock, {"type": "file", "name": name, "size": size,
                                "sha256": digest, "proof": proof})
        if reply.get("status") != "ready":
            raise TransferError(f"Receiver refused the file: {reply.get('error', reply)}")
        try:
            send_file_data(sock, path, size, "Sending")
            result = recv_msg(sock)
        except socket.timeout:
            raise TransferError("\nTransfer failed: the connection stalled (timed out).")
        except (ConnectionError, OSError, ValueError) as e:
            raise TransferError(f"\nTransfer failed: {e}")

    if result.get("status") == "ok":
        print(f"Done. Receiver verified the checksum and saved it as '{result['saved_as']}'.")
    else:
        raise TransferError(f"Transfer failed: {result.get('error', result)}")


def run_get(name, host, port, password, dest):
    """Request type 'get': download a file from the other computer into our dest folder."""
    dest = prepare_dest(dest)
    sock, proof = connect(host, port, password)
    with sock:
        reply = exchange(sock, {"type": "get", "name": name, "proof": proof})
        if reply.get("status") != "sending":
            raise TransferError(f"Could not get '{name}': {reply.get('error', reply)}")
        try:
            remote_name = safe_filename(reply["name"])
            size = int(reply["size"])
            expected_hash = str(reply["sha256"])
            if size < 0:
                raise ValueError("negative size")
        except (KeyError, ValueError, TypeError):
            raise TransferError("The other computer sent a malformed reply.")

        free = shutil.disk_usage(dest).free
        if size > free:
            raise TransferError(
                f"Not enough disk space (need {human_size(size)}, have {human_size(free)})."
            )

        print(f"Downloading {remote_name} ({human_size(size)})")
        try:
            saved = receive_file_data(sock, dest, remote_name, size, expected_hash, "Receiving")
        except socket.timeout:
            raise TransferError("\nDownload failed: the connection stalled (timed out).")
        except (ConnectionError, OSError) as e:
            raise TransferError(f"\nDownload failed: {e}")

        # Tell the server how it went (if this fails, our copy is still fine).
        try:
            if saved:
                send_msg(sock, {"status": "ok"})
            else:
                send_msg(sock, {"status": "error", "error": "checksum mismatch"})
        except OSError:
            pass

    if not saved:
        raise TransferError("Download failed: checksum mismatch - the file was corrupted in transit. "
                            "Nothing was saved.")
    print(f"Done. Verified and saved to: {saved}")


# -------------------- SERVER SIDE (answering requests)

def handle_connection(conn, dest, password):
    """Greet the client, check its password proof, then hand the request to the right handler."""
    nonce = secrets.token_hex(16)
    send_msg(conn, {"type": "hello", "host": socket.gethostname(),
                    "auth": bool(password), "nonce": nonce})
    request = recv_msg(conn)

    if password and not same_secret(request.get("proof", ""), sign(password, nonce)):
        send_msg(conn, {"status": "error", "error": "wrong password"})
        print("  Rejected: wrong or missing password.")
        return

    kind = request.get("type")
    if kind == "ping":
        handle_ping(conn)
    elif kind == "list":
        handle_list(conn, dest)
    elif kind == "file":
        handle_upload(conn, dest, request)
    elif kind == "get":
        handle_download(conn, dest, request)
    else:
        send_msg(conn, {"status": "error", "error": f"unknown request type '{kind}'"})
        print(f"  Rejected: unknown request type '{kind}'.")


def handle_ping(conn):
    """Answer 'ping': just say we're here."""
    send_msg(conn, {"status": "pong", "host": socket.gethostname()})
    print("  Ping received - connection works.")


def handle_list(conn, dest):
    """Answer 'list': send the names and sizes of the files we share."""
    files = shareable_files(dest)
    send_msg(conn, {"status": "ok", "host": socket.gethostname(), "files": files})
    print(f"  Sent the list of shared files ({len(files)}).")


def handle_upload(conn, dest, request):
    """Answer 'file': check the request, then receive, verify and save the file."""
    try:
        name = safe_filename(request["name"])
        size = int(request["size"])
        expected_hash = str(request["sha256"])
        if size < 0:
            raise ValueError("negative file size")
    except (KeyError, ValueError, TypeError) as e:
        send_msg(conn, {"status": "error", "error": f"bad request: {e}"})
        print(f"  Rejected: bad request ({e})")
        return

    free = shutil.disk_usage(dest).free
    if size > free:
        msg = f"not enough disk space (need {human_size(size)}, have {human_size(free)})"
        send_msg(conn, {"status": "error", "error": msg})
        print(f"  Rejected: {msg}")
        return

    print(f"  Incoming: {name} ({human_size(size)})")
    send_msg(conn, {"status": "ready"})
    saved = receive_file_data(conn, dest, name, size, expected_hash, "  Receiving")

    if not saved:
        send_msg(conn, {"status": "error", "error": "checksum mismatch - file was corrupted in transit"})
        print("  FAILED: checksum mismatch, partial file deleted.")
        return
    send_msg(conn, {"status": "ok", "saved_as": os.path.basename(saved)})
    print(f"  Saved and verified: {saved}")


def handle_download(conn, dest, request):
    """Answer 'get': send one of our shared files to the client."""
    try:
        name = safe_filename(request["name"])
    except (KeyError, ValueError):
        send_msg(conn, {"status": "error", "error": "bad file name"})
        return

    if name not in {f["name"] for f in shareable_files(dest)}:
        send_msg(conn, {"status": "error", "error": f"no shared file named '{name}'"})
        print(f"  Request for '{name}' - no such file.")
        return

    path = os.path.join(dest, name)
    size = os.path.getsize(path)
    print(f"  Request for {name} ({human_size(size)})")
    send_msg(conn, {"status": "sending", "name": name, "size": size,
                    "sha256": sha256_of_file(path)})
    send_file_data(conn, path, size, "  Sending")

    result = recv_msg(conn)
    if result.get("status") == "ok":
        print("  Sent - the other computer verified the checksum.")
    else:
        print(f"  The other computer reported a problem: {result.get('error', result)}")


def open_server(port):
    """Create the listening socket, or explain why we couldn't."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", port))      # listen on all network interfaces
    except OSError as e:
        server.close()
        raise TransferError(
            f"Could not listen on port {port} ({e}).\n"
            "  Is another copy of this program already running? Try a different --port."
        )
    server.listen(1)
    return server


def serve(server, dest, password, once=False):
    """Accept connections forever, handling one at a time."""
    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            return                          # server socket was closed
        with conn:
            conn.settimeout(TIMEOUT_SECONDS)
            print(f"\nConnection from {addr[0]}")
            try:
                handle_connection(conn, dest, password)
            except socket.timeout:
                print("  ERROR: connection stalled (timed out).")
            except (ConnectionError, ValueError, OSError) as e:
                print(f"  ERROR: {e}")
        if once:
            break


def prepare_dest(dest):
    """Create the folder if needed and return its full path."""
    os.makedirs(dest, exist_ok=True)
    return os.path.abspath(dest)


def run_receiver(port, dest, password, once):
    """One-way mode: answer requests until stopped."""
    dest = prepare_dest(dest)
    with open_server(port) as server:
        print(f"Receiver listening on {local_ip()}:{port}")
        print(f"Saving files to: {dest}")
        print("Password required." if password else "No password set - anyone on the network can send files.")
        print("Press Ctrl+C to stop.\n")
        serve(server, dest, password, once)