"""Squeeze - desktop UI.

Run with:  python app.py
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from squeeze import Fidelity, Result, Settings, compress_file, human, parse_size
from squeeze.core import GB, KB, MB
from squeeze.engines.media import FFMPEG
from squeeze.engines.image import HEIF_OK
from squeeze.pipeline import pick_engine

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_OK = True
except Exception:  # pragma: no cover
    DND_OK = False

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
BG = "#0e1016"
CARD = "#161923"
CARD_HI = "#1d2130"
BORDER = "#262b3b"
TEXT = "#e8eaf0"
MUTED = "#868ea3"
FAINT = "#5d6478"
ACCENT = "#6366f1"
ACCENT_HI = "#818cf8"

TIER_COLOR = {
    Fidelity.LOSSLESS: "#34d399",
    Fidelity.NEAR_LOSSLESS: "#22d3ee",
    Fidelity.LOSSY: "#fbbf24",
    Fidelity.UNCHANGED: "#8b93a7",
    Fidelity.FAILED: "#f87171",
}

BADGE = {
    "image": ("IMG", "#8b5cf6"),
    "document": ("PDF", "#ef4444"),
    "media": ("A/V", "#0ea5e9"),
    "package": ("ZIP", "#f59e0b"),
    "file": ("FILE", "#64748b"),
}

PRESETS = [("100 KB", 100 * KB), ("500 KB", 500 * KB), ("1 MB", MB),
           ("5 MB", 5 * MB), ("8 MB", 8 * MB), ("25 MB", 25 * MB)]


def short_path(path: Path, limit: int = 42) -> str:
    text = str(path)
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def reveal(path: Path) -> None:
    """Open a folder, or select the file inside it."""
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if path.is_file() else "", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception:
        pass


# --------------------------------------------------------------------------
# one row per queued file
# --------------------------------------------------------------------------

class FileRow(ctk.CTkFrame):
    def __init__(self, master, path: Path, on_remove, fonts):
        super().__init__(master, fg_color=CARD, corner_radius=10)
        self.path = path
        self.result: Result | None = None
        self._on_remove = on_remove
        self.grid_columnconfigure(1, weight=1)

        size = path.stat().st_size
        kind = pick_engine(path).kind
        text, color = BADGE.get(kind, BADGE["file"])

        self.badge = ctk.CTkLabel(self, text=text, font=fonts["badge"], text_color="#ffffff",
                                  fg_color=color, corner_radius=6, width=42, height=22)
        self.badge.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=12, sticky="n")

        self.name = ctk.CTkLabel(self, text=path.name, font=fonts["row_title"],
                                 text_color=TEXT, anchor="w")
        self.name.grid(row=0, column=1, sticky="ew", pady=(11, 0))

        self.sizes = ctk.CTkLabel(self, text=human(size), font=fonts["row_size"],
                                  text_color=MUTED, anchor="e")
        self.sizes.grid(row=0, column=2, sticky="e", padx=(8, 8), pady=(11, 0))

        self.tier = ctk.CTkLabel(self, text="queued", font=fonts["badge"],
                                 text_color=FAINT, anchor="e", width=104)
        self.tier.grid(row=0, column=3, sticky="e", padx=(0, 6), pady=(11, 0))

        self.close = ctk.CTkButton(self, text="×", width=26, height=26, corner_radius=6,
                                   font=fonts["close"], fg_color="transparent",
                                   text_color=FAINT, hover_color=CARD_HI,
                                   command=lambda: self._on_remove(self))
        self.close.grid(row=0, column=4, rowspan=2, padx=(0, 8), pady=(9, 0), sticky="n")

        self.detail = ctk.CTkLabel(self, text="waiting to start", font=fonts["row_detail"],
                                   text_color=FAINT, anchor="w", justify="left")
        self.detail.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(1, 11))

        self.bar = ctk.CTkProgressBar(self, height=3, corner_radius=2,
                                      fg_color=CARD_HI, progress_color=ACCENT)
        self.bar.set(0)

        for widget in (self, self.name, self.detail, self.sizes):
            widget.bind("<Double-Button-1>", self._open)

    # -- state transitions --------------------------------------------------

    def set_pending(self):
        self.result = None
        self.tier.configure(text="queued", text_color=FAINT)
        self.detail.configure(text="waiting to start", text_color=FAINT)
        self.sizes.configure(text=human(self.path.stat().st_size), text_color=MUTED)
        self.bar.grid_forget()

    def set_running(self):
        self.tier.configure(text="working", text_color=ACCENT_HI)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, columnspan=5, sticky="ew", padx=12, pady=(0, 10))

    def set_progress(self, fraction: float, message: str):
        self.bar.set(max(0.0, min(1.0, fraction)))
        self.detail.configure(text=message, text_color=MUTED)

    def set_result(self, result: Result):
        self.result = result
        self.bar.grid_forget()
        color = TIER_COLOR[result.fidelity]

        if result.error:
            self.tier.configure(text="failed", text_color=color)
            self.detail.configure(text=result.error, text_color=color)
            self.sizes.configure(text=human(result.original), text_color=MUTED)
            return

        percent = int(round(result.ratio * 100))
        self.sizes.configure(
            text=f"{human(result.original)}  →  {human(result.final)}   ({percent}%)",
            text_color=TEXT,
        )
        label = result.fidelity.label.lower()
        if not result.met_target and result.fidelity is not Fidelity.UNCHANGED:
            label += " • over"
        self.tier.configure(text=label, text_color=color)

        line = result.detail
        if result.metric:
            line += f"   —   {result.metric}"
        if not result.met_target:
            line = f"target not reached — {line}"
        self.detail.configure(text=line, text_color=MUTED if result.met_target else "#fbbf24")

    def _open(self, _event=None):
        target = self.result.dst if (self.result and self.result.dst) else self.path
        reveal(Path(target))


# --------------------------------------------------------------------------
# settings card helper
# --------------------------------------------------------------------------

def card(parent, title: str, fonts) -> ctk.CTkFrame:
    wrapper = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12,
                           border_width=1, border_color=BORDER)
    wrapper.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(wrapper, text=title.upper(), font=fonts["card_title"],
                 text_color=FAINT, anchor="w").grid(
        row=0, column=0, sticky="ew", padx=16, pady=(13, 8))
    return wrapper


# --------------------------------------------------------------------------

_Base = TkinterDnD.DnDWrapper if DND_OK else object


class App(ctk.CTk, _Base):
    def __init__(self):
        super().__init__()
        if DND_OK:
            self.TkdndVersion = TkinterDnD._require(self)

        ctk.set_appearance_mode("dark")
        self.title("Squeeze — compress anything to a size")
        self.geometry("1200x780")
        self.minsize(1020, 680)
        self.configure(fg_color=BG)

        self.fonts = {
            "h1": ctk.CTkFont("Segoe UI Semibold", 23),
            "sub": ctk.CTkFont("Segoe UI", 12),
            "card_title": ctk.CTkFont("Segoe UI Semibold", 11),
            "body": ctk.CTkFont("Segoe UI", 12),
            "small": ctk.CTkFont("Segoe UI", 11),
            "badge": ctk.CTkFont("Segoe UI Semibold", 11),
            "row_title": ctk.CTkFont("Segoe UI Semibold", 13),
            "row_size": ctk.CTkFont("Consolas", 12),
            "row_detail": ctk.CTkFont("Segoe UI", 11),
            "close": ctk.CTkFont("Segoe UI", 17),
            "drop": ctk.CTkFont("Segoe UI", 13),
            "button": ctk.CTkFont("Segoe UI Semibold", 14),
        }

        self.rows: list[FileRow] = []
        self.output_dir: Path | None = None
        self.running = False
        self._stop = False
        self._last_tick = 0.0
        self._events: queue.Queue = queue.Queue()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_body()
        self._build_statusbar()

        if DND_OK:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._pump)

    # -- layout -------------------------------------------------------------

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 10))
        header.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(left, text="Squeeze", font=self.fonts["h1"],
                     text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Hits your target size losslessly when it can, and tells you exactly what it cost when it can't.",
            font=self.fonts["sub"], text_color=MUTED, anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        chips = ctk.CTkFrame(header, fg_color="transparent")
        chips.grid(row=0, column=2, sticky="e")
        for label, ok in (("images", True), ("pdf", True),
                          ("video/audio", bool(FFMPEG)), ("heic", HEIF_OK)):
            ctk.CTkLabel(
                chips, text=f"  {'●' if ok else '○'} {label}  ",
                font=self.fonts["badge"], height=24, corner_radius=12,
                fg_color=CARD, text_color="#34d399" if ok else FAINT,
            ).pack(side="left", padx=3)

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 6))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        # ---- drop zone
        self.drop = ctk.CTkFrame(body, fg_color=CARD, corner_radius=12,
                                 border_width=2, border_color=BORDER, height=92)
        self.drop.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.drop.grid_propagate(False)
        self.drop.grid_columnconfigure(0, weight=1)
        hint = ("Drop files or folders here" if DND_OK else "Click to add files")
        self.drop_label = ctk.CTkLabel(
            self.drop, text=f"{hint}   ·   or click to browse",
            font=self.fonts["drop"], text_color=MUTED,
        )
        self.drop_label.grid(row=0, column=0, pady=(24, 2))
        ctk.CTkLabel(self.drop, text="images · pdf · video · audio · office · anything else",
                     font=self.fonts["small"], text_color=FAINT).grid(row=1, column=0, pady=(0, 22))
        for widget in (self.drop, self.drop_label):
            widget.bind("<Button-1>", lambda _e: self._browse_files())
            widget.bind("<Enter>", lambda _e: self.drop.configure(border_color=ACCENT))
            widget.bind("<Leave>", lambda _e: self.drop.configure(border_color=BORDER))
        if DND_OK:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        # ---- file list
        self.list = ctk.CTkScrollableFrame(body, fg_color="transparent",
                                           scrollbar_button_color=BORDER)
        self.list.grid(row=1, column=0, sticky="nsew")
        self.list.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(self.list, text="No files queued yet.",
                                  font=self.fonts["body"], text_color=FAINT)
        self.empty.grid(row=0, column=0, pady=40)

        # ---- settings column: cards scroll, action buttons stay pinned so
        # Compress is reachable at any window size.
        side = ctk.CTkFrame(body, fg_color="transparent", width=356)
        side.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(16, 0))
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1, minsize=356)
        side.grid_rowconfigure(0, weight=1)

        cards = ctk.CTkScrollableFrame(side, fg_color="transparent", width=336,
                                       scrollbar_button_color=BORDER)
        cards.grid(row=0, column=0, sticky="nsew")
        cards.grid_columnconfigure(0, weight=1)

        self._build_target_card(cards)
        self._build_advanced_card(cards)
        self._build_output_card(cards)
        self._build_actions(side)

    def _build_target_card(self, parent):
        box = card(parent, "Target size", self.fonts)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.mode = ctk.CTkSegmentedButton(
            box, values=["Exact size", "% of original"], command=self._on_mode,
            font=self.fonts["small"], height=30, corner_radius=8,
            selected_color=ACCENT, selected_hover_color=ACCENT_HI,
            unselected_color=CARD_HI, unselected_hover_color=BORDER,
            fg_color=CARD_HI, text_color=TEXT,
        )
        self.mode.set("Exact size")
        self.mode.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.exact = ctk.CTkFrame(box, fg_color="transparent")
        self.exact.grid(row=2, column=0, sticky="ew", padx=16)
        self.exact.grid_columnconfigure(0, weight=1)
        self.size_entry = ctk.CTkEntry(
            self.exact, font=ctk.CTkFont("Consolas", 15), height=38, corner_radius=8,
            fg_color=CARD_HI, border_color=BORDER, text_color=TEXT, justify="center",
        )
        self.size_entry.insert(0, "2")
        self.size_entry.grid(row=0, column=0, sticky="ew")
        self.unit = ctk.CTkOptionMenu(
            self.exact, values=["KB", "MB", "GB"], width=80, height=38, corner_radius=8,
            font=self.fonts["body"], fg_color=CARD_HI, button_color=CARD_HI,
            button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=CARD_HI,
        )
        self.unit.set("MB")
        self.unit.grid(row=0, column=1, padx=(8, 0))

        chips = ctk.CTkFrame(box, fg_color="transparent")
        chips.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 0))
        for index, (label, value) in enumerate(PRESETS):
            chips.grid_columnconfigure(index % 3, weight=1)
            ctk.CTkButton(
                chips, text=label, height=26, corner_radius=6, font=self.fonts["small"],
                fg_color=CARD_HI, hover_color=BORDER, text_color=MUTED,
                command=lambda v=value: self._apply_preset(v),
            ).grid(row=index // 3, column=index % 3, sticky="ew", padx=2, pady=2)

        self.percent_box = ctk.CTkFrame(box, fg_color="transparent")
        self.percent_box.grid_columnconfigure(0, weight=1)
        self.percent_label = ctk.CTkLabel(self.percent_box, text="50% of each file",
                                          font=self.fonts["body"], text_color=TEXT)
        self.percent_label.grid(row=0, column=0, pady=(2, 6))
        self.percent = ctk.CTkSlider(self.percent_box, from_=5, to=95, number_of_steps=18,
                                     command=self._on_percent, button_color=ACCENT,
                                     button_hover_color=ACCENT_HI, progress_color=ACCENT,
                                     fg_color=CARD_HI)
        self.percent.set(50)
        self.percent.grid(row=1, column=0, sticky="ew")

        ctk.CTkFrame(box, fg_color="transparent", height=6).grid(row=4, column=0)

    def _build_advanced_card(self, parent):
        box = card(parent, "How hard to push", self.fonts)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self.downscale = self._switch(box, 1, "Allow resizing images & video", True)
        self.strip = self._switch(box, 2, "Strip metadata (EXIF, XMP)", True)
        self.raster = self._switch(box, 3, "Let PDFs be rasterised", False)
        ctk.CTkLabel(box, text="last resort — makes PDF text unselectable",
                     font=self.fonts["small"], text_color=FAINT, anchor="w").grid(
            row=4, column=0, sticky="ew", padx=(56, 16), pady=(0, 8))

        floor = ctk.CTkFrame(box, fg_color="transparent")
        floor.grid(row=5, column=0, sticky="ew", padx=16, pady=(2, 4))
        floor.grid_columnconfigure(0, weight=1)
        self.floor_label = ctk.CTkLabel(floor, text="Quality floor  40",
                                        font=self.fonts["small"], text_color=MUTED, anchor="w")
        self.floor_label.grid(row=0, column=0, sticky="w")
        self.floor = ctk.CTkSlider(floor, from_=20, to=90, number_of_steps=14,
                                   command=self._on_floor, button_color=ACCENT,
                                   button_hover_color=ACCENT_HI, progress_color=ACCENT,
                                   fg_color=CARD_HI)
        self.floor.set(40)
        self.floor.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        codec = ctk.CTkFrame(box, fg_color="transparent")
        codec.grid(row=6, column=0, sticky="ew", padx=16, pady=(10, 14))
        codec.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(codec, text="Video", font=self.fonts["small"],
                     text_color=MUTED).grid(row=0, column=0, sticky="w")
        self.codec = ctk.CTkOptionMenu(
            codec, values=["H.264 (fast)", "H.265 (smaller)"], height=28, corner_radius=6,
            font=self.fonts["small"], fg_color=CARD_HI, button_color=CARD_HI,
            button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=CARD_HI,
        )
        self.codec.grid(row=0, column=1, sticky="e")

    def _switch(self, parent, row, text, default):
        variable = ctk.BooleanVar(value=default)
        ctk.CTkSwitch(parent, text=text, variable=variable, font=self.fonts["body"],
                      text_color=TEXT, progress_color=ACCENT, button_color="#cbd5e1",
                      fg_color=BORDER, width=36, height=18, switch_width=36,
                      switch_height=18).grid(row=row, column=0, sticky="w",
                                             padx=16, pady=5)
        return variable

    def _build_output_card(self, parent):
        box = card(parent, "Output", self.fonts)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        self.out_label = ctk.CTkLabel(box, text="Beside each original file",
                                      font=self.fonts["small"], text_color=MUTED,
                                      anchor="w", justify="left")
        self.out_label.grid(row=1, column=0, sticky="ew", padx=16)

        buttons = ctk.CTkFrame(box, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        buttons.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(buttons, text="Choose folder", height=28, corner_radius=6,
                      font=self.fonts["small"], fg_color=CARD_HI, hover_color=BORDER,
                      text_color=TEXT, command=self._choose_output).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(buttons, text="Beside original", height=28, corner_radius=6,
                      font=self.fonts["small"], fg_color=CARD_HI, hover_color=BORDER,
                      text_color=TEXT, command=self._reset_output).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

    def _build_actions(self, parent):
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        actions.grid_columnconfigure(0, weight=1)
        self.go = ctk.CTkButton(actions, text="Compress", height=46, corner_radius=10,
                                font=self.fonts["button"], fg_color=ACCENT,
                                hover_color=ACCENT_HI, command=self._start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.clear = ctk.CTkButton(actions, text="Clear list", height=30, corner_radius=8,
                                   font=self.fonts["small"], fg_color="transparent",
                                   hover_color=CARD, text_color=FAINT, command=self._clear)
        self.clear.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=34)
        bar.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 14))
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="Ready.", font=self.fonts["small"],
                                   text_color=MUTED, anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.total = ctk.CTkProgressBar(bar, height=4, corner_radius=2, width=260,
                                        fg_color=CARD, progress_color=ACCENT)
        self.total.set(0)
        self.total.grid(row=0, column=1, sticky="e")

    # -- file intake --------------------------------------------------------

    def _browse_files(self):
        paths = filedialog.askopenfilenames(title="Add files to compress")
        self._add(paths)

    def _on_drop(self, event):
        self._add(self.tk.splitlist(event.data))

    def _add(self, paths):
        known = {row.path for row in self.rows}
        added = 0
        for raw in paths:
            path = Path(str(raw).strip("{}")).expanduser()
            candidates = (
                sorted(p for p in path.rglob("*") if p.is_file())[:400]
                if path.is_dir() else [path]
            )
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                    if resolved in known or not resolved.is_file() or resolved.stat().st_size == 0:
                        continue
                except OSError:
                    continue
                known.add(resolved)
                row = FileRow(self.list, resolved, self._remove, self.fonts)
                row.grid(row=len(self.rows), column=0, sticky="ew", pady=(0, 8))
                self.rows.append(row)
                added += 1
        self._refresh_empty()
        if added:
            self.status.configure(text=f"Added {added} file{'s' if added > 1 else ''}.")

    def _remove(self, row: FileRow):
        if self.running:
            return
        row.destroy()
        self.rows.remove(row)
        for index, item in enumerate(self.rows):
            item.grid(row=index, column=0, sticky="ew", pady=(0, 8))
        self._refresh_empty()

    def _clear(self):
        if self.running:
            return
        for row in self.rows:
            row.destroy()
        self.rows.clear()
        self.total.set(0)
        self.status.configure(text="Ready.")
        self._refresh_empty()

    def _refresh_empty(self):
        if self.rows:
            self.empty.grid_forget()
        else:
            self.empty.grid(row=0, column=0, pady=40)

    # -- settings widgets ---------------------------------------------------

    def _on_mode(self, value):
        if value == "Exact size":
            self.percent_box.grid_forget()
            self.exact.grid(row=2, column=0, sticky="ew", padx=16)
        else:
            self.exact.grid_forget()
            self.percent_box.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))

    def _on_percent(self, value):
        self.percent_label.configure(text=f"{int(value)}% of each file")

    def _on_floor(self, value):
        self.floor_label.configure(text=f"Quality floor  {int(value)}")

    def _apply_preset(self, value: int):
        self.mode.set("Exact size")
        self._on_mode("Exact size")
        unit, divisor = ("MB", MB) if value >= MB else ("KB", KB)
        number = value / divisor
        self.size_entry.delete(0, "end")
        self.size_entry.insert(0, f"{number:g}")
        self.unit.set(unit)

    def _choose_output(self):
        folder = filedialog.askdirectory(title="Where should results go?")
        if folder:
            self.output_dir = Path(folder)
            self.out_label.configure(text=short_path(self.output_dir))

    def _reset_output(self):
        self.output_dir = None
        self.out_label.configure(text="Beside each original file")

    def _settings(self) -> Settings | None:
        if self.mode.get() == "Exact size":
            raw = self.size_entry.get().strip()
            value = parse_size(raw) if re.search(r"[a-zA-Z]", raw) else parse_size(raw + self.unit.get())
            if not value:
                messagebox.showwarning("Squeeze", "That target size doesn't look like a number.")
                return None
            if value > 64 * GB:
                messagebox.showwarning("Squeeze", "That target is implausibly large.")
                return None
            mode, target, percent = "absolute", value, 50.0
        else:
            mode, target, percent = "percent", 0, float(self.percent.get())

        return Settings(
            mode=mode, target_bytes=target, percent=percent,
            output_dir=self.output_dir,
            allow_downscale=bool(self.downscale.get()),
            quality_floor=int(self.floor.get()),
            comfort_quality=max(int(self.floor.get()), 78),
            strip_metadata=bool(self.strip.get()),
            allow_rasterize_pdf=bool(self.raster.get()),
            video_codec="h265" if self.codec.get().startswith("H.265") else "h264",
        )

    # -- the run ------------------------------------------------------------

    def _start(self):
        if self.running:
            self._stop = True
            self.go.configure(text="Stopping…", state="disabled")
            return
        if not self.rows:
            self._browse_files()
            return
        settings = self._settings()
        if settings is None:
            return

        for row in self.rows:
            row.set_pending()
        self.running = True
        self._stop = False
        self.go.configure(text="Stop after this file", fg_color="#7f1d1d", hover_color="#991b1b")
        self.clear.configure(state="disabled")
        self.total.set(0)
        threading.Thread(target=self._worker, args=(list(self.rows), settings),
                         daemon=True).start()

    def _worker(self, rows: list[FileRow], settings: Settings):
        results: list[Result] = []
        for index, row in enumerate(rows):
            if self._stop:
                break
            self._ui(row.set_running)
            self._ui(self.status.configure,
                     text=f"[{index + 1}/{len(rows)}]  {row.path.name}")

            def report(fraction, message, row=row, index=index):
                now = time.monotonic()
                if now - self._last_tick < 0.05 and fraction < 1.0:
                    return
                self._last_tick = now
                self._ui(row.set_progress, fraction, message)
                self._ui(self.total.set, (index + fraction) / len(rows))

            try:
                result = compress_file(row.path, settings, report)
            except Exception as exc:  # belt and braces; pipeline catches its own
                result = Result(row.path, None, row.path.stat().st_size, 0, 0,
                                Fidelity.FAILED, error=str(exc))
            results.append(result)
            self._ui(row.set_result, result)
            self._ui(self.total.set, (index + 1) / len(rows))

        self._ui(self._finish, results)

    def _finish(self, results: list[Result]):
        self.running = False
        self._stop = False
        self.go.configure(text="Compress", state="normal",
                          fg_color=ACCENT, hover_color=ACCENT_HI)
        self.clear.configure(state="normal")

        done = [r for r in results if not r.error]
        if not done:
            self.status.configure(text="Nothing finished." if results else "Cancelled.")
            return
        before = sum(r.original for r in done)
        after = sum(r.final for r in done)
        missed = sum(1 for r in done if not r.met_target)
        failed = len(results) - len(done)
        saved = (1 - after / before) * 100 if before else 0

        parts = [f"{len(done)} file{'s' if len(done) != 1 else ''}",
                 f"{human(before)} → {human(after)}",
                 f"{saved:.0f}% smaller"]
        if missed:
            parts.append(f"{missed} could not reach the target")
        if failed:
            parts.append(f"{failed} failed")
        self.status.configure(text="   ·   ".join(parts))
        self.total.set(1)

    def _ui(self, function, *args, **kwargs):
        """Hand a UI call to the main thread.

        Tcl is not thread-safe and `after()` called from a worker thread is
        silently dropped, so everything goes through a queue that the main
        thread drains in `_pump`.
        """
        self._events.put((function, args, kwargs))

    def _pump(self):
        while True:
            try:
                function, args, kwargs = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                function(*args, **kwargs)
            except Exception:
                traceback.print_exc()
        self.after(40, self._pump)

    def _on_close(self):
        self._stop = True
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
