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
"""
import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QFileInfo, QSize, Qt, QTimer
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFileIconProvider,
    QHBoxLayout, QHeaderView, QListWidgetItem, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, FluentIcon as FIF, FluentWindow, InfoBar,
    InfoBarIcon, InfoBarPosition, LineEdit, ListWidget, MessageBox,
    MessageBoxBase, PrimaryPushButton, PushButton, SearchLineEdit,
    SegmentedWidget, StrongBodyLabel, SubtitleLabel, SwitchButton,
    TableWidget, Theme, TitleLabel, setTheme,
)

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


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1200, 780)
        self.menu_page = NewMenuPage(self)
        self.backup_page = BackupPage(self)
        self.addSubInterface(self.menu_page, fic("ADD"), "Меню «Создать»")
        self.addSubInterface(self.backup_page, fic("HISTORY"), "Резервные копии")
        self.stackedWidget.currentChanged.connect(
            lambda _: self.backup_page.refresh() if self.stackedWidget.currentWidget() is self.backup_page else None)


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
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()