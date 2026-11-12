import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger("trayy")

VERSION = "0.95"
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


# .pyz mode: locate the zip from argv[0]; runpy may set it to an internal path like ".../trayy.pyz/trayy_ui.py".
if ".pyz" in (__file__ or ""):
    _candidate = sys.argv[0] if sys.argv[0] else ""
    if not _candidate:
        _candidate = str(Path(__file__).parent)
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

# XDG lookup order; earlier entries mask later ones.
APPLICATION_DIRS = [
    str(Path.home() / ".local" / "share" / "applications"),
    "/var/lib/flatpak/exports/share/applications",
    str(
        Path.home()
        / ".local"
        / "share"
        / "flatpak"
        / "exports"
        / "share"
        / "applications"
    ),
    "/usr/share/applications",
]

ICON_THEME_DIRS = [
    str(
        Path.home()
        / ".local"
        / "share"
        / "flatpak"
        / "exports"
        / "share"
        / "icons"
        / "hicolor"
    ),
    "/var/lib/flatpak/exports/share/icons/hicolor",
    "/usr/share/icons/hicolor",
    "/usr/share/icons/breeze",
    str(Path.home() / ".local" / "share" / "icons" / "hicolor"),
]

# Earlier entries take precedence.
ICON_SCAN_SIZES = [
    "128x128",
    "512x512",
    "256x256",
    "64x64",
    "96x96",
    "48x48",
    "32x32",
    "24x24",
]

# Raster size for Qt theme lookups.
QT_ICON_SIZE = 128


def read_desktop_field(desktop: Path, field: str) -> str | None:
    try:
        for line in desktop.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{field}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def find_desktop_by_title(title: str) -> list[Path]:
    """Exact Name= matches first, then closest partial matches."""
    if not title or title.startswith("regex:"):
        return []
    needle = title.strip().lower()
    exact: list[Path] = []
    partial: list[tuple[int, Path]] = []
    for d in APPLICATION_DIRS:
        apps_dir = Path(d)
        if not apps_dir.is_dir():
            continue
        for desktop in apps_dir.glob("*.desktop"):
            name = read_desktop_field(desktop, "Name")
            if not name:
                continue
            n = name.strip().lower()
            if n == needle:
                exact.append(desktop)
            elif needle in n or n in needle:
                partial.append((abs(len(n) - len(needle)), desktop))
    return [*exact, *[d for _, d in sorted(partial)]]


def find_theme_icon(icon_name: str) -> Path | None:
    if icon_name.startswith("/"):
        p = Path(icon_name).expanduser()
        if p.is_file():
            return p
        icon_name = p.stem
    for theme_dir in ICON_THEME_DIRS:
        base = Path(theme_dir)
        if not base.exists():
            continue
        for size in ICON_SCAN_SIZES:
            category_dir = base / size / "apps"
            if not category_dir.is_dir():
                continue
            for ext in (".png", ".svg"):
                candidate = category_dir / (icon_name + ext)
                if candidate.is_file():
                    return candidate
    return None


def desktop_icon_path(desktop: Path) -> Path | None:
    icon_ref = read_desktop_field(desktop, "Icon")
    if not icon_ref:
        return None
    return find_theme_icon(icon_ref)


def find_desktop_by_class(app_class: str) -> list[Path]:
    """Exact filename matches first, then ranked partial stems."""
    cls = (app_class or "").lower()
    if not cls or cls.startswith("regex:"):
        return []
    exact: list[Path] = []
    partial: list[tuple[int, int, Path]] = []  # (ends-with rank, stem length, path)
    for d in APPLICATION_DIRS:
        base = Path(d)
        if not base.is_dir():
            continue
        for name in (f"org.kde.{cls}", cls):
            p = base / f"{name}.desktop"
            if p.is_file():
                exact.append(p)
        for candidate in base.glob("*.desktop"):
            stem = candidate.stem.lower()
            if cls in stem:
                rank = 0 if stem.endswith(cls) else 1
                partial.append((rank, len(stem), candidate))
    ranked = [*exact, *[p for _, _, p in sorted(partial)]]
    return list(dict.fromkeys(ranked))


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
