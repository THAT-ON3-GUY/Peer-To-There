"""
transfer.py - both sides of a file transfer: the SENDER and the RECEIVER.

They're in one file because they are two halves of the same conversation.
Read them side by side to follow a transfer step by step:

    SENDER (connect / run_sender)              RECEIVER (handle_connection)
    -----------------------------              ----------------------------
    opens TCP connection           ------->    accepts it
                                   <-------    1. hello: {nonce, auth needed?}
    2. header: {name, size,        ------->    checks password proof,
       sha256, proof}                          file name, disk space
                                   <-------    3. {"status": "ready"}  (or an error)
    4. raw file bytes ...          ------->    writes to name.part, hashing as it goes
                                   <-------    5. {"status": "ok"}  (or "error" if the
                                                  checksum didn't match)

A "ping" is the same, except step 2 has type "ping" and the receiver answers "pong".
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


# -------------------- SENDING SIDE

def connect(host, port, password):
    """Connect to a receiver and do the greeting. Returns (socket, proof)."""
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
            "  Make sure both computers have the same, latest version of filetransfer.py."
        )

    if hello.get("auth") and not password:
        sock.close()
        raise TransferError("That computer requires a password. Start this program with --password.")
    proof = sign(password, str(hello.get("nonce", ""))) if password else ""
    return sock, proof


def run_ping(host, port, password):
    start = time.monotonic()
    sock, proof = connect(host, port, password)
    with sock:
        try:
            send_msg(sock, {"type": "ping", "proof": proof})
            reply = recv_msg(sock)
        except (ConnectionError, OSError, ValueError) as e:
            raise TransferError(f"Ping failed: {e}")
    if reply.get("status") != "pong":
        raise TransferError(f"Ping failed: {reply.get('error', reply)}")
    ms = (time.monotonic() - start) * 1000
    print(f"Connected to '{reply.get('host', host)}' at {host}:{port} ({ms:.0f} ms). Ready to send files.")


def run_sender(path, host, port, password):
    if not os.path.isfile(path):
        raise TransferError(f"File not found: {path}")

    size = os.path.getsize(path)
    name = os.path.basename(path)
    print(f"Preparing {name} ({human_size(size)}) - computing checksum...")
    digest = sha256_of_file(path)

    sock, proof = connect(host, port, password)
    with sock:
        try:
            send_msg(sock, {"type": "file", "name": name, "size": size,
                            "sha256": digest, "proof": proof})
            reply = recv_msg(sock)
            if reply.get("status") != "ready":
                raise TransferError(f"Receiver refused the file: {reply.get('error', reply)}")

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
            raise TransferError("\nTransfer failed: the connection stalled (timed out).")
        except (ConnectionError, OSError, ValueError) as e:
            raise TransferError(f"\nTransfer failed: {e}")

    if result.get("status") == "ok":
        print(f"Done. Receiver verified the checksum and saved it as '{result['saved_as']}'.")
    else:
        raise TransferError(f"Transfer failed: {result.get('error', result)}")


# -------------------- RECEIVING SIDE

def handle_connection(conn, dest, password):
    nonce = secrets.token_hex(16)
    send_msg(conn, {"type": "hello", "host": socket.gethostname(),
                    "auth": bool(password), "nonce": nonce})
    header = recv_msg(conn)

    if password and not same_secret(header.get("proof", ""), sign(password, nonce)):
        send_msg(conn, {"status": "error", "error": "wrong password"})
        print("  Rejected: wrong or missing password.")
        return

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

    if hasher.hexdigest() != expected_hash:
        os.remove(part_path)
        send_msg(conn, {"status": "error", "error": "checksum mismatch - file was corrupted in transit"})
        print("  FAILED: checksum mismatch, partial file deleted.")
        return

    os.replace(part_path, final_path)
    send_msg(conn, {"status": "ok", "saved_as": os.path.basename(final_path)})
    print(f"  Saved and verified: {final_path}")


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
                print("  ERROR: connection stalled (timed out). Partial file deleted.")
            except (ConnectionError, ValueError, OSError) as e:
                print(f"  ERROR: {e}")
        if once:
            break


def prepare_dest(dest):
    os.makedirs(dest, exist_ok=True)
    return os.path.abspath(dest)


def run_receiver(port, dest, password, once):
    dest = prepare_dest(dest)
    with open_server(port) as server:
        print(f"Receiver listening on {local_ip()}:{port}")
        print(f"Saving files to: {dest}")
        print("Password required." if password else "No password set - anyone on the network can send files.")
        print("Press Ctrl+C to stop.\n")
        serve(server, dest, password, once)
