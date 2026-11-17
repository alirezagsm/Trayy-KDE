import asyncio
import json
import logging
import os
import tempfile
from typing import Optional

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)

KWIN_SERVICE = "org.kde.KWin"
KWIN_SCRIPTING_PATH = "/Scripting"
KWIN_SCRIPTING_IFACE = "org.kde.kwin.Scripting"
KWIN_SCRIPT_IFACE = "org.kde.kwin.Script"


class KWinScripting:
    def __init__(self):
        self.bus: Optional[MessageBus] = None
        self._pending_events: dict[str, asyncio.Event] = {}
        self._pending_results: dict[str, str] = {}
        self._callback_counter: int = 0
        self._execute_lock: asyncio.Semaphore = asyncio.Semaphore(1)

    async def connect(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self.bus.add_message_handler(self._on_callback)
        match_rule = f"type='method_call',destination='{self.bus.unique_name}'"
        add_match_msg = Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[match_rule],
        )
        await self.bus.call(add_match_msg)

    async def disconnect(self):
        if self.bus:
            try:
                await self.bus.disconnect()
            except Exception:
                pass

    def _on_callback(self, msg: Message) -> bool:
        if (
            msg.message_type != MessageType.METHOD_CALL
            or msg.destination != self.bus.unique_name
            or not msg.member
        ):
            return False

        callback_key = msg.member
        if callback_key in self._pending_events:
            self._pending_results[callback_key] = msg.body[0] if msg.body else ""
            self._pending_events[callback_key].set()
        return True

    def _register_callback(self, member: str) -> asyncio.Event:
        event = asyncio.Event()
        self._pending_events[member] = event
        return event

    async def _wait_for_callback(
        self, member: str, event: asyncio.Event, timeout: float = 5.0
    ) -> str:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            result = self._pending_results.pop(member, "")
            return result
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for callback: %s", member)
            raise
        finally:
            self._pending_events.pop(member, None)

    def _make_method_call(
        self, service: str, path: str, iface: str, member: str, signature: str, *body
    ) -> Message:
        return Message(
            destination=service,
            path=path,
            interface=iface,
            member=member,
            signature=signature,
            body=list(body),
        )

    async def kwin_load_script(self, script_path: str, script_name: str) -> int:
        msg = self._make_method_call(
            KWIN_SERVICE,
            KWIN_SCRIPTING_PATH,
            KWIN_SCRIPTING_IFACE,
            "loadScript",
            "ss",
            script_path,
            script_name,
        )
        reply = await self.bus.call(msg)
        return reply.body[0]

    async def kwin_unload_script(self, script_name: str):
        msg = self._make_method_call(
            KWIN_SERVICE,
            KWIN_SCRIPTING_PATH,
            KWIN_SCRIPTING_IFACE,
            "unloadScript",
            "s",
            script_name,
        )
        await self.bus.call(msg)

    async def kwin_run_script(self, script_id: int):
        await self._script_cmd(script_id, "run")

    async def kwin_stop_script(self, script_id: int):
        await self._script_cmd(script_id, "stop")

    async def _script_cmd(self, script_id: int, member: str):
        msg = Message(
            destination=KWIN_SERVICE,
            path=f"/Scripting/Script{script_id}",
            interface=KWIN_SCRIPT_IFACE,
            member=member,
            signature="",
            body=[],
        )
        await self.bus.send(msg)

    def _next_callback_name(self, prefix: str) -> str:
        self._callback_counter += 1
        safe_prefix = prefix.replace(".", "_").replace(" ", "_").replace("-", "_")
        return f"{safe_prefix}_{self._callback_counter}"

    async def execute_script(
        self, js_content: str, callback_prefix: str, timeout: float = 5.0
    ) -> str:
        async with self._execute_lock:
            unique_name = self.bus.unique_name
            callback_name = self._next_callback_name(callback_prefix)

            js_content = js_content.replace("__UNIQUE_NAME__", unique_name)
            js_content = js_content.replace("__CALLBACK_NAME__", callback_name)

            fd, script_path = tempfile.mkstemp(suffix=".js", prefix="trayy_kwin_")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(js_content)

                script_name = f"trayy_{callback_name}"

                try:
                    await self.kwin_unload_script(script_name)
                except Exception:
                    pass

                script_id = await self.kwin_load_script(script_path, script_name)
                try:
                    callback_event = self._register_callback(callback_name)
                    await self.kwin_run_script(script_id)
                    result = await self._wait_for_callback(
                        callback_name, callback_event, timeout=timeout
                    )
                    return result
                finally:
                    await self.kwin_stop_script(script_id)
            finally:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass


