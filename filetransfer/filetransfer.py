#!/usr/bin/env python3
"""
filetransfer.py - the program you run. Handles everything you type.

TWO-WAY MODE (easiest): run this on BOTH computers (or just press Run in VS Code).
    python filetransfer.py
They find each other automatically. Then, at the > prompt on either computer:
    > send photo.jpg       upload a file to the other computer
    > list                 see the files the other computer is sharing
    > get photo.jpg        download one of them

ONE-WAY MODE:
    python filetransfer.py receive                       (on the computer answering requests)
    python filetransfer.py ping 192.168.1.42             (on the other computer)
    python filetransfer.py send photo.jpg 192.168.1.42
    python filetransfer.py list 192.168.1.42
    python filetransfer.py get photo.jpg 192.168.1.42

The program asks for a password when it starts (press Enter for none).
Use the same password on both computers. You can also give it as --password <value>.

Files in this program:
    filetransfer.py  - this file: command line and the two-way > prompt
    transfer.py      - sending and receiving files over TCP
    discovery.py     - finding other computers on the network (UDP)
    common.py        - settings and shared helpers used by all of the above

Ports: TCP 5001 for transfers (change with --port), UDP 5002 for discovery.
Note: transfers are not encrypted. Someone on the same network could capture file contents.
"""

import argparse
import getpass
import socket
import sys
import threading

from common import DEFAULT_PORT, TransferError, local_ip
from discovery import Discovery
from transfer import (
    open_server, prepare_dest, run_get, run_list, run_ping, run_receiver, run_sender, serve,
)

ASK = object()      # marker meaning "--password was given without a value"


# -------------------- two-way mode (the > prompt)

HELP_TEXT = """Commands:
  peers            list computers found on the network
  peer <n | ip>    choose the other computer: a number from 'peers', or an IP address
  ping             test the connection to the chosen computer
  send <file>      upload a file (tip: type 'send ' then drag a file into this window)
  list             see the files the chosen computer is sharing
  get <name>       download one of those files
  help             show this list
  quit             exit"""


