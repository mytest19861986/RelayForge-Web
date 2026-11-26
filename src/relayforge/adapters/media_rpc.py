import json
import os
import socket

MAX_RESPONSE = 48 * 1024 * 1024


def media_rpc(command, **payload):
    try:
        address = os.environ["RF_MEDIA_SOCKET_PATH"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(130)
            client.connect(address)
            client.sendall(json.dumps({"command": command, **payload}).encode() + b"\n")
            with client.makefile("rb") as stream:
                response = stream.readline(MAX_RESPONSE + 1)
        if len(response) > MAX_RESPONSE or not response.endswith(b"\n"):
            raise ValueError
        result = json.loads(response)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (KeyError, OSError, ValueError):
        raise ValueError("MEDIA_WORKER_UNAVAILABLE") from None
