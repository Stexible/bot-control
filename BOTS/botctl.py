#!/usr/bin/env python3
"""
botctl — псевдографический (curses) менеджер systemd-сервисов для ваших ботов.

Все юниты, которыми управляет утилита, имеют префикс "bot-" и входят
в bots.target — так они не путаются с системными и программными сервисами.

Запуск:
    sudo python3 botctl.py

Требует root (для systemctl start/stop/enable/disable системных юнитов
и для записи unit-файлов в /etc/systemd/system).
"""

import curses
import curses.textpad
import locale
import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime

PREFIX = "bot-"
TARGET_NAME = "bots.target"
UNIT_DIR = "/etc/systemd/system"
NOTES_DIR = "/etc/mybots/notes"

# ---------------------------------------------------------------------------
# Низкоуровневые обёртки над systemctl / файловой системой
# ---------------------------------------------------------------------------

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def systemctl(*args):
    return run(["systemctl"] + list(args))


def ensure_target():
    path = os.path.join(UNIT_DIR, TARGET_NAME)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("[Unit]\nDescription=All my custom bot services\n")
        systemctl("daemon-reload")


def ensure_notes_dir():
    os.makedirs(NOTES_DIR, exist_ok=True)


def list_bot_units():
    """Список всех *.service с префиксом bot- (включая неактивные/failed)."""
    result = systemctl("list-unit-files", f"{PREFIX}*.service", "--no-legend")
    names = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            names.append(parts[0])
    return sorted(set(names))


def list_all_service_units():
    """Все .service юниты в системе (для 'усыновления')."""
    result = systemctl("list-unit-files", "--type=service", "--no-legend")
    names = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            names.append(parts[0])
    return sorted(set(names))


def unit_show(name, prop):
    result = systemctl("show", name, "-p", prop, "--value")
    return result.stdout.strip()


def get_active_state(name):
    return unit_show(name, "ActiveState") or "unknown"


def get_enabled_state(name):
    result = systemctl("is-enabled", name)
    val = result.stdout.strip()
    return val if val else "unknown"


def get_description(name):
    return unit_show(name, "Description")


def short_name(unit):
    n = unit
    if n.startswith(PREFIX):
        n = n[len(PREFIX):]
    if n.endswith(".service"):
        n = n[: -len(".service")]
    return n


def note_path(unit):
    return os.path.join(NOTES_DIR, f"{short_name(unit)}.md")


def read_note(unit):
    p = note_path(unit)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return ""


def write_note(unit, text):
    ensure_notes_dir()
    with open(note_path(unit), "w", encoding="utf-8") as f:
        f.write(text)


def first_note_line(unit):
    for line in read_note(unit).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return get_description(unit) or ""


def collect_rows():
    rows = []
    for unit in list_bot_units():
        rows.append({
            "unit": unit,
            "name": short_name(unit),
            "active": get_active_state(unit),
            "enabled": get_enabled_state(unit),
            "desc": first_note_line(unit),
        })
    return rows


# ---------------------------------------------------------------------------
# curses-хелперы: цвета, модальные окна
# ---------------------------------------------------------------------------

