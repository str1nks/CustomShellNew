# -*- coding: utf-8 -*-
"""
CustomShellNew (CSNew) — менеджер меню «Создать» (контекстное меню Проводника Windows 11)

Установка:
    pip install PyQt6 "PyQt6-Fluent-Widgets[full]"

Запуск (лучше от имени администратора):
    python new_menu_manager.py

Как это работает — два слоя:

1) Список Проводника (управляет ВСЕМИ пунктами, в том числе от Store-приложений вроде Paint):
       HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Discardable\\PostSetup\\ShellNew
       значение "Classes" (REG_MULTI_SZ) — какие пункты показывать и в каком порядке.
   Проводник сам перезаписывает это значение, поэтому программа «закрепляет» ключ
   (явный запрет Deny SetValue для Everyone) — так делает и ContextMenuManager.

2) Ключи ShellNew в HKCR\\.расширение (шаблоны, названия, значки, удаление, добавление новых типов).
   Отключение пункта дополнительно переименовывает ShellNew -> ShellNew.disabled.

3) Вкладка «Пуск»: категория каждого приложения в разделе «Все» меню «Пуск» (режим «По категориям»), перетаскиванием:
       HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Start\\TileProperties\\<приложение>
       значение "Category" (REG_DWORD). Права администратора не нужны.
"""
import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QFileInfo, QItemSelectionModel, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFileIconProvider, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QListWidgetItem, QMenu,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, FluentIcon as FIF, FluentWindow, InfoBar,
    InfoBarIcon, InfoBarPosition, LineEdit, ListWidget, MessageBox,
    MessageBoxBase, PrimaryPushButton, PushButton, SearchLineEdit,
    SegmentedWidget, StrongBodyLabel, SubtitleLabel, SwitchButton,
    TableWidget, Theme, TitleLabel, setTheme, themeColor,
)
try:
    from qfluentwidgets import SimpleCardWidget
except ImportError:          # старые версии библиотеки
    SimpleCardWidget = QFrame

# ───────────────────────────── Реестр: общее ─────────────────────────────

ROOT = winreg.HKEY_CLASSES_ROOT
HKCU = winreg.HKEY_CURRENT_USER
R = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
W = winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
ON, OFF = "ShellNew", "ShellNew.disabled"
ORDER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Discardable\PostSetup\ShellNew"

APP_NAME = "CustomShellNew"
APP_SHORT = "CSNew"
_APPDATA = Path(os.environ.get("APPDATA", "."))
APP_DIR = _APPDATA / APP_NAME
_OLD_APP_DIR = _APPDATA / "NewMenuManager"
if _OLD_APP_DIR.exists() and not APP_DIR.exists():   # перенос бэкапов/настроек со старого имени
    try:
        _OLD_APP_DIR.rename(APP_DIR)
    except OSError:
        APP_DIR = _OLD_APP_DIR
BACKUPS = APP_DIR / "backups"
TEMPLATES = APP_DIR / "templates"
STATE_FILE = APP_DIR / "state.json"
WINDOW_FILE = APP_DIR / "window.json"

SPECIAL_NAMES = {"Folder": "Папку", ".lnk": "Ярлык", ".library-ms": "Библиотеку",
                 "Briefcase": "Портфель"}
ALWAYS_VISIBLE = ("Folder", ".lnk")
# Пункты Store-приложений, которые Windows может не держать в списке, пока они скрыты
KNOWN_STORE = {".bmp": "Точечный рисунок"}


def resource_path(name: str) -> str:
    """Путь к ресурсу и в обычном запуске, и внутри собранного .exe (PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


APP_ID = f"{APP_NAME}.{APP_SHORT}"
ICON_FILE = "icon.ico"


def app_icon() -> QIcon:
    """icon.ico из ресурсов .exe или из папки рядом со скриптом/exe."""
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__)),
                 os.path.dirname(os.path.abspath(sys.argv[0]))):
        if base:
            f = os.path.join(base, ICON_FILE)
            if os.path.isfile(f):
                return QIcon(f)
    print(f"[{APP_SHORT}] не найден {ICON_FILE} — положите его рядом с программой", file=sys.stderr)
    return QIcon()


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def notify_shell():
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def enum_keys(path: str) -> list[str]:
    out = []
    try:
        with winreg.OpenKey(ROOT, path, 0, R) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return out


def enum_values(path: str) -> dict:
    """{имя_в_нижнем_регистре: (оригинальное_имя, значение, тип)}"""
    out = {}
    try:
        with winreg.OpenKey(ROOT, path, 0, R) as k:
            i = 0
            while True:
                try:
                    n, v, t = winreg.EnumValue(k, i)
                    out[n.lower()] = (n, v, t)
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return out


def read_str(path: str, name: str = "") -> str:
    try:
        with winreg.OpenKey(ROOT, path, 0, R) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v if isinstance(v, str) else ""
    except OSError:
        return ""


def key_exists(path: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(ROOT, path, 0, R))
        return True
    except OSError:
        return False


def resolve_indirect(s: str) -> str:
    if not s or not s.startswith("@"):
        return s
    buf = ctypes.create_unicode_buffer(512)
    try:
        if ctypes.windll.shlwapi.SHLoadIndirectString(s, buf, 512, None) == 0:
            return buf.value
    except Exception:
        pass
    return ""


def copy_tree(src: str, dst: str):
    with winreg.OpenKey(ROOT, src, 0, R) as s, winreg.CreateKeyEx(ROOT, dst, 0, W) as d:
        i = 0
        while True:
            try:
                n, v, t = winreg.EnumValue(s, i)
                winreg.SetValueEx(d, n, 0, t, v)
                i += 1
            except OSError:
                break
    for sub in enum_keys(src):
        copy_tree(f"{src}\\{sub}", f"{dst}\\{sub}")


def delete_tree(path: str):
    for sub in enum_keys(path):
        delete_tree(f"{path}\\{sub}")
    winreg.DeleteKeyEx(ROOT, path, winreg.KEY_WOW64_64KEY, 0)


# ─────────────── Список Проводника (Classes) и его блокировка ───────────────

_PS_COMMON = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sub = '@KEY@'
$sid = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
$rk  = [Microsoft.Win32.Registry]::CurrentUser
function Open-Key {
  # Только DACL: Set-Acl пытается писать ещё и владельца/группу -> PermissionDenied
  $rk.OpenSubKey($sub, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,
    [System.Security.AccessControl.RegistryRights]'ReadKey,ChangePermissions')
}
function Get-Denies($acl) {
  @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) |
    Where-Object { $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Value -eq 'S-1-1-0' })
}
""".replace("@KEY@", ORDER_KEY)

_PS_LOCK = _PS_COMMON + r"""
$rk.CreateSubKey($sub).Close()
$key = Open-Key
$acl = $key.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
if ((Get-Denies $acl).Count -eq 0) {
  $rule = New-Object System.Security.AccessControl.RegistryAccessRule($sid,
    [System.Security.AccessControl.RegistryRights]'SetValue,CreateSubKey,Delete',
    [System.Security.AccessControl.InheritanceFlags]'ContainerInherit',
    [System.Security.AccessControl.PropagationFlags]'None',
    [System.Security.AccessControl.AccessControlType]'Deny')
  $acl.AddAccessRule($rule)
  $key.SetAccessControl($acl)
}
$key.Close()
"""

_PS_UNLOCK = _PS_COMMON + r"""
$key = Open-Key
if ($key) {
  $acl = $key.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
  foreach ($r in (Get-Denies $acl)) { [void]$acl.RemoveAccessRule($r) }
  $key.SetAccessControl($acl)
  $key.Close()
}
"""

_PS_STATE = _PS_COMMON + r"""
$key = Open-Key
if ($key) {
  $acl = $key.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
  $key.Close()
  if ((Get-Denies $acl).Count -gt 0) { '1' } else { '0' }
} else { '0' }
"""


def _ps(script: str) -> str:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-OutputFormat", "Text", "-EncodedCommand", enc],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        msg = (r.stderr or "").strip()
        if msg.startswith("#< CLIXML"):
            msg = "ошибка изменения прав на ключ"
        raise RuntimeError("PowerShell: " + (msg or "ошибка изменения прав на ключ"))
    return r.stdout.strip()


def is_locked() -> bool:
    return _ps(_PS_STATE) == "1"


def set_lock(lock: bool):
    _ps(_PS_LOCK if lock else _PS_UNLOCK)


def read_classes() -> list[str] | None:
    try:
        with winreg.OpenKey(HKCU, ORDER_KEY, 0, winreg.KEY_READ) as k:
            v, t = winreg.QueryValueEx(k, "Classes")
            return [str(x) for x in v] if t == winreg.REG_MULTI_SZ else None
    except OSError:
        return None


