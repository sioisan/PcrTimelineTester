"""选人窗口的三列虚拟列表。只绘制可见行，头像在后台读取。"""

from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import font as tkfont

from .datas import cached_icon


def _thumbnail(unit):
    path = cached_icon(unit)
    if path is None:
        return None, True
    try:
        from PIL import Image

        with Image.open(path) as source:
            return source.convert("RGBA").resize((28, 28)), False
    except (ImportError, OSError, ValueError):
        return None, False


class CharacterGrid(tk.Canvas):
    def __init__(self, parent, chosen, icons, request_icons, *, background, foreground, accent, empty):
        super().__init__(parent, width=670, height=380, background=background,
                         highlightthickness=0, takefocus=True)
        self.chosen = chosen
        self.icons = icons
        self.request_icons = request_icons
        self.foreground = foreground
        self.accent = accent
        self.empty = empty
        self.line_height = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        self.row_height = max(36, 2 * self.line_height + 4)
        self.configure(yscrollincrement=self.row_height)
        self.rows = []
        self.columns = 3
        self.cell_width = 1
        self.active = 0
        self.cells = {}
        self.loader = ThreadPoolExecutor(max_workers=2, thread_name_prefix="picker-icon")
        self.pending = {}
        self.unavailable = set()
        self.missing = set()
        self.requested = set()
        self.timer = None
        self.bind("<Configure>", self._resize)
        self.bind("<Button-1>", self._click)
        self.bind("<KeyPress>", self._key)
        self.bind("<FocusIn>", self._focus)
        self.bind("<FocusOut>", self._focus)

    def set_colors(self, background, foreground, accent, empty):
        self.configure(background=background)
        self.foreground, self.accent, self.empty = foreground, accent, empty
        self._layout()

    def set_rows(self, rows):
        self.rows = rows
        self.active = 0
        self.yview_moveto(0)
        self._layout()

    def _resize(self, event):
        self.columns = max(1, min(3, event.width // 200))
        self.cell_width = max(1, event.width // self.columns)
        self._layout()

    def _layout(self):
        self.delete("all")
        self.cells.clear()
        count = (len(self.rows) + self.columns - 1) // self.columns
        self.configure(scrollregion=(0, 0, self.columns * self.cell_width, count * self.row_height))
        self.paint()

    def paint(self):
        first = max(0, int(self.canvasy(0)) // self.row_height - 1) * self.columns
        end = (int(self.canvasy(self.winfo_height())) // self.row_height + 2) * self.columns
        visible = range(first, min(end, len(self.rows)))
        for index in self.cells.keys() - visible:
            self.delete(f"cell{index}")
            del self.cells[index]
        for index in visible:
            if index in self.cells:
                continue
            character = self.rows[index]
            unit = character.unit_id
            x = index % self.columns * self.cell_width + 4
            y = (index // self.columns + 0.5) * self.row_height
            tag = f"cell{index}"
            checked = unit in self.chosen
            box = self.create_rectangle(x, y-6, x+12, y+6, tags=tag,
                                        fill=self.empty, outline=self.accent if checked else "#aca7bf")
            if checked:
                self.create_line(x+2, y, x+5, y+3, x+10, y-4, fill=self.foreground, width=2, tags=tag)
            avatar = self.create_image(x+31, y, image=self.icons.get(unit, ""), tags=tag)
            self.create_text(x+50, y-self.line_height/2, tags=tag, anchor="w", font="TkDefaultFont",
                             text=character.label, fill=self.foreground)
            self.create_text(x+50, y+self.line_height/2, tags=tag, anchor="w", font="TkDefaultFont",
                             text=character.group_label, fill=self.accent)
            self.cells[index] = (box, avatar)
        self._focus()
        if self.timer is None:
            self.timer = self.after(16, self._icons_tick)

    def _icons_tick(self):
        self.timer = None
        for unit, task in list(self.pending.items()):
            if not task.done():
                continue
            del self.pending[unit]
            image, missing = task.result()
            if image is None:
                self.unavailable.add(unit)
                if missing:
                    self.missing.add(unit)
            elif unit not in self.icons:
                from PIL import ImageTk
                self.icons[unit] = ImageTk.PhotoImage(image, master=self)
            if unit in self.icons:
                for i, (_, avatar) in self.cells.items():
                    if self.rows[i].unit_id == unit:
                        self.itemconfigure(avatar, image=self.icons[unit])
        for i in self.cells:
            unit = self.rows[i].unit_id
            if len(self.pending) >= 6:
                break
            if unit not in self.icons and unit not in self.unavailable and unit not in self.pending:
                self.pending[unit] = self.loader.submit(_thumbnail, unit)
        if self.pending:
            self.timer = self.after(16, self._icons_tick)
        else:
            wanted = self.missing - self.requested
            if wanted and self.request_icons(wanted):
                self.requested.update(wanted)

    def icons_changed(self):
        self.unavailable.difference_update(self.missing)
        self.missing.clear()
        self.paint()

    def _focus(self, event=None):
        self.delete("cursor")
        if self.active in self.cells and str(self.tk.call("focus", "-displayof", self)) == str(self):
            box, _ = self.cells[self.active]
            x, y, _, _ = self.coords(box)
            self.create_line(x, y+15, x+24, y+15, fill=self.accent, width=2, tags="cursor")

    def _toggle(self):
        unit = self.rows[self.active].unit_id
        self.chosen.symmetric_difference_update((unit,))
        self.delete(f"cell{self.active}")
        self.cells.pop(self.active, None)
        self.paint()

    def _click(self, event):
        col = event.x // self.cell_width
        index = int(self.canvasy(event.y)) // self.row_height * self.columns + col
        if col < self.columns and 0 <= index < len(self.rows):
            self.focus_set()
            self.active = index
            self._toggle()

    def _key(self, event):
        if not self.rows:
            return
        if event.keysym == "space":
            self._toggle()
            return "break"
        delta = {"Left": -1, "Right": 1, "Up": -self.columns, "Down": self.columns}.get(event.keysym)
        if delta is None:
            return
        self.active = min(len(self.rows)-1, max(0, self.active + delta))
        top = self.active // self.columns * self.row_height
        height = self.winfo_height()
        offset = self.canvasy(0)
        if top < offset or top + self.row_height > offset + height:
            total = ((len(self.rows) + self.columns - 1) // self.columns) * self.row_height
            self.yview_moveto((top if top < offset else top + self.row_height - height) / total)
        self.paint()
        return "break"

    def destroy(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
        self.loader.shutdown(wait=False, cancel_futures=True)
        super().destroy()