COLOR_ACTIVE = 1
COLOR_INACTIVE = 2
COLOR_FAILED = 3
COLOR_ENABLED = 4
COLOR_HEADER = 5
COLOR_SELECTED = 6
COLOR_DIM = 7


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_ACTIVE, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_INACTIVE, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_FAILED, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_ENABLED, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(COLOR_SELECTED, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(COLOR_DIM, curses.COLOR_WHITE, -1)


def centered_win(stdscr, height, width, title=""):
    max_y, max_x = stdscr.getmaxyx()
    y = max(0, (max_y - height) // 2)
    x = max(0, (max_x - width) // 2)
    win = curses.newwin(height, width, y, x)
    win.keypad(True)
    win.box()
    if title:
        win.addstr(0, 2, f" {title} ", curses.A_BOLD)
    return win


def message_box(stdscr, text, title="Сообщение"):
    lines = []
    for para in text.split("\n"):
        lines.extend(textwrap.wrap(para, 60) or [""])
    height = len(lines) + 4
    width = max(len(title) + 6, max((len(l) for l in lines), default=20) + 4, 30)
    win = centered_win(stdscr, height, width, title)
    for i, line in enumerate(lines):
        win.addstr(1 + i, 2, line)
    win.addstr(height - 2, 2, "Нажмите любую клавишу...", curses.A_DIM)
    win.refresh()
    win.getch()


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def run_with_progress(stdscr, message, fn, *args, **kwargs):
    """Выполняет fn(*args, **kwargs) в фоновом потоке, показывая крутящийся
    спиннер и текст, чтобы было видно, что программа не зависла, а работает.
    Возвращает результат fn (или None, если fn упала с исключением — тогда
    исключение пробрасывается дальше после закрытия окна)."""
    box = {}

    def target():
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — пробросим наверх после окна
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    height, width = 5, max(len(message) + 8, 36)
    win = centered_win(stdscr, height, width, "Выполняется")
    i = 0
    while thread.is_alive():
        frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
        win.move(2, 2)
        win.clrtoeol()
        try:
            win.addstr(2, 2, f"{frame} {message}")
        except curses.error:
            pass
        win.box()
        win.refresh()
        curses.napms(90)
        i += 1
    thread.join()

    if "error" in box:
        raise box["error"]
    return box.get("result")


def wait_for_active_state(stdscr, unit, message=None, timeout=5.0):
    """Ждём, пока юнит выйдет из переходного состояния 'activating'
    (например, из-за Type=notify), показывая спиннер, максимум timeout секунд.
    Возвращает финальное ActiveState."""
    if message is None:
        message = f"Проверяю запуск {unit}..."

    def poll():
        elapsed = 0.0
        step = 0.25
        state = get_active_state(unit)
        while state == "activating" and elapsed < timeout:
            time.sleep(step)
            elapsed += step
            state = get_active_state(unit)
        return state

    return run_with_progress(stdscr, message, poll)


def confirm(stdscr, text, title="Подтвердите"):
    lines = textwrap.wrap(text, 60)
    height = len(lines) + 4
    width = max(len(title) + 6, max((len(l) for l in lines), default=20) + 4, 30)
    win = centered_win(stdscr, height, width, title)
    for i, line in enumerate(lines):
        win.addstr(1 + i, 2, line)
    win.addstr(height - 2, 2, "[y] Да    [n/Esc] Нет", curses.A_BOLD)
    win.refresh()
    while True:
        ch = win.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def _read_text_field(win, y, x, maxlen, initial=""):
    """Однострочный ввод с поддержкой Backspace/Delete/стрелок и Unicode (кириллица).

    Используем get_wch() вместо getstr()/getch() — только он корректно
    декодирует многобайтовые UTF-8 последовательности (иначе кириллица
    приходит побайтово и превращается в мусор).
    """
    chars = list(initial)
    pos = len(chars)
    curses.curs_set(1)
    while True:
        win.move(y, x)
        win.clrtoeol()
        display = "".join(chars)
        if len(display) > maxlen:
            display = display[-maxlen:]
        try:
            win.addstr(y, x, display)
        except curses.error:
            pass
        win.move(y, x + min(pos, maxlen))
        win.refresh()
        try:
            ch = win.get_wch()
        except curses.error:
            continue

        if isinstance(ch, str):
            code = ord(ch) if len(ch) == 1 else -1
            if code in (10, 13):  # Enter
                break
            elif code == 27:  # Esc
                curses.curs_set(0)
                return None
            elif code in (8, 127):  # Backspace (разные терминалы шлют по-разному)
                if pos > 0:
                    chars.pop(pos - 1)
                    pos -= 1
            elif code >= 32 or code == 0:
                if len(chars) < 500:
                    chars.insert(pos, ch)
                    pos += 1
        else:
            if ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(chars), pos + 1)
            elif ch == curses.KEY_BACKSPACE:
                if pos > 0:
                    chars.pop(pos - 1)
                    pos -= 1
            elif ch == curses.KEY_DC:  # Delete — стереть символ справа от курсора
                if pos < len(chars):
                    chars.pop(pos)
            elif ch == curses.KEY_HOME:
                pos = 0
            elif ch == curses.KEY_END:
                pos = len(chars)
    curses.curs_set(0)
    return "".join(chars)


def prompt_line(stdscr, title, initial=""):
    height, width = 5, 60
    win = centered_win(stdscr, height, width, title)
    win.addstr(2, 2, "> ")
    win.refresh()
    result = _read_text_field(win, 2, 4, width - 6, initial)
    return (result if result is not None else initial).strip()


def edit_multiline(stdscr, title, initial_text=""):
    """Многострочный редактор описания.

    Собственная реализация (без curses.textpad.Textbox), потому что
    стандартный Textbox не поддерживает клавишу Delete и ломает
    кириллицу/UTF-8 при вводе. Ctrl-G — сохранить, Esc — отменить.
    """
    max_y, max_x = stdscr.getmaxyx()
    height, width = max_y - 4, max_x - 8
    win = centered_win(stdscr, height, width, f"{title}  (Ctrl-G сохранить, Esc отмена)")
    inner_h = height - 2
    inner_w = width - 2

    lines = initial_text.splitlines() or [""]
    cy, cx = 0, 0
    top = 0
    curses.curs_set(1)

    while True:
        if cy < top:
            top = cy
        elif cy >= top + inner_h:
            top = cy - inner_h + 1

        for row in range(inner_h):
            win.move(1 + row, 1)
            win.clrtoeol()
            i = top + row
            if i < len(lines):
                try:
                    win.addstr(1 + row, 1, lines[i][:inner_w])
                except curses.error:
                    pass
        win.box()
        win.addstr(0, 2, f" {title} ", curses.A_BOLD)
        screen_y = 1 + (cy - top)
        screen_x = 1 + min(cx, inner_w - 1)
        try:
            win.move(screen_y, screen_x)
        except curses.error:
            pass
        win.refresh()

        try:
            ch = win.get_wch()
        except curses.error:
            continue

        if isinstance(ch, str):
            code = ord(ch) if len(ch) == 1 else -1
            if code == 7:  # Ctrl-G — сохранить
                curses.curs_set(0)
                return "\n".join(lines)
            elif code == 27:  # Esc — отмена
                curses.curs_set(0)
                return None
            elif code in (10, 13):  # Enter — разбить строку
                line = lines[cy]
                lines[cy] = line[:cx]
                lines.insert(cy + 1, line[cx:])
                cy += 1
                cx = 0
            elif code in (8, 127):  # Backspace
                if cx > 0:
                    line = lines[cy]
                    lines[cy] = line[: cx - 1] + line[cx:]
                    cx -= 1
                elif cy > 0:
                    prev_len = len(lines[cy - 1])
                    lines[cy - 1] += lines[cy]
                    del lines[cy]
                    cy -= 1
                    cx = prev_len
            elif code >= 32 or code == 0:
                line = lines[cy]
                lines[cy] = line[:cx] + ch + line[cx:]
                cx += 1
        else:
            if ch == curses.KEY_LEFT:
                if cx > 0:
                    cx -= 1
                elif cy > 0:
                    cy -= 1
                    cx = len(lines[cy])
            elif ch == curses.KEY_RIGHT:
                if cx < len(lines[cy]):
                    cx += 1
                elif cy < len(lines) - 1:
                    cy += 1
                    cx = 0
            elif ch == curses.KEY_UP:
                if cy > 0:
                    cy -= 1
                    cx = min(cx, len(lines[cy]))
            elif ch == curses.KEY_DOWN:
                if cy < len(lines) - 1:
                    cy += 1
                    cx = min(cx, len(lines[cy]))
            elif ch == curses.KEY_HOME:
                cx = 0
            elif ch == curses.KEY_END:
                cx = len(lines[cy])
            elif ch == curses.KEY_BACKSPACE:
                if cx > 0:
                    line = lines[cy]
                    lines[cy] = line[: cx - 1] + line[cx:]
                    cx -= 1
                elif cy > 0:
                    prev_len = len(lines[cy - 1])
                    lines[cy - 1] += lines[cy]
                    del lines[cy]
                    cy -= 1
                    cx = prev_len
            elif ch == curses.KEY_DC:  # Delete — стереть символ СПРАВА от курсора
                line = lines[cy]
                if cx < len(line):
                    lines[cy] = line[:cx] + line[cx + 1:]
                elif cy < len(lines) - 1:
                    lines[cy] += lines[cy + 1]
                    del lines[cy + 1]
            elif ch == curses.KEY_NPAGE:
                cy = min(len(lines) - 1, cy + inner_h)
                cx = min(cx, len(lines[cy]))
            elif ch == curses.KEY_PPAGE:
                cy = max(0, cy - inner_h)
                cx = min(cx, len(lines[cy]))


def select_from_list(stdscr, title, items):
    """Простой список с выбором стрелками. Возвращает индекс или None."""
    if not items:
        message_box(stdscr, "Список пуст.", title)
        return None
    max_y, max_x = stdscr.getmaxyx()
    height = min(len(items) + 4, max_y - 4)
    width = min(max((len(i) for i in items), default=20) + 6, max_x - 4)
    win = centered_win(stdscr, height, width, title)
    idx = 0
    top = 0
    visible = height - 3
    while True:
        for row in range(visible):
            i = top + row
            win.move(1 + row, 1)
            win.clrtoeol()
            win.addstr(1 + row, 1, " " * (width - 2))
            if i < len(items):
                attr = curses.color_pair(COLOR_SELECTED) if i == idx else curses.A_NORMAL
                win.addstr(1 + row, 2, items[i][: width - 4], attr)
        win.box()
        win.addstr(0, 2, f" {title} ", curses.A_BOLD)
        win.refresh()
        ch = win.getch()
        if ch in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = min(len(items) - 1, idx + 1)
        elif ch == curses.KEY_NPAGE:
            idx = min(len(items) - 1, idx + visible)
        elif ch == curses.KEY_PPAGE:
            idx = max(0, idx - visible)
        elif ch in (10, 13):
            return idx
        elif ch == 27:
            return None
        if idx < top:
            top = idx
        elif idx >= top + visible:
            top = idx - visible + 1


def show_logs(stdscr, unit):
    result = run(["journalctl", "-u", unit, "-n", "300", "--no-pager"])
    lines = result.stdout.splitlines() or ["(нет записей в журнале)"]
    max_y, max_x = stdscr.getmaxyx()
    pad = curses.newpad(max(len(lines) + 1, max_y), max_x)
    for i, line in enumerate(lines):
        try:
            pad.addstr(i, 0, line[: max_x - 1])
        except curses.error:
            pass
    top = max(0, len(lines) - (max_y - 3))
    while True:
        stdscr.addstr(max_y - 1, 0,
                       f" Логи {unit} — ↑/↓ прокрутка, q выход ".ljust(max_x - 1),
                       curses.color_pair(COLOR_HEADER))
        stdscr.refresh()
        pad.refresh(top, 0, 0, 0, max_y - 2, max_x - 1)
        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        elif ch == curses.KEY_UP:
            top = max(0, top - 1)
        elif ch == curses.KEY_DOWN:
            top = min(max(0, len(lines) - (max_y - 2)), top + 1)
        elif ch == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - (max_y - 2)), top + (max_y - 2))
        elif ch == curses.KEY_PPAGE:
            top = max(0, top - (max_y - 2))


