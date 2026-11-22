#!/usr/bin/env python3
"""ProxyCommand shim for SSH: HTTP CONNECT through a proxy, then relay stdio.

Written for an agent VM that can only reach the network through an HTTP
CONNECT proxy (here: credentials and proxy host come from the HTTPS_PROXY
environment variable, and the proxy port is 3130). If your agent reaches
the tailnet directly, you do not need this shim at all — plain ssh works.
Adjust the port to your proxy."""
import base64
import os
import re
import select
import socket
import sys
import time

target_host, target_port = sys.argv[1], int(sys.argv[2])
m = re.match(r'http://([^@]+)@([^:/]+):(\d+)', os.environ['HTTPS_PROXY'])
creds, phost = m.group(1), m.group(2)

s = socket.create_connection((phost, 3130), timeout=30)
auth = base64.b64encode(creds.encode()).decode()
req = (
    f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
    f"Host: {target_host}:{target_port}\r\n"
    f"Proxy-Authorization: Basic {auth}\r\n"
    f"Proxy-Connection: Keep-Alive\r\n\r\n"
)
s.sendall(req.encode())

buf = b""
while b"\r\n\r\n" not in buf:
    chunk = s.recv(4096)
    if not chunk:
        sys.stderr.write("shim: proxy closed during CONNECT\n")
        sys.exit(1)
    buf += chunk
status = buf.split(b"\r\n")[0]
if b" 200" not in status:
    sys.stderr.write(f"shim: CONNECT failed: {status!r}\n")
    sys.exit(1)
rest = buf.split(b"\r\n\r\n", 1)[1]
if rest:
    sys.stdout.buffer.write(rest)
    sys.stdout.buffer.flush()

s.setblocking(False)
pending = b""  # stdin bytes not yet fully sent to the socket
stdin_open = True
while True:
    readers = [s]
    if stdin_open and len(pending) < 1 << 20:
        # Backpressure: stop reading stdin while a large buffer is unsent.
        readers.append(sys.stdin.buffer)
    writers = [s] if pending else []
    r, w, _ = select.select(readers, writers, [])
    if s in r:
        try:
            data = s.recv(65536)
        except BlockingIOError:
            data = None  # spurious wakeup on a non-blocking socket; not EOF
        if data == b"":
            # Real EOF on the read side. That is a half-close: the peer may
            # still be able to receive, so rescue whatever stdin input is
            # already available and flush it on a best-effort basis
            # (bounded to ~1s) before terminating.
            deadline = time.monotonic() + 1.0
            if stdin_open:
                os.set_blocking(0, False)
            while time.monotonic() < deadline:
                if stdin_open:
                    try:
                        chunk = os.read(0, 65536)
                    except BlockingIOError:
                        chunk = None
                    except OSError:
                        chunk = b""
                    if chunk:
                        pending += chunk
                    elif chunk == b"":
                        stdin_open = False
                if pending:
                    _, w, _ = select.select([], [s], [], 0.05)
                    if s in w:
                        try:
                            sent = s.send(pending)
                        except (BlockingIOError, OSError):
                            break
                        pending = pending[sent:]
                if not pending and not stdin_open:
                    break
                if not pending:
                    select.select([sys.stdin.buffer], [], [], 0.05)
            break
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    if stdin_open and sys.stdin.buffer in r:
        chunk = os.read(0, 65536)
        if not chunk:
            stdin_open = False
            if not pending:
                try:
                    s.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        else:
            pending += chunk
    if s in w and pending:
        try:
            sent = s.send(pending)
        except BlockingIOError:
            sent = 0
        pending = pending[sent:]
        if not pending and not stdin_open:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
