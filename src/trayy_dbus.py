import asyncio
import logging
from dataclasses import dataclass

from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import (
    ServiceInterface,
    dbus_method,
    PropertyAccess,
    dbus_property,
    dbus_signal,
)

logger = logging.getLogger("trayy.dbus")

SNI_IFACE = "org.kde.StatusNotifierItem"
SNW_IFACE = "org.kde.StatusNotifierWatcher"
MENU_IFACE = "com.canonical.dbusmenu"
KWIN_WATCHER_IFACE = "org.kde.Trayy.WindowWatcher"


@dataclass
class WindowEvent:
    resource_class: str
    resource_name: str
    caption: str
    win_id: str


from collections import namedtuple

_MenuItem = namedtuple("_MenuItem", ["label", "callback"])


class AppDBusMenuInterface(ServiceInterface):
    """Subclasses declare menu items via _MENU_ITEMS class attribute."""

    _MENU_ITEMS: list[_MenuItem] = []

    def __init__(self, app_name: str, **callbacks):
        super().__init__(MENU_IFACE)
        self.app_name = app_name
        # Build items with callbacks bound directly in the namedtuple
        self._menu_items = [
            _MenuItem(item.label, callbacks.get(f"on_item_{iid}"))
            for iid, item in enumerate(self._MENU_ITEMS, 1)
        ]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @dbus_method()
    def GetProperty(self, itemId: "i", propertyName: "s") -> "v":
        item = self._item_props(itemId)
        if item is None:
            return Variant("s", "")
        val = item.get(propertyName)
        if val is None:
            return Variant("s", "")
        sig = "b" if isinstance(val, bool) else "s"
        return Variant(sig, val)

    @dbus_method()
    def GetGroupProperties(self, itemIds: "ai", propertyNames: "as") -> "a{ia{sv}}":
        result = {}
        for iid in itemIds:
            props = {}
            for pn in propertyNames:
                item = self._item_props(iid)
                if item and pn in item:
                    props[pn] = Variant(
                        "s" if pn in ("type", "label", "action") else "b", item[pn]
                    )
            result[iid] = props
        return result

    @dbus_method()
    def GetAll(self, itemId: "i", parentCheck: "b") -> "a{sv}":
        item = self._item_props(itemId)
        if item is None:
            return {}
        return {
            k: Variant("s" if k in ("type", "label", "action") else "b", v)
            for k, v in item.items()
        }

    @dbus_method()
    def Event(self, itemId: "i", eventId: "s", data: "v", timestamp: "u"):
        if eventId not in ("selected", "clicked", "2", "action"):
            return
        if itemId <= 0:
            return
        if 1 <= itemId <= len(self._menu_items):
            callback = self._menu_items[itemId - 1].callback
            if callback:
                asyncio.create_task(callback())

    @dbus_method()
    def EventGroup(self, events: "a(iiavasv)"):
        pass

    @staticmethod
    def _to_variant_props(props: dict) -> dict:
        return {
            k: Variant("b" if v is True or v is False else "s", v)
            for k, v in props.items()
        }

    @dbus_method()
    def GetLayout(
        self, parentId: "i", recursionDepth: "i", propertyNames: "as"
    ) -> "u(ia{sv}av)":
        revision = 1
        root_data = self._item_props(0)
        if root_data is None:
            return [revision, [parentId, {}, []]]

        root_props = self._to_variant_props(root_data)

        if parentId == 0:
            children = []
            for iid in range(1, len(self._menu_items) + 1):
                child_data = self._item_props(iid)
                if child_data is None:
                    continue
                child_props = self._to_variant_props(child_data)
                children.append(Variant("(ia{sv}av)", [iid, child_props, []]))
            return [revision, [0, root_props, children]]
        for iid in range(1, len(self._menu_items) + 1):
            if parentId == iid:
                child_data = self._item_props(iid)
                if child_data is None:
                    return [revision, [parentId, {}, []]]
                child_props = self._to_variant_props(child_data)
                return [revision, [iid, child_props, []]]
        return [revision, [parentId, {}, []]]

    @dbus_method()
    def AboutToShow(self, itemId: "i") -> "b":
        return False

    @dbus_method()
    def AboutToShowGroup(self, itemIds: "ai") -> "ai":
        return []

    @dbus_signal()
    def ItemActivationRequested(self):
        pass

    @dbus_signal()
    def ItemsPropertiesUpdated(self):
        pass

    @dbus_signal()
    def LayoutUpdated(self):
        pass

    def _item_props(self, itemId: int):
        if itemId == 0:
            return {
                "type": "standard",
                "label": self.app_name.title(),
                "visible": True,
                "enabled": True,
                "children-display": "submenu",
            }
        if 1 <= itemId <= len(self._menu_items):
            label = self._menu_items[itemId - 1].label
            return {
                "type": "standard",
                "label": label,
                "visible": True,
                "enabled": True,
            }
        return None


