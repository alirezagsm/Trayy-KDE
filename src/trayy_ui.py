import asyncio
import logging
import os
import shlex
import signal
import subprocess
import sys
import zipfile
from pathlib import Path

from PyQt6.QtCore import QThread, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFontMetrics, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from trayy import (
    SRC_DIR,
    CONFIG_FILE,
    PID_FILE,
    VERSION as CURRENT_VERSION,
    REPO_URL,
    ICONPATH_PREFIX,
    parse_config,
)
import trayy_update

logger = logging.getLogger("trayy.ui")

# Rasterize theme icons at this size; _update_icon scales down to display.
_ICON_SOURCE = 64


def format_app(app: tuple[str, ...]) -> str:
    cls = app[0]
    title = app[1] if len(app) > 1 else ""
    return f"{cls} {title}" if title else cls


def parse_app_text(text: str) -> tuple[str, str, str]:
    parts = text.split(None, 1)
    cls = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else ""
    return (cls, title, "")


def save_config(no_taskbar: bool, no_close: bool, apps: list[tuple[str, ...]]):
    lines = [f"NO_TASKBAR {1 if no_taskbar else 0}", f"NO_CLOSE {1 if no_close else 0}"]
    for app in apps:
        cls = app[0]
        title = app[1] if len(app) > 1 else ""
        icon_path = app[2] if len(app) > 2 else ""
        line = f"{cls} {title}".strip()
        if icon_path:
            line += f" {ICONPATH_PREFIX}{icon_path}"
        lines.append(line)
    CONFIG_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


def _resolve_icon_pixmap(
    icon_ref: str, app_class: str = "", title: str = ""
) -> QPixmap | None:
    """Resolve an icon path or theme name to a pixmap; None if not found."""
    if not icon_ref:
        return None
    p = Path(icon_ref).expanduser()
    if p.is_file():
        pm = QPixmap(str(p))
        return pm if not pm.isNull() else None
    home = Path.home()
    icon_search_dirs = [
        f"{home}/.local/share/flatpak/exports/share/icons/hicolor",
        "/var/lib/flatpak/exports/share/icons/hicolor",
        "/usr/share/icons/hicolor",
        "/usr/share/icons/breeze",
        f"{home}/.local/share/icons/hicolor",
    ]
    preferred_sizes = ["48x48", "64x64", "32x32", "128x128", "96x96", "24x24"]
    for icon_dir in icon_search_dirs:
        base = Path(icon_dir)
        if not base.exists():
            continue
        for size in preferred_sizes:
            apps_dir = base / size / "apps"
            if not apps_dir.is_dir():
                continue
            for ext in (".png", ".svg"):
                candidate = apps_dir / (icon_ref + ext)
                if candidate.is_file():
                    pm = QPixmap(str(candidate))
                    return pm if not pm.isNull() else None
    theme_icon = QIcon.fromTheme(icon_ref)
    if not theme_icon.isNull():
        return theme_icon.pixmap(_ICON_SOURCE, _ICON_SOURCE)
    return None


def notify_daemon_reload():
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGHUP)
        logger.debug("sent SIGHUP to daemon (pid=%d)", pid)
    except (ValueError, ProcessLookupError, PermissionError) as e:
        logger.warning("could not signal daemon: %s", e)


def _placeholder_icon_pixmap() -> QPixmap | None:
    """Generic placeholder icon."""
    theme_icon = QIcon.fromTheme("notification-empty")
    if theme_icon.isNull():
        return None
    return theme_icon.pixmap(_ICON_SOURCE, _ICON_SOURCE)


