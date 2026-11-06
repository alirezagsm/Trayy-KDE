import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger("trayy")

VERSION = "0.9"
REPO_URL = "https://github.com/alirezagsm/Trayy-KDE"
BOOT_GRACE_SECONDS = 20
STARTUP_DELAY = 10.0

ICONPATH_PREFIX = "iconpath:"


def ensure_config():
    if CONFIG_FILE.exists():
        return
    default_config = "\n".join(
        [
            "NO_TASKBAR 1",
            "NO_CLOSE 1",
            "",
            "# Add your apps below, one per line:",
            "# app_class [window_title]",
            "",
        ]
    )
    CONFIG_FILE.write_text(default_config)
    print(f"Created default .config at {CONFIG_FILE}")


def check_dependencies():
    missing = []
    for prog, desc in [("qdbus-qt6", "QDBus CLI tool")]:
        if shutil.which(prog) is None:
            missing.append(f"  - {prog} ({desc})")
    if not TRAYD_BINARY.exists():
        missing.append(f"  - trayyd (local binary at {TRAYD_BINARY})")
    if missing:
        print("Missing dependencies:")
        for m in missing:
            print(m)
        sys.exit(1)


# Resolve paths: .pyz mode uses sys.argv[0], dev mode uses __file__.
if ".pyz" in (__file__ or ""):
    # Three cases for sys.argv[0]:
    # 1. `python trayy.pyz`          → "/path/to/trayy.pyz"
    # 2. `python -m trayy_ui` (via PYTHONPATH=trayy.pyz)
    #    → "/path/to/trayy.pyz/trayy_ui.py"  (runpy sets it to the internal path)
    # 3. empty string (edge case)
    _candidate = sys.argv[0] if sys.argv[0] else ""
    if not _candidate:
        _candidate = str(Path(__file__).parent)
    # If argv[0] points inside the zip (e.g., ".../trayy.pyz/trayy_ui.py"),
    # extract the .pyz path by finding the ".pyz/" segment.
    if ".pyz/" in _candidate:
        _candidate = _candidate[: _candidate.index(".pyz/") + 4]
    SRC_DIR = Path(_candidate).parent.resolve()
    PROJECT_DIR = SRC_DIR
else:
    SRC_DIR = Path(__file__).resolve().parent
    PROJECT_DIR = SRC_DIR.parent

CONFIG_FILE = PROJECT_DIR / ".config"
TRAYD_BINARY = SRC_DIR / "trayyd"
PID_FILE = Path("/tmp/.trayy.pid")


def _read_flag(lines: list[str], index: int, name: str) -> bool:
    if index >= len(lines):
        return True
    parts = lines[index].strip().split()
    if not parts or parts[0].upper() != name:
        return True
    return len(parts) > 1 and parts[1] == "1"


def parse_config(
    path: Path = CONFIG_FILE,
) -> tuple[bool, bool, list[tuple[str, str, str]]]:
    """Parse the config file. App lines: ``app_class [window_title] [iconpath:<file>]``."""
    if not path.exists():
        logger.error("no .config file found at %s", path)
        raise SystemExit(1)

    lines = path.read_text().splitlines()
    no_taskbar = _read_flag(lines, 0, "NO_TASKBAR")
    no_close = _read_flag(lines, 1, "NO_CLOSE")

    apps = []
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        cls = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if ICONPATH_PREFIX in rest:
            title, icon_path = rest.split(ICONPATH_PREFIX, 1)
            title = title.strip()
        else:
            title, icon_path = rest, ""
        apps.append((cls, title, icon_path))

    if not apps:
        logger.warning("no apps listed in .config file — nothing to track")

    return no_taskbar, no_close, apps


def main():
    check_dependencies()
    ensure_config()

    no_taskbar, no_close, apps = parse_config()
    app_names = ", ".join(cls for cls, _, _ in apps)
    logger.info("apps configured: %d (%s)", len(apps), app_names)

    # Wait for input devices / remappers to finish initializing (boot-only)
    try:
        with open("/proc/uptime") as f:
            _uptime = float(f.read().split()[0])
    except Exception:
        _uptime = -1.0
    if 0.0 < _uptime < BOOT_GRACE_SECONDS:
        logger.info("startup delay: waiting %.1fs for input devices…", STARTUP_DELAY)
        time.sleep(STARTUP_DELAY)

    from trayy_window import run_daemon

    try:
        asyncio.run(run_daemon(no_taskbar=no_taskbar, no_close=no_close))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _debug = os.environ.get("DEBUG", "")
    logging.basicConfig(
        level=logging.DEBUG if _debug else logging.ERROR,
        format="%(levelname)s: %(message)s",
    )
    main()
