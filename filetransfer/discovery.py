"""
discovery.py - finds other computers running this program, using UDP broadcasts.

This is an optional add-on: transfer.py works without it if you type an IP address.
It only *finds* computers; it never sends files. When it finds one, it calls back into
filetransfer.py (the on_found function) so the prompt can tell you and select it.
"""

import json
import secrets
import socket
import threading
import time

from common import (
    APP_ID, BEACON_INTERVAL, DISCOVERY_PORT, PEER_EXPIRY,
    TransferError, sign, same_secret, local_ip,
)


class Discovery:
    """Finds other copies of this program on the network using UDP broadcasts."""

    def __init__(self, tcp_port, password, on_found):
        self.tcp_port = tcp_port
        self.password = password
        self.on_found = on_found            # called as on_found(ip, port, name) for new peers
        self.my_id = secrets.token_hex(8)   # lets us ignore our own broadcasts
        self.name = socket.gethostname()
        self.peers = {}                     # (ip, port) -> {"name": ..., "last_seen": ...}
        self.lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind(("", DISCOVERY_PORT))
        except OSError as e:
            self.sock.close()
            raise TransferError(f"Could not start discovery on UDP port {DISCOVERY_PORT} ({e}).")

    def _beacon(self):
        msg = {"app": APP_ID, "id": self.my_id, "name": self.name, "port": self.tcp_port}
        if self.password:
            msg["sig"] = sign(self.password, f"{self.my_id}|{self.name}|{self.tcp_port}")
        return json.dumps(msg).encode("utf-8")

    def _targets(self):
        # 255.255.255.255 reaches the local network; the x.y.z.255 address is a backup
        # for computers with several network adapters (common on Windows).
        targets = ["255.255.255.255"]
        parts = local_ip().split(".")
        if len(parts) == 4 and parts[0] != "127":
            targets.append(".".join(parts[:3] + ["255"]))
        return targets

    def _announce_loop(self):
        while True:
            for target in self._targets():
                try:
                    self.sock.sendto(self._beacon(), (target, DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(BEACON_INTERVAL)

    def _listen_loop(self):
        while True:
            try:
                data, (ip, _) = self.sock.recvfrom(4096)
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
                if not isinstance(msg, dict) or msg.get("app") != APP_ID or msg.get("id") == self.my_id:
                    continue
                name = str(msg.get("name", "?"))[:64]
                port = int(msg["port"])
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                continue

            # Only pair with programs that use the same password (or both use none).
            if self.password:
                expected = sign(self.password, f"{msg['id']}|{msg.get('name', '?')}|{msg['port']}")
                if not same_secret(msg.get("sig", ""), expected):
                    continue
            elif "sig" in msg:
                continue

            key = (ip, port)
            now = time.monotonic()
            with self.lock:
                old = self.peers.get(key)
                is_new = old is None or now - old["last_seen"] > PEER_EXPIRY
                self.peers[key] = {"name": name, "last_seen": now}
            if is_new:
                try:
                    self.sock.sendto(self._beacon(), (ip, DISCOVERY_PORT))  # answer right away
                except OSError:
                    pass
                self.on_found(ip, port, name)

    def start(self):
        threading.Thread(target=self._announce_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def current_peers(self):
        """Computers heard from recently, as a list of (ip, port, name)."""
        now = time.monotonic()
        with self.lock:
            return sorted((ip, port, info["name"]) for (ip, port), info in self.peers.items()
                          if now - info["last_seen"] <= PEER_EXPIRY)

    def close(self):
        self.sock.close()
