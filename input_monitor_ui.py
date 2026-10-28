#!/usr/bin/env python3
"""Tkinter control panel for input_monitor.py.

Start/stop the hook, watch the log live, change settings while stopped, and
manage the logon autostart entry — all from one window.

    pythonw input_monitor_ui.py
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import input_monitor as kl

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

BG = "#0e1116"
SURFACE = "#161b22"
SURFACE2 = "#1e252e"
BORDER = "#2a333e"
FG = "#e6eaf0"
MUTED = "#8b95a3"
ACCENT = "#4c8dff"
ACCENT_HOVER = "#6ba1ff"
OK = "#3fb950"
DANGER = "#f85149"
WARN = "#d29922"
PURPLE = "#bc8cff"
LOGBG = "#0a0d12"
LOGFG = "#d5dbe4"

HINT = "Ayarlar yalnızca kayıt durdurulmuşken değiştirilebilir."
MAX_LOG_LINES = 3000


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class Toggle(tk.Frame):
    """Checkbox drawn by hand: clam's native indicator renders as a cross."""

    def __init__(self, parent, text: str, variable: tk.BooleanVar,
                 command=None, bg: str = SURFACE, fg: str = FG,
                 font=None, wraplength: int = 0):
        super().__init__(parent, bg=bg)
        self.var = variable
        self.command = command
        self.enabled = True
        self._fg = fg

        self.box = tk.Canvas(self, width=18, height=18, bg=bg, highlightthickness=0,
                             bd=0, cursor="hand2")
        self.box.pack(side="left", pady=1)
        self.label = tk.Label(self, text=text, bg=bg, fg=fg, font=font, anchor="w",
                              justify="left", cursor="hand2", wraplength=wraplength)
        self.label.pack(side="left", padx=(9, 0))

        for widget in (self, self.box, self.label):
            widget.bind("<Button-1>", self._click)
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _click(self, _event=None) -> None:
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self) -> None:
        self.box.delete("all")
        on = bool(self.var.get())
        if not self.enabled:
            fill, outline, mark = SURFACE2, BORDER, "#4d5866"
        elif on:
            fill, outline, mark = ACCENT, ACCENT, "#08111f"
        else:
            fill, outline, mark = "#11161d", "#3d4854", None
        self.box.create_rectangle(2, 2, 16, 16, fill=fill, outline=outline, width=1)
        if on:
            self.box.create_line(5, 9, 8, 12, 13, 5, fill=mark, width=2,
                                 capstyle="round", joinstyle="round")

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        cursor = "hand2" if enabled else "arrow"
        self.label.configure(fg=self._fg if enabled else "#5b6472", cursor=cursor)
        self.box.configure(cursor=cursor)
        self._draw()

    def state(self, spec) -> None:  # ttk-compatible API so callers stay uniform
        self.set_enabled("disabled" not in spec)


# ---------------------------------------------------------------------------
# Log tail
# ---------------------------------------------------------------------------