def write_classes(classes: list[str], was_locked: bool, lock_after: bool):
    if was_locked:
        set_lock(False)
    with winreg.CreateKeyEx(HKCU, ORDER_KEY, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "Classes", 0, winreg.REG_MULTI_SZ, classes)
    if lock_after:
        set_lock(True)


def delete_classes_value():
    try:
        with winreg.OpenKey(HKCU, ORDER_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "Classes")
    except OSError:
        pass


def restart_explorer():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], creationflags=flags)
    subprocess.Popen(["explorer.exe"])


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(items: list["Entry"], lock_on: bool):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = {"order": [e.ext for e in items],
            "store": {e.ext: e.name for e in items if not e.registry},
            "lock": lock_on}
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────── Модель ───────────────────────────────

@dataclass
class Entry:
    ext: str                 # имя класса: ".txt", "Folder", "Briefcase"
    base: str                # путь к родителю ключа ShellNew ("" для пунктов приложений)
    key: str                 # "ShellNew" / "ShellNew.disabled" ("" для пунктов приложений)
    progid: str
    values: dict = field(default_factory=dict)
    name: str = ""           # подпись в меню
    new_name: str = ""       # имя нового файла (ItemName)
    registry: bool = True    # есть ключ ShellNew в реестре
    in_classes: bool = False # есть в списке Проводника
    visible: bool = False    # реально показывается в меню

    @property
    def path(self) -> str:
        return f"{self.base}\\{self.key}"

    @property
    def enabled(self) -> bool:
        return self.registry and self.key.lower() == ON.lower()

    def val(self, name: str, default=""):
        return self.values.get(name.lower(), (name, default, 0))[1]

    def has(self, name: str) -> bool:
        return name.lower() in self.values

    @property
    def locked_template(self) -> bool:
        return self.has("Handler") or self.has("Command")

    @property
    def special_only(self) -> bool:
        return self.registry and self.locked_template and self.ext not in ALWAYS_VISIBLE

    @property
    def kind(self) -> str:
        if not self.registry:
            return "Приложение (Store)"
        if self.special_only:
            return "Системный (спец. папки)"
        if self.has("Handler"):
            return "Системный обработчик"
        if self.has("Command"):
            return "Команда"
        if self.has("FileName"):
            return "Из шаблона"
        if self.has("Data"):
            return "Файл с данными"
        if self.has("NullFile"):
            return "Пустой файл"
        return "Другое"


def friendly_name(cls: str, progid: str) -> str:
    if cls in SPECIAL_NAMES:
        return SPECIAL_NAMES[cls]
    if progid:
        for v in ("FriendlyTypeName", ""):
            n = resolve_indirect(read_str(progid, v))
            if n:
                return n
    return ""


def _display_name(e: Entry) -> str:
    n = friendly_name(e.ext, e.progid)
    if n:
        return n
    n = resolve_indirect(str(e.val("ItemName", "")))
    return n or f"Файл {e.ext.lstrip('.').upper()}"


def scan(extra: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    names = [n for n in enum_keys("") if n.startswith(".") or n == "Folder"]
    names += [c for c in extra if c not in names]
    wanted = (ON.lower(), OFF.lower())
    for ext in names:
        if ext.startswith("."):
            progid = read_str(ext)
        else:
            progid = ext
        for sub in enum_keys(ext):
            if sub.lower() in wanted:
                found = [(ext, sub, progid)]
            elif ext.startswith("."):
                found = [(f"{ext}\\{sub}", s2, sub)
                         for s2 in enum_keys(f"{ext}\\{sub}") if s2.lower() in wanted]
            else:
                found = []
            for base, key, pid in found:
                e = Entry(ext, base, key, pid, enum_values(f"{base}\\{key}"))
                e.name = _display_name(e)
                e.new_name = resolve_indirect(str(e.val("ItemName", "")))
                entries.append(e)
    return entries


def build_items(state: dict) -> tuple[list[Entry], bool]:
    """Собирает единый список: ключи ShellNew + пункты приложений + порядок."""
    classes = read_classes()
    known = classes is not None
    cset = set(classes or [])

    by: dict[str, Entry] = {}
    for e in scan([c for c in (classes or []) if not c.startswith(".")]):
        cur = by.get(e.ext)
        if cur is None or (e.enabled and not cur.enabled):
            by[e.ext] = e
    for e in by.values():
        e.in_classes = e.ext in cset
        e.visible = (e.in_classes and e.enabled) if known else (e.enabled and not e.special_only)

    # пункты без ключа ShellNew (Store-приложения) + скрытые ранее
    store = dict(state.get("store", {}))
    for c in cset:
        if c not in by and c not in store:
            store[c] = friendly_name(c, read_str(c) if c.startswith(".") else c) or c
    for c, nm in KNOWN_STORE.items():
        if c not in by and c not in store:
            store[c] = nm
    for c, nm in store.items():
        if c in by:
            continue
        e = Entry(c, "", "", "", {}, name=nm, registry=False)
        e.in_classes = c in cset
        e.visible = e.in_classes
        by[c] = e

    order = [c for c in state.get("order", []) if c in by]
    for c in (classes or []):
        if c in by and c not in order:
            order.append(c)
    for c in sorted(by, key=lambda x: by[x].name.casefold()):
        if c not in order:
            order.append(c)
    return [by[c] for c in order], known


# ─────────────────────── Операции над ключами ShellNew ───────────────────────

def set_enabled(e: Entry, enable: bool):
    new = ON if enable else OFF
    if e.key == new:
        return
    dst = f"{e.base}\\{new}"
    if key_exists(dst):
        delete_tree(dst)
    copy_tree(e.path, dst)
    delete_tree(e.path)


def backup_entry(e: Entry) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    data = {"path": e.path, "name": e.name,
            "values": [[n, v.hex() if isinstance(v, bytes) else v, t]
                       for n, v, t in e.values.values()]}
    f = BACKUPS / f"{datetime.now():%Y%m%d_%H%M%S}_{e.ext.strip('.') or 'x'}.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def restore_backup(file: Path):
    d = json.loads(Path(file).read_text(encoding="utf-8"))
    with winreg.CreateKeyEx(ROOT, d["path"], 0, W) as k:
        for n, v, t in d["values"]:
            if t in (winreg.REG_BINARY, winreg.REG_NONE) and isinstance(v, str):
                v = bytes.fromhex(v)
            winreg.SetValueEx(k, n, 0, t, v)


def delete_entry(e: Entry):
    backup_entry(e)
    delete_tree(e.path)


def import_template(ext: str, src: str) -> str:
    p = Path(src)
    if not p.is_file() or TEMPLATES in p.parents:
        return src
    TEMPLATES.mkdir(parents=True, exist_ok=True)
    dst = TEMPLATES / f"{ext.strip('.')}_{p.name}"
    shutil.copy2(p, dst)
    return str(dst)


def set_label(ext: str, progid: str, name: str) -> str:
    if not progid:
        progid = f"{APP_SHORT}{ext}"
        with winreg.CreateKeyEx(ROOT, ext, 0, W) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, progid)
    with winreg.CreateKeyEx(ROOT, progid, 0, W) as p:
        winreg.SetValueEx(p, "", 0, winreg.REG_SZ, name)
        try:
            winreg.QueryValueEx(p, "FriendlyTypeName")
            winreg.SetValueEx(p, "FriendlyTypeName", 0, winreg.REG_SZ, name)
        except OSError:
            pass
    return progid


def _drop(k, n):
    try:
        winreg.DeleteValue(k, n)
    except OSError:
        pass


def create_entry(ext: str, name: str, template: str, icon: str, new_name: str):
    if key_exists(f"{ext}\\{ON}") or key_exists(f"{ext}\\{OFF}"):
        raise ValueError(f"Для {ext} элемент ShellNew уже существует.")
    with winreg.CreateKeyEx(ROOT, ext, 0, W):
        pass
    set_label(ext, read_str(ext), name)
    with winreg.CreateKeyEx(ROOT, f"{ext}\\{ON}", 0, W) as k:
        if template:
            winreg.SetValueEx(k, "FileName", 0, winreg.REG_SZ, import_template(ext, template))
        else:
            winreg.SetValueEx(k, "NullFile", 0, winreg.REG_SZ, "")
        if new_name:
            winreg.SetValueEx(k, "ItemName", 0, winreg.REG_SZ, new_name)
        if icon:
            winreg.SetValueEx(k, "IconPath", 0, winreg.REG_SZ, icon)


