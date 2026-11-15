# Overview

Peer-To-There is a command-line program for sending files directly between two computers on the same local network, with no cloud service, USB drive or email attachment in between. As a software engineer, I wanted to understand what actually happens when two programs talk over a network: how a connection is opened, how a stream of bytes is split into meaningful messages, how one computer can find another without being told its address, and how to make sure a file arrives exactly as it was sent.

Every copy of the program is a **peer**: it listens for requests from other computers while also letting you make requests of your own. When two copies are running on the same network, they find each other automatically, and then either side can:

| Command | What it does |
|---|---|
| `ping` | checks that the other computer is reachable and answering |
| `list` | shows the files the other computer is sharing |
| `send <file>` | uploads a file to the other computer |
| `get <name>` | downloads one of the other computer's shared files |

Every transfer is verified with a SHA-256 checksum. A file is only saved if it arrives byte-for-byte identical to the original, and interrupted transfers never leave half-written files behind. An optional shared password keeps other people on the network from sending you files or browsing yours.

## How to use it

Follow [Setup](#setup) first (install Python and allow the program through your firewall). Then, on **both** computers:

1. Open a terminal in the `filetransfer` folder and run:
   ```
   python filetransfer.py
   ```
   (or open `filetransfer.py` in VS Code and press **Run**).
2. Type a password when asked. Use the **same password** on both computers, or press Enter on both for no password.
3. Within a few seconds each computer prints something like `Found 'LAPTOP' at 192.168.1.57. Selected it`.
4. At the `>` prompt on either computer, type commands:
   ```
   > ping
   > send C:\Users\me\Pictures\photo.jpg
   > list
   > get photo.jpg
   > quit
   ```
   Tip: type `send ` and then drag a file into the terminal window to paste its full path.

Received and downloaded files are saved in a `received` folder inside the folder you started the program from. The files in that folder are also the ones your computer shares for `list` and `get`.

| Prompt command | Purpose |
|---|---|
| `peers` | list the computers found on the network |
| `peer <number>` / `peer <ip>` | choose which computer to talk to (if discovery can't find it, type its IP) |
| `ping`, `list`, `send <file>`, `get <name>` | the four requests described above |
| `help` / `quit` | show the commands / exit |

### One-way (client/server) mode

The same program can also run as a dedicated server and a one-shot client:

```
python filetransfer.py receive                        # on the server: answer requests until Ctrl+C
python filetransfer.py ping 192.168.1.42              # on the client
python filetransfer.py list 192.168.1.42
python filetransfer.py send photo.jpg 192.168.1.42
python filetransfer.py get photo.jpg 192.168.1.42
```

Useful options: `--password <value>` (skip the password prompt), `--port <n>` (default 5001), `--dest <folder>` (where to save files, default `received`), `--no-discovery` (for `start`). Run `python filetransfer.py <command> --help` for details.

## Setup

### 1. Install Python (both computers)
1. Download Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
   - **Windows:** on the first installer screen, check **"Add python.exe to PATH."**
   - **macOS:** use `python3` wherever these instructions say `python`.
2. Confirm it works: `python --version`
3. Get the code onto both computers (`git clone https://github.com/THAT-ON3-GUY/Peer-To-There.git`, or copy the `filetransfer` folder). All four `.py` files must stay together in the same folder.

No extra packages are needed. The program uses only Python's standard library.

### 2. Allow the program through the firewall (both computers)

The program needs two incoming ports:

| Port | Protocol | Used for |
|---|---|---|
| 5001 | TCP | requests and file transfers |
| 5002 | UDP | automatically finding other computers |

**Windows.** Open PowerShell **as Administrator** and run:
```powershell
New-NetFirewallRule -DisplayName "filetransfer TCP" -Direction Inbound -Protocol TCP -LocalPort 5001 -RemoteAddress LocalSubnet -Action Allow -Profile Public,Private
New-NetFirewallRule -DisplayName "filetransfer UDP" -Direction Inbound -Protocol UDP -LocalPort 5002 -RemoteAddress LocalSubnet -Action Allow -Profile Public,Private
```
- `LocalSubnet` only allows devices on your own network, and these rules work whether Windows treats the network as Public or Private. You don't need to change your network to Private.
- On a shared network (apartment, dorm or office Wi-Fi), **always use a password**, because these rules let in any device on that network.
- If Windows shows an **"Allow Python?"** pop-up, click **Allow access**. Clicking Cancel creates a hidden rule that blocks Python, and block rules override allow rules. To remove such a rule:
  ```powershell
  Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*python*" -and $_.Action -eq "Block" } | Remove-NetFirewallRule
  ```
- To remove this program's rules later: `Remove-NetFirewallRule -DisplayName "filetransfer*"`

**macOS.** When prompted, allow incoming connections for Python, or add Python under System Settings → Network → Firewall → Options.
**Linux (ufw).** `sudo ufw allow 5001/tcp && sudo ufw allow 5002/udp`

### 3. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Computers never find each other | The network may block broadcasts. Type `peer <ip>` instead; the other computer's IP is printed when it starts. |
| `ping` says "connection refused" | The program isn't running on the other computer, or is using a different `--port`. |
| `ping` says "timed out" | A firewall on the other computer is blocking TCP 5001. On the computer you're connecting *from*, run `Test-NetConnection <other ip> -Port 5001` in PowerShell. `TcpTestSucceeded : False` means the port is blocked: check the rules above, any hidden Python block rule, and third-party antivirus firewalls (Norton, McAfee, etc.). |
| Discovery works but nothing else does | Some shared networks block direct device-to-device connections ("client isolation"). Use a phone hotspot or an Ethernet cable between the two computers. |
| "requires a password" / "wrong password" | Start both computers with the same password. |

{Describe your purpose for writing this software.}

[Software Demo Video](http://youtube.link.goes.here)

# Network Communication

### Architecture

**Peer-to-peer.** In the default mode (`start`), every copy of the program is both a server and a client at the same time:
- A background thread runs a TCP server that answers requests from other peers.
- The `>` prompt makes requests to the selected peer.
- Two more background threads handle discovery: one broadcasts a "hello" every 3 seconds, and one listens for other peers' hellos.

The program also supports a traditional **client/server** mode: `receive` runs only the server, while `ping`, `list`, `send` and `get` run a single client request and exit.

### Protocols and ports

| Protocol | Port | Purpose |
|---|---|---|
| TCP | 5001 (configurable with `--port`) | all requests and file data. TCP guarantees that bytes arrive complete and in order, which is essential for file transfer. |
| UDP | 5002 | discovery broadcasts. UDP supports broadcasting to every device on the network, and a lost "hello" doesn't matter because another one follows 3 seconds later. |

### Message format

**Control messages (TCP)** are JSON objects encoded as UTF-8, each preceded by a **4-byte big-endian unsigned length** (`struct` format `!I`):

```
[ 4-byte length N ][ N bytes of UTF-8 JSON ]
```

TCP delivers a continuous stream of bytes with no message boundaries, so the length prefix tells the receiver exactly where each message ends. Messages larger than 1 MB are rejected.

**File contents** are not wrapped in JSON. After the header messages, the raw bytes are streamed in 64 KB chunks, and the receiver reads exactly the number of bytes announced in the header.

**Every TCP conversation** starts with a greeting from the server, followed by one request from the client:

```
server -> {"type": "hello", "host": "DESKTOP", "auth": true, "nonce": "<random hex>"}
client -> {"type": "<request type>", "proof": "<HMAC-SHA256 of the nonce, keyed by the password>", ...}
```

The password itself is never sent over the network. The client proves it knows the password by signing the server's random one-time nonce, and the server checks the signature with a constant-time comparison.

**The four request types:**

| Request | Client sends | Server replies |
|---|---|---|
| `ping` | `{"type": "ping"}` | `{"status": "pong", "host": ...}` |
| `list` | `{"type": "list"}` | `{"status": "ok", "host": ..., "files": [{"name": ..., "size": ...}, ...]}` |
| `file` (upload) | `{"type": "file", "name", "size", "sha256"}`, then the raw bytes once the server says ready | `{"status": "ready"}`, then `{"status": "ok", "saved_as": ...}` after verifying the checksum |
| `get` (download) | `{"type": "get", "name"}`, then `{"status": "ok"}` after verifying the checksum | `{"status": "sending", "name", "size", "sha256"}`, then the raw bytes |

Any request can instead be answered with `{"status": "error", "error": "<reason>"}`. Examples: wrong password, not enough disk space, no such file, checksum mismatch, or unknown request type.

**Discovery messages (UDP)** are a single UTF-8 JSON datagram, broadcast to `255.255.255.255` and to the local subnet's broadcast address:
```
{"app": "filetransfer-v4", "id": "<random id>", "name": "DESKTOP", "port": 5001, "sig": "<HMAC-SHA256, if a password is set>"}
```
Peers only pair with copies that use the same `app` version and the same password.

### Safety measures
- Incoming file names are reduced to a bare name, so a sender can't write outside the destination folder (for example with `../../file`).
- Existing files are never overwritten: a second `photo.jpg` is saved as `photo (1).jpg`.
- Data is written to `name.part` and only renamed once the SHA-256 checksum matches. The partial file is deleted if the connection drops, stalls for 30 seconds, or the checksum fails.
- Free disk space is checked before a transfer starts.
- Transfers are **not encrypted**. Someone else on the same network could capture file contents, so avoid sending sensitive files over shared networks.

# Development Environment

**Tools**
- Visual Studio Code with the Python extension
- Git and GitHub for version control
- Windows PowerShell for running the program, configuring Windows Defender Firewall, and testing connectivity (`Test-NetConnection`)
- Two Windows computers (a desktop and a laptop) on the same local network for testing

**Language and libraries**

Python 3, using only the standard library:

| Library | Used for |
|---|---|
| `socket` | TCP connections and UDP broadcasts |
| `struct` | packing the 4-byte length prefix in network byte order |
| `json` | encoding and decoding messages |
| `hashlib` | SHA-256 checksums of files |
| `hmac` | password proofs and signed discovery messages, plus constant-time comparison |
| `secrets` | random nonces and peer IDs |
| `threading` | running the server and discovery in the background while the prompt waits for input |
| `argparse` | command-line commands and options |
| `getpass` | typing the password without showing it on screen |
| `os`, `shutil` | file paths, file sizes, and free disk space |

**Project structure**

| File | Responsibility |
|---|---|
| `filetransfer.py` | entry point: command-line commands and the two-way `>` prompt |
| `transfer.py` | the TCP protocol: client requests and server handlers for `ping`, `list`, `file` and `get` |
| `discovery.py` | finding other peers with UDP broadcasts |
| `common.py` | settings, message framing, password helpers, checksum, progress bar and other shared helpers |

# Useful Websites

**Python networking**
* [Python docs: socket - Low-level networking interface](https://docs.python.org/3/library/socket.html)
* [Python docs: Socket Programming HOWTO](https://docs.python.org/3/howto/sockets.html)
* [Python docs: threading - Thread-based parallelism](https://docs.python.org/3/library/threading.html)

**Message format and encoding**
* [Python docs: json - JSON encoder and decoder](https://docs.python.org/3/library/json.html)
* [Python docs: str.encode / bytes.decode (UTF-8 text to bytes)](https://docs.python.org/3/library/stdtypes.html#str.encode)
* [RFC 8259: The JSON Data Interchange Format (requires UTF-8 between systems)](https://www.rfc-editor.org/rfc/rfc8259)
* [Python docs: struct - Interpret bytes as packed binary data](https://docs.python.org/3/library/struct.html)

**Integrity and security**
* [Python docs: hashlib - Secure hashes (SHA-256)](https://docs.python.org/3/library/hashlib.html)
* [Python docs: hmac - Keyed-hashing for message authentication](https://docs.python.org/3/library/hmac.html)
* [Python docs: secrets - Generate secure random numbers](https://docs.python.org/3/library/secrets.html)

**Command line and files**
* [Python docs: argparse - Command-line parsing](https://docs.python.org/3/library/argparse.html)
* [Python docs: getpass - Portable password input](https://docs.python.org/3/library/getpass.html)
* [Python docs: shutil.disk_usage](https://docs.python.org/3/library/shutil.html#shutil.disk_usage)

**Setup and tools**
* [Python downloads](https://www.python.org/downloads/)
* [VS Code: Getting started with Python](https://code.visualstudio.com/docs/python/python-tutorial)
* [Microsoft Learn: New-NetFirewallRule](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)
* [Microsoft Learn: Test-NetConnection](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection)
* [Git documentation: gitignore](https://git-scm.com/docs/gitignore)

# Future Work

{Make a list of things that you need to fix, improve, and add in the future.}
* Item 1
* Item 2
* Item 3

# AI Usage

{Describe how you used AI tools during this project: what you asked for help with, how it helped you learn or move faster, what you changed or verified yourself, and anything you would do differently next time.}
