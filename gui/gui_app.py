import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext

try:
    from license.licensing import (
        DEFAULT_LICENSE_PATH,
        LicenseError,
        create_activation_key,
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
        create_activation_key,
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
    TRIAL_DAYS = 7
    API_KEYS_FILENAME = "binance_keys.json"
    API_ENV_KEY = "binance_api_key"
    API_ENV_SECRET = "binance_api_secret"
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
        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.api_result_var = tk.StringVar(value="Enter your Binance API key and secret.")
        self.api_show_secret_var = tk.BooleanVar(value=False)
        self.api_window = None
        self.api_keys_ready = False

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

        self.manage_api_btn = tk.Button(
            root,
            text="Manage API Keys",
            width=20,
            command=self.open_api_window,
        )
        self.manage_api_btn.pack(pady=4)

        self.trial_btn = tk.Button(
            root,
            text="Start Trial",
            width=20,
            command=self.start_trial,
        )
        self.trial_btn_visible = False

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

        self._ensure_api_keys()

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

    def _api_keys_path(self) -> Path:
        return Path(DEFAULT_LICENSE_PATH).parent / self.API_KEYS_FILENAME

    def _read_api_keys(self) -> dict:
        path = self._api_keys_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_api_keys(self, data: dict) -> None:
        path = self._api_keys_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(f"{path}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _set_api_env(self, key: str, secret: str) -> None:
        os.environ[self.API_ENV_KEY] = key
        os.environ[self.API_ENV_KEY.upper()] = key
        os.environ[self.API_ENV_SECRET] = secret
        os.environ[self.API_ENV_SECRET.upper()] = secret

    def _load_api_keys_into_vars(self) -> bool:
        data = self._read_api_keys()
        key = data.get(self.API_ENV_KEY) or data.get(self.API_ENV_KEY.upper(), "")
        secret = data.get(self.API_ENV_SECRET) or data.get(self.API_ENV_SECRET.upper(), "")

        self.api_key_var.set(key)
        self.api_secret_var.set(secret)

        if key and secret:
            self._set_api_env(key, secret)
            return True
        return False

    def _set_api_result(self, message: str, color: str = "blue") -> None:
        self.api_result_var.set(message)
        if hasattr(self, "api_result_label") and self.api_result_label.winfo_exists():
            self.api_result_label.config(fg=color)

    def _toggle_api_secret(self) -> None:
        if hasattr(self, "api_secret_entry") and self.api_secret_entry.winfo_exists():
            self.api_secret_entry.config(
                show="" if self.api_show_secret_var.get() else "*"
            )

    def _show_trial_button(self) -> None:
        if self.trial_btn_visible:
            return
        self.trial_btn.pack(pady=4, before=self.alert_label)
        self.trial_btn_visible = True

    def _ensure_api_keys(self) -> None:
        if self._load_api_keys_into_vars():
            self.api_keys_ready = True
            self._show_trial_button()
            return
        self.api_keys_ready = False
        self.open_api_window(require_confirm=True)

    def open_api_window(self, require_confirm: bool = False) -> None:
        if self.api_window is not None and self.api_window.winfo_exists():
            self.api_window.lift()
            self.api_window.focus_force()
            return

        self._load_api_keys_into_vars()
        self.api_show_secret_var.set(False)
        self.api_result_var.set("Enter your Binance API key and secret.")

        win = tk.Toplevel(self.root)
        win.title("Binance API Keys")
        win.resizable(False, False)
        self._apply_app_icon(win)
        if self.root.state() != "withdrawn":
            win.transient(self.root)
        self.api_window = win

        frame = tk.Frame(win, padx=14, pady=12)
        frame.pack()
        frame.columnconfigure(0, weight=1)

        tk.Label(
            frame,
            text="API Key",
            font=("Arial", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.api_key_entry = tk.Entry(
            frame,
            textvariable=self.api_key_var,
            width=62,
        )
        self.api_key_entry.grid(row=1, column=0, pady=(2, 8), sticky="ew")

        tk.Label(
            frame,
            text="API Secret",
            font=("Arial", 9, "bold"),
        ).grid(row=2, column=0, sticky="w")

        self.api_secret_entry = tk.Entry(
            frame,
            textvariable=self.api_secret_var,
            show="*",
            width=62,
        )
        self.api_secret_entry.grid(row=3, column=0, pady=(2, 6), sticky="ew")

        tk.Checkbutton(
            frame,
            text="Show secret",
            variable=self.api_show_secret_var,
            command=self._toggle_api_secret,
        ).grid(row=4, column=0, sticky="w")

        tk.Label(
            frame,
            text=f"Saved to: {self._api_keys_path()}",
            fg="#555555",
        ).grid(row=5, column=0, pady=(2, 8), sticky="w")

        button_row = tk.Frame(frame)
        button_row.grid(row=6, column=0, pady=(0, 6))

        tk.Button(
            button_row,
            text="Save & Continue",
            width=18,
            command=self.save_api_keys,
        ).pack(side="left", padx=4)

        if not require_confirm:
            tk.Button(
                button_row,
                text="Close",
                width=10,
                command=self._close_api_window,
            ).pack(side="left", padx=4)

        self.api_result_label = tk.Label(
            frame,
            textvariable=self.api_result_var,
            fg="blue",
        )
        self.api_result_label.grid(row=7, column=0, sticky="w")

        def on_close():
            if require_confirm and not self.api_keys_ready:
                messagebox.showwarning(
                    "API Key Required",
                    "Please save your Binance API key and secret to continue.",
                )
                return
            self.api_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self.api_key_entry.focus_set()

        if require_confirm:
            win.grab_set()
            self.root.wait_window(win)

    def _close_api_window(self) -> None:
        if self.api_window is None:
            return
        self.api_window.destroy()
        self.api_window = None

    def save_api_keys(self) -> None:
        key = self.api_key_var.get().strip()
        secret = self.api_secret_var.get().strip()
        if not key or not secret:
            self._set_api_result("Both fields are required.", "red")
            return

        data = {
            self.API_ENV_KEY: key,
            self.API_ENV_SECRET: secret,
        }
        try:
            self._write_api_keys(data)
            self._set_api_env(key, secret)
        except OSError as exc:
            self._set_api_result(f"Save failed: {exc}", "red")
            return

        self.api_keys_ready = True
        self._set_api_result("API keys saved.", "green")
        self._show_trial_button()

        if self.api_window is not None and self.api_window.winfo_exists():
            self.api_window.destroy()
            self.api_window = None

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
            if self.trial_btn_visible:
                self.trial_btn.config(state="normal")
            return False
        except LicenseError as exc:
            self.status_label.config(
                text=f"Invalid license file: {exc}",
                fg="red",
            )
            self._set_license_result(f"Invalid license file: {exc}", "red")
            self.start_btn.config(state="disabled")
            if self.trial_btn_visible:
                self.trial_btn.config(state="normal")
            return False

        active, message = is_license_active(key, expected_machine_id=self.machine_id)
        if not active:
            self.status_label.config(
                text=f"License inactive: {message}",
                fg="red",
            )
            self._set_license_result(f"License inactive: {message}", "red")
            self.start_btn.config(state="disabled")
            if self.trial_btn_visible:
                self.trial_btn.config(state="normal")
            return False

        self.status_label.config(
            text=message,
            fg="green",
        )
        self._set_license_result(message, "green")
        self.start_btn.config(state="normal")
        if self.trial_btn_visible:
            self.trial_btn.config(state="disabled")
        return True

    def start_trial(self):
        if not self.api_keys_ready:
            messagebox.showwarning(
                "API Keys Required",
                "Please save your Binance API key and secret before starting the trial.",
            )
            self.open_api_window()
            return

        license_path = Path(DEFAULT_LICENSE_PATH)
        if license_path.exists():
            try:
                existing_key = load_license_file()
                active, message = is_license_active(
                    existing_key,
                    expected_machine_id=self.machine_id,
                )
                if active:
                    messagebox.showinfo(
                        "Trial",
                        "A license is already active. Trial not started.",
                    )
                    return
                replace = messagebox.askyesno(
                    "Replace License",
                    f"Existing license is inactive: {message}. "
                    "Replace it with a trial license?",
                )
                if not replace:
                    return
            except LicenseError:
                replace = messagebox.askyesno(
                    "Replace License",
                    "Existing license file is invalid or expired. "
                    "Replace it with a trial license?",
                )
                if not replace:
                    return

        trial_key = create_activation_key(
            days=self.TRIAL_DAYS,
            license_type="trial",
            machine_id=self.machine_id,
        )
        save_activation_key_file(trial_key)
        self.check_license()
        messagebox.showinfo(
            "Trial Started",
            f"Trial activated for {self.TRIAL_DAYS} days.",
        )
        if self.trial_btn_visible:
            self.trial_btn.config(state="disabled")

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





