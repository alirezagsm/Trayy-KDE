import asyncio
import logging
import os
import re
import shutil
import signal as sigmod
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

from dbus_fast import Message, MessageType, BusType
from dbus_fast.aio import MessageBus

from trayy import (
    SRC_DIR,
    CONFIG_FILE,
    PID_FILE,
    TRAYD_BINARY,
    VERSION as version,
    REPO_URL,
    desktop_icon_path,
    find_desktop_by_class,
    find_desktop_by_title,
    parse_config,
)
from trayy_dbus import (
    AppMenuInterface,
    ManagerDBusMenuInterface,
    AppSNIInterface,
    KWinWindowWatcherInterface,
    SNW_IFACE,
    WindowEvent,
    ensure_kwin_window_watcher,
)
import trayy_kwin as trayy_kwin
import trayy_update

logger = logging.getLogger("trayy.window")

_NO_TASKBAR = True
_NO_CLOSE = True


async def discover_windows(app_name: str, title: str = "") -> list[str]:
    full = await discover_windows_full(app_name, title)
    return [str(w["windowId"]) for w in full]


async def discover_windows_full(app_name: str, title: str = "") -> list[dict]:
    try:
        results = await trayy_kwin.discover_windows(app_name, title)
    except asyncio.TimeoutError:
        logger.warning("discover_windows('%s') timed out", app_name)
        return []
    if results:
        for w in results:
            w["windowId"] = str(w["windowId"])
        return results
    return []


async def _skip_taskbar(window_id: str):
    if _NO_TASKBAR:
        await trayy_kwin.skip_taskbar(window_id)


async def _hide_window(window_id: str):
    await trayy_kwin.hide_window(window_id)


async def _show_and_activate(window_id: str):
    if _NO_TASKBAR:
        await trayy_kwin.unskip_taskbar(window_id)
    await trayy_kwin.raise_and_activate(window_id)


async def _register_sni(bus: MessageBus, service_name: str):
    msg = Message(
        destination="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        member="RequestName",
        signature="su",
        body=[service_name, 0],
    )
    await bus.send(msg)
    await asyncio.sleep(0.2)
    msg = Message(
        destination="org.kde.StatusNotifierWatcher",
        path="/StatusNotifierWatcher",
        interface=SNW_IFACE,
        member="RegisterStatusNotifierItem",
        signature="s",
        body=[service_name],
    )
    await bus.send(msg)


async def _unregister_sni(bus: MessageBus, service_name: str):
    msg = Message(
        destination="org.kde.StatusNotifierWatcher",
        path="/StatusNotifierWatcher",
        interface=SNW_IFACE,
        member="UnregisterStatusNotifierItem",
        signature="s",
        body=[service_name],
    )
    await bus.send(msg)


def _get_icon_bytes(update_available: bool = False) -> bytes | None:
    png_name = "logo_update.png" if update_available else "logo.png"
    try:
        with zipfile.ZipFile(SRC_DIR / "trayy.pyz") as zf:
            return zf.read(f"media/{png_name}")
    except (FileNotFoundError, KeyError):
        pass
    png = SRC_DIR.parent / "media" / png_name
    if png.exists():
        return png.read_bytes()
    return None


def _get_icon_path(
    update_available: bool = False, data: bytes | None = None
) -> str | None:
    """Path Plasma can load; extracts the embedded logo to ~/.cache/trayy/ when media/ is absent."""
    png_name = "logo_update.png" if update_available else "logo.png"
    png = SRC_DIR.parent / "media" / png_name
    if png.exists():
        return str(png)

    if data is None:
        data = _get_icon_bytes(update_available)
    if data is None:
        return None
    cache_dir = Path.home() / ".cache" / "trayy"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / png_name
        cache_file.write_bytes(data)
        return str(cache_file)
    except OSError:
        return None


def _get_python() -> str:
    venv_python = SRC_DIR / ".venv" / "bin" / "python3"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