def _read_desktop_field(path: Path, field: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(field + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _icon_from_desktop_file(path: Path) -> QPixmap | None:
    icon_ref = _read_desktop_field(path, "Icon")
    if not icon_ref:
        return None
    pm = _resolve_icon_pixmap(icon_ref)
    if pm is not None:
        return pm
    if icon_ref.startswith("/"):
        return _resolve_icon_pixmap(Path(icon_ref).stem)
    return None


def _auto_discover_icon(app_class: str, title: str) -> QPixmap | None:
    """Find an app's icon from desktop files by class and title."""
    home = Path.home()
    search_dirs = [
        f"{home}/.local/share/applications",
        "/usr/share/applications",
        f"{home}/.local/share/flatpak/exports/share/applications",
        "/var/lib/flatpak/exports/share/applications",
    ]
    cls_l = app_class.lstrip(".").lower()
    candidates: list[Path] = []
    for sd in search_dirs:
        base = Path(sd)
        if not base.is_dir():
            continue
        for f in base.glob("*.desktop"):
            stem_l = f.stem.lower()
            if cls_l and cls_l in stem_l:
                candidates.append(f)
    if title:
        for f in candidates:
            if _read_desktop_field(f, "Name") == title:
                pm = _icon_from_desktop_file(f)
                if pm is not None:
                    return pm
    if candidates:
        best = min(candidates, key=lambda f: len(f.stem))
        return _icon_from_desktop_file(best)
    return None


class AppRow(QWidget):
    def __init__(
        self, app: tuple[str, ...], on_remove, on_rename=None, on_icon_changed=None
    ):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.app = app
        display_name = format_app(app)
        self._on_icon_changed = on_icon_changed

        self._icon_label = QLabel()
        self._icon_label.setFixedSize(24, 24)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setToolTip(
            "Click to choose a new icon\u00b7Right-click to clear"
        )
        self._icon_label.mousePressEvent = self._on_icon_click
        layout.addWidget(self._icon_label)

        self._label = QLabel(display_name)
        self._label.setStyleSheet("font-size: 14px; padding: 6px;")
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._label.setToolTip(display_name)
        self._label.setMinimumWidth(40)
        self._label.setMaximumWidth(250)
        layout.addWidget(self._label, stretch=1)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(24, 24)
        remove_btn.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #c0392b; }"
        )
        remove_btn.clicked.connect(lambda: on_remove(self))
        layout.addWidget(remove_btn)

        self._on_rename = on_rename
        self._editing = False
        self._update_icon()

    def _current_icon_path(self) -> str:
        return self.app[2] if len(self.app) > 2 else ""

    def _set_app_with_icon(self, icon_path: str):
        cls = self.app[0]
        title = self.app[1] if len(self.app) > 1 else ""
        self.app = (cls, title, icon_path)
        self._update_icon()
        if self._on_icon_changed:
            self._on_icon_changed()

    def _update_icon(self):
        pm = None
        icon_path = self._current_icon_path()
        if icon_path:
            pm = _resolve_icon_pixmap(icon_path)
        if pm is None and not icon_path:
            cls = self.app[0]
            title = self.app[1] if len(self.app) > 1 else ""
            pm = _auto_discover_icon(cls, title)
        if pm is None:
            pm = _placeholder_icon_pixmap()
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(
                24,
                24,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._icon_label.setPixmap(scaled)
        else:
            self._icon_label.clear()

    def _on_icon_click(self, e):
        pos = e.globalPosition().toPoint()
        if e.button() == Qt.MouseButton.RightButton:
            if not self._current_icon_path():
                return
            menu = QMenu(self.window())
            clear_act = menu.addAction("Clear Custom Icon")
            if menu.exec(pos) is clear_act:
                self._set_app_with_icon("")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.window(),
                "Choose Icon",
                str(Path.home()),
                "Images (*.png *.svg *.jpg *.jpeg *.bmp);;All Files (*)",
            )
            if path:
                self._set_app_with_icon(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        fm = QFontMetrics(self._label.font())
        available = self._label.width() - 12
        if available > 0:
            elided = fm.elidedText(
                format_app(self.app), Qt.TextElideMode.ElideMiddle, available
            )
            self._label.setText(elided)

    def mouseDoubleClickEvent(self, event):
        if self._editing:
            return
        super().mouseDoubleClickEvent(event)
        self._start_edit()

    def _start_edit(self):
        self._editing = True
        self._edit = QLineEdit(format_app(self.app))
        self._edit.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._edit.setMinimumWidth(40)
        self._edit.setStyleSheet("font-size: 14px; padding: 6px;")
        idx = self.layout().indexOf(self._label)
        self.layout().replaceWidget(self._label, self._edit)
        self._label.hide()
        self._edit.show()
        self._edit.selectAll()
        self._edit.setFocus()
        self._edit.returnPressed.connect(self._commit_edit)
        self._edit.editingFinished.connect(self._finish_edit)

    def _commit_edit(self):
        text = self._edit.text().strip()
        if text:
            cls, title, _ = parse_app_text(text)
            new_app = (cls, title, self._current_icon_path())
            if new_app != self.app:
                self.app = new_app
                self._update_icon()
                if self._on_rename:
                    self._on_rename(new_app)

    def _finish_edit(self):
        if not self._editing:
            return
        self._commit_edit()
        text = self._edit.text().strip()
        if text:
            cls, title, _ = parse_app_text(text)
            self.app = (cls, title, self._current_icon_path())
        self._label.setText(format_app(self.app))
        idx = self.layout().indexOf(self._edit)
        self.layout().replaceWidget(self._edit, self._label)
        self._edit.deleteLater()
        self._label.show()
        self._editing = False


class _AddLineEdit(QLineEdit):
    escapePressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
        else:
            super().keyPressEvent(event)


class AddRow(QWidget):
    def __init__(self, on_commit, on_cancel):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.edit = _AddLineEdit()
        self.edit.setPlaceholderText("App class [optional title] (e.g. kwrite)")
        self.edit.setStyleSheet("font-size: 14px; padding: 6px;")
        layout.addWidget(self.edit, stretch=1)

        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self.edit.returnPressed.connect(self._commit)
        self.edit.escapePressed.connect(lambda: on_cancel())

    def _commit(self):
        name = self.edit.text().strip()
        if name:
            self._on_commit(name)
        else:
            self._on_cancel()

    def focus(self):
        self.edit.setFocus()


def _setup_window_icon(window):
    icon_bytes = None
    # Zipapp mode
    try:
        with zipfile.ZipFile(SRC_DIR / "trayy.pyz") as zf:
            icon_bytes = zf.read("media/logo.png")
    except (FileNotFoundError, KeyError):
        pass
    if icon_bytes is None:
        # Dev mode
        logo = SRC_DIR.parent / "media" / "logo.png"
        if logo.exists():
            icon_bytes = logo.read_bytes()
    if icon_bytes:
        pixmap = QPixmap()
        pixmap.loadFromData(icon_bytes, "PNG")
        window.setWindowIcon(QIcon(pixmap))


class _UpdateWorker(QThread):
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, install_dir: str, parent=None):
        super().__init__(parent)
        self._install_dir = install_dir

    def run(self):
        def cb(msg: str):
            self.progress.emit(msg)

        try:
            ok, msg = asyncio.run(
                trayy_update.apply_update(self._install_dir, progress_cb=cb)
            )
        except Exception as e:
            ok, msg = False, f"update failed: {e}"
        self.finished_ok.emit(ok, msg)


class TrayyManagerWindow(QMainWindow):
    _highlight_color = "#33cc33"
    _normal_color = "#cccccc"

    def __init__(self, update_available: bool = False):
        super().__init__()
        self.setWindowTitle("Trayy Manager")
        self.setFixedSize(310, 450)
        _setup_window_icon(self)

        try:
            no_taskbar, no_close, apps = parse_config()
        except SystemExit:
            no_taskbar, no_close, apps = True, True, []
        apps = list(apps)
        self._no_taskbar, self._no_close = no_taskbar, no_close
        self._update_available = update_available
        self._update_worker: _UpdateWorker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(4)

        # Update button. shown at the top when a newer release exists
        if self._update_available:
            update_btn = QPushButton("⬆  Update Available")
            update_btn.setStyleSheet(
                "QPushButton { background-color: #2ecc71; color: white; "
                "font-weight: bold; padding: 10px; border-radius: 4px; font-size: 13px; }"
                "QPushButton:hover { background-color: #27ae60; }"
            )
            update_btn.clicked.connect(self._on_update_click)
            root.addWidget(update_btn)

        # Manager UI (main content)
        self._manager_widget = QWidget()
        self._manager_layout = QVBoxLayout(self._manager_widget)
        self._manager_layout.setContentsMargins(0, 0, 0, 0)
        self._manager_layout.setSpacing(4)

        settings_layout = QVBoxLayout()
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(4)

        # First row: checkbox + help button inline
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(4)

        self._cb_no_taskbar = QCheckBox("Hide Apps from Taskbar")
        self._cb_no_taskbar.setChecked(self._no_taskbar)
        self._cb_no_taskbar.setStyleSheet("font-size: 13px; padding: 4px;")
        self._cb_no_taskbar.stateChanged.connect(self._on_setting_changed)
        top_row.addWidget(self._cb_no_taskbar, stretch=1)

        help_btn = QPushButton("?")
        help_btn.setFixedSize(24, 24)
        help_btn.setStyleSheet(
            "QPushButton { background-color: #3498db; color: white; "
            "border-radius: 12px; font-weight: bold; font-size: 13px; }"
            "QPushButton:hover { background-color: #2980b9; }"
        )
        help_btn.setToolTip("About Trayy")
        help_btn.clicked.connect(self._toggle_help)
        top_row.addWidget(help_btn)

        settings_layout.addLayout(top_row)

        self._cb_no_close = QCheckBox("Map Close Button → Minimize")
        self._cb_no_close.setChecked(self._no_close)
        self._cb_no_close.setStyleSheet("font-size: 13px; padding: 4px;")
        self._cb_no_close.stateChanged.connect(self._on_setting_changed)
        settings_layout.addWidget(self._cb_no_close)

        self._manager_layout.addLayout(settings_layout)

        sep = QLabel("─" * 40)
        sep.setStyleSheet(f"color: {self._normal_color}; padding: 2px;")
        self._manager_layout.addWidget(sep)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_container)
        self._manager_layout.addWidget(scroll, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        add_btn = QPushButton("+  Add")
        add_btn.setStyleSheet(
            "QPushButton { background-color: #2ecc71; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #27ae60; }"
        )
        add_btn.clicked.connect(self.add_application)
        btn_layout.addWidget(add_btn, stretch=1)

        pick_btn = QPushButton("🖱  Pick")
        pick_btn.setStyleSheet(
            "QPushButton { background-color: #9b59b6; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #8e44ad; }"
        )
        pick_btn.clicked.connect(self.pick_from_window)
        btn_layout.addWidget(pick_btn, stretch=1)

        self._manager_layout.addLayout(btn_layout)

        root.addWidget(self._manager_widget)

        # Help overlay; covers the entire window when shown
        self._help_widget = QWidget()
        self._help_widget.setStyleSheet("background-color: #1e1e1e;")
        self._help_layout = QVBoxLayout(self._help_widget)
        self._help_layout.setContentsMargins(4, 4, 4, 4)
        self._help_layout.setSpacing(4)

        version_label = QLabel(f"Trayy version {CURRENT_VERSION}")
        version_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; padding: 4px; color: {self._highlight_color};"
        )
        self._help_layout.addWidget(version_label)

        help_scroll = QScrollArea()
        help_scroll.setWidgetResizable(True)
        help_scroll.setStyleSheet(
            "QScrollArea { border: none; background-color: #1e1e1e; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #555; border-radius: 4px; }"
        )
        help_content = QWidget()
        help_content.setStyleSheet("background-color: #1e1e1e;")
        help_content.setFixedWidth(270)
        help_content_layout = QVBoxLayout(help_content)
        help_content_layout.setContentsMargins(0, 0, 0, 0)
        help_content_layout.setSpacing(6)

        # Options
        self._add_help_text(
            help_content_layout, "Options:", bullet=False, color=self._highlight_color
        )
        self._add_help_text(
            help_content_layout,
            "Hide Apps from Taskbar",
            "Listed apps will not appear on the Plasma taskbar when running.",
        )
        self._add_help_text(
            help_content_layout,
            "Map Close Button to Minimize",
            "Clicking the X button will minimize the app to the system tray instead of closing it.",
        )

        # App List Rules
        self._add_help_text(
            help_content_layout,
            "App List Rules:",
            bullet=False,
            color=self._highlight_color,
        )
        self._add_help_text(
            help_content_layout,
            "Entry format",
            "app_class [window_title]",
        )
        self._add_help_text(
            help_content_layout,
            "app_class",
            "Window resource class or name to match (e.g. firefox, thunderbird, org.gimp.GIMP).",
        )
        self._add_help_text(
            help_content_layout,
            "window_title [optional]",
            "Restricts matching to only windows whose title contains this string.",
        )
        self._add_help_text(
            help_content_layout,
            "Matching",
            "Matching is a case-sensitive substring match. You can prefix app_class and/or window_title with regex: to use a regular expression instead (e.g. kwrite regex:^Daily Notes).",
        )

        # App Icons
        self._add_help_text(
            help_content_layout,
            "App Icons:",
            bullet=False,
            color=self._highlight_color,
        )
        self._add_help_text(
            help_content_layout,
            "Custom icon",
            "Click an app's icon to pick a custom image file. It shows up in both the manager and the system tray, and sticks around across restarts.",
        )
        self._add_help_text(
            help_content_layout,
            "Clear custom icon",
            "Right-click an app's icon to go back to icon auto-detection.",
        )

        help_scroll.setWidget(help_content)
        self._help_layout.addWidget(help_scroll, stretch=1)

        support_btn = QPushButton("Support the Trayy project")
        support_btn.setStyleSheet(
            "QPushButton { background-color: #2ecc71; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #27ae60; }"
        )
        support_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        self._help_layout.addWidget(support_btn)

        close_help_btn = QPushButton("Back")
        close_help_btn.setStyleSheet(
            "QPushButton { background-color: #3498db; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2980b9; }"
        )
        close_help_btn.clicked.connect(self._toggle_help)
        self._help_layout.addWidget(close_help_btn)

        self._help_widget.hide()
        root.addWidget(self._help_widget)

        self._pending_add: AddRow | None = None
        for app in apps:
            self._add_app_row(app)

    def _add_app_row(self, app: tuple[str, ...]):
        row = AppRow(
            app, self._remove_row, self._rename_row, on_icon_changed=self._save
        )
        index = self.list_layout.count() - 1
        if self._pending_add is not None:
            index -= 1
        self.list_layout.insertWidget(index, row)

    def _save(self):
        apps = sorted(self._current_apps(), key=lambda a: (a[0].lower(), a[1].lower()))
        save_config(self._no_taskbar, self._no_close, apps)
        notify_daemon_reload()

    def _rename_row(self, new_app: tuple[str, str]):
        self._save()

    def _remove_row(self, row: AppRow):
        self.list_layout.removeWidget(row)
        row.deleteLater()
        self._save()

    def _current_apps(self) -> list[tuple[str, str]]:
        apps = []
        for i in range(self.list_layout.count()):
            item = self.list_layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, AppRow):
                apps.append(widget.app)
        return apps

    def add_application(self):
        if self._pending_add is not None:
            self._pending_add.focus()
            return
        self._pending_add = AddRow(self._commit_pending_add, self._cancel_pending_add)
        self.list_layout.insertWidget(self.list_layout.count() - 1, self._pending_add)
        self._pending_add.focus()

    def pick_from_window(self):
        cls = None
        try:
            result = subprocess.run(
                ["qdbus-qt6", "org.kde.KWin", "/KWin", "queryWindowInfo"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("resourceClass:"):
                        cls = line.split(":", 1)[1].strip()
                        if cls:
                            break
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("failed to query window class: %s", e)
        if not cls or cls == "trayy-manager":
            logger.warning("could not detect active window class")
            return
        app = (cls, "", "")
        if app in self._current_apps():
            logger.warning("'%s' is already in the list", cls)
            return
        self._cancel_pending_add()
        self._add_app_row(app)
        self._save()

    def _commit_pending_add(self, name: str):
        app = parse_app_text(name)
        if app in self._current_apps():
            QMessageBox.information(self, "Trayy", f"'{name}' is already in the list")
        else:
            self._add_app_row(app)
            self._save()
        self._cancel_pending_add()

    def _cancel_pending_add(self):
        if self._pending_add is None:
            return
        self.list_layout.removeWidget(self._pending_add)
        self._pending_add.deleteLater()
        self._pending_add = None

    def _on_setting_changed(self):
        self._no_taskbar = self._cb_no_taskbar.isChecked()
        self._no_close = self._cb_no_close.isChecked()
        self._save()

    def _toggle_help(self):
        if self._help_widget.isVisible():
            self._help_widget.hide()
            self._manager_widget.show()
        else:
            self._help_widget.show()
            self._manager_widget.hide()

    def _on_update_click(self):
        if self._update_worker is not None:
            return  # update already in progress

        self._update_worker = _UpdateWorker(str(SRC_DIR), parent=self)
        self._update_worker.progress.connect(self._on_update_progress)
        self._update_worker.finished_ok.connect(self._on_update_finished)

        self._update_dialog = QProgressDialog("Starting update…", None, 0, 0, self)
        self._update_dialog.setWindowTitle("Updating Trayy")
        self._update_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._update_dialog.setMinimumDuration(0)
        self._update_dialog.setCancelButton(None)
        self._update_dialog.show()

        self._update_worker.start()

    def _on_update_progress(self, msg: str):
        if hasattr(self, "_update_dialog"):
            self._update_dialog.setLabelText(msg)

    def _on_update_finished(self, ok: bool, msg: str):
        if hasattr(self, "_update_dialog"):
            self._update_dialog.close()
            del self._update_dialog

        if not ok:
            QMessageBox.critical(self, "Update Failed", msg)
            self._update_worker = None
            return

        # Relaunch the new version after this process exits. The relauncher is
        # detached so it survives the daemon shutdown that terminates us.
        pyz = SRC_DIR / "trayy.pyz"
        relauncher = (
            f"(sleep 2 && {shlex.quote(str(pyz))} >/dev/null 2>&1 &)"
            f" </dev/null >/dev/null 2>&1 &"
        )
        subprocess.Popen(relauncher, shell=True, start_new_session=True)

        # Stop the daemon; its clean shutdown terminates this UI process.
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                return
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        QApplication.quit()

    def _add_help_text(
        self,
        layout: QVBoxLayout,
        label: str,
        text: str = "",
        color: str | None = None,
        bullet: bool = True,
    ):
        color = color or self._highlight_color
        row = QHBoxLayout()
        # Indent bulleted items so they don't line up with section headers.
        row.setContentsMargins(16 if bullet else 0, 0, 0, 0)
        row.setSpacing(4)
        if bullet:
            b = QLabel("•")
            b.setStyleSheet(f"color: {self._normal_color}; font-size: 14px;")
            row.addWidget(b)
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lbl.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {color};")
        row.addWidget(lbl)
        layout.addLayout(row)
        if text:
            desc = QLabel(text)
            desc.setWordWrap(True)
            # Left padding lines the description up under the label (indent + bullet).
            desc.setStyleSheet(
                f"font-size: 12px; padding: 0 0 4px 34px; color: {self._normal_color};"
            )
            layout.addWidget(desc)


def _check_update_sync() -> bool:
    """Quick synchronous update check via urllib (no async needed for UI)."""
    try:
        import json, urllib.request

        url = trayy_update.github_releases_latest_url()
        req = urllib.request.Request(url, headers={"User-Agent": "Trayy-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            latest = data.get("tag_name", "")
        trayy_update.LATEST_VERSION = latest
        return trayy_update.needs_update(CURRENT_VERSION, latest)
    except Exception:
        return False


def run_ui():
    """Entry point for launching the UI — called by trayy.py or directly."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    update_available = _check_update_sync()

    os.environ["QT_QPA_PLATFORM_PORTAL_REGISTER_APPID"] = "0"
    app = QApplication(sys.argv)
    app.setApplicationName("trayy-manager")
    window = TrayyManagerWindow(update_available=update_available)
    window.show()
    ret = app.exec()
    sys.exit(ret)


def main():
    run_ui()


if __name__ == "__main__":
    main()