class Tail:
    """Incremental reader that survives truncation and multi-byte boundaries."""

    def __init__(self, path: Path, initial_bytes: int = 200_000):
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.initial_bytes = initial_bytes

    def reset(self) -> None:
        self.offset = 0
        self.pending = b""

    def rebind(self, path: Path) -> None:
        self.path = Path(path)
        self.reset()

    def load(self) -> str:
        """Read the existing tail of the file (called once after rebinding)."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if not size:
            return ""
        start = max(0, size - self.initial_bytes)
        with self.path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
        self.offset = start + len(data)
        text = data.decode("utf-8", "replace")
        if start:
            text = "(... önceki kayıtlar kırpıldı ...)\n" + text.split("\n", 1)[-1]
        return text

    def read_new(self) -> str:
        if not self.path.exists():
            return ""
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self.offset:  # truncated / rewritten
            self.reset()
        if size == self.offset:
            return ""
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        self.offset += len(data)
        if not data:
            return ""
        return self._decode(self.pending + data)

    def _decode(self, buf: bytes) -> str:
        try:
            self.pending = b""
            return buf.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A character may be split across reads; hold the incomplete tail.
            if exc.start >= len(buf) - 3:
                self.pending = buf[exc.start:]
                return buf[: exc.start].decode("utf-8", "replace")
            self.pending = b""
            return buf.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class Panel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.engine: kl.Engine | None = None
        self.tail = Tail(Path(kl.build_parser().get_default("log")))
        self._autostart_guard = False
        self._mutex = None

        self.title("InputMonitor · Kontrol Paneli")
        self.configure(bg=BG)
        self.minsize(920, 600)
        self.option_add("*tearOff", False)
        self._center(1080, 710)

        self._init_fonts()
        self._init_style()
        self._init_vars()
        self._build()
        self._restore_log()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F5>", lambda _e: self._toggle())
        self.bind("<Control-l>", lambda _e: self._clear())
        self.after(300, lambda: self.entry_path.xview_moveto(1.0))
        self.after(150, self._tick)

    # -- construction ------------------------------------------------------
    def _center(self, width: int, height: int) -> None:
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _init_fonts(self) -> None:
        family = "Segoe UI"
        for name, size, weight in (
            ("TkDefaultFont", 10, "normal"),
            ("TkTextFont", 10, "normal"),
            ("TkMenuFont", 10, "normal"),
            ("TkHeadingFont", 10, "bold"),
            ("TkTooltipFont", 9, "normal"),
        ):
            try:
                font = tkfont.nametofont(name)
            except tk.TclError:
                continue
            font.configure(family=family, size=size, weight=weight)
        self.font_title = tkfont.Font(family=family, size=16, weight="bold")
        self.font_h2 = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=9)
        self.font_mono = tkfont.Font(family="Consolas", size=9)
        self.font_mono_bold = tkfont.Font(family="Consolas", size=9, weight="bold")

    def _init_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, bordercolor=BORDER,
                        focuscolor=BORDER, troughcolor=SURFACE2)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("Row.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=SURFACE, foreground=FG)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED,
                        font=self.font_small)
        style.configure("Status.TLabel", background=BG, foreground=MUTED,
                        font=self.font_small)

        style.configure("TButton", background=SURFACE2, foreground=FG,
                        bordercolor=BORDER, lightcolor=SURFACE2, darkcolor=SURFACE2,
                        padding=(14, 8), relief="flat")
        style.map("TButton",
                  background=[("active", "#2a3442"), ("disabled", SURFACE)],
                  foreground=[("disabled", "#5b6472")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#08111f",
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        font=(self.font_h2.actual("family"), 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("disabled", "#26303d")],
                  foreground=[("disabled", "#626c7a")])
        style.configure("Danger.TButton", background="#2a1d20", foreground=DANGER,
                        bordercolor="#3d2529", lightcolor="#2a1d20", darkcolor="#2a1d20")
        style.map("Danger.TButton", background=[("active", "#3a2429"), ("disabled", SURFACE)],
                  foreground=[("disabled", "#5b6472")])

        style.configure("TSpinbox", fieldbackground=SURFACE2, background=SURFACE2,
                        foreground=FG, arrowcolor=FG, bordercolor=BORDER,
                        lightcolor=SURFACE2, darkcolor=SURFACE2, padding=4)
        style.configure("TEntry", fieldbackground=SURFACE2, foreground=FG,
                        insertcolor=FG, bordercolor=BORDER, lightcolor=SURFACE2,
                        darkcolor=SURFACE2, padding=6)
        style.map("TEntry", fieldbackground=[("disabled", "#12171d")],
                  foreground=[("disabled", "#5b6472")])
        style.configure("TSeparator", background=BORDER)
        style.configure("Vertical.TScrollbar", background=SURFACE2,
                        troughcolor=LOGBG, bordercolor=LOGBG, arrowcolor=MUTED,
                        lightcolor=SURFACE2, darkcolor=SURFACE2)

    def _init_vars(self) -> None:
        default_log = str(Path(kl.build_parser().get_default("log")))
        self.var_path = tk.StringVar(value=default_log)
        self.var_clip = tk.BooleanVar(value=False)
        self.var_headers = tk.BooleanVar(value=True)
        self.var_injected = tk.BooleanVar(value=False)
        self.var_exclude = tk.BooleanVar(value=True)
        self.var_idle = tk.IntVar(value=120)
        self.var_scroll = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=bool(kl.autostart_get()))

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._set_running(False)

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Card.TFrame", padding=(20, 16))
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)

        ttk.Label(header, text="InputMonitor", style="Card.TLabel",
                  font=self.font_title).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Yerel tuş kaydı · düşük seviye klavye kancası",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.pill = tk.Label(header, text="●  Durdu", bg=SURFACE2, fg=MUTED,
                             font=(self.font_h2.actual("family"), 10, "bold"),
                             padx=14, pady=6)
        self.pill.grid(row=0, column=1, rowspan=2, sticky="e")

        stats = ttk.Frame(header, style="Card.TFrame")
        stats.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.lbl_elapsed = ttk.Label(stats, text="00:00:00", style="Card.TLabel",
                                     font=(self.font_mono_bold.actual("family"), 11, "bold"))
        self.lbl_elapsed.pack(side="left")
        ttk.Label(stats, text="   •   ", style="Muted.TLabel").pack(side="left")
        self.lbl_keys = ttk.Label(stats, text="0 tuş", style="Muted.TLabel")
        self.lbl_keys.pack(side="left")
        self.lbl_size = ttk.Label(stats, text="0 B", style="Muted.TLabel")
        self.lbl_size.pack(side="right")

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))

        self.btn_start = ttk.Button(bar, text="▶  Başlat", style="Accent.TButton",
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Durdur", style="Danger.TButton",
                                   command=self._stop)
        self.btn_stop.pack(side="left", padx=(8, 0))

        ttk.Button(bar, text="Temizle", command=self._clear).pack(side="left", padx=(24, 0))
        ttk.Button(bar, text="Klasörü aç", command=self._open_folder).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Dışa aktar", command=self._export).pack(side="left", padx=(8, 0))

        self.chk_autostart = Toggle(
            bar, "Oturum açılışında başlat", self.var_autostart,
            command=self._toggle_autostart, bg=SURFACE)
        self.chk_autostart.pack(side="right", padx=(0, 4))

    def _build_body(self) -> None:
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 8))
        body.grid_columnconfigure(0, weight=0, minsize=384)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_settings(body)
        self._build_logview(body)

    def _build_settings(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(18, 16))
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        card.grid_columnconfigure(0, weight=1)

        ttk.Label(card, text="Ayarlar", style="Card.TLabel",
                  font=self.font_h2).grid(row=0, column=0, sticky="w")

        ttk.Label(card, text="Kayıt dosyası", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(14, 4))

        path_row = ttk.Frame(card, style="Row.TFrame")
        path_row.grid(row=2, column=0, sticky="ew")
        path_row.grid_columnconfigure(0, weight=1)
        self.entry_path = ttk.Entry(path_row, textvariable=self.var_path)
        self.entry_path.grid(row=0, column=0, sticky="ew")
        self.btn_browse = ttk.Button(path_row, text="Gözat", width=6, command=self._browse)
        self.btn_browse.grid(row=0, column=1, padx=(6, 0))

        self.chk_clip = Toggle(card, "Pano değişikliklerini de kaydet", self.var_clip)
        self.chk_clip.grid(row=3, column=0, sticky="w", pady=(16, 0))
        self.chk_headers = Toggle(card, "Pencere ve uygulama başlıklarını yaz",
                                  self.var_headers)
        self.chk_headers.grid(row=4, column=0, sticky="w", pady=(7, 0))
        self.chk_injected = Toggle(card, "Enjekte edilen tuşları da kaydet",
                                   self.var_injected)
        self.chk_injected.grid(row=5, column=0, sticky="w", pady=(7, 0))
        self.chk_exclude = Toggle(card, "Kendi penceremdeki tuşları yoksay",
                                  self.var_exclude)
        self.chk_exclude.grid(row=6, column=0, sticky="w", pady=(7, 0))

        idle_row = ttk.Frame(card, style="Row.TFrame")
        idle_row.grid(row=7, column=0, sticky="w", pady=(16, 0))
        ttk.Label(idle_row, text="Boşta ayırıcı", style="Card.TLabel").pack(side="left")
        self.spin_idle = ttk.Spinbox(idle_row, from_=5, to=3600, increment=5,
                                     width=6, textvariable=self.var_idle)
        self.spin_idle.pack(side="left", padx=(8, 6))
        ttk.Label(idle_row, text="saniye", style="Muted.TLabel").pack(side="left")

        ttk.Separator(card, orient="horizontal").grid(row=8, column=0, sticky="ew", pady=18)

        self.lbl_hint = ttk.Label(card, text=HINT, style="Muted.TLabel",
                                  wraplength=330, justify="left")
        self.lbl_hint.grid(row=9, column=0, sticky="w")

        ttk.Label(card, text="Kanca, oturum açıldığında arka planda\n"
                             "kendiliğinden başlar. Yönetim için bu paneli\n"
                             "elle açmanız gerekir.",
                  style="Muted.TLabel", justify="left").grid(row=10, column=0,
                                                             sticky="sw", pady=(18, 0))

        card.grid_rowconfigure(11, weight=1)

        self._settings_widgets = [
            self.entry_path, self.btn_browse, self.chk_clip, self.chk_headers,
            self.chk_injected, self.chk_exclude, self.spin_idle,
        ]

    def _build_logview(self, parent) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        card.grid(row=0, column=1, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        head = ttk.Frame(card, style="Row.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.grid_columnconfigure(1, weight=1)

        ttk.Label(head, text="Canlı kayıt", style="Card.TLabel",
                  font=self.font_h2).grid(row=0, column=0, sticky="w")
        self.lbl_path = ttk.Label(head, text="", style="Muted.TLabel", anchor="e")
        self.lbl_path.grid(row=0, column=1, sticky="e", padx=(12, 14))
        self.chk_scroll = Toggle(head, "Otomatik kaydır", self.var_scroll,
                                 command=self._scroll_to_end)
        self.chk_scroll.grid(row=0, column=2, sticky="e")

        wrap = ttk.Frame(card, style="Row.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)

        self.text = tk.Text(wrap, bg=LOGBG, fg=LOGFG, insertbackground=LOGFG,
                            relief="flat", bd=0, highlightthickness=0,
                            padx=10, pady=8, wrap="word", font=self.font_mono,
                            spacing1=1, spacing3=1, state="disabled",
                            selectbackground="#2f4d7a")
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview,
                               style="Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)

        self.text.tag_configure("sep", foreground=ACCENT, font=self.font_mono_bold)
        self.text.tag_configure("hdr", foreground=MUTED)
        self.text.tag_configure("marker", foreground=WARN)
        self.text.tag_configure("clip", foreground=PURPLE)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_status = ttk.Label(bar, text="Hazır.", style="Status.TLabel")
        self.lbl_status.grid(row=0, column=0, sticky="w")
        self.lbl_meta = ttk.Label(bar, text="", style="Status.TLabel")
        self.lbl_meta.grid(row=0, column=1, sticky="e")

    # -- log rendering -----------------------------------------------------
    def _append(self, chunk: str) -> None:
        if not chunk:
            return
        self.text.configure(state="normal")
        first = int(self.text.index("end-1c").split(".")[0])
        self.text.insert("end", chunk)
        last = int(self.text.index("end-1c").split(".")[0])
        self.text.configure(state="disabled")
        for line in range(max(1, first), last + 1):
            self._style_line(line)
        self._trim()
        if self.var_scroll.get():
            self.text.see("end")

    def _style_line(self, line: int) -> None:
        start, end = f"{line}.0", f"{line}.end"
        for tag in ("sep", "hdr", "marker", "clip"):
            self.text.tag_remove(tag, start, end)
        content = self.text.get(start, end)
        if not content:
            return
        if content.startswith("====="):
            self.text.tag_add("sep", start, end)
        elif " [CLIPBOARD] " in content:
            self.text.tag_add("clip", start, end)
        elif re.match(r"^\[\d{2}:\d{2}:\d{2}\]", content):
            self.text.tag_add("hdr", start, end)
        for match in re.finditer(r"<[A-Z0-9+*/.\-]+>|\^[A-Z\\\]\^_]", content):
            self.text.tag_add("marker", f"{line}.{match.start()}", f"{line}.{match.end()}")

    def _trim(self) -> None:
        total = int(self.text.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.text.configure(state="normal")
            self.text.delete("1.0", f"{total - MAX_LOG_LINES}.0")
            self.text.configure(state="disabled")

    def _restore_log(self) -> None:
        text = self.tail.load()
        if text:
            self._append(text)
            self._status(f"Mevcut kayıt yüklendi ({self.tail.path.name}).")

    def _scroll_to_end(self) -> None:
        if self.var_scroll.get():
            self.text.see("end")

    def _refresh_path_label(self) -> None:
        path = self.var_path.get()
        try:
            shown = _human(Path(path).stat().st_size)
        except OSError:
            shown = "yok"
        display = path if len(path) <= 58 else "…" + path[-57:]
        self.lbl_path.configure(text=f"{display}  ·  {shown}")
        self.lbl_size.configure(text=shown)

    # -- actions -----------------------------------------------------------
    def _start(self) -> None:
        if self.engine and self.engine.running:
            return
        raw = self.var_path.get().strip().strip('"') or "input-monitor.log"
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_dir():
            messagebox.showerror("Geçersiz yol", "Kayıt yolu bir klasör olamaz.")
            return
        if not path.suffix:
            path = path.with_suffix(".txt")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Geçersiz yol", f"Klasör oluşturulamadı:\n{exc}")
            return
        self.var_path.set(str(path))

        # Seek to the end of the existing file before the engine writes its
        # session header, so that header still shows up in the live view.
        self.tail.rebind(path)
        self.tail.load()

        try:
            idle = max(1, int(self.var_idle.get()))
        except (tk.TclError, ValueError):
            idle = 120
            self.var_idle.set(idle)

        if kl.instance_running(kl.MUTEX_CLI):
            proceed = messagebox.askyesno(
                "Başka bir oturum açık",
                "Arka planda çalışan bir kayıt oturumu var (otomatik başlatma).\n"
                "İkisi de aynı dosyaya yazarsa tuşlar mükerrer görünür.\n\n"
                "Yine de başlatılsın mı?",
            )
            if not proceed:
                self._status("Başlatma iptal edildi.")
                return

        try:
            engine = kl.Engine(
                path,
                clipboard=self.var_clip.get(),
                include_injected=self.var_injected.get(),
                window_titles=self.var_headers.get(),
                idle=idle,
                exclude_self=self.var_exclude.get(),
            )
            engine.start()
        except Exception as exc:
            messagebox.showerror("Başlatılamadı", f"Klavye kancası kurulamadı:\n\n{exc}")
            self._status("Başlatma başarısız.")
            return

        self.engine = engine
        self._mutex = kl.acquire_mutex(kl.MUTEX_CLI)
        self._refresh_path_label()
        self.entry_path.xview_moveto(1.0)  # show the file name, not the drive prefix
        self._set_running(True)
        self._status("Kayıt başladı.")
        self.text.see("end")

    def _stop(self) -> None:
        if not (self.engine and self.engine.running):
            return
        self.engine.stop("panel: durduruldu")
        kl.release_mutex(self._mutex)
        self._mutex = None
        self._set_running(False)
        self._status("Kayıt durduruldu.")
        self._tick_once()

    def _toggle(self) -> None:
        if self.engine and self.engine.running:
            self._stop()
        else:
            self._start()

    def _clear(self) -> None:
        if not messagebox.askyesno("Temizle", "Kayıt dosyasındaki tüm içerik silinsin mi?"):
            return
        try:
            if self.engine and self.engine.running:
                self.engine.clear()
            else:
                kl.truncate_file(Path(self.var_path.get()))
        except OSError as exc:
            messagebox.showerror("Hata", f"Dosya temizlenemedi:\n{exc}")
            return
        self.tail.reset()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._status("Kayıt dosyası temizlendi.")
        self._refresh_path_label()

    def _open_folder(self) -> None:
        folder = Path(self.var_path.get()).parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # noqa: S606 (deliberate shell open)
        except OSError as exc:
            messagebox.showerror("Hata", f"Klasör açılamadı:\n{exc}")

    def _export(self) -> None:
        source = Path(self.var_path.get())
        if not source.exists():
            messagebox.showinfo("Dışa aktar", "Henüz kayıt dosyası yok.")
            return
        target = filedialog.asksaveasfilename(
            title="Kaydı farklı kaydet",
            initialdir=str(source.parent),
            initialfile=f"input-monitor-{datetime.now():%Y%m%d-%H%M}.txt",
            defaultextension=".txt",
            filetypes=[("Metin", "*.txt"), ("Tüm dosyalar", "*.*")],
        )
        if not target:
            return
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            messagebox.showerror("Hata", f"Kopyalanamadı:\n{exc}")
            return
        self._status(f"Dışa aktarıldı: {target}")

    def _browse(self) -> None:
        current = Path(self.var_path.get())
        target = filedialog.asksaveasfilename(
            title="Kayıt dosyası seç",
            initialdir=str(current.parent if current.parent.exists() else Path.home()),
            initialfile=current.name or "input-monitor.log",
            defaultextension=".txt",
            filetypes=[("Metin", "*.txt"), ("Tüm dosyalar", "*.*")],
        )
        if not target:
            return
        self.var_path.set(target)
        self.tail.rebind(Path(target))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._restore_log()
        self._refresh_path_label()
        self.entry_path.xview_moveto(1.0)

    def _toggle_autostart(self) -> None:
        if self._autostart_guard:
            return
        self._autostart_guard = True
        try:
            try:
                if self.var_autostart.get():
                    command = kl.install_autostart(kl.DEFAULT_VALUE_NAME, self.var_path.get())
                    self._status("Otomatik başlatma eklendi.")
                    self.lbl_hint.configure(text=f"Oturum açılışında çalışacak komut:\n{command}",
                                            wraplength=330)
                else:
                    kl.uninstall_autostart(kl.DEFAULT_VALUE_NAME)
                    self._status("Otomatik başlatma kaldırıldı.")
                    self.lbl_hint.configure(text=HINT)
            except OSError as exc:
                messagebox.showerror("Kayıt defteri",
                                     f"Otomatik başlatma değiştirilemedi:\n{exc}")
            self.var_autostart.set(bool(kl.autostart_get()))
        finally:
            self._autostart_guard = False

    # -- state -------------------------------------------------------------
    def _set_running(self, running: bool) -> None:
        state = ["disabled"] if running else ["!disabled"]
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        for widget in self._settings_widgets:
            widget.state(state)
        self.lbl_hint.configure(text=HINT)

    def _status(self, message: str) -> None:
        self.lbl_status.configure(text=message)

    def _tick(self) -> None:
        try:
            self._tick_once()
        finally:
            self.after(400, self._tick)

    def _tick_once(self) -> None:
        self._append(self.tail.read_new())

        running = bool(self.engine and self.engine.running)
        if running:
            self.lbl_elapsed.configure(text=_hms(self.engine.elapsed))
            self.lbl_keys.configure(text=f"{self.engine.key_count:,} tuş".replace(",", "."))
            self.pill.configure(text="●  Kayıt ediyor", fg=OK)
            self.lbl_meta.configure(
                text=f"pid {os.getpid()}  ·  kanca aktif  ·  F5 başlat/durdur")
        else:
            self.pill.configure(text="●  Durdu", fg=MUTED)
            self.lbl_meta.configure(
                text=f"pid {os.getpid()}  ·  kanca pasif  ·  F5 başlat/durdur")
            if self.engine is not None:
                self.lbl_elapsed.configure(text=_hms(self.engine.elapsed))
                self.lbl_keys.configure(
                    text=f"{self.engine.key_count:,} tuş".replace(",", "."))

        self._refresh_path_label()

    # -- lifecycle ---------------------------------------------------------
    def _on_close(self) -> None:
        if self.engine and self.engine.running:
            if not messagebox.askyesno("Kayıt sürüyor",
                                       "Kayıt devam ediyor. Durdurup çıkılsın mı?"):
                return
            self.engine.stop("panel: kapatıldı")
        kl.release_mutex(self._mutex)
        self._mutex = None
        self.destroy()


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def main() -> int:
    enable_dpi_awareness()
    if not kl.single_instance(kl.MUTEX_UI):
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("InputMonitor", "Kontrol paneli zaten açık.")
        root.destroy()
        return 0
    app = Panel()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