def update_entry(e: Entry, name: str, template: str, icon: str, new_name: str):
    if e.ext not in SPECIAL_NAMES and name != e.name:
        set_label(e.ext, e.progid, name)
    with winreg.CreateKeyEx(ROOT, e.path, 0, W) as k:
        if new_name != e.new_name:
            if new_name:
                winreg.SetValueEx(k, "ItemName", 0, winreg.REG_SZ, new_name)
            else:
                _drop(k, "ItemName")
        if not e.locked_template and template != str(e.val("FileName", "")):
            if template:
                winreg.SetValueEx(k, "FileName", 0, winreg.REG_SZ, import_template(e.ext, template))
                _drop(k, "NullFile")
                _drop(k, "Data")
            else:
                _drop(k, "FileName")
                winreg.SetValueEx(k, "NullFile", 0, winreg.REG_SZ, "")
        if icon:
            winreg.SetValueEx(k, "IconPath", 0, winreg.REG_SZ, icon)
        else:
            _drop(k, "IconPath")


# ─────────────────────────────── Значки ───────────────────────────────

_provider = QFileIconProvider()
_icon_cache: dict[str, QIcon] = {}


def icon_for(ext: str) -> QIcon:
    if ext not in _icon_cache:
        if ext == "Folder":
            ic = _provider.icon(QFileIconProvider.IconType.Folder)
        elif ext.startswith("."):
            ic = _provider.icon(QFileInfo(f"x{ext}"))
        else:
            ic = QIcon()
        _icon_cache[ext] = ic if not ic.isNull() else FIF.DOCUMENT.icon()
    return _icon_cache[ext]


def fic(name: str):
    return getattr(FIF, name, FIF.INFO)


# ─────────────────────────────── Диалог ───────────────────────────────

class EntryDialog(MessageBoxBase):
    def __init__(self, parent, entry: Entry | None = None):
        super().__init__(parent)
        self.entry = entry
        self.viewLayout.addWidget(
            SubtitleLabel("Изменить элемент" if entry else "Новый элемент меню «Создать»"))

        self.extEdit = LineEdit()
        self.extEdit.setPlaceholderText(".md")
        self.nameEdit = LineEdit()
        self.nameEdit.setPlaceholderText("Документ Markdown")
        self.newNameEdit = LineEdit()
        self.newNameEdit.setPlaceholderText("Необязательно, например: Новый документ")
        self.tplEdit = LineEdit()
        self.tplEdit.setPlaceholderText("Пусто — будет создаваться пустой файл")
        self.iconEdit = LineEdit()
        self.iconEdit.setPlaceholderText("Необязательно: .ico / .exe / .dll")

        self._row("Расширение файла", self.extEdit)
        self._row("Название в меню (описание типа файла)", self.nameEdit)
        self._row("Имя нового файла по умолчанию", self.newNameEdit)
        self._row("Файл-шаблон", self.tplEdit, self._browse(self.tplEdit, "Все файлы (*.*)", False))
        self._row("Значок", self.iconEdit,
                  self._browse(self.iconEdit, "Значки (*.ico *.exe *.dll);;Все файлы (*.*)", True))

        self.err = CaptionLabel("")
        self.err.setStyleSheet("color:#d13438;")
        self.viewLayout.addWidget(self.err)

        if entry:
            self.extEdit.setText(entry.ext)
            self.extEdit.setEnabled(False)
            self.nameEdit.setText(entry.name)
            self.newNameEdit.setText(entry.new_name)
            if entry.ext in SPECIAL_NAMES:
                self.nameEdit.setEnabled(False)
            self.tplEdit.setText(str(entry.val("FileName", "")))
            self.iconEdit.setText(str(entry.val("IconPath", "")))
            if entry.locked_template:
                self.tplEdit.setEnabled(False)
                self.tplEdit.setPlaceholderText("Этот элемент создаётся системой — шаблон недоступен")

        self.yesButton.setText("Сохранить")
        self.cancelButton.setText("Отмена")
        self.widget.setMinimumWidth(520)

    def _row(self, caption, edit, button=None):
        self.viewLayout.addWidget(CaptionLabel(caption))
        if button:
            h = QHBoxLayout()
            h.addWidget(edit, 1)
            h.addWidget(button)
            self.viewLayout.addLayout(h)
        else:
            self.viewLayout.addWidget(edit)

    def _browse(self, edit: LineEdit, flt: str, icon_mode: bool):
        btn = PushButton("Обзор…")

        def pick():
            f, _ = QFileDialog.getOpenFileName(self, "Выберите файл", "", flt)
            if f:
                f = os.path.normpath(f)
                if icon_mode and not f.lower().endswith(".ico"):
                    f += ",0"
                edit.setText(f)

        btn.clicked.connect(pick)
        return btn

    def validate(self) -> bool:
        ext = self.extEdit.text().strip()
        if ext and not ext.startswith("."):
            ext = "." + ext
            self.extEdit.setText(ext)
        if not self.entry:
            if not re.fullmatch(r"\.[A-Za-z0-9_+\-]{1,32}", ext):
                self.err.setText("Расширение: точка и латинские буквы/цифры, например .md")
                return False
            if key_exists(f"{ext}\\{ON}") or key_exists(f"{ext}\\{OFF}"):
                self.err.setText(f"Для {ext} элемент уже есть в списке.")
                return False
        if not self.nameEdit.text().strip():
            self.err.setText("Укажите название элемента.")
            return False
        tpl = self.tplEdit.text().strip()
        orig = str(self.entry.val("FileName", "")) if self.entry else ""
        if tpl and tpl != orig and not os.path.isfile(tpl):
            self.err.setText("Файл-шаблон не найден.")
            return False
        return True

    def data(self):
        return (self.extEdit.text().strip(), self.nameEdit.text().strip(),
                self.tplEdit.text().strip(), self.iconEdit.text().strip(),
                self.newNameEdit.text().strip())


# ─────────────────────────────── Страницы ───────────────────────────────

class NewMenuPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("newMenuPage")
        self.win = win
        self.items: list[Entry] = []
        self.rows: list[Entry] = []
        self.known = False
        self.filter = "all"
        self._select: str | None = None

        st = load_state()
        self.lock_on: bool = bool(st.get("lock", True))
        try:
            self.locked_now = is_locked()
        except Exception:
            self.locked_now = False

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 20, 32, 20)
        root.setSpacing(10)

        root.addWidget(TitleLabel("Меню «Создать»"))
        root.addWidget(BodyLabel("Все пункты меню ПКМ → Создать, включая добавленные приложениями. "
                                 "Справа — предпросмотр в том порядке, который будет в Проводнике."))

        if not is_admin():
            bar = InfoBar(icon=InfoBarIcon.WARNING, title="Нет прав администратора",
                          content="Порядок и скрытие работают, но шаблоны, названия и удаление "
                                  "системных ключей реестра могут не сработать.",
                          orient=Qt.Orientation.Horizontal, isClosable=False,
                          position=InfoBarPosition.NONE, duration=-1, parent=self)
            b = PushButton("Перезапустить от администратора")
            b.clicked.connect(self.relaunch_admin)
            bar.addWidget(b)
            root.addWidget(bar)

        self.warn = CaptionLabel("Windows ещё не сформировала список меню. Откройте в Проводнике "
                                 "ПКМ → Создать, затем нажмите «Обновить».")
        self.warn.setStyleSheet("color:#d13438;")
        root.addWidget(self.warn)

        # поиск + фильтр
        top = QHBoxLayout()
        self.search = SearchLineEdit()
        self.search.setPlaceholderText("Поиск по названию или расширению")
        self.search.setFixedWidth(320)
        self.search.textChanged.connect(self.refilter)
        self.seg = SegmentedWidget()
        for key, text in (("all", "Все"), ("on", "В меню"), ("off", "Скрытые")):
            self.seg.addItem(key, text, onClick=lambda _=False, k=key: self.set_filter(k))
        self.seg.setCurrentItem("all")
        top.addWidget(self.search)
        top.addSpacing(12)
        top.addWidget(self.seg)
        top.addStretch(1)
        self.stats = CaptionLabel("")
        top.addWidget(self.stats)
        root.addLayout(top)

        # действия
        btns = QHBoxLayout()
        self._btn(btns, PrimaryPushButton(fic("ADD"), "Добавить", self), self.add)
        self._btn(btns, PushButton(fic("EDIT"), "Изменить", self), self.edit)
        self._btn(btns, PushButton(fic("DELETE"), "Удалить", self), self.remove)
        btns.addSpacing(16)
        self._btn(btns, PushButton(fic("UP"), "Выше", self), lambda: self.move(-1))
        self._btn(btns, PushButton(fic("DOWN"), "Ниже", self), lambda: self.move(1))
        btns.addStretch(1)
        root.addLayout(btns)

        # закрепление и сервис
        svc = QHBoxLayout()
        svc.addWidget(BodyLabel("Закрепить меню:"))
        self.lock_sw = SwitchButton()
        self.lock_sw.setOnText("Да")
        self.lock_sw.setOffText("Нет")
        self.lock_sw.checkedChanged.connect(self.on_lock)
        svc.addWidget(self.lock_sw)
        svc.addWidget(CaptionLabel("— без этого Проводник сам вернёт порядок и скрытые пункты "
                                   "(но и новые программы не смогут добавить свои)"))
        svc.addStretch(1)
        self._btn(svc, PushButton(fic("SYNC"), "Обновить", self), self.reload)
        self._btn(svc, PushButton(fic("ROTATE"), "Перезапустить Проводник", self), self.restart_explorer)
        self._btn(svc, PushButton(fic("CANCEL"), "Сбросить", self), self.reset)
        root.addLayout(svc)

        # таблица + предпросмотр
        body = QHBoxLayout()
        body.setSpacing(16)
        self.table = TableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["№", "Элемент", "Расширение", "Тип", "В меню"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setIconSize(QSize(22, 22))
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit())
        body.addWidget(self.table, 3)

        right = QVBoxLayout()
        right.addWidget(StrongBodyLabel("Так выглядит меню «Создать»"))
        self.preview = ListWidget()
        self.preview.setIconSize(QSize(20, 20))
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        right.addWidget(self.preview, 1)
        note = CaptionLabel("Проводник может закреплять «Папку» и «Ярлык» отдельной группой вверху "
                            "независимо от порядка. Пункты приложений (Paint и т.п.) можно скрывать "
                            "и двигать, но не переименовывать.")
        note.setWordWrap(True)
        right.addWidget(note)
        body.addLayout(right, 2)
        root.addLayout(body, 1)

        self.detail = CaptionLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)

        QShortcut(QKeySequence("Alt+Up"), self, activated=lambda: self.move(-1))
        QShortcut(QKeySequence("Alt+Down"), self, activated=lambda: self.move(1))

        QTimer.singleShot(0, self.reload)

    @staticmethod
    def _btn(layout, button, slot):
        button.clicked.connect(slot)
        layout.addWidget(button)

    # ── служебное ──
    def toast(self, kind: str, title: str, text: str = ""):
        getattr(InfoBar, kind)(title=title, content=text, orient=Qt.Orientation.Horizontal,
                               isClosable=True, position=InfoBarPosition.TOP_RIGHT,
                               duration=4000, parent=self.win)

    def guarded(self, fn, ok_title: str, select: str | None = None):
        self._select = select
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fn()
        except PermissionError:
            self.toast("error", "Недостаточно прав", "Запустите программу от имени администратора.")
        except Exception as ex:  # noqa: BLE001
            self.toast("error", "Ошибка", str(ex))
        else:
            notify_shell()
            self.toast("success", ok_title,
                       "Проводник держит список в памяти: если меню не изменилось, "
                       "нажмите «Перезапустить Проводник».")
        finally:
            QApplication.restoreOverrideCursor()
        QTimer.singleShot(0, self.reload)

    def relaunch_admin(self):
        if getattr(sys, "frozen", False):          # собранный .exe
            exe, args = sys.executable, sys.argv[1:]
        else:                                       # запуск из .py
            exe, args = sys.executable, [os.path.abspath(sys.argv[0])] + sys.argv[1:]
        params = " ".join(f'"{a}"' for a in args)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        QApplication.quit()

    def current(self) -> Entry | None:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def commit(self, strict: bool = True, lock: bool | None = None):
        """Записывает порядок/состав меню в список Проводника и (опционально) закрепляет его."""
        if not self.known:
            if strict:
                raise RuntimeError("Windows ещё не сформировала список меню. Откройте в Проводнике "
                                   "ПКМ → Создать, нажмите «Обновить» и повторите.")
            return
        lock = self.lock_on if lock is None else lock
        write_classes([e.ext for e in self.items if e.visible], self.locked_now, lock)
        self.locked_now = lock
        save_state(self.items, self.lock_on)

    # ── данные ──
    def reload(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.items, self.known = build_items(load_state())
        finally:
            QApplication.restoreOverrideCursor()
        self.warn.setVisible(not self.known)
        self.lock_sw.blockSignals(True)
        self.lock_sw.setChecked(self.lock_on)
        self.lock_sw.blockSignals(False)
        self.refilter()

    def set_filter(self, k: str):
        self.filter = k
        self.refilter()

    def refilter(self):
        q = self.search.text().strip().lower()
        self.rows = [
            e for e in self.items
            if (self.filter == "all" or (self.filter == "on") == e.visible)
            and (not q or q in e.name.lower() or q in e.ext.lower())
        ]
        self.fill_table()
        self.fill_preview()
        shown = sum(e.visible for e in self.items)
        self.stats.setText(f"Всего: {len(self.items)} · в меню: {shown}")

    def fill_table(self):
        num = {e.ext: i + 1 for i, e in enumerate(x for x in self.items if x.visible)}
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(self.rows))
        for r, e in enumerate(self.rows):
            self.table.setItem(r, 0, QTableWidgetItem(str(num.get(e.ext, "—"))))
            self.table.setItem(r, 1, QTableWidgetItem(icon_for(e.ext), e.name))
            self.table.setItem(r, 2, QTableWidgetItem(e.ext))
            self.table.setItem(r, 3, QTableWidgetItem(e.kind))
            sw = SwitchButton()
            sw.setOnText("Вкл")
            sw.setOffText("Выкл")
            sw.setChecked(e.visible)
            sw.checkedChanged.connect(lambda c, ent=e: self.toggle(ent, c))
            holder = QWidget()
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(8, 0, 8, 0)
            lay.addWidget(sw, 0, Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(r, 4, holder)
        self.table.blockSignals(False)
        if self._select:
            for r, e in enumerate(self.rows):
                if e.ext == self._select:
                    self.table.selectRow(r)
                    break
            self._select = None
        self.show_detail()

    def fill_preview(self):
        self.preview.clear()
        for e in self.items:
            if e.visible:
                self.preview.addItem(QListWidgetItem(icon_for(e.ext), e.name))

    def show_detail(self):
        e = self.current()
        if not e:
            self.detail.setText("Выберите элемент, чтобы увидеть подробности. "
                                "Порядок меняют кнопки «Выше/Ниже» или Alt+↑ / Alt+↓.")
            return
        if not e.registry:
            self.detail.setText("Пункт добавляет приложение (ключа ShellNew в реестре нет). "
                                "Управляется через список Проводника: скрытие и порядок.")
            return
        parts = [f"Реестр: HKEY_CLASSES_ROOT\\{e.path}"]
        parts.append("в списке Проводника: " + ("да" if e.in_classes else "нет"))
        if e.new_name:
            parts.append(f"Имя нового файла: {e.new_name}")
        for label, k in (("Шаблон", "FileName"), ("Значок", "IconPath"), ("Команда", "Command")):
            if e.has(k):
                parts.append(f"{label}: {e.val(k)}")
        self.detail.setText("   |   ".join(parts))

    # ── действия ──
    def toggle(self, e: Entry, show: bool):
        def job():
            if e.registry:
                try:
                    set_enabled(e, show)
                except PermissionError:
                    if show:
                        raise
            e.visible = e.in_classes = show
            self.commit()

        self.guarded(job, "Пункт показан" if show else "Пункт скрыт", select=e.ext)

    def move(self, d: int):
        if self.filter != "all" or self.search.text().strip():
            return self.toast("warning", "Сбросьте поиск и фильтр", "Порядок меняется только в полном списке.")
        e = self.current()
        if not e:
            return self.toast("warning", "Сначала выберите элемент")
        i = self.items.index(e)
        j = i + d
        if e.visible:
            while 0 <= j < len(self.items) and not self.items[j].visible:
                j += d
        if not 0 <= j < len(self.items):
            return

        def job():
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self.commit()

        self.guarded(job, "Порядок изменён", select=e.ext)

    def add(self):
        dlg = EntryDialog(self.win)
        if dlg.exec():
            ext, name, tpl, icon, nn = dlg.data()

            def job():
                create_entry(ext, name, tpl, icon, nn)
                self.items, self.known = build_items(load_state())
                for e in self.items:
                    if e.ext == ext:
                        e.visible = e.in_classes = True
                self.commit(strict=False)

            self.guarded(job, "Элемент добавлен", select=ext)

    def edit(self):
        e = self.current()
        if not e:
            return self.toast("warning", "Сначала выберите элемент")
        if not e.registry:
            return self.toast("info", "Пунктом управляет приложение",
                              "Название и шаблон менять нельзя — только скрывать и двигать.")
        dlg = EntryDialog(self.win, e)
        if dlg.exec():
            _, name, tpl, icon, nn = dlg.data()
            self.guarded(lambda: update_entry(e, name, tpl, icon, nn), "Изменения сохранены", select=e.ext)

    def remove(self):
        e = self.current()
        if not e:
            return self.toast("warning", "Сначала выберите элемент")
        if e.registry:
            text = (f"«{e.name}» ({e.ext}) будет удалён из реестра и из меню «Создать».\n"
                    "Резервная копия сохранится на вкладке «Резервные копии».")
            ok = "Элемент удалён"
        else:
            text = (f"«{e.name}» добавлен приложением, физически удалить его нельзя. "
                    "Пункт будет убран из меню; вернуть его можно переключателем «В меню».")
            ok = "Пункт убран из меню"
        box = MessageBox("Удалить элемент?", text, self.win)
        box.yesButton.setText("Удалить" if e.registry else "Убрать")
        box.cancelButton.setText("Отмена")
        if not box.exec():
            return

        def job():
            if e.registry:
                delete_entry(e)
                self.items.remove(e)
            else:
                e.visible = e.in_classes = False
            self.commit(strict=not e.registry or self.known)

        self.guarded(job, ok)

    def on_lock(self, checked: bool):
        def job():
            if checked:
                self.commit(lock=True)
                self.lock_on = True
            else:
                if self.locked_now:
                    set_lock(False)
                    self.locked_now = False
                self.lock_on = False
            save_state(self.items, self.lock_on)

        self.guarded(job, "Меню закреплено" if checked else "Закрепление снято")

    def reset(self):
        box = MessageBox("Сбросить настройки?",
                         "Будет снято закрепление и удалён сохранённый список Проводника — Windows "
                         "соберёт меню заново (порядок и скрытые пункты приложений вернутся к "
                         "стандартным). Пункты, отключённые переименованием ключей, останутся "
                         "отключёнными.", self.win)
        box.yesButton.setText("Сбросить")
        box.cancelButton.setText("Отмена")
        if not box.exec():
            return

        def job():
            if self.locked_now:
                set_lock(False)
                self.locked_now = False
            delete_classes_value()
            STATE_FILE.unlink(missing_ok=True)
            self.lock_on = True

        self.guarded(job, "Настройки сброшены. Откройте ПКМ → Создать, чтобы Windows пересобрала список")

    def restart_explorer(self):
        box = MessageBox("Перезапустить Проводник?",
                         "Окна Проводника закроются и откроются заново. Это нужно, если изменения "
                         "не появились в меню сразу.", self.win)
        box.yesButton.setText("Перезапустить")
        box.cancelButton.setText("Отмена")
        if box.exec():
            self.guarded(restart_explorer, "Проводник перезапущен")


class BackupPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("backupPage")
        self.win = win
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 20, 32, 20)
        root.setSpacing(12)
        root.addWidget(TitleLabel("Резервные копии"))
        root.addWidget(BodyLabel("Копии создаются автоматически перед удалением элемента из реестра."))

        row = QHBoxLayout()
        res = PrimaryPushButton(fic("HISTORY"), "Восстановить", self)
        dele = PushButton(fic("DELETE"), "Удалить копию", self)
        opn = PushButton(fic("FOLDER"), "Открыть папку", self)
        res.clicked.connect(self.restore)
        dele.clicked.connect(self.delete)
        opn.clicked.connect(self.open_folder)
        for b in (res, dele, opn):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        self.list = ListWidget()
        root.addWidget(self.list, 1)
        self.refresh()

    def refresh(self):
        self.list.clear()
        if BACKUPS.exists():
            for f in sorted(BACKUPS.glob("*.json"), reverse=True):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    it = QListWidgetItem(f"{f.stem[:15]}   —   {d.get('name', '?')}   ({d.get('path', '')})")
                    it.setData(Qt.ItemDataRole.UserRole, str(f))
                    self.list.addItem(it)
                except Exception:
                    continue

    def _file(self):
        it = self.list.currentItem()
        return Path(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def restore(self):
        f = self._file()
        if not f:
            return
        page = self.win.menu_page
        try:
            restore_backup(f)
            notify_shell()
            page.toast("success", "Элемент восстановлен",
                       "Включите его переключателем «В меню» на первой вкладке.")
            page.reload()
        except PermissionError:
            page.toast("error", "Недостаточно прав", "Запустите от администратора.")
        except Exception as ex:  # noqa: BLE001
            page.toast("error", "Ошибка", str(ex))

    def delete(self):
        f = self._file()
        if f:
            f.unlink(missing_ok=True)
            self.refresh()

    def open_folder(self):
        BACKUPS.mkdir(parents=True, exist_ok=True)
        os.startfile(BACKUPS)  # noqa: S606


# ───────────────────── Пуск: категории приложений ─────────────────────

START_KEY = r"Software\Microsoft\Windows\CurrentVersion\Start\TileProperties"
START_BACKUPS = APP_DIR / "start_backups"
SW = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY

# (название, номер для записи, все номера этой категории). Microsoft значения не документирует —
# в одну категорию входит несколько номеров (подкатегории). Порядок = порядок плиток на доске.
START_CATS = [
    ("Другое", 0, (0,)),
    ("Специальные возможности", 1, (1,)),
    ("Производительность", 5, (2, 5, 13)),
    ("Игры", 3, (3,)),
    ("Путешествия", 4, (4,)),
    ("Креативность", 7, (6, 7, 22, 23, 30)),
    ("Социальные сети", 8, (8,)),
    ("Служебные программы и средства", 9, (9,)),
    ("Дети и семья", 10, (10,)),
    ("Медицина", 11, (11,)),
    ("Здоровье и фитнес", 12, (12,)),
    ("Информация и чтение", 14, (14, 21, 24, 25, 31)),
    ("Средства разработчика", 15, (15,)),
    ("Развлечения", 16, (16,)),
    ("Музыка", 17, (17,)),
    ("Личные финансы", 18, (18,)),
    ("Образование", 19, (19,)),
    ("Служебные", 20, (20,)),
    ("Спорт", 26, (26,)),
    ("Стиль жизни", 27, (27,)),
    ("Покупки", 28, (28,)),
    ("Еда и питание", 29, (29,)),
    ("(Категория без названия)", 32, ()),   # все номера 32 и выше
]
START_OTHER = 0      # «Другое» (значение 0); всё неизвестное (отрицательное и т.п.) тоже сюда
START_UNNAMED = 32   # номера 32+ — «категория без названия»; при записи используется 32
START_NAMES = [c[0] for c in START_CATS]
START_VALUES = [c[1] for c in START_CATS]
START_GROUP = {v: c[1] for c in START_CATS for v in c[2]}
START_LABEL = {c[1]: c[0] for c in START_CATS}

_PS_START_APPS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$a = @(Get-StartApps | ForEach-Object { [pscustomobject]@{ n = $_.Name; i = $_.AppID } })
ConvertTo-Json -InputObject $a -Compress
"""


def start_group(v: int) -> int:
    if v >= START_UNNAMED:
        return START_UNNAMED
    return START_GROUP.get(v, START_OTHER)


def start_label(v: int) -> str:
    return START_LABEL[start_group(v)]


def start_app_id(rel: str) -> str:
    """«P~Пакет!App» -> «Пакет!App»; «W~{GUID}\\x.exe» -> «{GUID}\\x.exe» (так же, как AppID в Get-StartApps)."""
    return rel[2:] if rel[:2] in ("P~", "W~") else rel


def start_fallback_name(rel: str) -> str:
    app_id = start_app_id(rel)
    if rel.startswith("P~"):
        return app_id.split("!")[0].split("_")[0]
    last = app_id.rsplit("\\", 1)[-1]
    base, ext = os.path.splitext(last)
    return base if ext.lower() in (".exe", ".lnk", ".chm", ".msc", ".cpl", ".url", ".bat", ".cmd") else last


def scan_start_tiles() -> list[tuple[str, int]]:
    """[(путь ключа относительно TileProperties, Category)] — ключи могут быть вложенными (W~{GUID}\\app.exe)."""
    out: list[tuple[str, int]] = []

    def walk(rel: str):
        path = START_KEY + (f"\\{rel}" if rel else "")
        subs = []
        try:
            with winreg.OpenKey(HKCU, path, 0, R) as k:
                if rel:
                    try:
                        v, t = winreg.QueryValueEx(k, "Category")
                        if t == winreg.REG_DWORD:
                            out.append((rel, int(v)))
                    except OSError:
                        pass
                i = 0
                while True:
                    try:
                        subs.append(winreg.EnumKey(k, i))
                        i += 1
                    except OSError:
                        break
        except OSError:
            return
        for s in subs:
            walk(f"{rel}\\{s}" if rel else s)

    walk("")
    return out


def set_start_category(rel: str, value: int):
    with winreg.OpenKey(HKCU, f"{START_KEY}\\{rel}", 0, SW) as k:
        winreg.SetValueEx(k, "Category", 0, winreg.REG_DWORD, value)


def backup_start(apps: list["StartApp"]) -> Path:
    START_BACKUPS.mkdir(parents=True, exist_ok=True)
    f = START_BACKUPS / f"{datetime.now():%Y%m%d_%H%M%S}.json"
    f.write_text(json.dumps({"key": START_KEY, "categories": {a.rel: a.category for a in apps}},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def latest_start_backup() -> Path | None:
    files = sorted(START_BACKUPS.glob("*.json")) if START_BACKUPS.exists() else []
    return files[-1] if files else None


def restore_start(file: Path) -> int:
    cats = json.loads(Path(file).read_text(encoding="utf-8")).get("categories", {})
    done = 0
    for rel, v in cats.items():
        try:
            set_start_category(rel, int(v))
            done += 1
        except OSError:
            continue   # приложение удалено — ключа уже нет
    return done


# ── «Найти удалённые»: записи TileProperties от приложений, которых уже нет ──

_PS_START_STATE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$apps = @(Get-StartApps | ForEach-Object { [string]$_.AppID })
$pkgs = @(Get-AppxPackage | ForEach-Object { [string]$_.PackageFamilyName })
ConvertTo-Json -InputObject ([pscustomobject]@{ apps = $apps; pkgs = $pkgs }) -Compress
"""


def _known_folder(guid: str) -> str:
    """{GUID известной папки} -> путь (например, Program Files) или '' если не удалось."""
    try:
        rfid = _GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
    except ValueError:
        return ""
    ptr = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(rfid), 0, None, ctypes.byref(ptr)) != 0:
        return ""
    try:
        return ptr.value or ""
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(ptr, ctypes.c_void_p))


