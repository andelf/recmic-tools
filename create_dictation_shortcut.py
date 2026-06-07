from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PACKAGES = [
    "sherpa-onnx",
    "sounddevice",
    "numpy",
    "pynput",
    "pywinusb",
]
REQUIREMENTS_FILE = "requirements.txt"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"[RUN] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def ensure_venv(repo_dir: Path, dry_run: bool) -> Path:
    venv_dir = repo_dir / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"

    if not venv_python.exists():
        if dry_run:
            print(f"[DRY] Would create virtual environment: {venv_dir}")
        else:
            print("[INFO] Creating virtual environment...")
            run([sys.executable, "-m", "venv", str(venv_dir)], cwd=repo_dir)
    else:
        print("[INFO] Virtual environment already exists.")

    if not dry_run and not venv_python.exists():
        raise RuntimeError("Failed to create virtual environment")

    return venv_python


def install_dependencies(venv_python: Path, repo_dir: Path, dry_run: bool) -> None:
    print("[INFO] Installing/updating Python dependencies...")
    req_path = repo_dir / REQUIREMENTS_FILE
    if dry_run:
        print(f"[DRY] Would run: {venv_python} -m pip install --upgrade pip setuptools wheel")
        if req_path.exists():
            print(f"[DRY] Would run: {venv_python} -m pip install -r {req_path}")
        else:
            print(f"[DRY] Would run: {venv_python} -m pip install {' '.join(PACKAGES)}")
        return

    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=repo_dir)
    if req_path.exists():
        run([str(venv_python), "-m", "pip", "install", "-r", str(req_path)], cwd=repo_dir)
    else:
        run([str(venv_python), "-m", "pip", "install", *PACKAGES], cwd=repo_dir)


def create_shortcut(
    name: str,
    repo_dir: Path,
    venv_python: Path,
    target_script: Path,
    dry_run: bool,
) -> Path:
    desktop = Path(os.path.expandvars(r"%USERPROFILE%\Desktop"))
    shortcut_path = desktop / f"{name}.lnk"

    if not desktop.exists():
        raise RuntimeError(f"Desktop folder not found: {desktop}")

    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{shortcut_path}'); "
        f"$sc.TargetPath = '{venv_python}'; "
        f"$sc.Arguments = '\"{target_script}\"'; "
        f"$sc.WorkingDirectory = '{repo_dir}'; "
        "$sc.Description = 'Run OLYMPUS DR dictation script'; "
        "$sc.IconLocation = 'C:\\Windows\\System32\\SHELL32.dll,167'; "
        "$sc.Save();"
    )

    if dry_run:
        print(f"[DRY] Would create shortcut: {shortcut_path}")
        print(f"[DRY] Target: {venv_python}")
        print(f"[DRY] Args: {target_script}")
        return shortcut_path

    run([
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps,
    ])

    return shortcut_path


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This script currently supports Windows only.")

    parser = argparse.ArgumentParser(
        description="One-click setup: create .venv, install dependencies, and create a desktop shortcut.",
    )
    parser.add_argument(
        "--name",
        default="Olympus Dictation",
        help="Shortcut display name on desktop.",
    )
    parser.add_argument(
        "--script",
        default="dictation.py",
        help="Target script path (relative to this script folder or absolute path).",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip package installation (still ensures .venv exists).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without changing system.",
    )
    args = parser.parse_args()

    repo_dir = Path(__file__).resolve().parent
    target_script = Path(args.script)
    if not target_script.is_absolute():
        target_script = repo_dir / target_script

    if not target_script.exists():
        raise SystemExit(f"Target script not found: {target_script}")

    print(f"[INFO] Repository: {repo_dir}")
    print(f"[INFO] Target script: {target_script}")

    venv_python = ensure_venv(repo_dir, args.dry_run)

    if not args.skip_install:
        install_dependencies(venv_python, repo_dir, args.dry_run)
    else:
        print("[INFO] Skipping dependency installation.")

    shortcut_path = create_shortcut(
        name=args.name,
        repo_dir=repo_dir,
        venv_python=venv_python,
        target_script=target_script,
        dry_run=args.dry_run,
    )

    print(f"[OK] Shortcut ready: {shortcut_path}")


if __name__ == "__main__":
    main()