class AppTrayIcon:
    def __init__(
        self,
        app_name: str,
        title_filter: str = "",
        icon_override: str = "",
        on_shutdown=None,
    ):
        self.bus = None
        self.app_name = app_name
        self.title_filter = title_filter
        self.icon_override = icon_override
        suffix = (
            app_name.replace(".", "")
            .replace("-", "_")
            .replace(" ", "_")
            .replace(":", "_")
        )
        if title_filter:
            title_suffix = (
                title_filter.replace(".", "")
                .replace("-", "_")
                .replace(" ", "_")
                .replace(":", "_")
            )
            suffix = f"{suffix}_{title_suffix}"
        self.service_name = f"org.kde.Trayy.{suffix}"
        self.sni_path = "/StatusNotifierItem"
        self.menu_path = "/StatusNotifierItem/menu"
        self.windows: list[str] = []
        self.visible_window: str | None = None
        self.registered = False
        self.icon_name = "application-x-executable"
        self.icon_theme_path = ""
        self.icon_data: bytes | None = None
        self._on_shutdown = on_shutdown

    async def setup(self):
        icon_result = await self._resolve_icon()
        self.icon_name = icon_result[0] if icon_result else "application-x-executable"
        self.icon_theme_path = icon_result[1] if icon_result else ""
        self.icon_data = (
            icon_result[2] if icon_result and len(icon_result) > 2 else None
        )
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

        sni = AppSNIInterface(
            self.app_name,
            self.icon_name,
            self.on_activate,
            self.on_quit,
            icon_theme_path=self.icon_theme_path,
            icon_data=self.icon_data,
        )
        menu = AppMenuInterface(self.app_name, self.on_activate, self.on_quit)

        self.bus.export(self.sni_path, sni)
        self.bus.export(self.menu_path, menu)
        await _register_sni(self.bus, self.service_name)
        self.registered = True

    async def teardown(self):
        if not self.registered:
            return
        await _unregister_sni(self.bus, self.service_name)
        self.registered = False
        self.bus.disconnect()
        self.bus = None

    async def on_activate(self, x: int = 0, y: int = 0):
        if not self.windows:
            await self.discover_and_hide_windows()
        if not self.windows:
            launched = await self._try_launch_app()
            if launched:
                await self.discover_and_hide_windows()
            if not self.windows:
                return

        full = await discover_windows_full(self.app_name, self.title_filter)
        fresh_ids = [w["windowId"] for w in full]
        self.windows = list(dict.fromkeys(self.windows + fresh_ids))

        active_w = None
        minimized_w = None
        visible_w = None
        for w in full:
            wid = w["windowId"]
            if w.get("active", False):
                active_w = wid
            elif w.get("minimized", False):
                if minimized_w is None:
                    minimized_w = wid
            else:
                if visible_w is None:
                    visible_w = wid

        if active_w is not None:
            await _hide_window(active_w)
            self.visible_window = None
            return

        if minimized_w is not None:
            await _show_and_activate(minimized_w)
            self.visible_window = minimized_w
            return

        if visible_w is not None:
            await _show_and_activate(visible_w)
            self.visible_window = visible_w
            return

        w = self.windows[0]
        await _show_and_activate(w)
        self.visible_window = w

    async def on_quit(self):
        if not self.windows:
            await self._teardown_and_notify()
            return

        w = self.windows[0]
        await trayy_kwin.close_window(w)
        await asyncio.sleep(0.5)

        # If the window is still alive (e.g., confirmation dialog), reveal it
        still_windows = await discover_windows(self.app_name, self.title_filter)
        if still_windows:
            await _show_and_activate(w)
        else:
            self.clear_windows()
            await self._teardown_and_notify()

    async def _teardown_and_notify(self):
        await self.teardown()
        if self._on_shutdown:
            self._on_shutdown()

    async def discover_and_hide_windows(self):
        await asyncio.sleep(0.5)
        all_windows = await discover_windows(self.app_name, self.title_filter)
        if not all_windows:
            return
        for w in all_windows:
            await trayy_kwin.hide_window(w)
        self.visible_window = all_windows[0]
        self.windows = list(dict.fromkeys(self.windows + all_windows))

    async def _try_launch_app(self) -> bool:
        import configparser
        import shlex

        matches = find_desktop_by_class(self.app_name)
        if not matches:
            return False
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(str(matches[0]))
            exec_cmd = cp.get("Desktop Entry", "Exec", fallback="").strip()
            if not exec_cmd:
                return False

            exec_cmd = re.sub(r"%[fFuUdDnNikv]", "", exec_cmd).strip()
            cmd_parts = shlex.split(exec_cmd) or exec_cmd.split()
            if not cmd_parts:
                return False

            subprocess.Popen(cmd_parts, start_new_session=True)
            logger.info("[%s] launched: %s", self.app_name, exec_cmd)
            return True
        except Exception as e:
            logger.warning("[%s] failed to launch: %s", self.app_name, e)
            return False

    def clear_windows(self):
        self.windows = []
        self.visible_window = None

    async def reveal_all_windows(self):
        for w in self.windows:
            await trayy_kwin.reveal_window(w)
        self.visible_window = None

    async def _resolve_icon(self) -> tuple[str, str, bytes | None] | None:
        if self.icon_override:
            p = Path(self.icon_override).expanduser()
            if p.is_file():
                return p.name, str(p.parent), p.read_bytes()
            logger.warning("icon override not found: %s", self.icon_override)
            return "notification-empty", "", None

        for candidate in find_desktop_by_title(self.title_filter):
            icon_file = desktop_icon_path(candidate)
            if icon_file is not None:
                return self._icon_result(icon_file)

        result = self._icon_for_class(self.app_name)
        if result:
            return result

        try:
            full = await discover_windows_full(self.app_name, self.title_filter)
        except Exception:
            return None
        tried = {self.app_name}
        for w in full:
            for candidate in (
                w.get("resourceClass", ""),
                w.get("resourceName", ""),
            ):
                if not candidate or candidate in tried:
                    continue
                tried.add(candidate)
                result = self._icon_for_class(candidate)
                if result:
                    return result
        return None

    @staticmethod
    def _icon_result(icon_file: Path) -> tuple[str, str, bytes]:
        return icon_file.name, str(icon_file.parent), icon_file.read_bytes()

    def _icon_for_class(self, app_class: str) -> tuple[str, str, bytes] | None:
        for desktop in find_desktop_by_class(app_class):
            icon_file = desktop_icon_path(desktop)
            if icon_file is not None:
                return self._icon_result(icon_file)
        pwa_icon = self._find_chrome_pwa_icon(app_class)
        if pwa_icon is not None:
            return self._icon_result(pwa_icon)
        return None

    def _find_chrome_pwa_icon(self, app_class: str) -> Path | None:
        """Find a flatpak Chrome PWA manifest icon for a chrome-<id>-Default class."""
        m = re.match(r"^chrome-([a-z0-9]{30,40})-Default$", app_class)
        if not m:
            return None
        pwa_id = m.group(1)

        home = Path.home()
        icons_dir = (
            home
            / ".var"
            / "app"
            / "com.google.Chrome"
            / "config"
            / "google-chrome"
            / "Default"
            / "Web Applications"
            / "Manifest Resources"
            / pwa_id
            / "Icons"
        )

        if not icons_dir.exists():
            return None

        preferred_sizes = [64, 48, 32, 128, 96, 16]
        for size in preferred_sizes:
            icon = icons_dir / f"{size}.png"
            if icon.exists():
                return icon

        pngs = sorted(icons_dir.glob("*.png"), key=lambda p: int(p.stem), reverse=True)
        if pngs:
            return pngs[0]

        return None