def _tile_target(app_id: str) -> str:
    """Путь к файлу классического приложения из AppID: «{GUID}\\папка\\x.exe» или обычный путь. Иначе ''."""
    m = re.match(r"^\{([0-9A-Fa-f-]{36})\}[\\/](.+)$", app_id)
    if m:
        base = _known_folder(m.group(1))
        return os.path.join(base, m.group(2)) if base else ""
    p = os.path.expandvars(app_id)
    return p if re.match(r"^[A-Za-z]:[\\/]", p) else ""


def find_ghost_tiles(tiles: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """[(ключ, причина)] — записи, у которых приложения уже нет.
    Осторожно: запись считается «призраком», только если приложения нет в Get-StartApps И
    (Store) пакет не установлен / (классическое) файл по пути из AppID не существует.
    Всё, в чём нельзя быть уверенным, остаётся нетронутым."""
    raw = _ps(_PS_START_STATE)
    data = json.loads(raw) if raw else {}

    def as_set(v) -> set[str]:
        if isinstance(v, str):
            v = [v]
        return {str(x).lower() for x in (v or [])}

    apps, pkgs = as_set(data.get("apps")), as_set(data.get("pkgs"))
    if not apps:
        raise RuntimeError("Get-StartApps вернул пустой список — определить удалённые приложения нельзя")
    out: list[tuple[str, str]] = []
    for rel, _ in tiles:
        app_id = start_app_id(rel)
        if app_id.lower() in apps:
            continue
        if rel.startswith("P~"):
            pfn = app_id.split("!")[0].lower()
            if pkgs and pfn and pfn not in pkgs:
                out.append((rel, "Пакет Store не установлен"))
            continue
        path = _tile_target(app_id)
        if path and not os.path.exists(path):
            out.append((rel, f"Файл не найден: {path}"))
    return out


def delete_start_tile(rel: str):
    """Удаляет ключ приложения из TileProperties (и опустевшие родительские ключи W~{GUID})."""
    path = f"{START_KEY}\\{rel}"
    with winreg.OpenKey(HKCU, path, 0, W) as k:
        try:
            winreg.EnumKey(k, 0)
            has_sub = True
        except OSError:
            has_sub = False
        if has_sub:                        # вложенные ключи — чужие приложения, их не трогаем
            winreg.DeleteValue(k, "Category")
            return
    winreg.DeleteKeyEx(HKCU, path, winreg.KEY_WOW64_64KEY, 0)
    parent = rel.rpartition("\\")[0]
    while parent:
        ppath = f"{START_KEY}\\{parent}"
        try:
            with winreg.OpenKey(HKCU, ppath, 0, W) as pk:
                nsub, nval, _ = winreg.QueryInfoKey(pk)
            if nsub or nval:
                break
            winreg.DeleteKeyEx(HKCU, ppath, winreg.KEY_WOW64_64KEY, 0)
        except OSError:
            break
        parent = parent.rpartition("\\")[0]


# ── значки приложений: IShellItemImageFactory через ctypes (без pywin32) ──

class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort),
                ("bmBitsPixel", ctypes.c_ushort), ("bmBits", ctypes.c_void_p)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def shell_icon_bits(app_id: str, size: int = 32):
    """BGRA-пиксели значка приложения из shell:AppsFolder\\<AppID> -> (w, h, bytes) или None."""
    vp = ctypes.c_void_p
    shell32, gdi32, user32 = ctypes.windll.shell32, ctypes.windll.gdi32, ctypes.windll.user32
    create = shell32.SHCreateItemFromParsingName
    create.argtypes = [ctypes.c_wchar_p, vp, ctypes.POINTER(_GUID), ctypes.POINTER(vp)]
    create.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [vp, ctypes.c_int, vp]
    gdi32.GetDIBits.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_uint, vp, vp, ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [vp]
    user32.GetDC.argtypes = [vp]
    user32.GetDC.restype = vp
    user32.ReleaseDC.argtypes = [vp, vp]

    iid = _GUID.from_buffer_copy(uuid.UUID("BCC18B79-BA16-442F-80C4-8A59C30C463B").bytes_le)
    obj = vp()
    if create("shell:AppsFolder\\" + app_id, None, ctypes.byref(iid), ctypes.byref(obj)) < 0 or not obj.value:
        return None
    vt = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(vp))[0], ctypes.POINTER(vp))
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, vp)(vt[2])
    get_image = ctypes.WINFUNCTYPE(ctypes.c_long, vp, _SIZE, ctypes.c_uint, ctypes.POINTER(vp))(vt[3])
    hbm = vp()
    try:
        if get_image(obj, _SIZE(size, size), 0x4, ctypes.byref(hbm)) < 0 or not hbm.value:   # SIIGBF_ICONONLY
            return None
    finally:
        release(obj)
    try:
        bm = _BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, bm.bmHeight
        if w <= 0 or h <= 0:
            return None
        bmi = _BMIH()
        bmi.biSize, bmi.biWidth, bmi.biHeight = ctypes.sizeof(_BMIH), w, -h
        bmi.biPlanes, bmi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        try:
            gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0)
        finally:
            user32.ReleaseDC(None, hdc)
        data = bytearray(buf.raw)
        if not any(data[3::4]):          # у части значков нет альфа-канала
            data[3::4] = b"\xff" * (w * h)
        return w, h, bytes(data)
    finally:
        gdi32.DeleteObject(hbm)