# ---------------------------------------------------------------------------
# Действия
# ---------------------------------------------------------------------------

def action_toggle_active(stdscr, unit):
    state = get_active_state(unit)
    verb = "stop" if state == "active" else "start"
    verb_ru = "Останавливаю" if verb == "stop" else "Запускаю"
    result = run_with_progress(stdscr, f"{verb_ru} {unit}...", systemctl, verb, unit)
    if result.returncode != 0:
        message_box(stdscr, f"Ошибка:\n{result.stderr.strip()}", "Не удалось выполнить")
    elif verb == "start":
        # даём сервису шанс выйти из переходного состояния, чтобы таблица
        # сразу показала честный статус, а не мимолётный "activating"
        wait_for_active_state(stdscr, unit, f"Проверяю запуск {unit}...", timeout=3.0)


def action_toggle_enabled(stdscr, unit):
    state = get_enabled_state(unit)
    verb = "disable" if state == "enabled" else "enable"
    verb_ru = "Отключаю автозапуск" if verb == "disable" else "Включаю автозапуск"
    result = run_with_progress(stdscr, f"{verb_ru} {unit}...", systemctl, verb, unit)
    if result.returncode != 0:
        message_box(stdscr, f"Ошибка:\n{result.stderr.strip()}", "Не удалось выполнить")


