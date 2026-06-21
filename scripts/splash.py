"""Splash screen do tradutor desktop.

Janela tkinter sem bordas, centralizada, mostrada enquanto o servidor sobe
(carrega Whisper na GPU). Polling em /health via tk.after() — quando o
servidor responde, fecha splash e dispara callback (abrir browser).
"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from urllib.error import URLError
from urllib.request import urlopen


# Cores (mesma paleta do display.html — tema do app)
COLOR_BG = "#0f1115"
COLOR_PANEL = "#161a22"
COLOR_FG = "#f5f5f5"
COLOR_DIM = "#8a92a3"
COLOR_PT = "#5fcf80"
COLOR_ES = "#ffb455"


class Splash:
    """Splash window com polling em /health.

    Use:
        splash = Splash(icon_path="tradutor_icon_preview.png",
                        health_url="http://127.0.0.1:8765/health",
                        timeout_s=240,
                        on_ready=lambda: open_browser())
        splash.run()   # bloqueia ate fechar
    """

    def __init__(self, icon_path=None, health_url=None,
                 timeout_s=240, on_ready=None, on_timeout=None):
        self.icon_path = icon_path
        self.health_url = health_url
        self.timeout_s = timeout_s
        self.on_ready_cb = on_ready
        self.on_timeout_cb = on_timeout

        self.root = tk.Tk()
        self.root.title("Tradutor PT-ES")
        # Sem barra de titulo / borda
        self.root.overrideredirect(True)
        self.root.configure(bg=COLOR_BG)
        self.root.attributes("-topmost", True)

        # Tamanho + centralizacao
        w, h = 460, 290
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Frame interno com padding e borda sutil
        frame = tk.Frame(self.root, bg=COLOR_PANEL,
                         highlightbackground="#262c38", highlightthickness=1)
        frame.place(x=1, y=1, width=w - 2, height=h - 2)

        # Icone
        self._tk_img = None
        if icon_path and Path(icon_path).exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(icon_path).resize((96, 96), Image.LANCZOS)
                self._tk_img = ImageTk.PhotoImage(img)
                tk.Label(frame, image=self._tk_img, bg=COLOR_PANEL).pack(
                    pady=(28, 10)
                )
            except Exception:
                pass
        if self._tk_img is None:
            # Fallback: bola colorida
            canvas = tk.Canvas(frame, width=96, height=96,
                               bg=COLOR_PANEL, highlightthickness=0)
            canvas.create_oval(0, 0, 95, 95, fill=COLOR_PT, outline="")
            canvas.create_text(48, 48, text="PT→ES",
                               font=("Segoe UI", 14, "bold"), fill=COLOR_FG)
            canvas.pack(pady=(28, 10))

        # Titulo
        tk.Label(
            frame,
            text="Tradutor PT ↔ ES",
            font=("Segoe UI", 17, "bold"),
            fg=COLOR_FG, bg=COLOR_PANEL,
        ).pack()

        # Status
        self._status_var = tk.StringVar(value="Iniciando servidor...")
        tk.Label(
            frame,
            textvariable=self._status_var,
            font=("Segoe UI", 10),
            fg=COLOR_DIM, bg=COLOR_PANEL,
        ).pack(pady=(14, 8))

        # Progress bar indeterminate
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Tradutor.Horizontal.TProgressbar",
            background=COLOR_PT,
            troughcolor="#262c38",
            bordercolor="#262c38",
            lightcolor=COLOR_PT,
            darkcolor=COLOR_PT,
        )
        self._progress = ttk.Progressbar(
            frame,
            mode="indeterminate",
            length=320,
            style="Tradutor.Horizontal.TProgressbar",
        )
        self._progress.pack(pady=4)
        self._progress.start(12)

        # Hint embaixo
        tk.Label(
            frame,
            text="Carregando modelos na GPU — leva ~10-20s",
            font=("Segoe UI", 8),
            fg="#6a6a6a", bg=COLOR_PANEL,
        ).pack(side="bottom", pady=12)

        # Permite arrastar splash
        def start_drag(e):
            self._drag_x = e.x
            self._drag_y = e.y

        def on_drag(e):
            x = self.root.winfo_pointerx() - self._drag_x
            y = self.root.winfo_pointery() - self._drag_y
            self.root.geometry(f"+{x}+{y}")

        for w_ in (self.root, frame):
            w_.bind("<ButtonPress-1>", start_drag)
            w_.bind("<B1-Motion>", on_drag)

        # Estado
        self._start_ts = self.root.tk.call("clock", "seconds")
        self._done = False
        self._callback_fired = False

        # Inicia loop de poll
        self.root.after(700, self._tick)

    def _now(self):
        return self.root.tk.call("clock", "seconds")

    def set_status(self, text):
        self._status_var.set(text)

    def _check_health(self):
        if not self.health_url:
            return True
        try:
            with urlopen(self.health_url, timeout=1) as r:
                return r.status == 200
        except URLError:
            return False
        except Exception:
            return False

    def _tick(self):
        if self._done:
            return
        elapsed = self._now() - self._start_ts
        if elapsed > 4 and elapsed < 12:
            self.set_status("Carregando Whisper large-v3 na GPU...")
        elif elapsed >= 12:
            self.set_status(f"Aquecendo modelo... ({int(elapsed)}s)")
        if self._check_health():
            self._done = True
            self.set_status("Pronto! Abrindo janela...")
            # Pequeno delay pra usuario ver "Pronto"
            self.root.after(500, self._finish_ready)
            return
        if elapsed > self.timeout_s:
            self._done = True
            self.set_status("Timeout: servidor nao respondeu")
            if self.on_timeout_cb and not self._callback_fired:
                self._callback_fired = True
                try:
                    self.on_timeout_cb()
                except Exception:
                    pass
            self.root.after(2000, self.close)
            return
        self.root.after(700, self._tick)

    def _finish_ready(self):
        if self.on_ready_cb and not self._callback_fired:
            self._callback_fired = True
            try:
                self.on_ready_cb()
            except Exception:
                pass
        self.close()

    def close(self):
        try:
            self._progress.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # Teste isolado: simula um servidor que demora 5s pra subir
    import threading
    import http.server
    import socketserver

    def fake_server():
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **kw): pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
        # Atraso de 5s antes de aceitar conexoes
        import time
        time.sleep(5)
        with socketserver.TCPServer(("127.0.0.1", 18765), H) as srv:
            srv.serve_forever()

    threading.Thread(target=fake_server, daemon=True).start()

    sp = Splash(
        icon_path="tradutor_icon_preview.png",
        health_url="http://127.0.0.1:18765/health",
        timeout_s=30,
        on_ready=lambda: print("READY!"),
    )
    sp.run()