@dataclass
class StartApp:
    rel: str
    name: str
    category: int

    @property
    def kind(self) -> str:
        return "Приложение Store" if self.rel.startswith("P~") else "Классическое"


class CategoryList(ListWidget):
    """Список приложений одной категории: принимает перетаскивание из других списков."""
    dropped = pyqtSignal(list, int)          # (ключи приложений, номер категории-приёмника)

    def __init__(self, value: int, parent=None):
        super().__init__(parent)
        self.value = value
        self._hover = False
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setIconSize(QSize(22, 22))
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def _set_hover(self, on: bool):
        if self._hover != on:
            self._hover = on
            self.viewport().update()

    def _accepts(self, e) -> bool:
        src = e.source()
        return isinstance(src, CategoryList) and src is not self

    def dragEnterEvent(self, e):
        if self._accepts(e):
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
            self._set_hover(True)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._accepts(e):
            super().dragMoveEvent(e)                 # автопрокрутка у краёв списка
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_hover(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_hover(False)
        if not self._accepts(e):
            e.ignore()
            return
        rels = [it.data(Qt.ItemDataRole.UserRole) for it in e.source().selectedItems()]
        # IgnoreAction: Qt сам ничего не переносит и не удаляет — список перестроит страница
        e.setDropAction(Qt.DropAction.IgnoreAction)
        e.accept()
        if rels:
            self.dropped.emit(rels, self.value)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.viewport().rect()
        if self.count() == 0:
            p.setPen(QColor(128, 128, 128))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Перетащите сюда")
        if self._hover:
            fill = QColor(themeColor())
            fill.setAlpha(28)
            p.setBrush(fill)
            p.setPen(QPen(themeColor(), 2))
            p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 6, 6)
        p.end()


