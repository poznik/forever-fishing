# -*- coding: utf-8 -*-
"""Fishing helper: listens to system audio and presses keys in WoW on a bobber splash.

  python fishbot.py --calibrate     live level meter: cast by hand, watch splash level
  python fishbot.py                 run: cast key U, loot key Y
  python fishbot.py --cast-key 1 --loot-key f --cast-min 1 --cast-max 2.5 --min-db -40

Hotkeys (global): F10 pause/resume, F12 quit.
"""
import argparse
import ctypes
import queue
import random
import sys
import time
from collections import deque
from ctypes import wintypes

import numpy as np
import pyaudiowpatch as pyaudio

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]

VK_PAUSE, VK_QUIT = 0x79, 0x7B  # F10, F12
SILENCE_DB = -100.0

# ---------------------------------------------------------------- keyboard

VK_NAMES = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
            "shift": 0x10, "ctrl": 0x11, "alt": 0x12}
for _i in range(10):
    VK_NAMES[f"num{_i}"] = 0x60 + _i


def vk_of(name):
    n = name.lower()
    if n in VK_NAMES:
        return VK_NAMES[n]
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 12:
        return 0x6F + int(n[1:])
    if len(n) == 1 and ("a" <= n <= "z" or "0" <= n <= "9"):
        return ord(n.upper())  # VK codes match ASCII; VkKeyScan fails for Latin keys under a Russian layout
    if len(n) == 1:
        r = user32.VkKeyScanW(ord(n))
        if r != -1:
            return r & 0xFF
    raise ValueError(f"неизвестная клавиша {name!r}")


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def find_window(title):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


class Keyboard:
    """Foreground mode: SendInput with scan codes (WoW must be the active window).
    Background mode: PostMessage straight to the WoW window."""

    def __init__(self, hwnd, background):
        self.hwnd = hwnd
        self.background = background

    def press(self, vk):
        """Returns an error text if the key could not be delivered, else None."""
        # Shift/Ctrl/Alt still held after a start hotkey would turn U into Ctrl+Shift+U
        while any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12)):
            time.sleep(0.02)
        scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        if self.background:
            # WoW renders at a capped FPS when inactive, so keep the key down for a few of its frames
            if not user32.PostMessageW(self.hwnd, 0x0100, vk, 1 | (scan << 16)):  # WM_KEYDOWN
                err = ctypes.get_last_error()
                hint = " - WoW запущен от администратора, запусти и утилиту так же" if err == 5 else ""
                return f"клавиша не дошла до WoW (ошибка {err}){hint}"
            time.sleep(random.uniform(0.1, 0.18))
            user32.PostMessageW(self.hwnd, 0x0101, vk, 1 | (scan << 16) | (3 << 30))  # WM_KEYUP
        else:
            if not self._send(scan, 0x0008):  # KEYEVENTF_SCANCODE
                return f"SendInput не сработал (ошибка {ctypes.get_last_error()})"
            time.sleep(random.uniform(0.04, 0.09))
            self._send(scan, 0x0008 | 0x0002)  # | KEYEVENTF_KEYUP
        return None

    @staticmethod
    def _send(scan, flags):
        inp = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags)))
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def target_ready(self):
        return self.background or user32.GetForegroundWindow() == self.hwnd


class Hotkeys:
    def __init__(self):
        self.prev = {}

    def pressed(self, vk):
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        was = self.prev.get(vk, False)
        self.prev[vk] = down
        return down and not was

# ---------------------------------------------------------------- audio