class ManagerTrayIcon:
    SERVICE_NAME = "org.kde.Trayy.Manager"
    GUI_WIDTH = 360
    GUI_HEIGHT = 480

    def __init__(
        self,
        on_quit_daemon,
        daemon: "TrayDaemon | None" = None,
        update_available: bool = False,
    ):
        self.bus = None
        self.sni_path = "/StatusNotifierItem"
        self.menu_path = "/StatusNotifierItem/menu"
        self._on_quit_daemon = on_quit_daemon
        self._daemon = daemon
        self._ui_proc: subprocess.Popen | None = None
        self._gui_visible = False
        self.registered = False
        self._sni: AppSNIInterface | None = None
        self._icon_data = _get_icon_bytes(update_available=update_available)
        self._icon_path = _get_icon_path(
            update_available=update_available, data=self._icon_data
        )

    async def setup(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        icon_name = self._icon_path or "application-x-executable"
        self._sni = AppSNIInterface(
            "manager",
            icon_name,
            self.on_activate,
            self.on_quit,
            icon_data=self._icon_data,
        )
        self._sni.title = "Trayy"
        menu = ManagerDBusMenuInterface(
            self.on_activate, self.on_hide_all, self.on_about, self.on_quit
        )

        self.bus.export(self.sni_path, self._sni)
        self.bus.export(self.menu_path, menu)
        await _register_sni(self.bus, self.SERVICE_NAME)
        self.registered = True

    def _stop_ui_proc(self):
        if self._ui_proc and self._ui_proc.poll() is None:
            self._ui_proc.terminate()
            try:
                self._ui_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._ui_proc.kill()
                self._ui_proc.wait()
        self._ui_proc = None

    async def teardown(self):
        self._stop_ui_proc()
        if self.registered and self.bus:
            await _unregister_sni(self.bus, self.SERVICE_NAME)
            self.registered = False
            self.bus.disconnect()
            self.bus = None

    async def on_activate(self, x: int = 0, y: int = 0):
        if self._ui_proc is not None and self._ui_proc.poll() is not None:
            self._ui_proc = None
            self._gui_visible = False

        if self._gui_visible:
            ids = await discover_windows("", "Trayy Manager")
            if ids:
                await _hide_window(ids[0])
                self._gui_visible = False
            elif self._ui_proc and self._ui_proc.poll() is None:
                return
            return

        if self._ui_proc and self._ui_proc.poll() is None:
            ids = await discover_windows("", "Trayy Manager")
            if ids:
                await _show_and_activate(ids[0])
                self._gui_visible = True
                return

        python = _get_python()
        ui_script = SRC_DIR / "trayy_ui.py"
        if ui_script.exists():
            self._ui_proc = subprocess.Popen([python, str(ui_script)])
        else:
            pyz_path = str(SRC_DIR / "trayy.pyz")
            env = {
                **os.environ,
                "PYTHONPATH": f"{pyz_path}:{os.environ.get('PYTHONPATH', '')}",
            }
            self._ui_proc = subprocess.Popen(
                [python, "-m", "trayy_ui"],
                env=env,
            )
        for _ in range(20):
            await asyncio.sleep(0.1)
            ids = await discover_windows("", "Trayy Manager")
            if ids:
                break
        self._gui_visible = True

    async def on_hide_all(self):
        if self._daemon is None:
            logger.warning("[manager] Hide All: no daemon reference")
            return
        tasks = []
        for app_class, app_title, _ in self._daemon.tracked_apps:
            try:
                full = await discover_windows_full(app_class, app_title)
                for w in full:
                    tasks.append(_hide_window(w["windowId"]))
            except Exception as e:
                logger.warning("[manager] Hide All: failed for %s: %s", app_class, e)
        if tasks:
            await asyncio.gather(*tasks)

    async def on_about(self):
        try:
            subprocess.Popen(["xdg-open", REPO_URL])
        except Exception:
            pass

    async def on_quit(self):
        await self._on_quit_daemon()


def _kwin_name_matches(
    app_name: str,
    resource_class: str,
    resource_name: str,
    caption: str = "",
    title_filter: str = "",
) -> bool:
    class_match = False
    if app_name.startswith("regex:"):
        pattern = app_name[6:]
        rc = resource_class or ""
        rn = resource_name or ""
        try:
            class_match = bool(re.search(pattern, rc) or re.search(pattern, rn))
        except re.error:
            class_match = False
    else:
        rc = resource_class or ""
        rn = resource_name or ""
        class_match = (app_name in rc) or (app_name in rn)
    if not class_match:
        return False

    if title_filter:
        if title_filter.startswith("regex:"):
            pattern = title_filter[6:]
            if not re.search(pattern, caption or ""):
                return False
        else:
            if (caption or "").find(title_filter) < 0:
                return False

    return True


def _deploy_kwin_script() -> str:
    dest = Path.home() / ".local/share/kwin/scripts" / "trayy"

    try:
        with zipfile.ZipFile(SRC_DIR / "trayy.pyz") as zf:
            metadata = zf.read("kwin/metadata.json").decode()
            main_js = zf.read("kwin/contents/code/main.js").decode()

        dest.mkdir(parents=True, exist_ok=True)
        (dest / "metadata.json").write_text(
            metadata.replace("__TRAYY_VERSION__", version).replace(
                "__REPO_URL__", REPO_URL
            )
        )
        (dest / "contents" / "code").mkdir(parents=True, exist_ok=True)
        (dest / "contents" / "code" / "main.js").write_text(main_js)
    except (FileNotFoundError, KeyError):
        kwin_src = SRC_DIR.parent / "kwin"
        if not kwin_src.exists():
            kwin_src = SRC_DIR / "kwin"
        shutil.copytree(kwin_src, dest, dirs_exist_ok=True)
        _patch_kwin_metadata(dest)

    return str(dest / "contents" / "code" / "main.js")


def _patch_kwin_metadata(dest: Path):
    meta_file = dest / "metadata.json"
    if not meta_file.exists():
        return
    text = meta_file.read_text()
    if "__TRAYY_VERSION__" in text or "__REPO_URL__" in text:
        meta_file.write_text(
            text.replace("__TRAYY_VERSION__", version).replace("__REPO_URL__", REPO_URL)
        )


def _read_config() -> tuple[list[tuple[str, str, str]], bool, bool] | None:
    try:
        no_taskbar, no_close, apps = parse_config()
        return (apps, no_taskbar, no_close)
    except SystemExit:
        return None


class TrayDaemon:
    def __init__(self, bus):
        self.bus = bus
        self.tracked_apps: list[tuple[str, str, str]] = []
        self.app_icons: dict[str, AppTrayIcon] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}
        self._window_cache: dict[str, dict] = {}
        self._cache_ready = False

    def _lock_for(self, app_name: str) -> asyncio.Lock:
        lock = self._launch_locks.get(app_name)
        if lock is None:
            lock = asyncio.Lock()
            self._launch_locks[app_name] = lock
        return lock

    def load_apps(self):
        result = _read_config()
        if result is None:
            logger.error("No .config found at %s — edit it and restart", CONFIG_FILE)
            return
        apps = result[0]
        self.tracked_apps = apps

    def reload_config(self):
        global _NO_TASKBAR, _NO_CLOSE
        result = _read_config()
        if result is None:
            logger.warning("reload_config: no .config found at %s", CONFIG_FILE)
            return
        new_apps, new_no_taskbar, new_no_close = result
        changed = self._update_tracked_apps(new_apps)
        tb_changed = self._update_taskbar_flag(new_no_taskbar)
        close_changed = self._update_close_flag(new_no_close)
        trayd_changed = close_changed or changed
        if trayd_changed:
            self._sync_trayyd(restart=changed is not None)
        if changed:
            asyncio.create_task(self._apply_state_to_all_windows(*changed))
        if tb_changed:
            asyncio.create_task(self._apply_state_to_all_windows())
        logger.info("config reloaded: %d app(s)", len(self.tracked_apps))

    def _update_tracked_apps(
        self, new_apps: list[tuple[str, str, str]]
    ) -> tuple[set, set] | None:
        old_set = set(self.tracked_apps)
        new_set = set(new_apps)
        added = new_set - old_set
        removed = old_set - new_set
        if not (added or removed):
            return None
        for app in added:
            self.tracked_apps.append(app)
        for app in removed:
            self.tracked_apps.remove(app)
        return added, removed

    def _update_taskbar_flag(self, new_no_taskbar: bool) -> bool:
        global _NO_TASKBAR
        if new_no_taskbar != _NO_TASKBAR:
            _NO_TASKBAR = new_no_taskbar
            return True
        return False

    def _update_close_flag(self, new_no_close: bool | None) -> bool:
        global _NO_CLOSE
        if new_no_close is not None and new_no_close != _NO_CLOSE:
            _NO_CLOSE = new_no_close
            return True
        return False

    def _sync_trayyd(self, restart: bool = False):
        if _NO_CLOSE and self.tracked_apps:
            if restart and _trayd_proc is not None:
                _stop_trayyd()
            _ensure_trayyd()
        else:
            _stop_trayyd()

    async def _apply_state_to_all_windows(
        self,
        added: set[tuple[str, str, str]] | None = None,
        removed: set[tuple[str, str, str]] | None = None,
    ):
        added = added or set()
        removed = removed or set()

        for app_class, app_title, _ in removed:
            key = (app_class, app_title)
            icon_mgr = self.app_icons.pop(key, None)
            if icon_mgr:
                await icon_mgr.reveal_all_windows()
                await icon_mgr.teardown()

        for app_class, app_title, icon_path in added:
            await self.on_app_launched(app_class, app_title, icon_override=icon_path)

        tasks = []
        for app_class, app_title, _ in self.tracked_apps:
            full = await discover_windows_full(app_class, app_title)
            for w in full:
                wid = w["windowId"]
                if _NO_TASKBAR:
                    if w.get("minimized", False) or w.get("active", False):
                        tasks.append(_skip_taskbar(wid))
                    else:
                        tasks.append(trayy_kwin.hide_window(wid))
                else:
                    tasks.append(trayy_kwin.unskip_taskbar(wid))
        if tasks:
            await asyncio.gather(*tasks)

    async def on_app_launched(
        self,
        app_class: str,
        app_title: str = "",
        icon_override: str = "",
        discover: bool = True,
    ):
        key = (app_class, app_title)
        async with self._lock_for(app_class):
            icon_mgr = self.app_icons.get(key)
            if not icon_mgr:

                def on_shutdown():
                    self.app_icons.pop(key, None)

                icon_mgr = AppTrayIcon(
                    app_class,
                    app_title,
                    icon_override=icon_override,
                    on_shutdown=on_shutdown,
                )
                await icon_mgr.setup()
                self.app_icons[key] = icon_mgr

            if discover:
                await icon_mgr.discover_and_hide_windows()

    async def on_kwin_window_added(self, ev: WindowEvent):
        self._window_cache[ev.win_id] = {
            "resourceClass": ev.resource_class,
            "resourceName": ev.resource_name,
            "caption": ev.caption,
            "windowId": ev.win_id,
        }
        for app_class, app_title, icon_path in self.tracked_apps:
            if _kwin_name_matches(
                app_class, ev.resource_class, ev.resource_name, ev.caption, app_title
            ):
                await self.on_app_launched(
                    app_class, app_title, icon_override=icon_path
                )
                return

    async def on_kwin_window_removed(self, ev: WindowEvent):
        # On removal KWin may report an empty/stale caption; match against the cached copy from windowAdded.
        cached = self._window_cache.pop(ev.win_id, None)
        if cached:
            resource_class = (
                cached.get("resourceClass", ev.resource_class) or ev.resource_class
            )
            resource_name = (
                cached.get("resourceName", ev.resource_name) or ev.resource_name
            )
            caption = cached.get("caption", ev.caption) or ev.caption
        else:
            resource_class = ev.resource_class
            resource_name = ev.resource_name
            caption = ev.caption

        matched_apps = set()
        for app_class, app_title, _ in self.tracked_apps:
            if not _kwin_name_matches(
                app_class, resource_class, resource_name, caption, app_title
            ):
                continue
            key = (app_class, app_title)
            if key not in self.app_icons:
                continue
            matched_apps.add(key)

        for key in matched_apps:
            app_class, app_title = key
            icon_mgr = self.app_icons.get(key)
            if icon_mgr is not None:
                if icon_mgr.visible_window == ev.win_id:
                    icon_mgr.visible_window = None
                icon_mgr.windows = [w for w in icon_mgr.windows if w != ev.win_id]

            await asyncio.sleep(0.3)
            remaining = await discover_windows(app_class, app_title)
            if remaining:
                icon_mgr = self.app_icons.get(key)
                if icon_mgr:
                    new_ids = [w for w in remaining if w not in icon_mgr.windows]
                    icon_mgr.windows = list(dict.fromkeys(icon_mgr.windows + new_ids))
                continue
            async with self._lock_for(app_class):
                icon_mgr = self.app_icons.pop(key, None)
                if icon_mgr:
                    logger.info("[%s] removed from tray", app_class)
                    icon_mgr.clear_windows()
                    await icon_mgr.teardown()

    async def on_kwin_window_minimized(self, ev: WindowEvent):
        for app_class, app_title, _ in self.tracked_apps:
            if not _kwin_name_matches(
                app_class, ev.resource_class, ev.resource_name, ev.caption, app_title
            ):
                continue
            key = (app_class, app_title)
            if key not in self.app_icons:
                continue
            icon_mgr = self.app_icons[key]
            for w in icon_mgr.windows:
                await _skip_taskbar(w)
            return

    async def _discover_one_app(
        self, app_class: str, app_title: str, icon_override: str = ""
    ):
        if self._cache_ready:
            existing = [
                w
                for w in self._window_cache.values()
                if _kwin_name_matches(
                    app_class,
                    w["resourceClass"],
                    w["resourceName"],
                    w.get("caption", ""),
                    app_title,
                )
            ]
        else:
            all_windows = await trayy_kwin.discover_windows(app_class, app_title) or []

            for w in all_windows:
                win_id = str(w["windowId"])
                if win_id not in self._window_cache:
                    self._window_cache[win_id] = w

            existing = [
                w
                for w in self._window_cache.values()
                if _kwin_name_matches(
                    app_class,
                    w.get("resourceClass", ""),
                    w.get("resourceName", ""),
                    w.get("title", w.get("caption", "")),
                    app_title,
                )
            ]
        if existing:
            logger.info("[%s] added to tray", app_class)
            await self.on_app_launched(
                app_class,
                app_title,
                icon_override=icon_override,
                discover=False,
            )

    async def discover_running_apps(self):
        tasks = [
            self._discover_one_app(app_class, app_title, icon_path)
            for app_class, app_title, icon_path in self.tracked_apps
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def shutdown(self, reveal: bool = True):
        for (_app_class, _app_title), icon_mgr in list(self.app_icons.items()):
            if reveal:
                await icon_mgr.reveal_all_windows()
            await icon_mgr.teardown()


_trayd_proc: subprocess.Popen | None = None
# avoid respawn on shutdown/reboot
_shutting_down = False
_system_shutdown = False


def _stop_trayyd():
    global _trayd_proc
    if _trayd_proc is None:
        return
    _trayd_proc.terminate()
    try:
        _trayd_proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _trayd_proc.kill()
        _trayd_proc.wait()
    _trayd_proc = None
    logger.debug("stopped trayyd")


def _trayyd_already_running() -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["busctl", "--user", "get-name-owner", "org.trayy.Daemon"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_trayyd():
    global _trayd_proc
    if _shutting_down:
        return
    if not TRAYD_BINARY.exists():
        logger.warning("trayyd binary not found at %s", TRAYD_BINARY)
        return
    if _trayd_proc is not None:
        if _trayd_proc.poll() is not None:
            logger.info("trayyd was dead (rc=%s) — restarting", _trayd_proc.returncode)
            _trayd_proc = None
    if _trayd_proc is None:
        if _trayyd_already_running():
            logger.debug("trayyd already running (D-Bus name owned) — skipping spawn")
            return
        _trayd_proc = subprocess.Popen(
            [str(TRAYD_BINARY), str(CONFIG_FILE)],
        )
        logger.debug("launched trayyd (pid=%s)", _trayd_proc.pid)


async def run_daemon(no_taskbar=True, no_close=True):
    global _NO_TASKBAR, _NO_CLOSE, _trayd_proc
    _NO_TASKBAR = no_taskbar
    _NO_CLOSE = no_close

    r = subprocess.run(["pgrep", "-x", "kwin_wayland"], capture_output=True)
    if r.returncode != 0:
        logger.error(
            "KWin not detected. Trayy requires a running KDE Plasma Wayland session"
        )
        return

    await trayy_kwin.init()

    if no_close:
        _ensure_trayyd()

    bus = await MessageBus(bus_type=BusType.SESSION).connect()

    async def _handle_minimize(window_id: str, resource_class: str):
        try:
            await trayy_kwin.hide_window(window_id)
            if resource_class:
                logger.info("[%s] Close intercepted: minimized to tray", resource_class)
        except Exception as e:
            logger.error("failed to minimize window: %s", e)

    def _on_trayd_minimize_signal(msg: Message) -> bool:
        if (
            msg.message_type != MessageType.SIGNAL
            or msg.interface != "org.trayy.Daemon"
            or msg.member != "MinimizeWindow"
        ):
            return False
        window_id = msg.body[0] if len(msg.body) > 0 else ""
        resource_class = msg.body[1] if len(msg.body) > 1 else ""
        asyncio.create_task(_handle_minimize(window_id, resource_class))
        return True

    bus.add_message_handler(_on_trayd_minimize_signal)
    await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[
                "type='signal',sender='org.trayy.Daemon',path='/org/trayy/Daemon',interface='org.trayy.Daemon',member='MinimizeWindow'"
            ],
        )
    )

    # Restart trayyd when it loses its D-Bus name (happens e.g. after sleep/wake).
    def _on_trayd_name_owner_changed(msg: Message) -> bool:
        if (
            msg.message_type != MessageType.SIGNAL
            or msg.interface != "org.freedesktop.DBus"
            or msg.member != "NameOwnerChanged"
        ):
            return False
        name = msg.body[0] if len(msg.body) > 0 else ""
        new_owner = msg.body[2] if len(msg.body) > 2 else ""
        if name == "org.trayy.Daemon" and new_owner == "" and _NO_CLOSE:
            if _shutting_down:
                logger.debug("trayyd lost D-Bus name during shutdown — not restarting")
                return False
            logger.info("trayyd lost D-Bus name — restarting")
            _ensure_trayyd()
        return False

    bus.add_message_handler(_on_trayd_name_owner_changed)
    await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[
                "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus'"
            ],
        )
    )

    daemon = TrayDaemon(bus)
    daemon.load_apps()

    bus.export(
        "/org/kde/Trayy",
        KWinWindowWatcherInterface(
            daemon.on_kwin_window_added,
            daemon.on_kwin_window_removed,
            daemon.on_kwin_window_minimized,
        ),
    )
    await bus.request_name("org.kde.Trayy")

    if _NO_TASKBAR and daemon.tracked_apps:
        by_app = await trayy_kwin.batch_discover_and_hide(
            [(cls, t) for cls, t, _ in daemon.tracked_apps], skip_taskbar=_NO_TASKBAR
        )
        icon_by_key = {(cls, t): ip for cls, t, ip in daemon.tracked_apps}
        for key, windows in by_app.items():
            if not windows:
                continue
            app_class, app_title = key.split("\x1f", 1)
            logger.info("[%s] found %d window(s) on startup", app_class, len(windows))
            for w in windows:
                win_id = str(w["windowId"])
                daemon._window_cache[win_id] = w
            await daemon.on_app_launched(
                app_class,
                app_title,
                icon_override=icon_by_key.get((app_class, app_title), ""),
                discover=False,
            )
            # so shutdown can reveal them
            icon_key = (app_class, app_title)
            if icon_key in daemon.app_icons:
                daemon.app_icons[icon_key].windows = [
                    str(w["windowId"]) for w in windows
                ]
    else:
        await daemon.discover_running_apps()

    try:
        script_path = _deploy_kwin_script()
        await ensure_kwin_window_watcher(bus, script_path, "trayy")
        daemon._cache_ready = True
    except Exception as e:
        logger.warning("KWin window-watcher setup failed: %s", e)

    logger.info("daemon started")

    update_available = False
    try:
        update_available = await asyncio.wait_for(
            trayy_update.check_for_updates(version), timeout=5.0
        )
    except Exception:
        pass

    stop = asyncio.get_event_loop().create_future()

    def handle_signal():
        global _shutting_down, _system_shutdown
        _shutting_down = True
        _system_shutdown = True
        if not stop.done():
            stop.set_result(None)

    async def handle_quit_daemon():
        global _shutting_down
        _shutting_down = True
        if not stop.done():
            stop.set_result(None)

    loop = asyncio.get_event_loop()
    for sig in (sigmod.SIGINT, sigmod.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    def handle_reload():
        daemon.reload_config()

    loop.add_signal_handler(sigmod.SIGHUP, handle_reload)

    manager_icon = ManagerTrayIcon(
        handle_quit_daemon, daemon=daemon, update_available=update_available
    )
    await manager_icon.setup()

    PID_FILE.write_text(str(os.getpid()))

    try:
        await stop
    finally:
        # Children share our cgroup; stop them before exit even if the timed cleanup below fails.
        _stop_trayyd()
        manager_icon._stop_ui_proc()

        async def _cleanup():
            await manager_icon.teardown()
            await daemon.shutdown(reveal=not _system_shutdown)
            await trayy_kwin.shutdown()
            bus.disconnect()
            PID_FILE.unlink(missing_ok=True)

        try:
            await asyncio.wait_for(_cleanup(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("shutdown cleanup timed out — forcing exit")