def action_remove(stdscr, unit):
    if not confirm(stdscr,
                    f"Удалить сервис {unit} полностью?\n"
                    f"Будет остановлен, отключён и unit-файл удалён.\n"
                    f"(Файл заметки останется на диске.)",
                    "Удаление сервиса"):
        return

    def do_remove():
        systemctl("stop", unit)
        systemctl("disable", unit)
        path = os.path.join(UNIT_DIR, unit)
        if os.path.exists(path):
            os.remove(path)
        systemctl("daemon-reload")

    run_with_progress(stdscr, f"Удаляю {unit}...", do_remove)
    message_box(stdscr, f"{unit} удалён.", "Готово")


def action_edit_description(stdscr, unit):
    current = read_note(unit)
    if not current:
        current = f"# {short_name(unit)}\n\nОписание: \nРепозиторий/путь: \nСоздан: {datetime.now():%Y-%m-%d}\n"
    new_text = edit_multiline(stdscr, f"Описание: {unit}", current)
    if new_text is not None:
        write_note(unit, new_text)


def action_new_bot(stdscr):
    name = prompt_line(stdscr, "Короткое имя бота (без bot- и .service)")
    if not name:
        return
    unit = f"{PREFIX}{name}.service"
    if os.path.exists(os.path.join(UNIT_DIR, unit)):
        message_box(stdscr, f"{unit} уже существует.", "Ошибка")
        return
    desc = prompt_line(stdscr, "Краткое описание (Description=)")
    script = prompt_line(stdscr, "Путь к скрипту бота (ExecStart python-скрипт)")
    workdir = prompt_line(stdscr, "Рабочая директория", os.path.dirname(script) or "/")
    user = prompt_line(stdscr, "Пользователь для запуска", os.environ.get("SUDO_USER", "root"))
    python_bin = prompt_line(stdscr, "Путь к python-интерпретатору", "/usr/bin/python3")

    unit_content = (
        "[Unit]\n"
        f"Description={desc}\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory={workdir}\n"
        f"ExecStart={python_bin} {script}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        f"WantedBy=multi-user.target {TARGET_NAME}\n"
    )

    def create_unit():
        with open(os.path.join(UNIT_DIR, unit), "w") as f:
            f.write(unit_content)
        systemctl("daemon-reload")
        write_note(unit, f"# {name}\n\nОписание: {desc}\nСкрипт: {script}\nСоздан: {datetime.now():%Y-%m-%d}\n")

    run_with_progress(stdscr, f"Создаю {unit}...", create_unit)

    if confirm(stdscr, f"Юнит {unit} создан. Включить автозапуск и запустить сейчас?", "Новый бот"):
        def enable_and_start():
            systemctl("enable", unit)
            systemctl("start", unit)

        run_with_progress(stdscr, f"Запускаю {unit}...", enable_and_start)
        wait_for_active_state(stdscr, unit, timeout=3.0)
    message_box(stdscr, f"{unit} готов.", "Готово")


