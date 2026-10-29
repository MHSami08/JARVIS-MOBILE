"""
AirGesture Pro — one-time setup.

Creates an isolated `.venv` virtual environment, installs all required Python
dependencies inside it, downloads the MediaPipe hand-tracking model, and writes
the default configuration file (`air_gesture_config.json`) if it is not present.

Also updates `Create_Desktop_Shortcut.ps1` (and optionally creates the desktop
shortcut) so that launching AirGesture Pro from the Desktop uses the `.venv`
interpreter automatically.
"""
import platform
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / ".venv"

MIN_PY = (3, 10)        # minimum supported Python version
MAX_PY = (3, 12)        # highest version MediaPipe wheels reliably support

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
MODEL_PATH = HERE / "hand_landmarker.task"


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


def _check_python() -> None:
    """Fail immediately and clearly rather than deep inside a pip resolver."""
    v = sys.version_info[:2]
    if v > MAX_PY:
        print(
            f"\n⚠️  Python {v[0]}.{v[1]} is newer than the "
            f"{MAX_PY[0]}.{MAX_PY[1]} MediaPipe is officially tested on. "
            f"Continuing — if `mediapipe` has no wheel for {v[0]}.{v[1]} yet, "
            f"install Python {MAX_PY[0]}.{MAX_PY[1]} and run setup with:\n"
            f"     py -{MAX_PY[0]}.{MAX_PY[1]} setup.py"
        )
        return
    if v < MIN_PY:
        print(
            f"\n❌ Python {v[0]}.{v[1]} detected — AirGesture Pro needs at "
            f"least Python {MIN_PY[0]}.{MIN_PY[1]}."
        )
        print("   Install a supported version and run setup with it, e.g.:")
        print(f"     py -{MIN_PY[0]}.{MIN_PY[1]} setup.py        (Windows)")
        print(f"     python{MIN_PY[0]}.{MIN_PY[1]} setup.py      (macOS / Linux)")
        sys.exit(1)


def _get_venv_python() -> Path:
    """Return the path to the Python executable inside `.venv`."""
    if OS == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _create_venv() -> Path:
    """Create `.venv` if it does not already exist and return its python path."""
    venv_py = _get_venv_python()
    if venv_py.exists():
        print(f"\n✔ Virtual environment already exists at: {VENV_DIR}")
        return venv_py

    print(f"\n▶ Creating virtual environment in {VENV_DIR}…")
    venv.create(VENV_DIR, with_pip=True)
    if not venv_py.exists():
        print(f"\n❌ Failed to locate {venv_py} after creating virtual environment.")
        sys.exit(1)
    return venv_py


def _ensure_model() -> None:
    """Download `hand_landmarker.task` up front so first launch is instant."""
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
        print(f"\n✔ Hand landmarker model verified ({MODEL_PATH.name}).")
        return
    print(f"\n▶ Downloading MediaPipe hand landmarker model ({MODEL_PATH.name})…")
    try:
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        print("✔ Model downloaded successfully.")
    except Exception as exc:
        print(
            f"\n⚠️  Could not download {MODEL_PATH.name} right now ({exc}).\n"
            "    AirGesture Pro will attempt to download it automatically on first launch."
        )


def _ensure_config(venv_py: Path) -> None:
    """Generate `air_gesture_config.json` with default values if missing."""
    cfg_path = HERE / "air_gesture_config.json"
    if cfg_path.exists():
        print(f"\n✔ Configuration file already exists ({cfg_path.name}) — keeping user settings.")
    else:
        _run(
            "Generating default configuration (air_gesture_config.json)…",
            [str(venv_py), str(HERE / "air_mouse.py"), "--write-config"],
        )


def main() -> None:
    print(
        f"⚙  AirGesture Pro setup — detected OS: {OS or 'unknown'}, "
        f"Python {sys.version_info[0]}.{sys.version_info[1]}"
    )
    _check_python()

    # 1. Create `.venv`
    venv_py = _create_venv()

    # 2. Upgrade pip inside `.venv` so binary wheels resolve cleanly
    _run(
        "Upgrading pip inside .venv…",
        [str(venv_py), "-m", "pip", "install", "--upgrade", "pip"],
    )

    # 3. Install dependencies from requirements.txt into `.venv`
    _run(
        "Installing Python dependencies into .venv…",
        [str(venv_py), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
    )

    # 4. Pre-fetch MediaPipe hand landmarker model
    _ensure_model()

    # 5. Initialize air_gesture_config.json if not present
    _ensure_config(venv_py)

    # 6. OS-specific notes & desktop shortcut helper
    if OS == "Windows":
        ps1 = HERE / "Create_Desktop_Shortcut.ps1"
        if ps1.exists():
            try:
                _run(
                    "Creating Desktop Shortcut ('AirGesture Pro')…",
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ps1),
                    ],
                )
            except subprocess.CalledProcessError as exc:
                print(
                    f"\n⚠️  Desktop shortcut could not be created automatically ({exc}).\n"
                    "    You can run Create_Desktop_Shortcut.bat manually anytime."
                )
    else:
        print(
            f"\nℹ️  {OS} note — OS mouse injection uses Windows SendInput.\n"
            "    On macOS / Linux, AirGesture Pro runs in `--dry-run` preview mode automatically."
        )

    print("\n✅ Setup complete!")
    if OS == "Windows":
        print("   1) Launch from Desktop: double-click 'AirGesture Pro'")
        print("      Or via terminal:     .venv\\Scripts\\python.exe air_mouse.py")
    else:
        print("   1) Launch via terminal: .venv/bin/python air_mouse.py --dry-run")
    print("   2) On first start, follow the 3-step on-screen pinch calibration (~12s).")
    print("   3) Hotkeys: F8 / SPACE = toggle on/off, C = recalibrate, D = debug, Q = quit.")


if __name__ == "__main__":
    main()
