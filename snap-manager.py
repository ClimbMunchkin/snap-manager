#!/usr/bin/env python3
# snap-manager.py — элегантный менеджер блокировок Snap (Python-порт)

import os
import sys
import re
import subprocess
import time
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QMessageBox,
    QProgressDialog, QTextEdit, QComboBox, QAbstractItemView,
    QAction, QActionGroup, QFrame, QMenu, QWidget, QSizePolicy,
    QShortcut, QMenuBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt5.QtGui import QKeySequence, QColor, QBrush, QPalette

LOG_DIR = "/var/log/snap-update-manager"

SYSTEM_SNAPS_RE = re.compile(
    r"^(bare|core.*|snapd.*|gnome-.*|mesa-.*|gtk.*|webkitgtk.*|firmware-updater)$"
)

TITLE_BASE = "Управление обновлениями Snap"


# ---------- Утилиты ----------
def run(cmd, check=False):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Команда не найдена: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)


def require_root():
    if os.geteuid() != 0:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "Ошибка", "Требуются права root. Запустите через sudo.")
        sys.exit(1)


def get_snapd_version():
    rc, out, _ = run(["snap", "version"])
    if rc != 0:
        return "unknown"
    for line in out.splitlines():
        if line.startswith("snapd"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return "unknown"


def check_snapd_version(ver):
    m = re.match(r"^(\d+)\.(\d+)", ver)
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (2, 58)


def cleanup_old_logs():
    if not os.path.isdir(LOG_DIR):
        return
    now = time.time()
    for f in os.listdir(LOG_DIR):
        if not f.endswith(".log"):
            continue
        path = os.path.join(LOG_DIR, f)
        try:
            if now - os.path.getmtime(path) > 30 * 86400:
                os.remove(path)
        except OSError:
            pass


def get_snap_statuses():
    rc, out, _ = run(["snap", "list"])
    if rc != 0:
        return {}
    statuses = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        pkg = parts[0]
        statuses[pkg] = "held" if "held" in parts[-1] else "active"
    return statuses


def is_global_held():
    rc, out, _ = run(["snap", "refresh", "--time"])
    if rc != 0:
        return False
    return bool(re.search(r"^hold:\s*(forever|\d)", out, re.MULTILINE))


# ---------- Фоновый обновитель ----------
class UpdateWorker(QThread):
    progress = pyqtSignal(int, str)
    log_line = pyqtSignal(str)
    ask_question = pyqtSignal(str, str)
    finished = pyqtSignal(int, int, str)

    def __init__(self, packages, master_log):
        super().__init__()
        self.packages = packages
        self.master_log = master_log
        self._answer = None
        self._answer_ready = False

    def _ask(self, pkg, text):
        from PyQt5.QtCore import QEventLoop
        loop = QEventLoop()
        self._answer_ready = False
        self._answer = None
        self.ask_question.emit(pkg, text)
        while not self._answer_ready:
            QApplication.processEvents()
            loop.processEvents()
        return self._answer

    def set_answer(self, ans):
        self._answer = ans
        self._answer_ready = True

    def run(self):
        total = len(self.packages)
        updated = 0
        error_msgs = ""
        with open(self.master_log, "a", encoding="utf-8") as mlog:
            mlog.write(f"=== Начало обновления: {datetime.now()} ===\n")
            mlog.write(f"Выбранные пакеты: {' '.join(self.packages)}\n")
            mlog.write("-" * 40 + "\n")

            for i, pkg in enumerate(self.packages, 1):
                percent = int(i * 100 / total)
                self.progress.emit(percent, f"Проверка {pkg} ({i} из {total})...")

                rc, _, _ = run(["pgrep", "-f", pkg])
                if rc == 0:
                    ans = self._ask(
                        pkg,
                        f"Обнаружен запущенный процесс, связанный с '{pkg}'.\n"
                        f"Он может помешать обновлению.\n\n"
                        f"Закрыть все связанные процессы принудительно?"
                    )
                    if ans:
                        run(["pkill", "-f", pkg])
                        time.sleep(1)
                    else:
                        error_msgs += f"[Пропущено] {pkg}: приложение запущено.\n"
                        mlog.write(f"[Пропущено] {pkg}: приложение запущено.\n")
                        self.log_line.emit(f"[Пропущено] {pkg}")
                        continue

                self.progress.emit(percent, f"Обновление {pkg}...")
                rc, out, err = run(["snap", "refresh", pkg])
                if rc == 0:
                    updated += 1
                    mlog.write(f"[УСПЕХ] {pkg} обновлен.\n{out}\n\n")
                    self.log_line.emit(f"[УСПЕХ] {pkg}")
                else:
                    detail = (err or out).splitlines()[-5:]
                    error_msgs += f"[Ошибка] {pkg}: {' '.join(detail)}\n"
                    mlog.write(f"[ОШИБКА] {pkg} не обновлен.\n{out}\n{err}\n")
                    mlog.write("-" * 40 + "\n")
                    self.log_line.emit(f"[ОШИБКА] {pkg}")

            mlog.write(f"=== Завершено: {datetime.now()} ===\n")
            mlog.write(f"Успешно обновлено: {updated} из {total}\n")

        self.progress.emit(100, "Готово!")
        self.finished.emit(updated, total, error_msgs)


# ---------- Стили ----------
STYLE_LIGHT = """
QDialog { background: #f7f8fa; }

QLabel#headerTitle {
    font-size: 10pt; color: #1a1a1a;
}
QLabel#headerStatus {
    font-size: 11pt; color: #555; padding: 2px 0;
}
QLabel#headerStatus[held="true"] { color: #000000; font-weight: 400; }
QLabel#headerStatus[held="false"] { color: #000000; font-weight: 400; }

QFrame#card {
    background: #ffffff;
    border: 1px solid #e3e6ea;
    border-radius: 10px;
}

/* --- Баннер глобального состояния (вариант A) --- */
QFrame#statusBanner {
    border-radius: 10px;
    background: #e9f7ee;
    border: 1px solid #bfe3ca;
}
QFrame#statusBanner[held="true"] {
    background: #fdecec;
    border: 1px solid #f3c2c4;
}
QFrame#statusBanner[held="false"] {
    background: #e9f7ee;
    border: 1px solid #bfe3ca;
}

QListWidget {
    background: #ffffff;
    border: 1px solid #e3e6ea;
    border-radius: 10px;
    padding: 6px;
    font-size: 10.5pt;
}
QListWidget::item {
    padding: 8px 6px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background: #e8f0fe;
    color: #1a1a1a;
}
QListWidget::item:hover {
    background: #f2f5f9;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #d0d4da;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 10pt;
    color: #1a1a1a;
}
QPushButton:hover { background: #f2f5f9; border-color: #b9bec6; }
QPushButton:pressed { background: #e6eaef; }
QPushButton:disabled { color: #a0a0a0; border-color: #e3e6ea; }

QPushButton#primary {
    background: #2d6cdf; color: #ffffff; border: none;
    font-weight: 600;
}
QPushButton#primary:hover { background: #2459b8; }
QPushButton#primary:pressed { background: #1e4a99; }
QPushButton#primary:disabled { background: #a9bce0; color: #f0f0f0; }

QComboBox {
    background: #ffffff;
    border: 1px solid #d0d4da;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 10pt;
    min-width: 180px;
    color: #1a1a1a;
    outline: 0;
}
QComboBox:hover { border-color: #b9bec6; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    border: 1px solid #e3e6ea;
    border-radius: 8px;
    background: #ffffff;
    selection-background-color: #e8f0fe;
    selection-color: #1a1a1a;
    padding: 4px;
    color: #1a1a1a;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    background: #ffffff;
    color: #1a1a1a;
    min-height: 22px;
}
QComboBox QAbstractItemView::item:hover {
    background: #e8f0fe;
}

QMenuBar {
    background: #ffffff;
    border-bottom: 1px solid #e3e6ea;
    font-size: 10pt;
    color: #1a1a1a;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}
QMenuBar::item:selected { background: #eef1f5; }
QMenu {
    background: #ffffff;
    border: 1px solid #e3e6ea;
    border-radius: 8px;
    padding: 4px;
    color: #1a1a1a;
}
QMenu::item {
    padding: 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected { background: #e8f0fe; }
QMenu::separator {
    height: 1px;
    background: #e3e6ea;
    margin: 4px 8px;
}
"""

STYLE_DARK = """
QDialog { background: #1e1f22; }

QLabel { color: #e6e6e6; }
QLabel#headerTitle {
    font-size: 10pt; color: #e6e6e6;
}
QLabel#headerStatus {
    font-size: 11pt; color: #b8bcc2; padding: 2px 0;
}
QLabel#headerStatus[held="true"] { color: #e6e6e6; font-weight: 400; }
QLabel#headerStatus[held="false"] { color: #e6e6e6; font-weight: 400; }

QFrame#card {
    background: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 10px;
}

/* --- Баннер глобального состояния (тёмная тема) --- */
QFrame#statusBanner {
    border-radius: 10px;
    background: #1f3a2a;
    border: 1px solid #2e5a3f;
}
QFrame#statusBanner[held="true"] {
    background: #3a1f22;
    border: 1px solid #6b2f35;
}
QFrame#statusBanner[held="false"] {
    background: #1f3a2a;
    border: 1px solid #2e5a3f;
}

QListWidget {
    background: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 10px;
    padding: 6px;
    font-size: 10.5pt;
    color: #e6e6e6;
}
QListWidget::item {
    padding: 8px 6px;
    border-radius: 6px;
    color: #e6e6e6;
}
QListWidget::item:selected {
    background: #2f4a7a;
    color: #ffffff;
}
QListWidget::item:hover {
    background: #2d3035;
}

QPushButton {
    background: #2a2c30;
    border: 1px solid #3a3d42;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 10pt;
    color: #e6e6e6;
}
QPushButton:hover { background: #32353a; border-color: #4a4e54; }
QPushButton:pressed { background: #3a3d42; }
QPushButton:disabled { color: #6b6f75; border-color: #2a2c30; }

QPushButton#primary {
    background: #4a86f7; color: #ffffff; border: none;
    font-weight: 600;
}
QPushButton#primary:hover { background: #3f76dc; }
QPushButton#primary:pressed { background: #3567c4; }
QPushButton#primary:disabled { background: #2f4a7a; color: #b8bcc2; }

QComboBox {
    background: #2a2c30;
    border: 1px solid #3a3d42;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 10pt;
    min-width: 180px;
    color: #e6e6e6;
    outline: 0;
}
QComboBox:hover { border-color: #4a4e54; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    border: 1px solid #3a3d42;
    border-radius: 8px;
    background: #26282c;
    selection-background-color: #2f4a7a;
    selection-color: #ffffff;
    padding: 4px;
    color: #e6e6e6;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    background: #26282c;
    color: #e6e6e6;
    min-height: 22px;
}
QComboBox QAbstractItemView::item:hover {
    background: #2f4a7a;
}
QComboBox QAbstractItemView::item:selected {
    background: #2f4a7a;
    color: #ffffff;
}

QMenuBar {
    background: #26282c;
    border-bottom: 1px solid #3a3d42;
    font-size: 10pt;
    color: #e6e6e6;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}
QMenuBar::item:selected { background: #32353a; }
QMenu {
    background: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 8px;
    padding: 4px;
    color: #e6e6e6;
}
QMenu::item {
    padding: 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected { background: #2f4a7a; }
QMenu::separator {
    height: 1px;
    background: #3a3d42;
    margin: 4px 8px;
}

QProgressDialog { background: #1e1f22; color: #e6e6e6; }
QProgressBar {
    background: #2a2c30; border: 1px solid #3a3d42;
    border-radius: 6px; text-align: center; color: #e6e6e6;
}
QProgressBar::chunk { background: #4a86f7; border-radius: 5px; }

QTextEdit {
    background: #26282c; color: #e6e6e6;
    border: 1px solid #3a3d42; border-radius: 8px;
}

QToolTip {
    background: #2a2c30; color: #e6e6e6;
    border: 1px solid #3a3d42; padding: 4px;
}
"""

STYLE = STYLE_LIGHT


# ---------- Главное окно ----------
class SnapManager(QDialog):
    def __init__(self, snapd_ver):
        super().__init__()
        self.snapd_ver = snapd_ver
        self.current_theme = "light"
        self.setWindowTitle(f"{TITLE_BASE} (v{snapd_ver})")
        self.resize(460, 680)
        self.setStyleSheet(STYLE_LIGHT)

        self.initial_status = {}
        self.pkg_display = {}
        self.is_system = {}
        self.current_checked = set()

        self._build_ui()
        self._load_data()

    # --- Палитры ---
    def _apply_dark_palette(self):
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#1e1f22"))
        pal.setColor(QPalette.WindowText, QColor("#e6e6e6"))
        pal.setColor(QPalette.Base, QColor("#26282c"))
        pal.setColor(QPalette.AlternateBase, QColor("#2a2c30"))
        pal.setColor(QPalette.ToolTipBase, QColor("#2a2c30"))
        pal.setColor(QPalette.ToolTipText, QColor("#e6e6e6"))
        pal.setColor(QPalette.Text, QColor("#e6e6e6"))
        pal.setColor(QPalette.Button, QColor("#2a2c30"))
        pal.setColor(QPalette.ButtonText, QColor("#e6e6e6"))
        pal.setColor(QPalette.BrightText, QColor("#ff6b6b"))
        pal.setColor(QPalette.Link, QColor("#4a86f7"))
        pal.setColor(QPalette.Highlight, QColor("#2f4a7a"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b6f75"))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b6f75"))
        QApplication.setPalette(pal)

    def _apply_light_palette(self):
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#f7f8fa"))
        pal.setColor(QPalette.WindowText, QColor("#1a1a1a"))
        pal.setColor(QPalette.Base, QColor("#ffffff"))
        pal.setColor(QPalette.AlternateBase, QColor("#f2f5f9"))
        pal.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
        pal.setColor(QPalette.ToolTipText, QColor("#1a1a1a"))
        pal.setColor(QPalette.Text, QColor("#1a1a1a"))
        pal.setColor(QPalette.Button, QColor("#ffffff"))
        pal.setColor(QPalette.ButtonText, QColor("#1a1a1a"))
        pal.setColor(QPalette.BrightText, QColor("#e5484d"))
        pal.setColor(QPalette.Link, QColor("#2d6cdf"))
        pal.setColor(QPalette.Highlight, QColor("#e8f0fe"))
        pal.setColor(QPalette.HighlightedText, QColor("#1a1a1a"))
        QApplication.setPalette(pal)

    # --- UI ---
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        # --- Баннер глобального состояния (вариант A) ---
        self.status_banner = QFrame()
        self.status_banner.setObjectName("statusBanner")
        self.status_banner.setAttribute(Qt.WA_StyledBackground, True)
        self.status_banner.setFrameShape(QFrame.NoFrame)
        card_lay = QHBoxLayout(self.status_banner)
        card_lay.setContentsMargins(14, 10, 14, 10)
        card_lay.setSpacing(8)

        self.global_label = QLabel()
        self.global_label.setObjectName("headerStatus")
        card_lay.addWidget(self.global_label)
        card_lay.addStretch()

        root.addWidget(self.status_banner)
        root.addSpacing(2)

        # --- Строка: сортировка слева ---
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        top_row.addWidget(QLabel("Сортировка:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Сначала системные", "По имени (А-Я)", "Сначала заблокированные"])
        self.sort_combo.currentIndexChanged.connect(self._rebuild_list)
        # Убираем внешнюю (квадратную) рамку popup-списка — остаётся только QSS-рамка
        self.sort_combo.view().setFrameShape(QFrame.NoFrame)
        self.sort_combo.view().setAutoFillBackground(False)
        top_row.addWidget(self.sort_combo)
        top_row.addStretch()
        root.addLayout(top_row)

        # --- Список ---
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.NoSelection)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list_widget, 1)

        # --- Нижняя панель действий ---
        bottom = QHBoxLayout()
        bottom.setSpacing(10)

        self.btn_apply = QPushButton("💾 Применить блокировку")
        self.btn_apply.setObjectName("primary")
        self.btn_apply.clicked.connect(self.on_apply)

        self.btn_update = QPushButton("🔄 Обновить выбранные")
        self.btn_update.clicked.connect(self.on_update_selected)

        bottom.addWidget(self.btn_apply)
        bottom.addWidget(self.btn_update)
        bottom.addStretch()
        root.addLayout(bottom)

        # --- Статус-бар ---
        self.status = QLabel()
        self.status.setObjectName("headerStatus")
        self.status.setStyleSheet("font-size: 9pt; color: #777;")
        root.addWidget(self.status)

        # --- Верхнее меню (Menu Bar) ---
        menubar = QMenuBar(self)

        # 1. Меню "Управление Snap"
        snap_menu = menubar.addMenu("Управление Snap")

        act_hold = QAction("⛔ Приостановить все обновления", self)
        act_hold.setShortcut(QKeySequence("Ctrl+Shift+H"))
        act_hold.triggered.connect(self.on_global_hold)
        snap_menu.addAction(act_hold)

        act_unhold = QAction("🌐 Возобновить все обновления", self)
        act_unhold.setShortcut(QKeySequence("Ctrl+Shift+U"))
        act_unhold.triggered.connect(self.on_global_unhold)
        snap_menu.addAction(act_unhold)

        snap_menu.addSeparator()

        # --- Подменю "Тема оформления" ---
        theme_menu = snap_menu.addMenu("🎨 Тема оформления")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)

        self.act_theme_light = QAction("☀️ Светлая тема", self, checkable=True)
        self.act_theme_light.setChecked(True)
        self.act_theme_light.triggered.connect(lambda: self.apply_theme("light"))
        theme_group.addAction(self.act_theme_light)
        theme_menu.addAction(self.act_theme_light)

        self.act_theme_dark = QAction("🌙 Тёмная тема", self, checkable=True)
        self.act_theme_dark.triggered.connect(lambda: self.apply_theme("dark"))
        theme_group.addAction(self.act_theme_dark)
        theme_menu.addAction(self.act_theme_dark)

        snap_menu.addSeparator()

        act_quit = QAction("Выход", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.reject)
        snap_menu.addAction(act_quit)

        # 2. Меню "Отметить пакеты"
        select_menu = menubar.addMenu("Отметить пакеты")

        act_check_all = QAction("✅ Выделить все", self)
        act_check_all.setShortcut(QKeySequence("Ctrl+A"))
        act_check_all.triggered.connect(self.select_all)
        select_menu.addAction(act_check_all)

        act_uncheck_all = QAction("⬜ Снять все", self)
        act_uncheck_all.setShortcut(QKeySequence("Ctrl+D"))
        act_uncheck_all.triggered.connect(self.select_none)
        select_menu.addAction(act_uncheck_all)

        root.setMenuBar(menubar)

        QShortcut(QKeySequence("Return"), self, activated=self.on_apply)

    # --- Тема ---
    def apply_theme(self, theme):
        self.current_theme = theme
        style = STYLE_DARK if theme == "dark" else STYLE_LIGHT

        if theme == "dark":
            self._apply_dark_palette()
        else:
            self._apply_light_palette()

        self.setStyleSheet(style)

        held_str = self.status_banner.property("held") or "false"
        for w in (self.status_banner, self.global_label):
            w.setProperty("held", held_str)
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()

        if theme == "dark":
            self.status.setStyleSheet("font-size: 9pt; color: #8b9097;")
        else:
            self.status.setStyleSheet("font-size: 9pt; color: #777;")

    # --- Данные ---
    def _load_data(self):
        self.initial_status.clear()
        self.pkg_display.clear()
        self.is_system.clear()
        self.current_checked.clear()

        statuses = get_snap_statuses()
        for pkg, st in statuses.items():
            self.initial_status[pkg] = 1 if st == "held" else 0
            if SYSTEM_SNAPS_RE.match(pkg):
                self.pkg_display[pkg] = f"⚠️  {pkg}"
                self.is_system[pkg] = 1
            else:
                self.pkg_display[pkg] = pkg
                self.is_system[pkg] = 0
            if self.initial_status[pkg] == 1:
                self.current_checked.add(pkg)

        held_globally = is_global_held()
        if held_globally:
            self.global_label.setText("🔴 Все обновления приостановлены")
        else:
            self.global_label.setText("🟢 Обновления активны (фоновый режим)")

        held_str = "true" if held_globally else "false"
        for w in (self.status_banner, self.global_label):
            w.setProperty("held", held_str)
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()

        self.setStyleSheet(STYLE_DARK if self.current_theme == "dark" else STYLE_LIGHT)

        self._rebuild_list()
        self._update_status()

    def _sorted_packages(self):
        mode = self.sort_combo.currentText()
        pkgs = list(self.initial_status.keys())
        if mode == "По имени (А-Я)":
            return sorted(pkgs)
        if mode == "Сначала заблокированные":
            return sorted(pkgs, key=lambda p: (-self.initial_status[p], p))
        return sorted(pkgs, key=lambda p: (-self.is_system[p], p))

    def _rebuild_list(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for pkg in self._sorted_packages():
            item = QListWidgetItem(self.pkg_display[pkg])
            item.setData(Qt.UserRole, pkg)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if pkg in self.current_checked else Qt.Unchecked)
            if self.is_system[pkg]:
                item.setToolTip("Системный компонент — блокировать не рекомендуется")
            if self.initial_status[pkg]:
                item.setToolTip((item.toolTip() + "\n" if item.toolTip() else "")
                                + "Сейчас заблокирован (--hold)")
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

    def _on_item_changed(self, item):
        pkg = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.current_checked.add(pkg)
        else:
            self.current_checked.discard(pkg)
        self._update_status()

    def _update_status(self):
        total = len(self.initial_status)
        checked = len(self.current_checked)
        held = sum(1 for v in self.initial_status.values() if v == 1)
        self.status.setText(
            f"Всего: {total}   •   Отмечено: {checked}   •   Заблокировано сейчас: {held}"
        )

    def _show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        act_check = menu.addAction("✅ Выделить")
        act_uncheck = menu.addAction("⬜ Снять")
        menu.addSeparator()
        act_toggle = menu.addAction("🔒 Переключить блокировку (без применения)")
        chosen = menu.exec_(self.list_widget.mapToGlobal(pos))
        if chosen == act_check:
            item.setCheckState(Qt.Checked)
        elif chosen == act_uncheck:
            item.setCheckState(Qt.Unchecked)
        elif chosen == act_toggle:
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )

    # --- Массовые операции ---
    def select_all(self):
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            it.setCheckState(Qt.Checked)
            self.current_checked.add(it.data(Qt.UserRole))
        self.list_widget.blockSignals(False)
        self._update_status()

    def select_none(self):
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            it.setCheckState(Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self.current_checked.clear()
        self._update_status()

    # --- Глобальные действия ---
    def on_global_hold(self):
        rc, _, _ = run(["snap", "refresh", "--hold"])
        if rc == 0:
            QMessageBox.information(
                self, self.windowTitle(),
                "⛔ Глобальная блокировка включена.\n\n"
                "Все Snap-пакеты больше не будут обновляться автоматически."
            )
        else:
            QMessageBox.critical(self, self.windowTitle(), "❌ Ошибка при глобальной блокировке.")
        self._load_data()

    def on_global_unhold(self):
        rc, _, _ = run(["snap", "refresh", "--unhold"])
        if rc == 0:
            QMessageBox.information(
                self, self.windowTitle(),
                "🟢 Глобальная блокировка снята.\n\n"
                "Автоматические обновления всех Snap-пакетов снова активны."
            )
        else:
            QMessageBox.critical(self, self.windowTitle(), "❌ Ошибка при снятии глобальной блокировки.")
        self._load_data()

    # --- Обычная блокировка ---
    def on_apply(self):
        held = unheld = 0
        errors = ""
        for pkg, init in self.initial_status.items():
            curr = 1 if pkg in self.current_checked else 0
            if init == 0 and curr == 1:
                rc, _, _ = run(["snap", "refresh", "--hold", pkg])
                if rc == 0: held += 1
                else: errors += f"Ошибка при блокировке {pkg}\n"
            elif init == 1 and curr == 0:
                rc, _, _ = run(["snap", "refresh", "--unhold", pkg])
                if rc == 0: unheld += 1
                else: errors += f"Ошибка при разблокировке {pkg}\n"

        if errors:
            QMessageBox.warning(
                self, "Готово, но были ошибки",
                f"🔒 Заблокировано: {held}\n🔓 Разблокировано: {unheld}\n\n{errors}"
            )
        elif held == 0 and unheld == 0:
            QMessageBox.information(self, "Без изменений",
                                    "Изменений нет.\nВсе галочки остались на своих местах.")
        else:
            QMessageBox.information(
                self, "Готово",
                f"🔒 Заблокировано: {held}\n🔓 Разблокировано: {unheld}"
            )
        self._load_data()

    # --- Разовое обновление ---
    def on_update_selected(self):
        to_update = list(self.current_checked)
        if not to_update:
            QMessageBox.information(self, "Нет выбора",
                                    "Не выбрано ни одного пакета для обновления.")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        master_log = os.path.join(
            LOG_DIR, f"update-{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )

        self.progress_dlg = QProgressDialog("Подготовка...", None, 0, 100, self)
        self.progress_dlg.setWindowTitle(self.windowTitle())
        self.progress_dlg.setWindowModality(Qt.WindowModal)
        self.progress_dlg.setMinimumDuration(0)
        self.progress_dlg.setCancelButton(None)
        self.progress_dlg.setValue(0)

        self.worker = UpdateWorker(to_update, master_log)
        self.worker.progress.connect(self._on_progress)
        self.worker.ask_question.connect(self._on_ask)
        self.worker.finished.connect(self._on_update_finished)
        self.worker.start()

    def _on_progress(self, percent, msg):
        if getattr(self, "progress_dlg", None):
            self.progress_dlg.setValue(percent)
            self.progress_dlg.setLabelText(msg)

    def _on_ask(self, pkg, text):
        ans = QMessageBox.question(
            self, "Приложение запущено", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.Yes
        if getattr(self, "worker", None):
            self.worker.set_answer(ans)

    def _on_update_finished(self, updated, total, errors):
        if getattr(self, "progress_dlg", None):
            self.progress_dlg.close()
            self.progress_dlg = None

        master_log = self.worker.master_log
        if errors:
            text = (
                f"Готово!\n\nУспешно обновлено: {updated}\n\n"
                f"Ошибки:\n{errors}\n\nПолный лог сохранен в:\n{master_log}"
            )
            dlg = QDialog(self)
            dlg.setWindowTitle("Результат обновления")
            dlg.resize(640, 420)
            lay = QVBoxLayout(dlg)
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText(text)
            lay.addWidget(te)
            close_btn = QPushButton("Закрыть")
            close_btn.clicked.connect(dlg.accept)
            lay.addWidget(close_btn, alignment=Qt.AlignRight)
            dlg.exec_()
        else:
            QMessageBox.information(
                self, "Результат обновления",
                f"Успешно обновлено пакетов: {updated}\n\n"
                f"Полный лог сохранен в:\n{master_log}"
            )
        self._load_data()


# ---------- main ----------
def main():
    require_root()
    cleanup_old_logs()

    snapd_ver = get_snapd_version()
    if not check_snapd_version(snapd_ver):
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None, "Версия не поддерживается",
            f"Текущая версия snapd ({snapd_ver}) ниже минимально требуемой 2.58.\n\n"
            f"Функция --hold недоступна, скрипт не может продолжить работу.\n\n"
            f"Пожалуйста, сначала обновите snapd:\n  sudo snap refresh snapd"
        )
        sys.exit(1)

    app = QApplication(sys.argv)
    dlg = SnapManager(snapd_ver)
    dlg.exec_()


if __name__ == "__main__":
    main()