_kwin: Optional[KWinScripting] = None


async def init() -> KWinScripting:
    global _kwin
    _kwin = KWinScripting()
    await _kwin.connect()
    return _kwin


async def shutdown():
    global _kwin
    if _kwin:
        await _kwin.disconnect()
        _kwin = None


def get() -> KWinScripting:
    if _kwin is None:
        raise RuntimeError("trayy_kwin not initialized — call await init() first")
    return _kwin


# Shared class + title matching; used by both discover and batch scripts.
_MATCHING_JS = """
        var classMatched = true;
        if (targetClass.length > 0) {
            classMatched = false;
            if (targetClass.lastIndexOf("regex:", 0) === 0) {
                try {
                    var re = new RegExp(targetClass.substring(6));
                    classMatched = re.test(rc) || re.test(rn);
                } catch(e) {
                    classMatched = false;
                }
            } else {
                classMatched = (rc.indexOf(targetClass) >= 0 || rn.indexOf(targetClass) >= 0);
            }
        }
        if (!classMatched) continue;
        var titleMatched = true;
        if (targetTitle.length > 0) {
            titleMatched = false;
            if (targetTitle.lastIndexOf("regex:", 0) === 0) {
                try {
                    var re = new RegExp(targetTitle.substring(6));
                    titleMatched = re.test(caption);
                } catch(e) {
                    titleMatched = false;
                }
            } else {
                titleMatched = caption.indexOf(targetTitle) >= 0;
            }
            if (!titleMatched) continue;
        }
"""


def build_discover_script(resource_class: str, title: str = "") -> str:
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"') if title else ""
    escaped_class = (
        resource_class.replace("\\", "\\\\").replace('"', '\\"')
        if resource_class
        else ""
    )
    return f"""
try {{
    var targetClass = "{escaped_class}";
    var targetTitle = "{escaped_title}";
    var results = [];
    var windows = workspace.windowList();
    for (var i = 0; i < windows.length; i++) {{
        var w = windows[i];
        var rc = w.resourceClass || '';
        var rn = w.resourceName || '';
        var caption = w.caption || '';
{_MATCHING_JS}
        results.push(JSON.stringify({{
            windowId: String(w.internalId),
            title: w.caption,
            resourceClass: w.resourceClass,
            resourceName: w.resourceName,
            pid: w.pid,
            minimized: w.minimized,
            skipTaskbar: w.skipTaskbar,
            active: (w === workspace.activeWindow)
        }}));
    }}
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", results.join("\x1f"));
}} catch(e) {{
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", "ERROR:" + e);
}}
"""


# Action → JS statement; each runs inside the matched window's "if (found)" block.
_ACTIONS: dict[str, str] = {
    "skipTaskbar": "w.skipTaskbar = true;",
    "skipTaskbarRemove": "w.skipTaskbar = false;",
    "close": "w.closeWindow();",
    "reveal": "w.skipTaskbar = false; w.minimized = false;",
    "hide": "w.skipTaskbar = true; w.minimized = true;",
    "showAndActivate": "w.skipTaskbar = false; w.minimized = false; w.desktops = [workspace.currentDesktop]; workspace.activeWindow = w; w.raise();",
}


def build_action_script(window_id: str, action: str) -> str:
    js_stmt = _ACTIONS.get(action)
    if js_stmt is None:
        raise ValueError(f"Unknown action: {action!r}")
    safe_id = window_id.replace("\\", "\\\\").replace('"', '\\"')
    return f"""
try {{
    var targetId = "{safe_id}";
    var windows = workspace.windowList();
    var found = false;
    for (var i = 0; i < windows.length; i++) {{
        var w = windows[i];
        if (String(w.internalId) === targetId) {{
            found = true;
            {js_stmt}
            break;
        }}
    }}
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", found ? "true" : "false");
}} catch(e) {{
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", "ERROR:" + e);
}}
"""


