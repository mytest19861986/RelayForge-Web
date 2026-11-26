"""Verify the native runtime prerequisites and emit host evidence."""

import argparse
import hashlib
import json
import os
import platform
import socket
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ubuntu-2404", action="store_true")
    args = parser.parse_args()
    host = platform.freedesktop_os_release()
    assert platform.system() == "Linux" and os.geteuid() != 0
    if args.require_ubuntu_2404:
        assert (host["ID"], host["VERSION_ID"]) == ("ubuntu", "24.04")
    confined = False
    with tempfile.TemporaryDirectory() as directory:
        address = Path(directory) / "media.sock"
        try:
            with socket.socket(socket.AF_UNIX) as channel:
                channel.bind(str(address))
                address.chmod(0o600)
                confined = stat.S_IMODE(address.stat().st_mode) == 0o600
        except PermissionError:
            if args.require_ubuntu_2404:
                raise
    if args.require_ubuntu_2404:
        assert confined
    with psycopg.connect(os.environ["TEST_DATABASE_ADMIN_URL"]) as conn:
        version = conn.info.server_version
    assert version // 10000 == 16
    schema = Path("RelayForge_Architecture/contracts/database_schema.sql")
    evidence = {
        "git_commit_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "sql_sha256": hashlib.sha256(schema.read_text().encode()).hexdigest(),
        "os": host["PRETTY_NAME"],
        "kernel": platform.release(),
        "uid": os.geteuid(),
        "postgres_version": version,
        "af_unix_mode_0600": confined,
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    }
    Path("tmp").mkdir(exist_ok=True)
    Path("tmp/m7-oc08-ubuntu2404-evidence.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