def strip_quotes(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text


def run_interactive(port, dest, password, peer_ip, discover):
    dest = prepare_dest(dest)
    server = open_server(port)
    threading.Thread(target=serve, args=(server, dest, password), daemon=True).start()

    state = {"peer": (peer_ip, DEFAULT_PORT, peer_ip) if peer_ip else None}
    state_lock = threading.Lock()

    def on_found(ip, peer_port, name):
        with state_lock:
            auto = state["peer"] is None
            if auto:
                state["peer"] = (ip, peer_port, name)
        msg = f"\nFound '{name}' at {ip}."
        msg += " Selected it - type 'send <file>' to send." if auto else " Type 'peers' to see everyone."
        print(msg)
        sys.stdout.write("> ")
        sys.stdout.flush()

    discovery = None
    if discover:
        try:
            discovery = Discovery(port, password, on_found)
            discovery.start()
        except TransferError as e:
            print(f"{e}\nDiscovery is off - use 'peer <ip>' instead.")

    print(f"Two-way mode. This computer is '{socket.gethostname()}' at {local_ip()} (port {port}).")
    print(f"Incoming files are saved to: {dest}")
    print("Password is set." if password else
          "No password set - anyone on the network running this program can send you files.")
    if discovery:
        print("Looking for other computers running this program...")
    print()
    print(HELP_TEXT)

    last_list = []
    while True:
        try:
            line = input("\n> ").strip()
        except EOFError:
            break
        if not line:
            continue
        command, _, rest = line.partition(" ")
        command = command.lower()
        rest = strip_quotes(rest)
        with state_lock:
            peer = state["peer"]

        try:
            if command in ("quit", "exit"):
                break
            elif command == "help":
                print(HELP_TEXT)
            elif command == "peers":
                last_list = discovery.current_peers() if discovery else []
                if not last_list:
                    print("No computers found yet. Is the program running on the other one, "
                          "with the same password?\nYou can also type 'peer <ip>'.")
                for i, (ip, p, name) in enumerate(last_list, 1):
                    mark = "  <- selected" if peer and peer[:2] == (ip, p) else ""
                    print(f"  {i}. {name}  ({ip}){mark}")
            elif command == "peer":
                if not rest:
                    print(f"Selected: {peer[2]} ({peer[0]})" if peer else "No computer selected.")
                    print("Usage: peer <number from 'peers'>  or  peer <ip>")
                elif rest.isdigit():
                    # A plain number means "pick from the peers list".
                    if not last_list and discovery:
                        last_list = discovery.current_peers()
                    if 1 <= int(rest) <= len(last_list):
                        ip, p, name = last_list[int(rest) - 1]
                        with state_lock:
                            state["peer"] = (ip, p, name)
                        print(f"Selected {name} ({ip}).")
                    else:
                        print(f"There's no number {rest} in the list. Type 'peers' to see it.")
                else:
                    with state_lock:
                        state["peer"] = (rest, DEFAULT_PORT, rest)
                    print(f"Selected {rest}.")
            elif command in ("ping", "send", "list", "get"):
                if not peer:
                    print("No computer selected yet. Type 'peers', or 'peer <ip>'.")
                elif command == "ping":
                    run_ping(peer[0], peer[1], password)
                elif command == "list":
                    run_list(peer[0], peer[1], password)
                elif not rest:
                    print(f"Usage: {command} <{'file' if command == 'send' else 'name'}>")
                elif command == "send":
                    run_sender(rest, peer[0], peer[1], password)
                else:
                    run_get(rest, peer[0], peer[1], password, dest)
            else:
                print(f"Unknown command '{command}'. Type 'help' for the list.")
        except TransferError as e:
            print(str(e).strip())

    if discovery:
        discovery.close()
    server.close()


# -------------------- command line

def main():
    parser = argparse.ArgumentParser(description="Send files between computers on your LAN over TCP.")
    sub = parser.add_subparsers(dest="command")

    def add_password(p):
        p.add_argument("--password", nargs="?", const=ASK, default=None,
                       help="shared password (leave the value off to type it hidden)")

    t = sub.add_parser("start", help="two-way mode: find other computers, send and receive")
    t.add_argument("peer", nargs="?", help="the other computer's IP (optional - it's found automatically)")
    t.add_argument("--port", type=int, default=DEFAULT_PORT)
    t.add_argument("--dest", default="received", help="folder to save files in (default: ./received)")
    t.add_argument("--no-discovery", action="store_true", help="don't look for other computers")
    add_password(t)

    r = sub.add_parser("receive", help="wait for incoming files")
    r.add_argument("--port", type=int, default=DEFAULT_PORT)
    r.add_argument("--dest", default="received", help="folder to save files in (default: ./received)")
    r.add_argument("--once", action="store_true", help="exit after one connection")
    add_password(r)

    p = sub.add_parser("ping", help="test the connection to a receiver")
    p.add_argument("host", help="receiver's IP address, e.g. 192.168.1.42")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    add_password(p)

    s = sub.add_parser("send", help="send a file to a receiver")
    s.add_argument("file")
    s.add_argument("host", help="receiver's IP address, e.g. 192.168.1.42")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    add_password(s)

    l = sub.add_parser("list", help="see the files another computer is sharing")
    l.add_argument("host", help="the other computer's IP address, e.g. 192.168.1.42")
    l.add_argument("--port", type=int, default=DEFAULT_PORT)
    add_password(l)

    g = sub.add_parser("get", help="download a file from another computer")
    g.add_argument("name", help="the file's name, as shown by 'list'")
    g.add_argument("host", help="the other computer's IP address, e.g. 192.168.1.42")
    g.add_argument("--port", type=int, default=DEFAULT_PORT)
    g.add_argument("--dest", default="received", help="folder to save it in (default: ./received)")
    add_password(g)

    args = parser.parse_args()
    if args.command is None:            # no command given (e.g. VS Code's Run button)
        args = parser.parse_args(["start"])

    try:
        password = args.password
        if password is None or password is ASK:
            password = getpass.getpass("Password (press Enter for none): ")
        if password == "":
            password = None

        if args.command == "start":
            run_interactive(args.port, args.dest, password, args.peer, not args.no_discovery)
        elif args.command == "receive":
            run_receiver(args.port, args.dest, password, args.once)
        elif args.command == "ping":
            run_ping(args.host, args.port, password)
        elif args.command == "list":
            run_list(args.host, args.port, password)
        elif args.command == "get":
            run_get(args.name, args.host, args.port, password, args.dest)
        else:
            run_sender(args.file, args.host, args.port, password)
    except TransferError as e:
        sys.exit(str(e).strip())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()