class GhostDialog(MessageBoxBase):
    """Подтверждение удаления одной записи. «Пропустить» — оставить, «Остановить» — прекратить обход."""

    def __init__(self, parent, pos: int, total: int, app: "StartApp", reason: str):
        super().__init__(parent)
        self.stopped = False
        self.viewLayout.addWidget(SubtitleLabel(f"Удалить запись из реестра? ({pos} из {total})"))
        self.viewLayout.addWidget(StrongBodyLabel(app.name))
        for text in (reason, f"HKEY_CURRENT_USER\\{START_KEY}\\{app.rel}"):
            lbl = CaptionLabel(text)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.viewLayout.addWidget(lbl)
        self.yesButton.setText("Удалить")
        self.cancelButton.setText("Пропустить")
        stop = PushButton("Остановить")
        stop.clicked.connect(self._stop)
        self.buttonLayout.addWidget(stop)
        self.widget.setMinimumWidth(520)

    def _stop(self):
        self.stopped = True
        self.reject()


class StartMenuPage(QWidget):
    _loaded = pyqtSignal(list, dict)
    _icons = pyqtSignal(dict)
    _ghosts = pyqtSignal(list, str)
    COLS = 6

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("startMenuPage")
        self.win = win
        self.apps: list[StartApp] = []
        self.by_rel: dict[str, StartApp] = {}
        self.items: dict[str, QListWidgetItem] = {}
        self.lists: dict[int, CategoryList] = {}
        self.counts: dict[int, CaptionLabel] = {}
        self._loading = False
        self._backed_up = False
        self._scanning = False
        self.icons: dict[str, QIcon] = {}
        self._icon_tried: set[str] = set()
        self._loaded.connect(self._on_loaded)
        self._icons.connect(self._on_icons)
        self._ghosts.connect(self._on_ghosts)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 20, 32, 20)
        root.setSpacing(10)
        root.addWidget(TitleLabel("Пуск"))
        root.addWidget(BodyLabel("Категория каждого приложения в разделе «Все» меню «Пуск» (режим «По категориям»). "
                                 "Перетаскивайте приложения мышью в нужную категорию — можно сразу несколько "
                                 "(Ctrl / Shift). Правая кнопка мыши — «Переместить в…»."))

        self.warn = CaptionLabel("В реестре нет приложений с категорией. Откройте «Пуск» → «Все» в режиме "
                                 "«По категориям» и нажмите «Обновить».")
        self.warn.setStyleSheet("color:#d13438;")
        self.warn.hide()
        root.addWidget(self.warn)

        top = QHBoxLayout()
        self.search = SearchLineEdit()
        self.search.setPlaceholderText("Поиск по названию или ключу реестра")
        self.search.setFixedWidth(260)
        self.search.textChanged.connect(self.refilter)
        top.addWidget(self.search)
        top.addStretch(1)
        self.stats = CaptionLabel("")
        top.addWidget(self.stats)
        top.addSpacing(12)
        self.btn_ghosts = PushButton(fic("SEARCH"), "Найти удалённые", self)
        for btn, slot in ((PushButton(fic("SYNC"), "Обновить", self), self.reload),
                          (PushButton(fic("CLEAR_SELECTION"), "Разгруппировать", self), self.ungroup),
                          (self.btn_ghosts, self.find_ghosts),
                          (PushButton(fic("HISTORY"), "Откатить из копии", self), self.rollback),
                          (PushButton(fic("ROTATE"), "Перезапустить Проводник", self), self.restart)):
            btn.clicked.connect(slot)
            top.addWidget(btn)
        root.addLayout(top)

        # доска категорий: сетка из COLS колонок, каждая ячейка — карточка со своим списком
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (name, value, _) in enumerate(START_CATS):
            card = SimpleCardWidget(self)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(10, 10, 10, 10)
            cl.setSpacing(6)
            head = QHBoxLayout()
            title = StrongBodyLabel(name)
            title.setWordWrap(True)
            cnt = CaptionLabel("0")
            head.addWidget(title, 1)
            head.addWidget(cnt, 0, Qt.AlignmentFlag.AlignTop)
            cl.addLayout(head)
            lst = CategoryList(value)
            lst.dropped.connect(self._on_drop)
            lst.itemSelectionChanged.connect(lambda l=lst: self._on_sel(l))
            lst.customContextMenuRequested.connect(lambda pos, l=lst: self._context_menu(l, pos))
            cl.addWidget(lst, 1)
            grid.addWidget(card, i // self.COLS, i % self.COLS)
            self.lists[value] = lst
            self.counts[value] = cnt
        for c in range(self.COLS):
            grid.setColumnStretch(c, 1)
        for r in range((len(START_CATS) + self.COLS - 1) // self.COLS):
            grid.setRowStretch(r, 1)
        root.addLayout(grid, 1)

        self.detail = CaptionLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)
        root.addWidget(CaptionLabel("Изменения применяются после перезапуска Проводника (или повторного входа в "
                                    "систему). Перед первым изменением за сеанс программа сохраняет копию "
                                    "всех категорий. Номера категорий в Windows не документированы."))
        self.show_detail()

        QTimer.singleShot(0, self.reload)

    # ── служебное ──
    def toast(self, kind: str, title: str, text: str = ""):
        getattr(InfoBar, kind)(title=title, content=text, orient=Qt.Orientation.Horizontal,
                               isClosable=True, position=InfoBarPosition.TOP_RIGHT,
                               duration=4000, parent=self.win)

    def selected(self) -> list[StartApp]:
        out = []
        for lst in self.lists.values():
            for it in lst.selectedItems():
                a = self.by_rel.get(it.data(Qt.ItemDataRole.UserRole))
                if a:
                    out.append(a)
        return out

    # ── данные ──
    def reload(self):
        if self._loading:
            return
        self._loading = True
        self.stats.setText("Загрузка…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self):
        tiles = scan_start_tiles()
        names: dict[str, str] = {}
        try:   # красивые названия берём у самой Windows (Get-StartApps)
            raw = _ps(_PS_START_APPS)
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = [data]
            names = {str(d.get("i", "")).lower(): str(d.get("n", "")) for d in data if d.get("i")}
        except Exception:  # noqa: BLE001
            pass
        self._loaded.emit(tiles, names)

    def _on_loaded(self, tiles: list, names: dict):
        keep = {a.rel for a in self.selected()}
        self._loading = False
        self.apps = [StartApp(rel, names.get(start_app_id(rel).lower()) or start_fallback_name(rel), v)
                     for rel, v in tiles]
        self.by_rel = {a.rel: a for a in self.apps}
        self.warn.setVisible(not self.apps)
        self.refilter(keep=keep)
        todo = [(a.rel, start_app_id(a.rel)) for a in self.apps if a.rel not in self._icon_tried]
        self._icon_tried.update(r for r, _ in todo)
        if todo:
            threading.Thread(target=self._icon_worker, args=(todo,), daemon=True).start()

    def _icon_worker(self, todo: list):
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)   # STA
        except Exception:  # noqa: BLE001
            pass
        batch: dict = {}
        for rel, app_id in todo:
            try:
                bits = shell_icon_bits(app_id)
            except Exception:  # noqa: BLE001
                bits = None
            if bits:
                batch[rel] = bits
            if len(batch) >= 20:
                self._icons.emit(batch)
                batch = {}
        if batch:
            self._icons.emit(batch)
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass

    def _on_icons(self, batch: dict):
        for rel, (w, h, data) in batch.items():
            img = QImage(data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
            self.icons[rel] = QIcon(QPixmap.fromImage(img))
            item = self.items.get(rel)
            if item is not None:
                try:
                    item.setIcon(self.icons[rel])
                except RuntimeError:     # элемент уже удалён при перестроении
                    pass

    def refilter(self, *_, keep=None):
        """Перестраивает доску: раскладывает приложения по спискам с учётом поиска."""
        keep = keep if keep is not None else {a.rel for a in self.selected()}
        q = self.search.text().strip().lower()
        shown = [a for a in self.apps if not q or q in a.name.lower() or q in a.rel.lower()]
        shown.sort(key=lambda a: a.name.casefold())
        default_icon = fic("APPLICATION").icon()

        for lst in self.lists.values():
            lst.blockSignals(True)
            lst.clear()
        self.items = {}
        for a in shown:
            lst = self.lists[start_group(a.category)]
            it = QListWidgetItem(self.icons.get(a.rel, default_icon), a.name)
            it.setData(Qt.ItemDataRole.UserRole, a.rel)
            it.setToolTip(f"{a.name}\n{a.kind}\nCategory = {a.category}\n{a.rel}")
            lst.addItem(it)
            self.items[a.rel] = it
            if a.rel in keep:
                it.setSelected(True)
        for v, lst in self.lists.items():
            lst.blockSignals(False)
            lst.viewport().update()
            self.counts[v].setText(str(lst.count()))
        self.stats.setText(f"Всего: {len(self.apps)} · показано: {len(shown)}")
        self.show_detail()

    def _on_sel(self, lst: CategoryList):
        """Выделение живёт только в одном списке — тот, откуда будем тащить."""
        if lst.selectedItems():
            for other in self.lists.values():
                if other is not lst and other.selectedItems():
                    other.blockSignals(True)
                    other.clearSelection()
                    other.blockSignals(False)
        self.show_detail()

    def _context_menu(self, lst: CategoryList, pos):
        it = lst.itemAt(pos)
        if it is None:
            return
        if not it.isSelected():
            lst.clearSelection()
            it.setSelected(True)
        rels = [i.data(Qt.ItemDataRole.UserRole) for i in lst.selectedItems()]
        menu = QMenu(self)
        sub = menu.addMenu("Переместить в…")
        for name, value, _ in START_CATS:
            if value != lst.value:
                act = sub.addAction(name)
                act.triggered.connect(lambda _=False, v=value: self._move(rels, v))
        menu.exec(lst.viewport().mapToGlobal(pos))

    def show_detail(self):
        sel = self.selected()
        if not sel:
            self.detail.setText("Перетащите приложение (или несколько выделенных) в нужную категорию.")
        elif len(sel) == 1:
            a = sel[0]
            self.detail.setText(f"Реестр: HKEY_CURRENT_USER\\{START_KEY}\\{a.rel}   |   Category = {a.category}")
        else:
            self.detail.setText(f"Выбрано приложений: {len(sel)}")

    # ── действия ──
    def _on_drop(self, rels: list, value: int):
        # откладываем: нельзя перестраивать список, пока Qt ещё завершает перетаскивание
        QTimer.singleShot(0, lambda: self._move(rels, value))

    def _move(self, rels: list, value: int):
        apps = [self.by_rel[r] for r in rels if r in self.by_rel]
        if not apps:
            return
        todo = [a for a in apps if start_group(a.category) != value]
        if not todo:
            return self.toast("info", "Уже в этой категории")
        keep = {a.rel for a in apps}
        changed = 0
        try:
            if not self._backed_up:
                backup_start(self.apps)
                self._backed_up = True
            for a in todo:
                set_start_category(a.rel, value)
                a.category = value
                changed += 1
        except Exception as ex:  # noqa: BLE001
            self.toast("error", "Ошибка", str(ex))
        else:
            notify_shell()
            self.toast("success", f"Перемещено: {changed} → «{START_LABEL[value]}»",
                       "Перезапустите Проводник, чтобы «Пуск» обновился.")
        finally:
            self.refilter(keep=keep)

    def ungroup(self):
        """Category = 0 для всех приложений (не только показанных поиском)."""
        todo = [a for a in self.apps if a.category != 0]
        if not todo:
            return self.toast("info", "Нечего разгруппировывать", "У всех приложений уже Category = 0.")
        box = MessageBox("Разгруппировать все приложения?",
                         f"Category = 0 будет записан для приложений: {len(todo)} из {len(self.apps)}. "
                         "Все они окажутся в «Другое». Перед этим сохранится копия — вернуть категории "
                         "можно кнопкой «Откатить из копии».", self.win)
        box.yesButton.setText("Разгруппировать")
        box.cancelButton.setText("Отмена")
        if not box.exec():
            return
        changed = 0
        try:
            if not self._backed_up:
                backup_start(self.apps)
                self._backed_up = True
            for a in todo:
                set_start_category(a.rel, 0)
                a.category = 0
                changed += 1
        except Exception as ex:  # noqa: BLE001
            self.toast("error", "Ошибка", str(ex))
        else:
            notify_shell()
            self.toast("success", f"Разгруппировано: {changed}",
                       "Перезапустите Проводник, чтобы «Пуск» обновился.")
        finally:
            self.refilter()

    def find_ghosts(self):
        if self._scanning or self._loading:
            return
        if not self.apps:
            return self.toast("info", "Список пуст", "Сначала нажмите «Обновить».")
        self._scanning = True
        self.btn_ghosts.setEnabled(False)
        self.stats.setText("Поиск удалённых…")
        tiles = [(a.rel, a.category) for a in self.apps]
        threading.Thread(target=self._ghost_worker, args=(tiles,), daemon=True).start()

    def _ghost_worker(self, tiles: list):
        try:
            found, err = find_ghost_tiles(tiles), ""
        except Exception as ex:  # noqa: BLE001
            found, err = [], str(ex)
        self._ghosts.emit(found, err)

    def _on_ghosts(self, found: list, err: str):
        self._scanning = False
        self.btn_ghosts.setEnabled(True)
        self.refilter()                                   # возвращает строку статистики
        if err:
            return self.toast("error", "Не удалось проверить", err)
        if not found:
            return self.toast("info", "Удалённых приложений не найдено")
        deleted = 0
        for i, (rel, reason) in enumerate(found, 1):
            app = self.by_rel.get(rel)
            if app is None:
                continue
            dlg = GhostDialog(self.win, i, len(found), app, reason)
            if dlg.exec():
                try:
                    if not self._backed_up:
                        backup_start(self.apps)
                        self._backed_up = True
                    delete_start_tile(rel)
                    deleted += 1
                except Exception as ex:  # noqa: BLE001
                    self.toast("error", f"Не удалось удалить «{app.name}»", str(ex))
            elif dlg.stopped:
                break
        if deleted:
            notify_shell()
            self.toast("success", f"Удалено записей: {deleted}")
            self.reload()
        else:
            self.toast("info", "Ничего не удалено")

    def rollback(self):
        f = latest_start_backup()
        if not f:
            return self.toast("info", "Копий пока нет", "Копия создаётся перед первым изменением.")
        try:
            when = datetime.strptime(f.stem, "%Y%m%d_%H%M%S").strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            when = f.stem
        box = MessageBox("Откатить категории?",
                         f"Категории всех приложений вернутся к состоянию на {when}.", self.win)
        box.yesButton.setText("Откатить")
        box.cancelButton.setText("Отмена")
        if not box.exec():
            return
        try:
            n = restore_start(f)
        except Exception as ex:  # noqa: BLE001
            return self.toast("error", "Ошибка", str(ex))
        notify_shell()
        self.toast("success", f"Восстановлено приложений: {n}", "Перезапустите Проводник, чтобы «Пуск» обновился.")
        self.reload()

    def restart(self):
        box = MessageBox("Перезапустить Проводник?",
                         "Окна Проводника закроются и откроются заново — после этого «Пуск» "
                         "подхватит новые категории.", self.win)
        box.yesButton.setText("Перезапустить")
        box.cancelButton.setText("Отмена")
        if box.exec():
            try:
                restart_explorer()
            except Exception as ex:  # noqa: BLE001
                return self.toast("error", "Ошибка", str(ex))
            self.toast("success", "Проводник перезапущен")


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.start_maximized = False
        self._restore_geometry()
        self.menu_page = NewMenuPage(self)
        self.start_page = StartMenuPage(self)
        self.backup_page = BackupPage(self)
        self.addSubInterface(self.menu_page, fic("ADD"), "Меню «Создать»")
        self.addSubInterface(self.start_page, fic("HOME"), "Пуск")
        self.addSubInterface(self.backup_page, fic("HISTORY"), "Резервные копии")
        self.stackedWidget.currentChanged.connect(
            lambda _: self.backup_page.refresh() if self.stackedWidget.currentWidget() is self.backup_page else None)

    def _restore_geometry(self):
        """Размер по умолчанию подгоняется под экран и центрируется; прошлые позиция/размер запоминаются."""
        scr = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = scr.availableGeometry()
        self.setMinimumSize(min(900, area.width()), min(560, area.height()))
        w, h = min(1200, int(area.width() * 0.9)), min(780, int(area.height() * 0.9))
        rect = QRect(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2, w, h)
        try:
            d = json.loads(WINDOW_FILE.read_text(encoding="utf-8"))
            saved = QRect(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))
            for s in QApplication.screens():
                vis = s.availableGeometry().intersected(saved)
                if saved.width() > 0 and vis.width() * vis.height() >= 0.6 * saved.width() * saved.height():
                    rect = saved
                    self.start_maximized = bool(d.get("max", False))
                    break
        except Exception:  # noqa: BLE001
            pass
        self.setGeometry(rect)

    def closeEvent(self, e):
        try:
            g = self.normalGeometry() if self.isMaximized() else self.geometry()
            APP_DIR.mkdir(parents=True, exist_ok=True)
            WINDOW_FILE.write_text(json.dumps({"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
                                               "max": self.isMaximized()}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(e)


def main():
    if os.name != "nt":
        print("Эта программа работает только в Windows.")
        return
    try:  # отдельный значок в панели задач (а не значок python/Проводника)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    try:
        setTheme(Theme.AUTO)
    except Exception:
        setTheme(Theme.LIGHT)
    w = MainWindow()
    w.showMaximized() if w.start_maximized else w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()