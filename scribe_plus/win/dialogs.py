"""
Small modal dialogs on the Tk thread (API keys, confirmations).

The app's root window is hidden, and Windows won't bring a hidden-owner
dialog to the front, so each dialog is its own always-on-top window that
asks for focus explicitly.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def _center(win: tk.Toplevel) -> None:
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")


def _modal(win: tk.Toplevel, focus: tk.Widget) -> None:
    win.attributes("-topmost", True)
    _center(win)
    win.deiconify()
    win.lift()
    win.after(50, lambda: (win.focus_force(), focus.focus_set()))
    try:
        win.grab_set()
    except tk.TclError:
        pass
    win.wait_window()


def ask_secret(root: tk.Tk, title: str, prompt: str) -> str | None:
    win = tk.Toplevel(root)
    win.withdraw()
    win.title(title)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=prompt, justify="left", wraplength=420).pack(anchor="w")
    value = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=value, show="*", width=56)
    entry.pack(fill="x", pady=(10, 12))
    result: dict = {"value": None}

    def ok(_e=None) -> None:
        result["value"] = value.get().strip()
        win.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e")
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(buttons, text="Save", command=ok).pack(side="right", padx=(0, 8))
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda _e: win.destroy())
    _modal(win, entry)
    return result["value"]


def confirm(root: tk.Tk, title: str, message: str) -> bool:
    win = tk.Toplevel(root)
    win.withdraw()
    win.title(title)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=message, justify="left", wraplength=380).pack(anchor="w", pady=(0, 12))
    result = {"ok": False}

    def yes(_e=None) -> None:
        result["ok"] = True
        win.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e")
    no_btn = ttk.Button(buttons, text="Cancel", command=win.destroy)
    no_btn.pack(side="right")
    ttk.Button(buttons, text="Yes", command=yes).pack(side="right", padx=(0, 8))
    win.bind("<Escape>", lambda _e: win.destroy())
    _modal(win, no_btn)
    return result["ok"]
