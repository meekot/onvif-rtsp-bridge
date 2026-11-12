#!/usr/bin/env python3

import socket
import hashlib
import re
import getpass

HOST = input("Bridge IPv4 address or hostname: ").strip()
PORT = int(input("RTSP port [554]: ") or "554")
USERNAME = input("Recorder username: ").strip()
PATH = input("RTSP path: ").strip()

password = getpass.getpass("Recorder password: ")
uri = "rtsp://{}:{}{}".format(HOST, PORT, PATH)


def md5(value):
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def request(authorization=None):
    sock = socket.create_connection((HOST, PORT), timeout=10)

    headers = [
        "DESCRIBE {} RTSP/1.0".format(uri),
        "CSeq: 1",
        "Accept: application/sdp"
    ]

    if authorization:
        headers.append("Authorization: " + authorization)

    message = "\r\n".join(headers) + "\r\n\r\n"
    sock.sendall(message.encode("utf-8"))

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk

    sock.close()
    return response.decode("utf-8", errors="replace")


first = request()
challenge = re.search(
    r'WWW-Authenticate:\s*Digest\s+realm="([^"]+)",\s*nonce="([^"]+)"',
    first,
    re.IGNORECASE
)

if not challenge:
    print(first.split("\r\n\r\n")[0])
    raise SystemExit("Digest challenge not found")

realm, nonce = challenge.groups()

ha1 = md5("{}:{}:{}".format(USERNAME, realm, password))
ha2 = md5("DESCRIBE:{}".format(uri))
digest = md5("{}:{}:{}".format(ha1, nonce, ha2))

authorization = (
    'Digest username="{}", realm="{}", nonce="{}", '
    'uri="{}", response="{}"'
).format(USERNAME, realm, nonce, uri, digest)

second = request(authorization)

print("First response:", first.split("\r\n")[0])
print("Authenticated response:", second.split("\r\n")[0])