class Loopback:
    """Captures what the default output device plays (WASAPI loopback) as a stream of dBFS levels.
    WASAPI sends no packets while nothing plays, so a missing block counts as silence."""

    def __init__(self):
        self.pa = pyaudio.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev.get("isLoopbackDevice"):
            for lb in self.pa.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
            else:
                raise SystemExit("Не нашёл loopback-устройство для вывода звука по умолчанию")
        self.name = dev["name"]
        rate = int(dev["defaultSampleRate"])
        self.levels = queue.Queue()
        self.last = (0.0, SILENCE_DB)  # (time, dB) of the latest block, for a level meter
        self.stream = self.pa.open(format=pyaudio.paInt16, channels=dev["maxInputChannels"], rate=rate,
                                   input=True, input_device_index=dev["index"],
                                   frames_per_buffer=rate // 50, stream_callback=self._cb)

    def _cb(self, data, frames, time_info, status):
        a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        db = 20 * np.log10(rms) if rms > 1e-5 else SILENCE_DB
        self.levels.put(db)
        self.last = (time.monotonic(), db)
        return None, pyaudio.paContinue

    def current(self):
        t, db = self.last
        return db if time.monotonic() - t < 0.2 else SILENCE_DB

    def next(self, timeout=0.05):
        try:
            return self.levels.get(timeout=timeout)
        except queue.Empty:
            return SILENCE_DB

    def drain(self):
        while not self.levels.empty():
            self.levels.get_nowait()

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

# ---------------------------------------------------------------- modes


def calibrate(audio, hot):
    print("Калибровка. Закинь удочку руками и дождись поклёвки.\n"
          "Каждый громкий звук выводится отдельной строкой. F12 или Ctrl+C - выход.\n")
    hist = deque(maxlen=150)  # background only: loud blocks are kept out
    shown_at, block_max = 0.0, SILENCE_DB
    ev_start = ev_max = quiet_since = None
    while not hot.pressed(VK_QUIT):
        lvl = audio.next()
        now = time.monotonic()
        floor = float(np.median(hist)) if hist else lvl
        if len(hist) >= 20 and lvl > floor + 8:
            if ev_start is None:
                ev_start, ev_max = now, lvl
            ev_max, quiet_since = max(ev_max, lvl), None
        else:
            hist.append(lvl)
            if ev_start is not None:
                quiet_since = quiet_since or now
                if now - quiet_since > 0.3:
                    print(f"\r  звук: пик {ev_max:6.1f} dB, {quiet_since - ev_start:.1f} с, фон {floor:6.1f} dB      ",
                          flush=True)
                    ev_start = None
        block_max = max(block_max, lvl)
        if now - shown_at >= 0.1:
            filled = int(max(0, min(20, (block_max + 80) / 4)))
            print(f"\r  {block_max:6.1f} dB  фон {floor:6.1f}  |{'#' * filled:<20}|", end="", flush=True)
            shown_at, block_max = now, SILENCE_DB
    print()


def print_log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


class Bot:
    """Fishing loop. Console mode polls F10/F12 itself; with hot=None the caller flips paused/quit."""

    def __init__(self, args, audio, kb, hot=None, log=print_log):
        self.a, self.audio, self.kb, self.hot, self.log = args, audio, kb, hot, log
        self.paused = False
        self.quit = False
        self.casts = self.bites = 0

    def poll(self):
        if self.hot is None:
            return
        if self.hot.pressed(VK_QUIT):
            self.quit = True
        if self.hot.pressed(VK_PAUSE):
            self.paused = not self.paused
            self.log("|| пауза (F10 - продолжить)" if self.paused else "> продолжаю")

    def sleep(self, sec):
        """Sleeps while watching hotkeys; False if interrupted by quit or pause."""
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.poll()
            if self.quit or self.paused:
                return False
            time.sleep(0.02)
        return True

    def wait_ready(self):
        """Blocks while paused or while WoW is missing / not the active window (foreground mode)."""
        warned = None
        while not self.quit:
            self.poll()
            if self.paused:
                warned = None
            else:
                if not user32.IsWindow(self.kb.hwnd):  # first run or the game was restarted
                    self.kb.hwnd = find_window(self.a.window)
                if self.kb.hwnd and self.kb.target_ready():
                    return True
                why = "окно WoW не активно" if self.kb.hwnd else "WoW не запущен"
                if why != warned:
                    self.log(why + " - жду")
                    warned = why
            time.sleep(0.05)
        return False

    def wait_bite(self, timeout):
        """Returns the splash level, None on timeout, False if interrupted."""
        self.audio.drain()
        hist = deque(maxlen=150)  # ~3 s of background level
        hits = 0
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.poll()
            if self.quit or self.paused:
                return False
            lvl = self.audio.next()
            if len(hist) >= 20 and lvl > max(float(np.median(hist)) + self.a.rise, self.a.min_db):
                hits += 1
                if hits >= 2:
                    return lvl
            else:
                hits = 0
                hist.append(lvl)
        return None

    def run(self):
        a = self.a
        while not self.quit:
            if not self.wait_ready():
                break
            self.press(a.cast_vk)
            self.casts += 1
            self.log(f"заброс #{self.casts}")
            if not self.sleep(a.grace):
                continue
            res = self.wait_bite(a.timeout - a.grace)
            if res is False:
                continue
            if res is None:
                self.log("поклёвки нет - перезабрасываю")
            else:
                self.bites += 1
                self.log(f"поклёвка! ({res:.1f} dB)")
                if not self.sleep(random.uniform(a.react_min, a.react_max)) or not self.wait_ready():
                    continue
                self.press(a.loot_vk)
            self.sleep(random.uniform(a.cast_min, a.cast_max))  # a cast right after start/resume skips this
        self.log(f"выход. Забросов: {self.casts}, поклёвок: {self.bites}")

    def press(self, vk):
        err = self.kb.press(vk)
        if err:
            self.log(err)


def build_parser():
    p = argparse.ArgumentParser(description="Рыбалка в WoW по звуку поклёвки")
    p.add_argument("--calibrate", action="store_true", help="показывать уровень звука, ничего не нажимать")
    p.add_argument("--cast-key", default="u", help="клавиша заброса (по умолчанию U)")
    p.add_argument("--loot-key", default="y", help="клавиша подсечки / взаимодействия (по умолчанию Y)")
    p.add_argument("--cast-min", type=float, default=1.0, help="мин. пауза перед забросом, с")
    p.add_argument("--cast-max", type=float, default=2.5, help="макс. пауза перед забросом, с")
    p.add_argument("--react-min", type=float, default=0.5, help="мин. пауза перед рывком после поклёвки, с")
    p.add_argument("--react-max", type=float, default=1.5, help="макс. пауза перед рывком после поклёвки, с")
    p.add_argument("--min-db", type=float, default=-40.0, help="поклёвка не тише этого уровня, dBFS")
    p.add_argument("--rise", type=float, default=10.0, help="поклёвка громче фона минимум на столько dB")
    p.add_argument("--grace", type=float, default=2.0, help="сколько секунд после заброса не слушать")
    p.add_argument("--timeout", type=float, default=22.0, help="перезабросить, если нет поклёвки столько секунд")
    p.add_argument("--background", action="store_true",
                   help="слать клавиши в окно WoW, даже если оно не активно "
                        "(в игре включи «Звук в фоновом режиме»)")
    p.add_argument("--window", default="World of Warcraft", help="заголовок окна игры")
    return p


def main():
    p = build_parser()
    a = p.parse_args()
    try:
        a.cast_vk, a.loot_vk = vk_of(a.cast_key), vk_of(a.loot_key)
    except ValueError as e:
        p.error(str(e))

    audio = Loopback()
    print(f"Слушаю: {audio.name}")
    hot = Hotkeys()
    try:
        if a.calibrate:
            calibrate(audio, hot)
            return
        hwnd = find_window(a.window)
        if not hwnd:
            raise SystemExit(f"Окно {a.window!r} не найдено - запусти игру")
        print(f"Заброс: {a.cast_key}   подсечка: {a.loot_key}   порог: >{a.min_db:.0f} dB и фон+{a.rise:.0f} dB")
        print(f"Пауза до рывка {a.react_min:g}-{a.react_max:g} с, до заброса {a.cast_min:g}-{a.cast_max:g} с")
        print("F10 - пауза, F12 - выход." + ("" if a.background else " Переключись в окно WoW."), flush=True)
        Bot(a, audio, Keyboard(hwnd, a.background), hot).run()
    except KeyboardInterrupt:
        print("\nПрервано")
    finally:
        audio.close()


if __name__ == "__main__":
    main()
