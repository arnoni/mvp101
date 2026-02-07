import sys
from pathlib import Path
import ast

ALLOWED_DIR_HINTS = {"migrations", "admin"}
ROOT = Path(__file__).resolve().parent

def is_allowed(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & ALLOWED_DIR_HINTS)

def file_has_forbidden_import(py_path: Path) -> bool:
    try:
        src = py_path.read_text(encoding="utf-8")
    except Exception:
        return False
    try:
        tree = ast.parse(src, filename=str(py_path))
    except Exception:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("sqlalchemy.orm"):
                for alias in node.names:
                    if alias.name == "Session":
                        return True
    return False

def main() -> int:
    violations = []
    for py_path in ROOT.rglob("*.py"):
        rel = py_path.relative_to(ROOT)
        if is_allowed(rel):
            continue
        if file_has_forbidden_import(py_path):
            violations.append(str(rel))

    if violations:
        print("Forbidden imports detected (sqlalchemy.orm.Session):")
        for v in violations:
            print(f" - {v}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
