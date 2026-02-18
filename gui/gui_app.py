import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext
import webbrowser

try:
    from license.licensing import (
        DEFAULT_LICENSE_PATH,
        LicenseError,
        generate_machine_id,
        is_license_active,
        load_license_file,
        save_activation_key_file,
        validate_license,
    )
except ModuleNotFoundError:
    from licensing import (  # type: ignore
        DEFAULT_LICENSE_PATH,
        LicenseError,
        generate_machine_id,
        is_license_active,
        load_license_file,
        save_activation_key_file,
        validate_license,
    )
from resource_path import resource_path


class GuiLogStream:
    def __init__(self, queue_handle, fallback):
        self.queue_handle = queue_handle
        self.fallback = fallback

    def write(self, message):
        if self.fallback:
            try:
                self.fallback.write(message)
            except Exception:
                pass
        if message:
            self.queue_handle.put(message)

    def flush(self):
        if self.fallback:
            try:
                self.fallback.flush()
            except Exception:
                pass


class TradingBotGUI:
    SPLASH_DURATION_MS = 1500
    SPLASH_WIDTH = 420
    SPLASH_HEIGHT = 240
    SPLASH_LOGO_SCALE = 0.5

    def __init__(self, root):
        self.root = root
        self.root.title("TradingAi")
        self.root.geometry("360x320")
        self.root.resizable(False, False)

        self._app_icon = None
        self._splash_logo = None
        self._apply_app_icon()

        self.root.withdraw()
        self._show_splash()

        self.bot_thread = None
        self.bot = None
        self.ip_monitor = None
        self.machine_id = generate_machine_id()
        self.machine_id_var = tk.StringVar(value=self.machine_id)
        self.license_key_var = tk.StringVar()
        self.license_result_var = tk.StringVar(value="Paste activation key and click Activate.")
        self.license_result_color = "blue"
        self.license_window = None

        self.signal_var = tk.StringVar(value="Signal: --")
        self.price_var = tk.StringVar(value="BTC Price: --")
        # ===============================
        # UI
        # ===============================

        tk.Label(
            root,
            textvariable=self.signal_var,
            font=("Arial", 12, "bold"),
            fg="purple"
        ).pack(pady=5)

        tk.Label(
            root,
            textvariable=self.price_var,
            font=("Arial", 12, "bold"),
            fg="black"
        ).pack(pady=5)

        tk.Label(
            root,
            text="TradingAi",
            font=("Arial", 16, "bold")
        ).pack(pady=10)

        self.status_label = tk.Label(
            root,
            text="Checking license...",
            fg="blue"
        )
        self.status_label.pack(pady=5)

        self.manage_license_btn = tk.Button(
            root,
            text="Manage License",
            width=20,
            command=self.open_license_window,
        )
        self.manage_license_btn.pack(pady=4)

        self.alert_var = tk.StringVar(value="Alert: --")
        self.alert_label = tk.Label(
            root,
            textvariable=self.alert_var,
            fg="#b91c1c"
        )
        self.alert_label.pack(pady=4)

        self.start_btn = tk.Button(
            root,
            text="▶ Start Bot",
            width=20,
            command=self.start_bot,
            state="disabled"
        )
        self.start_btn.pack(pady=8)

        self.stop_btn = tk.Button(
            root,
            text="⛔ Stop Bot",
            width=20,
            command=self.stop_bot,
            state="disabled"
        )
        self.stop_btn.pack(pady=5)

        self.log_text = scrolledtext.ScrolledText(
            root,
            height=8,
            width=44,
            state="disabled",
            wrap="word",
            font=("Consolas", 9)
        )
        self.log_text.pack(padx=8, pady=(6, 8), fill="both", expand=False)

        self._init_log_stream()

        self._build_link_buttons()

        self.root.update_idletasks()
        desired_height = max(320, self.root.winfo_reqheight())
        self.root.geometry(f"360x{desired_height}")

        self.root.deiconify()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.check_license():
            self.open_license_window()

    def _build_link_buttons(self):
        container = tk.Frame(self.root)
        container.pack(pady=(6, 10))

        tk.Label(
            container,
            text="Join Learning Program",
            font=("Arial", 10),
            fg="#555555",
        ).pack(pady=(0, 6))

        button_row = tk.Frame(container)
        button_row.pack()

        tk.Button(
            button_row,
            text="Discord",
            width=12,
            command=lambda: self._open_url("https://discord.gg/GHjeawSYcp"),
        ).grid(row=0, column=0, padx=8)

        tk.Button(
            button_row,
            text="Binance",
            width=12,
            command=lambda: self._open_url(
                "https://accounts.binance.com/register?ref=35023868"
            ),
        ).grid(row=0, column=1, padx=8)

    def open_license_window(self):
        if self.license_window is not None and self.license_window.winfo_exists():
            self.license_window.lift()
            self.license_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("License Activation")
        win.resizable(False, False)
        self._apply_app_icon(win)
        self.license_window = win

        frame = tk.Frame(win, padx=14, pady=12)
        frame.pack()

        tk.Label(
            frame,
            text="Machine ID",
            font=("Arial", 9, "bold"),
        ).pack()

        machine_entry = tk.Entry(
            frame,
            textvariable=self.machine_id_var,
            width=62,
            state="readonly",
            justify="center",
        )
        machine_entry.pack(pady=(2, 4))

        button_row = tk.Frame(frame)
        button_row.pack(pady=(0, 8))

        tk.Button(
            button_row,
            text="Copy Machine ID",
            width=16,
            command=self.copy_machine_id,
        ).pack(side="left", padx=4)

        tk.Button(
            button_row,
            text="Request Key on Discord",
            width=22,
            command=lambda: self._open_url("https://discord.gg/GHjeawSYcp"),
        ).pack(side="left", padx=4)

        tk.Label(
            frame,
            text="Activation Key",
            font=("Arial", 9, "bold"),
        ).pack()

        self.license_key_entry = tk.Entry(
            frame,
            textvariable=self.license_key_var,
            width=62,
        )
        self.license_key_entry.pack(pady=(2, 6))
        self.license_key_entry.bind("<Return>", lambda _event: self.activate_license())

        self.activate_btn = tk.Button(
            frame,
            text="Activate License",
            width=20,
            command=self.activate_license,
        )
        self.activate_btn.pack(pady=(0, 6))

        self.license_result_label = tk.Label(
            frame,
            textvariable=self.license_result_var,
            fg=self.license_result_color,
        )
        self.license_result_label.pack()

        def on_close():
            self.license_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self.license_key_entry.focus_set()

    def _open_url(self, url: str):
        try:
            webbrowser.open_new_tab(url)
        except Exception as exc:
            messagebox.showerror("Open Link", f"Unable to open link.\n{exc}")

    def _show_splash(self):
        splash = tk.Toplevel(self.root)
        splash.overrideredirect(True)
        splash.configure(bg="#0f172a")
        splash.attributes("-topmost", True)

        width, height = self.SPLASH_WIDTH, self.SPLASH_HEIGHT
        splash.update_idletasks()
        screen_w = splash.winfo_screenwidth()
        screen_h = splash.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        splash.geometry(f"{width}x{height}+{x}+{y}")
        splash.update()

        container = tk.Frame(splash, bg="#0f172a")
        container.pack(expand=True, fill="both")

        logo_size = int(self.SPLASH_HEIGHT * self.SPLASH_LOGO_SCALE)
        logo_image = self._load_logo_image(logo_size)
        if logo_image is not None:
            tk.Label(
                container,
                image=logo_image,
                bg="#0f172a",
            ).pack(pady=(30, 8))
            title_pady = (0, 8)
        else:
            title_pady = (60, 10)

        tk.Label(
            container,
            text="TradingAi",
            font=("Segoe UI", 24, "bold"),
            fg="#e2e8f0",
            bg="#0f172a",
        ).pack(pady=title_pady)

        tk.Label(
            container,
            text="Starting up...",
            font=("Segoe UI", 11),
            fg="#94a3b8",
            bg="#0f172a",
        ).pack()

        self.root.after(self.SPLASH_DURATION_MS, splash.destroy)
        self.root.wait_window(splash)

    def _load_logo_image(self, size_px):
        try:
            logo_path = resource_path("logo.png")
            if not os.path.isfile(logo_path):
                return None

            from PIL import Image, ImageTk

            image = Image.open(logo_path).convert("RGBA")
            image.thumbnail((size_px, size_px), Image.LANCZOS)
            self._splash_logo = ImageTk.PhotoImage(image)
            return self._splash_logo
        except Exception:
            return None

    def _apply_app_icon(self, window=None):
        if window is None:
            window = self.root

        ico_path = resource_path("logo.ico")
        png_path = resource_path("logo.png")

        if os.path.isfile(ico_path):
            try:
                window.iconbitmap(ico_path)
                return
            except Exception:
                pass

        if os.path.isfile(png_path):
            try:
                if self._app_icon is None:
                    self._app_icon = tk.PhotoImage(file=png_path)
                window.iconphoto(True, self._app_icon)
            except Exception:
                pass

    def _init_log_stream(self):
        self._log_queue = queue.Queue()
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = GuiLogStream(self._log_queue, self._stdout)
        sys.stderr = GuiLogStream(self._log_queue, self._stderr)
        self._drain_log_queue()

    def _restore_stdio(self):
        if hasattr(self, "_stdout") and self._stdout is not None:
            sys.stdout = self._stdout
        if hasattr(self, "_stderr") and self._stderr is not None:
            sys.stderr = self._stderr

    def _on_close(self):
        self._restore_stdio()
        self.root.destroy()

    def _append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_log_queue(self):
        try:
            while True:
                message = self._log_queue.get_nowait()
                if message:
                    self._append_log(message)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def show_alert(self, message, color="red"):
        self.root.after(0, lambda: self._set_alert(message, color))

    def _set_alert(self, message, color):
        self.alert_var.set(message)
        self.alert_label.config(fg=color)

    def _set_license_result(self, message, color="blue"):
        self.license_result_var.set(message)
        self.license_result_color = color
        if hasattr(self, "license_result_label") and self.license_result_label.winfo_exists():
            self.license_result_label.config(fg=color)

    def copy_machine_id(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.machine_id)
            self._set_license_result("Machine ID copied to clipboard.", "green")
        except Exception as exc:
            self._set_license_result(f"Copy failed: {exc}", "red")

    def update_signal(self, value):
        # Tkinter-safe update
        self.root.after(
            0,
            lambda: self.signal_var.set(f"Signal: {value}")
        )

    def update_price(self, value):
        def format_price():
            self.price_var.set(f"BTC Price: ${value:,.2f}")

        self.root.after(0, format_price)

    # ===============================
    # LICENSE CHECK
    # ===============================
    def activate_license(self):
        key = self.license_key_var.get().strip()
        if not key:
            self._set_license_result("Paste an activation key to continue.", "red")
            return

        try:
            validate_license(key, expected_machine_id=self.machine_id)
            save_activation_key_file(key)
        except LicenseError as exc:
            self._set_license_result(f"Activation failed: {exc}", "red")
            return
        except Exception as exc:
            self._set_license_result(f"Activation failed: {exc}", "red")
            return

        self._set_license_result("Activation successful.", "green")
        self.check_license()

    def check_license(self):
        try:
            key = load_license_file()
        except FileNotFoundError:
            self.status_label.config(
                text=f"License file not found: {DEFAULT_LICENSE_PATH}",
                fg="red",
            )
            self._set_license_result("No license key saved.", "red")
            self.start_btn.config(state="disabled")
            return False
        except LicenseError as exc:
            self.status_label.config(
                text=f"Invalid license file: {exc}",
                fg="red",
            )
            self._set_license_result(f"Invalid license file: {exc}", "red")
            self.start_btn.config(state="disabled")
            return False

        active, message = is_license_active(key, expected_machine_id=self.machine_id)
        if not active:
            self.status_label.config(
                text=f"License inactive: {message}",
                fg="red",
            )
            self._set_license_result(f"License inactive: {message}", "red")
            self.start_btn.config(state="disabled")
            return False

        self.status_label.config(
            text=message,
            fg="green",
        )
        self._set_license_result(message, "green")
        self.start_btn.config(state="normal")
        return True

    # ===============================
    # BOT CONTROL
    # ===============================
    def start_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            return

        from bot.trading_bot import TradingBot
        from ip_address.ip_address import start_ip_monitor

        if self.ip_monitor is None:
            self.ip_monitor = start_ip_monitor(
                check_interval_seconds=60,
                whitelist_cache_ttl_seconds=300,
            )
        else:
            self.ip_monitor.start()

        self.bot = TradingBot(
            on_signal=self.update_signal,
            on_price=self.update_price,
            on_alert=self.show_alert
        )

        self.bot_thread = threading.Thread(
            target=self.bot.run_trading_bot,
            daemon=True
        )
        self.bot_thread.start()

        self.status_label.config(
            text="🚀 Bot running...",
            fg="green"
        )
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def stop_bot(self):
        messagebox.showinfo(
            "Stop Bot",
            "Please close the application to stop the bot.\n"
            "(Safe shutdown)"
        )


# ===============================
# ENTRY POINT
# ===============================
if __name__ == "__main__":
    root = tk.Tk()
    app = TradingBotGUI(root)
    root.mainloop()