class AppMenuInterface(AppDBusMenuInterface):
    """Per-app tray menu: Toggle, Quit."""

    _MENU_ITEMS = [_MenuItem("Toggle", None), _MenuItem("Quit", None)]

    def __init__(self, app_name: str, on_activate=None, on_quit=None):
        super().__init__(app_name, on_item_1=on_activate, on_item_2=on_quit)


class ManagerDBusMenuInterface(AppDBusMenuInterface):
    """Manager tray menu: Toggle, Hide All, About, Quit."""

    _MENU_ITEMS = [
        _MenuItem("Toggle", None),
        _MenuItem("Hide All", None),
        _MenuItem("About", None),
        _MenuItem("Quit", None),
    ]

    def __init__(self, on_activate=None, on_hide_all=None, on_about=None, on_quit=None):
        super().__init__(
            "Trayy",
            on_item_1=on_activate,
            on_item_2=on_hide_all,
            on_item_3=on_about,
            on_item_4=on_quit,
        )


class AppSNIInterface(ServiceInterface):
    def __init__(
        self,
        app_name: str,
        icon_name: str,
        on_activate,
        on_quit,
        icon_theme_path: str = "",
        icon_data: bytes | None = None,
    ):
        super().__init__(SNI_IFACE)
        self.app_name = app_name
        self.icon_name = icon_name if icon_name else "application-x-executable"
        self.icon_theme_path = icon_theme_path
        self.title = app_name.title()
        self._on_activate = on_activate
        self._on_quit = on_quit
        self._icon_pixels = self._decode_icon(icon_data) if icon_data else []

    @staticmethod
    def _decode_icon(icon_data: bytes) -> list:
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(icon_data)).convert("RGBA")
            w, h = img.size
            raw = img.tobytes("raw", "RGBA")
            return [[w, h, raw]]
        except Exception:
            return []

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return f"trayy-{self.app_name}"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return self.title

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self.icon_name

    @dbus_property(access=PropertyAccess.READ)
    def Icon(self) -> "a(iiay)":
        return self._icon_pixels

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return self.icon_theme_path

    @dbus_property(access=PropertyAccess.READ)
    def Tooltips(self) -> "a{ss}":
        return {"title": self.title, "icon_name": ""}

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return "/StatusNotifierItem/menu"

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaLabel(self) -> "s":
        return self.title

    @dbus_method()
    def Activate(self, x: "i", y: "i"):
        asyncio.create_task(self._on_activate(x, y))

    @dbus_method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @dbus_method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @dbus_method()
    def XAyatanaSecondaryActivate(self, timestamp: "u"):
        pass

    @dbus_method()
    def ProvideXdgActivationToken(self, appId: "s") -> "s":
        return appId

    @dbus_method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass


class KWinWindowWatcherInterface(ServiceInterface):
    def __init__(self, on_window_added, on_window_removed, on_window_minimized):
        super().__init__(KWIN_WATCHER_IFACE)
        self._on_window_added = on_window_added
        self._on_window_removed = on_window_removed
        self._on_window_minimized = on_window_minimized

    @dbus_method()
    def WindowAdded(
        self, resourceClass: "s", resourceName: "s", caption: "s", winId: "s"
    ):
        ev = WindowEvent(resourceClass, resourceName, caption, winId)
        asyncio.create_task(self._on_window_added(ev))

    @dbus_method()
    def WindowRemoved(
        self, resourceClass: "s", resourceName: "s", caption: "s", winId: "s"
    ):
        ev = WindowEvent(resourceClass, resourceName, caption, winId)
        asyncio.create_task(self._on_window_removed(ev))

    @dbus_method()
    def WindowMinimized(
        self, resourceClass: "s", resourceName: "s", caption: "s", winId: "s"
    ):
        ev = WindowEvent(resourceClass, resourceName, caption, winId)
        asyncio.create_task(self._on_window_minimized(ev))


async def ensure_kwin_window_watcher(
    bus: MessageBus, script_path: str, plugin_name: str = "trayy-window-watcher"
) -> bool:
    def _call(member: str, signature: str = "", body=None):
        return bus.call(
            Message(
                destination="org.kde.KWin",
                path="/Scripting",
                interface="org.kde.kwin.Scripting",
                member=member,
                signature=signature,
                body=body or [],
            )
        )

    try:
        try:
            await _call("unloadScript", "s", [plugin_name])
        except Exception:
            pass

        resp = await _call("loadScript", "ss", [script_path, plugin_name])
        script_id = resp.body[0] if resp and resp.body else -1
        if not isinstance(script_id, int) or script_id < 0:
            logger.error("failed to load KWin script %s: %r", plugin_name, script_id)
            return False

        await _call("start")
        return True
    except Exception as e:
        logger.warning("could not set up KWin window-watcher script: %s", e)
        return False