def action_adopt(stdscr):
    all_units = [u for u in list_all_service_units() if not u.startswith(PREFIX)]
    idx = select_from_list(stdscr, "Выберите сервис для 'усыновления'", all_units)
    if idx is None:
        return
    old_unit = all_units[idx]
    old_path = os.path.join(UNIT_DIR, old_unit)
    if not os.path.exists(old_path):
        message_box(stdscr, "Файл юнита не найден на диске (возможно, генерируется динамически).", "Ошибка")
        return

    name = prompt_line(stdscr, "Новое короткое имя (без bot-/.service)", short_name(old_unit))
    if not name:
        return
    new_unit = f"{PREFIX}{name}.service"
    new_path = os.path.join(UNIT_DIR, new_unit)
    if os.path.exists(new_path):
        message_box(stdscr, f"{new_unit} уже существует.", "Ошибка")
        return
    desc = prompt_line(stdscr, "Описание (Description=)", get_description(old_unit))

    with open(old_path) as f:
        content = f.read()

    lines = content.splitlines()
    out_lines = []
    desc_written = False
    install_seen = False
    for line in lines:
        if line.strip().startswith("Description="):
            out_lines.append(f"Description={desc}")
            desc_written = True
        elif line.strip() == "[Install]":
            install_seen = True
            out_lines.append(line)
        else:
            out_lines.append(line)
    if not desc_written:
        out_lines.insert(1, f"Description={desc}")
    if install_seen:
        out_lines.append(f"WantedBy={TARGET_NAME}")
    else:
        out_lines.append("")
        out_lines.append("[Install]")
        out_lines.append(f"WantedBy=multi-user.target {TARGET_NAME}")

    was_enabled = get_enabled_state(old_unit) == "enabled"

    def do_adopt():
        with open(new_path, "w") as f:
            f.write("\n".join(out_lines) + "\n")
        systemctl("daemon-reload")
        if was_enabled:
            systemctl("enable", new_unit)
        systemctl("stop", old_unit)
        systemctl("start", new_unit)

    run_with_progress(stdscr, f"Переношу {old_unit} в {new_unit}...", do_adopt)

    state = wait_for_active_state(stdscr, new_unit, f"Проверяю запуск {new_unit}...", timeout=5.0)
    write_note(new_unit, f"# {name}\n\nОписание: {desc}\nУсыновлён из: {old_unit}\nДата: {datetime.now():%Y-%m-%d}\n")

    if state == "active":
        if confirm(stdscr,
                   f"{new_unit} запущен успешно.\n"
                   f"Отключить и удалить старый {old_unit}?",
                   "Усыновление прошло успешно"):
            def cleanup_old():
                systemctl("disable", old_unit)
                os.remove(old_path)
                systemctl("daemon-reload")
            run_with_progress(stdscr, f"Убираю старый {old_unit}...", cleanup_old)
        message_box(stdscr, f"{old_unit} -> {new_unit} готово.", "Готово")
    else:
        message_box(stdscr,
                    f"Новый сервис не стартовал (state={state}).\n"
                    f"Старый {old_unit} НЕ тронут, можно посмотреть логи и повторить.",
                    "Внимание")


