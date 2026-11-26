"""Check the checked-in deploy bundle and emit a runtime CycloneDX SBOM."""

import json
import re
import subprocess
from importlib import metadata
from pathlib import Path

DEV = {
    "pip",
    "pytest",
    "ruff",
    "import-linter",
    "grimp",
    "pluggy",
    "iniconfig",
    "packaging",
    "rich",
    "click",
    "markdown-it-py",
    "mdurl",
    "pygments",
    "setuptools",
    "wheel",
}
FALLBACK = {"relayforge": "NOASSERTION", "django-ninja": "MIT", "sqlparse": "BSD-3-Clause"}
# psycopg is an existing LGPL runtime dependency explicitly required by the work order.
EXCEPTIONS = {"psycopg": "LGPL-3.0-only", "psycopg-binary": "LGPL-3.0-only"}
PERMISSIVE = re.compile(r"MIT|BSD|Apache|PSF|ISC|MPL|CMU|HPND|PostgreSQL", re.I)
PRIVATE = re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----")
TOKEN = re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{32,}\b|\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b")


def main():
    paths = [
        Path(name.decode())
        for name in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
        if name
    ]
    assert sum(path.stat().st_size for path in paths) < 15_000_000
    assert sum(path.name.startswith("Dockerfile") for path in paths) == 2
    for path in paths:
        assert not set(path.parts) & {"node_modules", ".venv", "tmp"}
        assert path.suffix != ".rfbak"
        data = path.read_bytes()
        assert not PRIVATE.search(data) and not TOKEN.search(data), str(path)
    compose = Path("deploy/docker-compose.prod.yml").read_text()
    assert not re.search(r"^\s*privileged\s*:\s*true", compose, re.M)
    media = compose.split("  media-worker:", 1)[1].split("  migrate-job:", 1)[0]
    for state in (
        'network_mode: "none"',
        'cpus: "1.5"',
        'mem_limit: "1024m"',
        "pids_limit: 64",
        "read_only: true",
        'cap_drop: ["ALL"]',
    ):
        assert state in media
    assert re.search(r"^  data_net:\n    internal: true$", compose, re.M)
    assert not re.search(r"\b(5432|6432):(5432|6432)\b", compose)
    components = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"].lower()
        if name in DEV:
            continue
        license_name = (
            dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or FALLBACK.get(name, "")
        )
        if name in EXCEPTIONS:
            assert license_name == EXCEPTIONS[name], (name, license_name)
        elif name != "relayforge":
            assert not re.search("A?GPL", license_name, re.I), (name, license_name)
            assert PERMISSIVE.search(license_name), (name, license_name)
        components[name] = {
            "type": "library",
            "name": name,
            "version": dist.version,
            "licenses": [{"license": {"name": license_name}}],
            "purl": f"pkg:pypi/{name}@{dist.version}",
        }
    Path("tmp").mkdir(exist_ok=True)
    Path("tmp/sbom-cyclonedx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "version": 1,
                "components": [components[name] for name in sorted(components)],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
