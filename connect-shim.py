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
while True:
    r, _, _ = select.select([s, sys.stdin.buffer], [], [])
    if s in r:
        try:
            data = s.recv(65536)
        except BlockingIOError:
            data = b""
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    if sys.stdin.buffer in r:
        data = os.read(0, 65536)
        if not data:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            break
        s.sendall(data)