# ---------------------------------------------------------------------------
# Главный экран
# ---------------------------------------------------------------------------

def draw_table(stdscr, rows, selected, top):
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()

    header = " botctl — менеджер ботов-сервисов "
    stdscr.addstr(0, 0, header.ljust(max_x), curses.color_pair(COLOR_HEADER) | curses.A_BOLD)

    col_name = 18
    col_active = 12
    col_enabled = 12
    col_desc = max(10, max_x - col_name - col_active - col_enabled - 6)

    hdr = f"{'ИМЯ':<{col_name}} {'СТАТУС':<{col_active}} {'АВТОЗАПУСК':<{col_enabled}} ОПИСАНИЕ"
    stdscr.addstr(2, 0, hdr[:max_x - 1], curses.A_UNDERLINE | curses.A_BOLD)

    visible_rows = max_y - 6
    if not rows:
        stdscr.addstr(4, 2, "Сервисы с префиксом bot- не найдены. Нажмите [n] чтобы создать новый.")
    else:
        if selected < top:
            top = selected
        elif selected >= top + visible_rows:
            top = selected - visible_rows + 1

        for row_i in range(visible_rows):
            i = top + row_i
            if i >= len(rows):
                break
            r = rows[i]
            y = 3 + row_i

            active_attr = curses.color_pair(
                COLOR_ACTIVE if r["active"] == "active"
                else COLOR_FAILED if r["active"] == "failed"
                else COLOR_INACTIVE
            )
            enabled_attr = curses.color_pair(COLOR_ENABLED) if r["enabled"] == "enabled" else curses.A_DIM

            line_attr = curses.color_pair(COLOR_SELECTED) if i == selected else curses.A_NORMAL

            name_s = f"{r['name']:<{col_name}}"[:col_name]
            active_s = f"{r['active']:<{col_active}}"[:col_active]
            enabled_s = f"{r['enabled']:<{col_enabled}}"[:col_enabled]
            desc_s = (r["desc"] or "")[:col_desc]

            stdscr.addstr(y, 0, " " * (max_x - 1), line_attr)
            stdscr.addstr(y, 0, name_s, line_attr)
            stdscr.addstr(y, col_name + 1, active_s,
                          line_attr if i == selected else active_attr)
            stdscr.addstr(y, col_name + col_active + 2, enabled_s,
                          line_attr if i == selected else enabled_attr)
            stdscr.addstr(y, col_name + col_active + col_enabled + 3, desc_s, line_attr)

    footer1 = "[Enter/Space] Вкл/Выкл  [E] Автозапуск  [I] Описание  [L] Логи"
    footer2 = "[N] Новый бот  [A] Усыновить  [Del/X] Удалить  [R] Обновить  [Q] Выход"
    stdscr.addstr(max_y - 2, 0, footer1[:max_x - 1], curses.A_REVERSE)
    stdscr.addstr(max_y - 1, 0, footer2[:max_x - 1], curses.A_REVERSE)
    stdscr.refresh()
    return top


