import json
import math
import sys
import time
import tkinter as tk
from pathlib import Path


def _resource_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def _app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

from PIL import Image, ImageTk

ROOT = _app_dir()
STATE = ROOT / '.jarvis_ui_state.json'
BG = _resource_dir() / 'assets' / 'exact-user-background.jpg'

STATUS_LABELS = {
    'waiting': 'EN ATTENTE DU DOUBLE CLAQUEMENT',
    'activated': 'ACTIVÉ',
    'listening': 'ÉCOUTE EN COURS',
    'recognizing': 'RECONNAISSANCE VOCALE',
    'speaking': 'RÉPONSE VOCALE',
    'executing': 'EXÉCUTION',
    'locked': 'VOIX NON AUTORISÉE',
    'stopped': 'ARRÊTÉ',
}

class JarvisUI:
    def __init__(self):
        self.r = tk.Tk()
        self.r.title('JARVIS — Interface vocale')
        self.r.geometry('920x1180')
        self.r.minsize(760, 960)
        self.r.configure(bg='#02070b')
        self.r.attributes('-topmost', False)

        self.canvas = tk.Canvas(self.r, bg='#02070b', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        self.history = []
        self.zoom = 1.0
        self.status = 'waiting'
        self.detail = 'Frappez deux fois dans vos mains'
        self.level = 0.0
        self.target = 0.0
        self.last_t = 0.0
        self.bg_src = None
        self.bg_tk = None
        self.last_bg_size = None
        try:
            self.bg_src = Image.open(BG).convert('RGBA')
        except Exception:
            self.bg_src = None

        self.r.bind('<Configure>', self._on_resize)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self._zoom(1.1))
        self.canvas.bind('<Button-5>', lambda e: self._zoom(1 / 1.1))
        self.r.bind('<plus>', lambda e: self._zoom(1.1))
        self.r.bind('<minus>', lambda e: self._zoom(1 / 1.1))
        self.r.bind('<Key-0>', lambda e: setattr(self, 'zoom', 1.0))
        self.tick()

    def _on_resize(self, _e=None):
        self.draw(force_bg=True)

    def read_state(self):
        try:
            data = json.loads(STATE.read_text(encoding='utf-8'))
        except Exception:
            return
        t = float(data.get('time', 0.0))
        if t <= self.last_t:
            return
        self.last_t = t
        self.status = data.get('status', 'waiting')
        self.detail = data.get('detail', '') or ''
        self.target = float(data.get('level', 0.0))
        cmd = data.get('command')
        if cmd:
            self.history.insert(0, f'› {cmd}')
        if self.detail and self.status in {'locked', 'executing', 'speaking', 'recognizing'}:
            self.history.insert(1 if self.history else 0, f'  {self.detail[:120]}')
        self.history = self.history[:9]

    def _ensure_bg(self, w, h):
        if self.bg_src is None:
            self.bg_tk = None
            return
        size = (max(1, w), max(1, h))
        if self.last_bg_size == size and self.bg_tk is not None:
            return
        src_w, src_h = self.bg_src.size
        scale = max(size[0] / src_w, size[1] / src_h)
        new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
        img = self.bg_src.resize(new_size, Image.LANCZOS)
        x0 = max(0, (new_size[0] - size[0]) // 2)
        y0 = max(0, (new_size[1] - size[1]) // 2)
        img = img.crop((x0, y0, x0 + size[0], y0 + size[1]))
        overlay = Image.new('RGBA', size, (0, 8, 16, 68))
        img = Image.alpha_composite(img, overlay)
        self.bg_tk = ImageTk.PhotoImage(img)
        self.last_bg_size = size

    def draw(self, force_bg=False):
        c = self.canvas
        c.delete('all')
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        if force_bg:
            self.last_bg_size = None
        self._ensure_bg(w, h)
        if self.bg_tk is not None:
            c.create_image(w / 2, h / 2, image=self.bg_tk)
        c.create_rectangle(0, 0, w, h, fill='#02070b', stipple='gray25', outline='')

        cx = w / 2
        cy = h * 0.43
        base = min(w, h) * 0.16
        pulse = base * (1.0 + 0.28 * self.level)

        # outer glow
        for i, alpha in enumerate([0.04, 0.07, 0.11, 0.16], start=1):
            rr = pulse + 45 * i
            color = '#0ab8d8' if i < 4 else '#69f3ff'
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=color, width=2)

        # main futuristic circles
        ring_colors = ['#0b6172', '#0ea8bf', '#35def1', '#8bf9ff']
        for idx, mult in enumerate([1.70, 1.42, 1.17, 0.93]):
            rr = pulse * mult
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=ring_colors[idx], width=3)
        c.create_oval(cx - pulse * 0.58, cy - pulse * 0.58, cx + pulse * 0.58, cy + pulse * 0.58, outline='#5ef5ff', width=4)
        c.create_oval(cx - pulse * 0.22, cy - pulse * 0.22, cx + pulse * 0.22, cy + pulse * 0.22, fill='', outline='#a6fbff', width=3)

        # segmented arcs
        for offset, span, width, col in [
            (18, 106, 8, '#19d6f0'),
            (150, 64, 6, '#62f9ff'),
            (238, 92, 7, '#0d879c'),
        ]:
            rr = pulse * 0.78
            c.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=offset, extent=span, style='arc', outline=col, width=width)
        for offset, span, width, col in [
            (15, 52, 5, '#14b3c8'),
            (110, 50, 5, '#53ecff'),
            (215, 56, 5, '#27d1ea'),
            (300, 48, 5, '#0d6b80'),
        ]:
            rr = pulse * 1.10
            c.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=offset, extent=span, style='arc', outline=col, width=width)

        # radial lines / ticks
        for ang in range(0, 360, 15):
            rad = math.radians(ang)
            r1 = pulse * 0.95
            r2 = pulse * (1.35 if ang % 45 == 0 else 1.24)
            x1, y1 = cx + math.cos(rad) * r1, cy + math.sin(rad) * r1
            x2, y2 = cx + math.cos(rad) * r2, cy + math.sin(rad) * r2
            c.create_line(x1, y1, x2, y2, fill='#21d8ed', width=2 if ang % 45 == 0 else 1)

        # crosshair
        c.create_line(cx - pulse * 1.6, cy, cx + pulse * 1.6, cy, fill='#25ddee', width=1)
        c.create_line(cx, cy - pulse * 1.6, cx, cy + pulse * 1.6, fill='#25ddee', width=1)

        # floating nodes
        for ang, r, sz in [(52, 1.65, 7), (130, 1.48, 5), (220, 1.74, 6), (304, 1.56, 5)]:
            rad = math.radians(ang)
            x, y = cx + math.cos(rad) * pulse * r, cy + math.sin(rad) * pulse * r
            c.create_oval(x - sz, y - sz, x + sz, y + sz, fill='#65f7ff', outline='')

        # title + status
        c.create_text(cx, h * 0.11, text='J A R V I S', fill='#d7fdff', font=('Segoe UI', 28, 'bold'))
        c.create_text(cx, h * 0.145, text='ASSISTANT VOCAL • INTERFACE TACTIQUE', fill='#62f6ff', font=('Segoe UI', 12, 'bold'))
        status = STATUS_LABELS.get(self.status, self.status.upper())
        c.create_text(cx, cy + pulse * 1.82, text=status, fill='#66f6ff', font=('Segoe UI', 13, 'bold'))
        if self.detail:
            c.create_text(cx, cy + pulse * 1.98, text=self.detail[:90], fill='#c8fdff', font=('Segoe UI', 10))

        # small telemetry panels
        self._panel(c, 36, h * 0.72, w - 72, h * 0.21)
        c.create_text(54, h * 0.735, text='COMMANDES EXÉCUTÉES', anchor='w', fill='#7ff8ff', font=('Segoe UI', 13, 'bold'))
        c.create_line(54, h * 0.758, w - 54, h * 0.758, fill='#10798f')
        yy = h * 0.785
        for line in self.history[:8]:
            c.create_text(64, yy, text=line, anchor='w', width=w - 128, fill='#ddfdff', font=('Consolas', 11))
            yy += 28

        self._panel(c, 40, 34, 260, 105)
        c.create_text(56, 54, text='SOURCE VOCALE', anchor='w', fill='#69f3ff', font=('Segoe UI', 11, 'bold'))
        c.create_text(56, 78, text='ElevenLabs / Edge TTS / Windows', anchor='w', fill='#d7fdff', font=('Segoe UI', 10))
        c.create_text(56, 100, text='Verrou vocal local activé', anchor='w', fill='#9ddbe1', font=('Segoe UI', 9))

        self._panel(c, w - 300, 34, 260, 105)
        c.create_text(w - 284, 54, text='NIVEAU MICRO', anchor='w', fill='#69f3ff', font=('Segoe UI', 11, 'bold'))
        bar_x0, bar_y0 = w - 284, 80
        bar_w = 220
        c.create_rectangle(bar_x0, bar_y0, bar_x0 + bar_w, bar_y0 + 14, outline='#118095', width=1)
        c.create_rectangle(bar_x0 + 2, bar_y0 + 2, bar_x0 + 2 + max(1, int((bar_w - 4) * min(1.0, self.level))), bar_y0 + 12, fill='#39e6f8', outline='')
        c.create_text(w - 284, 104, text='Réponse accélérée et écoute continue', anchor='w', fill='#9ddbe1', font=('Segoe UI', 9))

        # apply interactive zoom to the whole scene
        try:
            c.scale('all', w / 2, h / 2, self.zoom, self.zoom)
        except Exception:
            pass
        c.create_text(cx, h - 24, text=f'Profil vocal local • Halo réactif au micro • Zoom {self.zoom:.2f}x (molette / + - / 0)', fill='#72b8c2', font=('Segoe UI', 10))

    def _panel(self, c, x, y, w, h):
        c.create_rectangle(x, y, x + w, y + h, outline='#0d7388', width=2)
        c.create_rectangle(x + 6, y + 6, x + w - 6, y + h - 6, outline='#083944', width=1)

    def _zoom(self, factor):
        self.zoom = max(0.5, min(4.0, self.zoom * factor))

    def _on_wheel(self, e):
        if getattr(e, 'delta', 0) > 0:
            self._zoom(1.1)
        else:
            self._zoom(1 / 1.1)

    def tick(self):
        self.read_state()
        self.level += (self.target - self.level) * 0.28
        self.target *= 0.84
        self.draw()
        self.r.after(55, self.tick)

    def run(self):
        self.r.mainloop()

if __name__ == '__main__':
    JarvisUI().run()
