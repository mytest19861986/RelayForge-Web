"""Seal and authenticate PostgreSQL base backups or WAL using bounded AES-GCM records."""

import argparse
import base64
import os
import struct
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RFPITR1"
CHUNK = 1024 * 1024


def transfer(source, destination, key, label, *, decrypt=False):
    if len(key) != 32:
        raise ValueError("A 256-bit archive key is required")
    cipher = AESGCM(key)
    target = Path(destination)
    with (
        open(source, "rb") as stream,
        tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary,
    ):
        pending = Path(temporary.name)
        try:
            if decrypt:
                header = stream.read(len(MAGIC) + 16)
                if not header.startswith(MAGIC) or len(header) != len(MAGIC) + 16:
                    raise ValueError("Invalid archive header")
            else:
                header = MAGIC + os.urandom(16)
                temporary.write(header)
            index = 0
            while True:
                aad = header + struct.pack(">Q", index) + label.encode()
                if decrypt:
                    length_bytes = stream.read(4)
                    if len(length_bytes) != 4:
                        raise ValueError("Truncated archive")
                    length = struct.unpack(">I", length_bytes)[0]
                    if length < 16 or length > CHUNK + 16:
                        raise ValueError("Invalid record size")
                    nonce, ciphertext = stream.read(12), stream.read(length)
                    data = cipher.decrypt(nonce, ciphertext, aad)
                    if len(ciphertext) != length or (not data and stream.read(1)):
                        raise ValueError("Invalid archive termination")
                    temporary.write(data)
                else:
                    data = stream.read(CHUNK)
                    nonce = os.urandom(12)
                    ciphertext = cipher.encrypt(nonce, data, aad)
                    temporary.write(struct.pack(">I", len(ciphertext)) + nonce + ciphertext)
                if not data:
                    break
                index += 1
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seal", "open"))
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("label")
    args = parser.parse_args()
    key = base64.b64decode(Path(os.environ["RF_PITR_KEY_FILE"]).read_bytes().strip(), validate=True)
    transfer(args.source, args.destination, key, args.label, decrypt=args.mode == "open")


if __name__ == "__main__":
    main()
