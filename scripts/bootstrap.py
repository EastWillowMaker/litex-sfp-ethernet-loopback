"""Fetch pinned sources and create this project's isolated Python environment."""

import json
from pathlib import Path
import subprocess


def main():
    root = Path(__file__).resolve().parent.parent
    sources = json.loads((root / "sources.json").read_text())
    for name, source in sources.items():
        directory = root / name
        if not directory.exists():
            subprocess.run(["git", "clone", source["url"], str(directory)], check=True)
            subprocess.run(["git", "-C", str(directory), "checkout", source["commit"]], check=True)
        revision = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(directory), "status", "--porcelain"], text=True).strip()
        if revision != source["commit"] or dirty:
            raise RuntimeError(f"{directory} differs from the recorded sources; preserve it and resolve before setup.")
    python = root / ".venv/bin/python"
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", "3.12", str(root / ".venv")], check=True)
    # Install ordinary wheels: editable installs conflict with the clone directory
    # names when running Python modules from the workspace root.
    subprocess.run(["uv", "pip", "install", "--python", str(python),
        "--reinstall", *(str(root / name) for name in sources),
        "pytest==9.1.1", "mypy==1.15.0", "pyserial==3.5"], check=True)


if __name__ == "__main__":
    main()