def main(stdscr):
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)

    if os.geteuid() != 0:
        message_box(stdscr,
                    "Утилита запущена не от root.\n"
                    "Управление systemd-юнитами и запись unit-файлов требуют прав root.\n"
                    "Перезапустите: sudo python3 botctl.py\n\n"
                    "Продолжаю в режиме только для чтения — часть действий будет недоступна.",
                    "Внимание")

    ensure_target()
    ensure_notes_dir()

    rows = collect_rows()
    selected = 0
    top = 0

    while True:
        top = draw_table(stdscr, rows, selected, top)
        ch = stdscr.getch()

        if ch in (ord("q"), ord("Q")):
            break

        elif ch in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = min(max(0, len(rows) - 1), selected + 1)

        elif ch in (ord("r"), ord("R")):
            rows = collect_rows()
            selected = min(selected, max(0, len(rows) - 1))

        elif ch in (ord("n"), ord("N")):
            action_new_bot(stdscr)
            rows = collect_rows()

        elif ch in (ord("a"), ord("A")):
            action_adopt(stdscr)
            rows = collect_rows()

        elif rows:
            unit = rows[selected]["unit"]
            if ch in (10, 13, ord(" ")):
                action_toggle_active(stdscr, unit)
                rows = collect_rows()
            elif ch in (ord("e"), ord("E")):
                action_toggle_enabled(stdscr, unit)
                rows = collect_rows()
            elif ch in (ord("i"), ord("I")):
                action_edit_description(stdscr, unit)
                rows = collect_rows()
            elif ch in (ord("l"), ord("L")):
                show_logs(stdscr, unit)
            elif ch in (curses.KEY_DC, ord("x"), ord("X")):
                action_remove(stdscr, unit)
                rows = collect_rows()
                selected = min(selected, max(0, len(rows) - 1))


def cli_report(no_color=False):
    """Headless-режим: печатает текущий статус всех bot-* сервисов в виде
    простых цветных строк, пригодных для вставки в bash-скрипты (например
    start_choice.sh при логине/SSH). Не требует root — is-active читается
    без привилегий. Список берётся динамически из systemd, поэтому при
    добавлении/переименовании/удалении бота через сам botctl вывод сразу
    отражает актуальное положение дел, без правки bash-скрипта."""
    if no_color:
        GREEN = RED = YELLOW = NC = ""
    else:
        GREEN = "\033[0;32m"
        RED = "\033[0;31m"
        YELLOW = "\033[0;33m"
        NC = "\033[0m"

    units = list_bot_units()
    if not units:
        print(f"{YELLOW}Сервисы с префиксом {PREFIX} не найдены.{NC}")
        return

    for unit in units:
        label = get_description(unit) or short_name(unit)
        state = get_active_state(unit)
        if state == "active":
            print(f"{label} ({unit}) работает: {GREEN}OK{NC}")
        elif state == "failed":
            print(f"{label} ({unit}): {RED}ОШИБКА (failed){NC}")
        elif state == "activating":
            print(f"{label} ({unit}): {YELLOW}ЗАПУСКАЕТСЯ...{NC}")
        else:
            print(f"{label} ({unit}): {RED}НЕ РАБОТАЕТ{NC}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--report", "-r", "--status"):
        cli_report(no_color="--no-color" in sys.argv)
        sys.exit(0)

    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
