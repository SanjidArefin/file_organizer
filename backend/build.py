"""Build the one-file sidecar using the installed PyInstaller module."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def rust_host_triple() -> str:
    result = subprocess.run(
        ["rustc", "-vV"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.partition(":")[2].strip()
    raise RuntimeError("Could not read the host target triple from `rustc -vV`.")


def main() -> int:
    try:
        target = rust_host_triple()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Unable to determine the Rust host target: {error}", file=sys.stderr)
        return 1

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target):
        print(f"Invalid Rust host target triple: {target!r}", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401  # Verify the human-installed module exists.
    except ImportError as error:
        print(f"PyInstaller is not available to {sys.executable}: {error}", file=sys.stderr)
        return 1

    executable_name = f"file-organizer-{target}"
    binaries_dir = ROOT / "src-tauri" / "binaries"
    work_dir = ROOT / "backend" / "build" / target
    binaries_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--console",
        "--name",
        executable_name,
        "--distpath",
        str(binaries_dir),
        "--workpath",
        str(work_dir / "work"),
        "--specpath",
        str(work_dir),
        str(ROOT / "backend" / "main.py"),
    ]
    build = subprocess.run(command, cwd=ROOT, check=False)
    if build.returncode != 0:
        return build.returncode

    suffix = ".exe" if sys.platform == "win32" else ""
    output = binaries_dir / f"{executable_name}{suffix}"
    if not output.is_file():
        print(f"PyInstaller completed without creating {output}.", file=sys.stderr)
        return 1

    print(f"Built sidecar for {target}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
