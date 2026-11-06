# -*- coding: utf-8 -*-
"""Window for fishbot: start/pause button, global hotkey Ctrl+Shift+Y, live sound level, log.

Run with pythonw (double-click the .pyw) or as forever-fishing.exe, so no console appears.
Settings typed in the window are saved to fishbot.json next to this file (or next to the exe).
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes
from tkinter import messagebox, ttk

import fishbot as fb

# In the PyInstaller exe __file__ lives in a temp unpack dir, so settings go next to the exe instead
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "fishbot.json")
SAVED = ("cast_key", "loot_key", "min_db", "cast_min", "cast_max", "react_min", "react_max", "background")
RANGES = {"cast_pause": ("cast_min", "cast_max"), "react_pause": ("react_min", "react_max")}  # field -> args

HOTKEY_NAME = "Ctrl+Shift+Y"
HOTKEY_MODS, HOTKEY_VK = 0x0002 | 0x0004 | 0x4000, 0x59  # MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, Y
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
TOGGLE = object()

user32 = fb.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

GREY, GREEN = "#7a7a7a", "#2e8b57"


class GlobalHotkey(threading.Thread):
    """RegisterHotKey delivers WM_HOTKEY to the registering thread, so it gets its own message loop."""

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.tid = None
        self.error = None
        self.ready = threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, HOTKEY_MODS, HOTKEY_VK):
            self.error = ctypes.get_last_error()
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


def parse_range(text):
    """'1-2.5' -> (1.0, 2.5); a single number means a fixed pause."""
    parts = text.replace(",", ".").replace("–", "-").split("-")
    if len(parts) not in (1, 2):
        raise ValueError(f"не понял диапазон {text!r}")
    lo, hi = float(parts[0]), float(parts[-1])
    if not 0 <= lo <= hi:
        raise ValueError(f"диапазон {text!r}: нужно 0 <= от <= до")
    return lo, hi


def beep(freq):
    try:
        winsound.Beep(freq, 80)
    except RuntimeError:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()  # bot and hotkey threads talk to Tk only through this
        self.args = fb.build_parser().parse_args([])
        self.args.background = True  # the window works with WoW inactive by default; console keeps it opt-in
        try:
            with open(CONFIG, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if k in SAVED:
                        setattr(self.args, k, v)
        except (OSError, ValueError):
            pass

        root.title("Рыбалка")
        root.resizable(False, False)
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both")
        self.status = tk.Label(frm, font=("Segoe UI", 15, "bold"), fg="white", pady=4)
        self.status.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.btn = ttk.Button(frm, command=self.toggle)
        self.btn.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0), ipady=4)
        ttk.Label(frm, text=f"Горячая клавиша: {HOTKEY_NAME}", foreground="#666").grid(
            row=2, column=0, columnspan=4, pady=(2, 10))

        self.meter = tk.Canvas(frm, width=240, height=14, bg="#2b2b2b", highlightthickness=0)
        self.meter.grid(row=3, column=0, columnspan=3, sticky="w")
        self.meter_lbl = ttk.Label(frm, width=8, anchor="e")
        self.meter_lbl.grid(row=3, column=3, sticky="e")

        self.vars = {}
        fields = (("cast_key", "Заброс"), ("loot_key", "Подсечка"), ("cast_pause", "До заброса, с"),
                  ("react_pause", "До рывка, с"), ("min_db", "Порог, dB"))
        for i, (key, label) in enumerate(fields):
            r, c = 4 + i // 2, (i % 2) * 2
            if key in RANGES:
                lo, hi = (getattr(self.args, name) for name in RANGES[key])
                text = f"{lo:g}-{hi:g}"
            else:
                text = str(getattr(self.args, key))
            ttk.Label(frm, text=label).grid(row=r, column=c, sticky="w", pady=(8 if r == 4 else 2, 2))
            var = tk.StringVar(value=text)
            ttk.Entry(frm, textvariable=var, width=8).grid(row=r, column=c + 1, sticky="w", padx=(4, 10),
                                                            pady=(8 if r == 4 else 2, 2))
            self.vars[key] = var

        self.bg_var = tk.BooleanVar(value=bool(self.args.background))
        ttk.Checkbutton(frm, text="Работать с неактивным окном WoW", variable=self.bg_var,
                        command=self.apply_background).grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.counts = ttk.Label(frm)
        self.counts.grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 2))
        self.logbox = tk.Text(frm, height=9, width=40, state="disabled", font=("Consolas", 9),
                              relief="flat", bg="#f4f4f4")
        self.logbox.grid(row=9, column=0, columnspan=4, sticky="ew")

        self.audio = fb.Loopback()
        self.log(f"звук: {self.audio.name}")
        self.kb = fb.Keyboard(None, bool(self.args.background))
        self.bot = fb.Bot(self.args, self.audio, self.kb, log=self.events.put)
        self.bot.paused = True
        threading.Thread(target=self.bot.run, daemon=True).start()

        self.hotkey = GlobalHotkey(lambda: self.events.put(TOGGLE))
        self.hotkey.start()
        self.hotkey.ready.wait(2)
        if self.hotkey.error:
            self.log(f"{HOTKEY_NAME} занята другой программой (ошибка {self.hotkey.error}), работает только кнопка")

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.show_state()
        self.tick()

    def log(self, msg):
        self.logbox.config(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        if int(self.logbox.index("end-1c").split(".")[0]) > 300:
            self.logbox.delete("1.0", "2.0")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def apply_settings(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        try:
            cast_vk, loot_vk = fb.vk_of(v["cast_key"]), fb.vk_of(v["loot_key"])
            min_db = float(v["min_db"].replace(",", "."))
            ranges = {key: parse_range(v[key]) for key in RANGES}
        except ValueError as e:
            self.log(f"ошибка в настройках: {e}")
            return False
        a = self.args
        a.cast_key, a.loot_key, a.cast_vk, a.loot_vk = v["cast_key"], v["loot_key"], cast_vk, loot_vk
        a.min_db = min_db
        for key, names in RANGES.items():
            for name, value in zip(names, ranges[key]):
                setattr(a, name, value)
        a.background = self.kb.background = self.bg_var.get()
        self.save_config()
        return True

    def apply_background(self):
        self.args.background = self.kb.background = self.bg_var.get()
        self.save_config()
        self.log("клавиши идут в окно WoW, даже неактивное" if self.args.background
                 else "клавиши идут только в активное окно WoW")

    def save_config(self):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({k: getattr(self.args, k) for k in SAVED}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def toggle(self):
        if self.bot.paused:
            if not self.apply_settings():
                return
            beep(880)  # before unpausing: the bot is deaf for 2 s after the cast anyway
            self.bot.paused = False
            self.log("> старт")
        else:
            self.bot.paused = True
            beep(440)
            self.log("|| пауза")
        self.show_state()

    def show_state(self):
        if self.bot.paused:
            self.status.config(text="ПАУЗА", bg=GREY)
            self.btn.config(text="▶  Старт")
        else:
            self.status.config(text="РЫБАЧУ", bg=GREEN)
            self.btn.config(text="❚❚  Пауза")

    def draw_meter(self):
        db = self.audio.current()
        w, h = int(self.meter["width"]), int(self.meter["height"])
        x = lambda d: int((max(-80.0, min(0.0, d)) + 80) / 80 * w)
        self.meter.delete("all")
        self.meter.create_rectangle(0, 0, x(db), h, width=0, fill="#e8743b" if db >= self.args.min_db else "#5aa469")
        tx = x(self.args.min_db)
        self.meter.create_line(tx, 0, tx, h, fill="white", width=2)
        self.meter_lbl.config(text="тишина" if db <= fb.SILENCE_DB else f"{db:.0f} dB")

    def tick(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev is TOGGLE:
                self.toggle()
            else:
                self.log(ev)
        self.draw_meter()
        self.counts.config(text=f"Забросов: {self.bot.casts}    Поклёвок: {self.bot.bites}")
        self.root.after(100, self.tick)

    def close(self):
        self.bot.quit = True
        self.hotkey.stop()
        self.audio.close()
        self.root.destroy()


def main():
    kernel32.CreateMutexW(None, False, "fishbot_gui_single_instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        messagebox.showinfo("Рыбалка", "Уже запущено.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        App(root)
    except (Exception, SystemExit) as e:
        root.withdraw()
        messagebox.showerror("Рыбалка", f"Не удалось запуститься:\n{e}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