async def discover_windows(
    resource_class: str, title: str = "", timeout: float = 5.0
) -> list[dict]:
    kwin = get()
    js = build_discover_script(resource_class, title)
    result_str = await kwin.execute_script(
        js, f"discover_{resource_class}", timeout=timeout
    )

    windows = []
    for entry in result_str.split("\x1f"):
        entry = entry.strip()
        if not entry:
            continue
        try:
            windows.append(json.loads(entry))
        except json.JSONDecodeError:
            logger.warning("Failed to parse window entry: %s", entry)
    return windows


async def skip_taskbar(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "skipTaskbar", timeout=timeout)


async def unskip_taskbar(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "skipTaskbarRemove", timeout=timeout)


async def hide_window(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "hide", timeout=timeout)


async def reveal_window(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "reveal", timeout=timeout)


async def raise_and_activate(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "showAndActivate", timeout=timeout)


async def close_window(window_id: str, timeout: float = 5.0) -> bool:
    return await _window_action(window_id, "close", timeout=timeout)


def build_batch_discover_and_hide_script(
    apps: list[tuple[str, str]], skip_taskbar: bool = True
) -> str:
    """Build a KWin script that discovers and hides all matching windows in one pass."""
    action = (
        "w.skipTaskbar = true; w.minimized = true;"
        if skip_taskbar
        else "w.minimized = true;"
    )

    apps_js = (
        "["
        + ", ".join(
            f'{{class: "{cls.replace("\\", "\\\\").replace('"', '\\"')}", title: "{t.replace("\\", "\\\\").replace('"', '\\"')}"}}'
            for cls, t in apps
        )
        + "]"
    )

    # Reuse _MATCHING_JS with "app.class" / "app.title" as targetClass/targetTitle
    batch_matching_js = (
        "            var targetClass = app.class;\n"
        "            var targetTitle = app.title;\n"
        + _MATCHING_JS.replace("        ", "            ")
    )

    return f"""
try {{
    var apps = {apps_js};
    var byApp = {{}};
    var windows = workspace.windowList();
    for (var i = 0; i < windows.length; i++) {{
        var w = windows[i];
        var rc = w.resourceClass || '';
        var rn = w.resourceName || '';
        var caption = w.caption || '';
        for (var j = 0; j < apps.length; j++) {{
            var app = apps[j];
{batch_matching_js}
            {action}
            var key = app.class + "\x1f" + app.title;
            if (!byApp[key]) byApp[key] = [];
            byApp[key].push({{
                windowId: String(w.internalId),
                title: w.caption,
                resourceClass: rc,
                resourceName: rn,
                pid: w.pid,
                minimized: w.minimized,
                skipTaskbar: w.skipTaskbar,
                active: (w === workspace.activeWindow)
            }});
            break;
        }}
    }}
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", JSON.stringify(byApp));
}} catch(e) {{
    callDBus("__UNIQUE_NAME__", "/", "", "__CALLBACK_NAME__", "ERROR:" + e);
}}
"""


async def batch_discover_and_hide(
    apps: list[tuple[str, str]], skip_taskbar: bool = True, timeout: float = 5.0
) -> dict[str, list[dict]]:
    """Single KWin script pass over all apps; returns {class\x1ftitle: [window_info, ...]}."""
    kwin = get()
    js = build_batch_discover_and_hide_script(apps, skip_taskbar=skip_taskbar)
    result_str = await kwin.execute_script(js, "batch_discover_hide", timeout=timeout)
    try:
        raw = json.loads(result_str)
        cleaned = {}
        for key, windows in raw.items():
            cleaned[key] = [
                {"windowId": str(w.get("windowId", "")), **w} for w in windows
            ]
        return cleaned
    except json.JSONDecodeError:
        logger.warning(
            "batch_discover_and_hide: failed to parse result: %s", result_str
        )
        return {}


async def _window_action(window_id: str, action: str, timeout: float = 5.0) -> bool:
    kwin = get()
    js = build_action_script(window_id, action)
    result = await kwin.execute_script(js, f"action_{action}", timeout=timeout)
    return result == "true"
