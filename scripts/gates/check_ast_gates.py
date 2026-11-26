"""Check core imports and tenant-aware commit registration."""

import ast
from importlib.util import resolve_name
from pathlib import Path

CORE = Path("relayforge/core")
WRAPPER = Path("relayforge/web/db_tx.py")
FORBIDDEN = (
    "django", "ninja", "psycopg", "httpx", "boto3",
    "relayforge.web", "relayforge.adapters", "relayforge.workers",
)


def imported_names(node, path):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level:
            module = resolve_name("." * node.level + module, ".".join(path.parent.parts))
        return [f"{module}.{alias.name}" for alias in node.names]
    return []


def called_name(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


def violations(source_root):
    for file in sorted(source_root.rglob("*.py")):
        path = file.relative_to(source_root)
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        core = path.is_relative_to(CORE)
        dynamic_imports = {"__import__", "import_module"}
        if core:
            dynamic_imports.update(
                alias.asname
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}
                for alias in node.names
                if alias.name in {"import_module", "__import__"} and alias.asname
            )
        for node in ast.walk(tree):
            call = called_name(node)
            if core and (
                call in dynamic_imports
                or any(
                    name == prefix or name.startswith(prefix + ".")
                    for name in imported_names(node, path)
                    for prefix in FORBIDDEN
                )
            ):
                yield f"{file}:{node.lineno}: CORE: forbidden dependency or dynamic import"

            if path != WRAPPER and (
                (isinstance(node, ast.Attribute) and node.attr == "on_commit")
                or (
                    isinstance(node, ast.ImportFrom)
                    and any(alias.name == "on_commit" for alias in node.names)
                )
                or (
                    call == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "on_commit"
                )
            ):
                yield f"{file}:{node.lineno}: OC-06: use on_tenant_commit"


def main():
    findings = list(violations(Path(__file__).resolve().parents[2] / "src"))
    for finding in findings:
        print(finding)
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
