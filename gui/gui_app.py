import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import tkinter as tk
import webbrowser
import html as html_lib
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext
from urllib.parse import parse_qs, urljoin, urlparse

import requests

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
from app.feature_flags import build_feature_flags
from app.db_manager import DatabaseManager
from app.license_service import LicenseService
from app.mode_manager import Mode, ModeManager
from app.settings import load_settings, save_settings
from bot.trading_bot import SignalConfig
from all_variable import Variable


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
    TRIAL_DAYS = 3
    API_KEYS_FILENAME = "binance_keys.json"
    API_ENV_KEY = "binance_api_key"
    API_ENV_SECRET = "binance_api_secret"
    PARTNER_DB_SOURCE_URL = (
        "https://drive.google.com/file/d/1BaKDQ5yIDFs_R2WGahINY81UZXGVCUxm/view?usp=sharing"
    )
    PARTNER_DB_DOWNLOAD_BASE = "https://drive.google.com/uc"
    BINANCE_API_WHITELIST_URL = "https://www.binance.com/en/my/settings/api-management"
    IP_CHECK_INTERVAL_MS = 60_000
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
        self.settings = load_settings()
        self.license_service = LicenseService(expected_machine_id=self.machine_id)
        self.mode_manager = ModeManager(self.license_service, self.settings)
        self.mode_ctx = self.mode_manager.resolve()
        self.features = build_feature_flags(self.mode_ctx)
        self.auto_profit_share_var = tk.BooleanVar(
            value=self.settings.auto_profit_share_enabled
        )
        self.public_ip_var = tk.StringVar(value="Public IP: --")
        self.ip_whitelist_var = tk.StringVar(value="IP whitelist: --")
        self._current_public_ip = ""
        self._current_ip_status_message = ""
        self._ip_section_visible = False
        self._ip_check_inflight = False
        self._ip_after_id = None
        self._last_ip_alert_message = ""
        self._binance_api_checker = None
        self._db_update_in_progress = False

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
            text="Enter License",
            width=20,
            command=self.open_license_window,
        )
        self.manage_license_btn.pack(pady=4)

        self.upgrade_btn = tk.Button(
            root,
            text="Upgrade to Partner",
            width=20,
            command=self._open_upgrade_link,
        )
        self.upgrade_btn.pack(pady=2)

        self.manage_api_btn = tk.Button(
            root,
            text="Manage API Keys",
            width=20,
            command=self.open_api_window,
        )
        self.manage_api_btn.pack(pady=4)
        self._api_btn_visible = True

        self.ip_frame = tk.Frame(root)
        self.ip_header_frame = tk.Frame(self.ip_frame)
        self.ip_header_frame.pack(fill="x")
        self.ip_label = tk.Label(
            self.ip_header_frame,
            textvariable=self.public_ip_var,
            fg="#1f2937",
            font=("Arial", 9, "bold"),
        )
        self.ip_label.pack(side="left")
        self.copy_ip_btn = tk.Button(
            self.ip_header_frame,
            text="Copy IP",
            width=10,
            command=self.copy_public_ip,
            state="disabled",
        )
        self.copy_ip_btn.pack(side="right")
        self.ip_status_label = tk.Label(
            self.ip_frame,
            textvariable=self.ip_whitelist_var,
            fg="#6b7280",
            justify="left",
            wraplength=320,
        )
        self.ip_status_label.pack(anchor="w", pady=(2, 0))

        self.trial_btn = tk.Button(
            root,
            text="Start Trial",
            width=20,
            command=self.start_trial,
        )
        self.trial_btn_visible = False

        self.auto_profit_share_chk = tk.Checkbutton(
            root,
            text="Enable Auto Profit Share",
            variable=self.auto_profit_share_var,
            command=self._toggle_auto_profit_share,
        )

        self.alert_var = tk.StringVar(value="Alert: --")
        self.alert_label = tk.Label(
            root,
            textvariable=self.alert_var,
            fg="#b91c1c"
        )
        self.alert_label.pack(pady=4)

        self.start_btn = tk.Button(
            root,
            text="Start Bot",
            width=20,
            command=self.start_bot,
            state="disabled"
        )
        self.start_btn.pack(pady=8)

        self.stop_btn = tk.Button(
            root,
            text="Stop Bot",
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
        self._refresh_mode()

        self.root.update_idletasks()
        desired_height = max(320, self.root.winfo_reqheight())
        self.root.geometry(f"360x{desired_height}")

        self.root.deiconify()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _open_upgrade_link(self):
        self._open_url("https://discord.gg/GHjeawSYcp")

    def _toggle_auto_profit_share(self):
        self.settings.auto_profit_share_enabled = bool(
            self.auto_profit_share_var.get()
        )
        save_settings(self.settings)
        self._refresh_mode()

    def _is_bot_running(self) -> bool:
        return bool(self.bot_thread and self.bot_thread.is_alive())

    def _refresh_mode(self):
        self.mode_manager = ModeManager(self.license_service, self.settings)
        self.mode_ctx = self.mode_manager.resolve()
        self.features = build_feature_flags(self.mode_ctx)
        if self.license_window is not None and self.license_window.winfo_exists():
            self._configure_license_window_menu(self.license_window)
        self._apply_mode()

    def _apply_mode(self):
        bot_running = self._is_bot_running()

        if self.mode_ctx.mode == Mode.PARTNER:
            self._show_api_button()
            self._show_profit_toggle()
            self._show_ip_section()
            self.manage_license_btn.config(text="Manage License")
            self.upgrade_btn.pack_forget()
            self._hide_trial_button()
            self._load_api_keys_into_vars()
            self.api_keys_ready = bool(
                self.api_key_var.get().strip() and self.api_secret_var.get().strip()
            )
            if bot_running:
                self.start_btn.config(state="disabled")
            elif self.api_keys_ready:
                self.start_btn.config(state="normal")
            else:
                self.start_btn.config(state="disabled")
            self.start_btn.config(text="Start Bot")
        else:
            self._hide_api_button()
            self._hide_profit_toggle()
            self._hide_ip_section()
            self.manage_license_btn.config(text="Enter License")
            if not self.upgrade_btn.winfo_ismapped():
                self.upgrade_btn.pack(pady=2)
            self._show_trial_button()
            trial_license_active = bool(self.mode_ctx.license_status.active)
            self.trial_btn.config(
                state="disabled" if (bot_running or trial_license_active) else "normal"
            )
            self.start_btn.config(
                state="normal" if (trial_license_active and not bot_running) else "disabled",
                text="Start Signals",
            )

        self.stop_btn.config(state="normal" if bot_running else "disabled")

        status = self.mode_ctx.license_status.message
        if self.mode_ctx.mode == Mode.TRIAL:
            self.status_label.config(text=f"Mode: TRIAL | {status}", fg="orange")
        else:
            self.status_label.config(text=status, fg="green")

    def _show_api_button(self):
        if not self._api_btn_visible:
            self.manage_api_btn.pack(pady=4, before=self.alert_label)
            self._api_btn_visible = True

    def _hide_api_button(self):
        if self._api_btn_visible:
            self.manage_api_btn.pack_forget()
            self._api_btn_visible = False

    def _show_profit_toggle(self):
        if not self.auto_profit_share_chk.winfo_ismapped():
            self.auto_profit_share_chk.pack(pady=2, before=self.alert_label)

    def _hide_profit_toggle(self):
        if self.auto_profit_share_chk.winfo_ismapped():
            self.auto_profit_share_chk.pack_forget()

    def _show_ip_section(self):
        if self._ip_section_visible:
            return
        self.ip_frame.pack(pady=2, before=self.alert_label)
        self._ip_section_visible = True

    def _hide_ip_section(self):
        if self._ip_after_id is not None:
            try:
                self.root.after_cancel(self._ip_after_id)
            except Exception:
                pass
            self._ip_after_id = None
        if not self._ip_section_visible:
            return
        self.ip_frame.pack_forget()
        self._ip_section_visible = False

    def copy_public_ip(self):
        ip_value = (self._current_public_ip or "").strip()
        if not ip_value:
            self.show_alert("Public IP not available yet.", "orange")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(ip_value)
            self.show_alert(f"IP copied: {ip_value}", "green")
            if "not whitelisted" in self._current_ip_status_message.lower():
                self._set_license_result(
                    "IP copied. Paste it into Binance API whitelist.",
                    "orange",
                )
        except Exception as exc:
            self.show_alert(f"Copy failed: {exc}", "red")

    def _schedule_ip_status_refresh(self, immediate: bool = False):
        if self.mode_ctx.mode != Mode.PARTNER:
            return
        can_run = self._is_bot_running() or (
            self.license_window is not None and self.license_window.winfo_exists()
        )
        if not can_run:
            return
        if self._ip_after_id is not None:
            try:
                self.root.after_cancel(self._ip_after_id)
            except Exception:
                pass
            self._ip_after_id = None
        delay = 0 if immediate else self.IP_CHECK_INTERVAL_MS
        self._ip_after_id = self.root.after(delay, self._refresh_ip_status)

    def _refresh_ip_status(self):
        self._ip_after_id = None
        if self.mode_ctx.mode != Mode.PARTNER:
            return
        if self._ip_check_inflight:
            self._schedule_ip_status_refresh(immediate=False)
            return
        self._ip_check_inflight = True
        threading.Thread(target=self._refresh_ip_status_worker, daemon=True).start()

    def _refresh_ip_status_worker(self):
        result = self._compute_ip_status()
        try:
            self.root.after(0, lambda: self._finish_ip_status_refresh(result))
        except Exception:
            pass

    def _finish_ip_status_refresh(self, result: dict):
        self._ip_check_inflight = False
        self._apply_ip_status(result)
        can_run = self._is_bot_running() or (
            self.license_window is not None and self.license_window.winfo_exists()
        )
        if self.mode_ctx.mode == Mode.PARTNER and self._ip_section_visible and can_run:
            self._schedule_ip_status_refresh(immediate=False)

    def _compute_ip_status(self) -> dict:
        current_ip = ""
        try:
            from ip_address.ip_address import PublicIPResolver, BinanceAPI
        except Exception as exc:
            return {
                "ip": current_ip,
                "message": f"IP check unavailable: {exc}",
                "color": "red",
                "alert": "",
            }

        try:
            ips = PublicIPResolver().fetch()
            current_ip = (ips.get("ipv4") or ips.get("ipv6") or "").strip()
        except Exception:
            current_ip = ""

        if not current_ip:
            return {
                "ip": "",
                "message": "Unable to resolve public IP.",
                "color": "orange",
                "alert": "",
            }

        api_key = os.environ.get(self.API_ENV_KEY) or os.environ.get(self.API_ENV_KEY.upper())
        api_secret = os.environ.get(self.API_ENV_SECRET) or os.environ.get(self.API_ENV_SECRET.upper())
        if not api_key or not api_secret:
            return {
                "ip": current_ip,
                "message": "Save API keys to check Binance IP whitelist.",
                "color": "orange",
                "alert": "",
            }

        if self._binance_api_checker is None:
            try:
                self._binance_api_checker = BinanceAPI(
                    api_key=api_key,
                    secret_key=api_secret,
                    timeout=8,
                )
            except Exception as exc:
                return {
                    "ip": current_ip,
                    "message": f"Whitelist check unavailable: {exc}",
                    "color": "orange",
                    "alert": "",
                }

        try:
            ip_restrict, ip_list, _, error_code, error_msg = self._binance_api_checker.get_whitelisted_ips()
        except Exception as exc:
            return {
                "ip": current_ip,
                "message": f"Whitelist check failed: {exc}",
                "color": "orange",
                "alert": "",
            }

        if error_code == -2015:
            msg = "IP may not be whitelisted in Binance. Add this IP."
            return {"ip": current_ip, "message": msg, "color": "red", "alert": msg}

        if ip_restrict:
            if current_ip in (ip_list or set()):
                return {
                    "ip": current_ip,
                    "message": "IP is whitelisted in Binance.",
                    "color": "green",
                    "alert": "",
                }
            msg = "IP NOT whitelisted. Add this IP to Binance API whitelist."
            return {"ip": current_ip, "message": msg, "color": "red", "alert": msg}

        return {
            "ip": current_ip,
            "message": "Binance IP restriction is OFF.",
            "color": "orange",
            "alert": "",
        }

    def _apply_ip_status(self, result: dict):
        ip_value = (result.get("ip") or "").strip()
        message = (result.get("message") or "IP whitelist: --").strip()
        color = (result.get("color") or "black").strip()
        alert = (result.get("alert") or "").strip()

        self._current_public_ip = ip_value
        self._current_ip_status_message = message
        self.public_ip_var.set(f"Public IP: {ip_value if ip_value else '--'}")
        self.ip_whitelist_var.set(message)
        self.ip_status_label.config(fg=color)
        self.copy_ip_btn.config(state="normal" if ip_value else "disabled")
        if self.license_window is not None and self.license_window.winfo_exists():
            self._configure_license_window_menu(self.license_window)

        if alert and alert != self._last_ip_alert_message:
            if self._is_bot_running():
                self.show_alert(alert, "red")
            else:
                self._set_license_result(alert, "red")
            self._last_ip_alert_message = alert
        if not alert:
            self._last_ip_alert_message = ""

    def _configure_license_window_menu(self, win):
        menu_bar = tk.Menu(win)
        tools_menu = tk.Menu(menu_bar, tearoff=0)
        whitelist_menu = tk.Menu(menu_bar, tearoff=0)
        partner_enabled = self.mode_ctx.mode == Mode.PARTNER

        tools_menu.add_command(
            label="Update Database",
            command=self._start_partner_database_update,
            state="normal" if partner_enabled else "disabled",
        )
        tools_menu.add_command(
            label="Open Update Link",
            command=lambda: self._open_url(self.PARTNER_DB_SOURCE_URL),
            state="normal" if partner_enabled else "disabled",
        )
        menu_bar.add_cascade(label="Menu", menu=tools_menu)

        whitelist_menu.add_command(
            label="Refresh IP Status",
            command=lambda: self._schedule_ip_status_refresh(immediate=True),
            state="normal" if partner_enabled else "disabled",
        )
        whitelist_menu.add_command(
            label="Copy Current IP",
            command=self.copy_public_ip,
            state="normal" if (partner_enabled and bool(self._current_public_ip)) else "disabled",
        )
        whitelist_menu.add_command(
            label="Open Binance API Page",
            command=lambda: self._open_url(self.BINANCE_API_WHITELIST_URL),
            state="normal" if partner_enabled else "disabled",
        )
        menu_bar.add_cascade(label="Whitelist", menu=whitelist_menu)
        win.config(menu=menu_bar)

    @staticmethod
    def _extract_drive_file_id(url: str) -> str | None:
        m = re.search(r"/d/([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _validate_sqlite_file(path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise RuntimeError("Downloaded database failed integrity_check.")

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
            if cur.fetchone() is None:
                raise RuntimeError("Downloaded database has no tables.")
        finally:
            conn.close()

    @staticmethod
    def _response_is_drive_file(response: requests.Response) -> bool:
        content_type = (response.headers.get("Content-Type") or "").lower()
        content_disposition = (response.headers.get("Content-Disposition") or "").lower()

        if "attachment" in content_disposition:
            return True
        if "text/html" in content_type:
            return False
        if "application/json" in content_type:
            return False
        return bool(content_type)

    @staticmethod
    def _extract_drive_confirm_from_html(
        html_text: str,
        file_id: str,
        current_url: str,
    ) -> tuple[str, dict] | None:
        form_match = re.search(
            r"<form[^>]+id=['\"]download-form['\"][^>]*>",
            html_text,
            flags=re.IGNORECASE,
        )
        if form_match:
            form_tag = form_match.group(0)
            action_match = re.search(
                r"action=['\"]([^'\"]+)['\"]",
                form_tag,
                flags=re.IGNORECASE,
            )
            action_url = (
                urljoin(current_url, html_lib.unescape(action_match.group(1)))
                if action_match
                else current_url
            )

            params: dict[str, str] = {}
            for input_match in re.finditer(
                r"<input[^>]+type=['\"]hidden['\"][^>]*>",
                html_text,
                flags=re.IGNORECASE,
            ):
                input_tag = input_match.group(0)
                name_match = re.search(
                    r"name=['\"]([^'\"]+)['\"]",
                    input_tag,
                    flags=re.IGNORECASE,
                )
                if not name_match:
                    continue
                value_match = re.search(
                    r"value=['\"]([^'\"]*)['\"]",
                    input_tag,
                    flags=re.IGNORECASE,
                )
                name = html_lib.unescape(name_match.group(1))
                value = html_lib.unescape(value_match.group(1) if value_match else "")
                params[name] = value

            if params:
                params.setdefault("id", file_id)
                params.setdefault("export", "download")
                return action_url, params

        href_match = re.search(
            r"""href=['"]([^'"]*confirm[^'"]*)['"]""",
            html_text,
            flags=re.IGNORECASE,
        )
        if href_match:
            confirm_url = html_lib.unescape(href_match.group(1))
            full_url = urljoin(current_url, confirm_url)
            parsed = urlparse(full_url)
            q = parse_qs(parsed.query)
            params = {k: (v[0] if v else "") for k, v in q.items()}
            params.setdefault("id", file_id)
            params.setdefault("export", "download")
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return base_url, params

        token_match = re.search(r"confirm=([0-9A-Za-z_]+)", html_text)
        if token_match:
            return (
                "https://drive.google.com/uc",
                {"export": "download", "id": file_id, "confirm": token_match.group(1)},
            )

        return None

    def _download_drive_file(self, source_url: str, output_path: Path) -> None:
        file_id = self._extract_drive_file_id(source_url)
        if not file_id:
            raise RuntimeError("Unable to parse Google Drive file id from update link.")

        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        request_url = self.PARTNER_DB_DOWNLOAD_BASE
        request_params = {"export": "download", "id": file_id}
        response = None

        for _ in range(6):
            response = session.get(
                request_url,
                params=request_params,
                stream=True,
                timeout=90,
                allow_redirects=True,
            )
            response.raise_for_status()

            if self._response_is_drive_file(response):
                break

            html_text = response.text
            confirm_request = self._extract_drive_confirm_from_html(
                html_text=html_text,
                file_id=file_id,
                current_url=response.url,
            )

            if not confirm_request:
                token = None
                for key, value in response.cookies.items():
                    if key.startswith("download_warning"):
                        token = value
                        break
                if token:
                    confirm_request = (
                        self.PARTNER_DB_DOWNLOAD_BASE,
                        {"export": "download", "id": file_id, "confirm": token},
                    )

            response.close()
            if not confirm_request:
                snippet = html_text[:260].replace("\n", " ").strip()
                raise RuntimeError(
                    "Google Drive did not return a DB file. "
                    f"Check sharing permissions. {snippet}"
                )

            request_url, request_params = confirm_request
        else:
            raise RuntimeError("Google Drive confirmation flow did not resolve a file download.")

        if response is None or not self._response_is_drive_file(response):
            snippet = ""
            try:
                snippet = response.text[:260].replace("\n", " ").strip() if response else ""
            except Exception:
                pass
            raise RuntimeError(
                "Google Drive did not return a DB file after confirmation. "
                f"{snippet}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        response.close()

        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError("Downloaded database file is empty or too small.")

    def _start_partner_database_update(self):
        self._refresh_mode()
        if self.mode_ctx.mode != Mode.PARTNER:
            messagebox.showwarning(
                "Partner Only",
                "Database update is available only in PARTNER mode.",
            )
            return

        if self._is_bot_running():
            messagebox.showwarning(
                "Stop Bot First",
                "Please stop the bot before replacing the database file.",
            )
            return

        if self._db_update_in_progress:
            messagebox.showinfo(
                "Database Update",
                "Database update is already running.",
            )
            return

        confirm = messagebox.askyesno(
            "Update Database",
            "Download latest database from partner link and replace current DB?\n"
            "A backup of the current DB will be created first.",
        )
        if not confirm:
            return

        self._db_update_in_progress = True
        self._set_license_result("Downloading database update...", "blue")
        self.show_alert("Downloading database update...", "orange")
        threading.Thread(target=self._partner_database_update_worker, daemon=True).start()

    def _partner_database_update_worker(self):
        db_path = Path(os.path.abspath(Variable.DATABASE))
        tmp_path = db_path.with_name(f"{db_path.name}.download")
        backup_path = None
        try:
            self._download_drive_file(self.PARTNER_DB_SOURCE_URL, tmp_path)
            self._validate_sqlite_file(tmp_path)

            if db_path.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = db_path.with_name(
                    f"{db_path.stem}_backup_{stamp}{db_path.suffix}"
                )
                shutil.copy2(db_path, backup_path)

            os.replace(tmp_path, db_path)
            if backup_path is not None:
                msg = (
                    f"Database updated.\nCurrent: {db_path}\n"
                    f"Backup: {backup_path}"
                )
            else:
                msg = f"Database updated.\nCurrent: {db_path}"
            self.root.after(
                0,
                lambda message=msg: self._finish_partner_database_update(True, message),
            )
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            err = f"Database update failed: {exc}"
            self.root.after(
                0,
                lambda message=err: self._finish_partner_database_update(False, message),
            )

    def _finish_partner_database_update(self, success: bool, message: str):
        self._db_update_in_progress = False
        if success:
            self._set_license_result("Database updated successfully.", "green")
            self.show_alert("Database updated successfully.", "green")
            messagebox.showinfo("Update Database", message)
        else:
            self._set_license_result(message, "red")
            self.show_alert(message, "red")
            messagebox.showerror("Update Database", message)

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
        self._configure_license_window_menu(win)

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
            if self._ip_after_id is not None and not self._is_bot_running():
                try:
                    self.root.after_cancel(self._ip_after_id)
                except Exception:
                    pass
                self._ip_after_id = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._schedule_ip_status_refresh(immediate=True)
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
        self._binance_api_checker = None

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

    def _hide_trial_button(self) -> None:
        if not self.trial_btn_visible:
            return
        self.trial_btn.pack_forget()
        self.trial_btn_visible = False

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
        self._apply_mode()

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

    def _stop_bot_sync(self, timeout_seconds: int = 10) -> bool:
        if self.bot is not None:
            try:
                self.bot.stop()
            except Exception:
                pass

        if self.ip_monitor is not None:
            try:
                self.ip_monitor.stop()
            except Exception:
                pass

        if self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.join(timeout=max(1, int(timeout_seconds)))

        if self.bot_thread and self.bot_thread.is_alive():
            return False

        self.bot_thread = None
        self.bot = None
        return True

    def _stop_bot_background(self):
        stopped = self._stop_bot_sync(timeout_seconds=10)
        self.root.after(0, lambda: self._finish_stop_bot(stopped))

    def _finish_stop_bot(self, stopped: bool):
        if stopped:
            if self._ip_after_id is not None and (
                self.license_window is None or not self.license_window.winfo_exists()
            ):
                try:
                    self.root.after_cancel(self._ip_after_id)
                except Exception:
                    pass
                self._ip_after_id = None
            self.show_alert("Bot stopped.", "green")
            self._refresh_mode()
            return

        self.status_label.config(
            text="Stop requested. Waiting for current cycle...",
            fg="orange",
        )
        self.stop_btn.config(state="normal")

    def _on_close(self):
        if self._ip_after_id is not None:
            try:
                self.root.after_cancel(self._ip_after_id)
            except Exception:
                pass
            self._ip_after_id = None
        self._stop_bot_sync(timeout_seconds=3)
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
            self.license_service.activate(key)
        except LicenseError as exc:
            self._set_license_result(f"Activation failed: {exc}", "red")
            return
        except Exception as exc:
            self._set_license_result(f"Activation failed: {exc}", "red")
            return

        self._set_license_result("Activation successful.", "green")
        self.check_license()

    def check_license(self):
        status = self.license_service.get_status()
        if status.active:
            self._set_license_result(status.message, "green")
        else:
            self._set_license_result(status.message, "red")
        self._refresh_mode()
        return status.active

    def start_trial(self):
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
        # Create empty schema for Trial so signal reads don't fail on missing tables.
        DatabaseManager().ensure_schema_async()
        self.check_license()
        messagebox.showinfo(
            "Trial Started",
            f"Trial activated for {self.TRIAL_DAYS} days.",
        )

    # ===============================
    # BOT CONTROL
    # ===============================
    def start_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            return
        self._refresh_mode()

        if not self.mode_ctx.license_status.active:
            self.status_label.config(
                text="No active license. Start trial or activate license.",
                fg="red",
            )
            self.show_alert("No active license.", "red")
            self.start_btn.config(state="disabled")
            return

        if self.features.live_trading_enabled and not self.api_keys_ready:
            self.open_api_window(require_confirm=True)
            if not self.api_keys_ready:
                return

        if self.features.live_trading_enabled:
            from ip_address.ip_address import start_ip_monitor
            if self.ip_monitor is None:
                self.ip_monitor = start_ip_monitor(
                    check_interval_seconds=60,
                    whitelist_cache_ttl_seconds=300,
                )
            else:
                self.ip_monitor.start()

        if self.mode_ctx.mode == Mode.TRIAL:
            signal_config = SignalConfig(
                timelines=list(self.settings.trial_timelines),
                lookback_minutes=int(self.settings.trial_lookback_minutes),
                refresh_db=False,
            )
        else:
            signal_config = SignalConfig(
                timelines=[5, 15, 30, 60, 240, 1440, 10080],
                lookback_minutes=1440 * 30,
                refresh_db=True,
            )

        from bot.trading_bot import TradingBot

        self.bot = TradingBot(
            on_signal=self.update_signal,
            on_price=self.update_price,
            on_alert=self.show_alert,
            features=self.features,
            signal_config=signal_config,
        )

        self.bot_thread = threading.Thread(
            target=self.bot.run_trading_bot,
            daemon=True
        )
        self.bot_thread.start()
        if self.mode_ctx.mode == Mode.PARTNER:
            self._schedule_ip_status_refresh(immediate=True)

        if self.mode_ctx.mode == Mode.TRIAL:
            self.status_label.config(text="Signals running (Trial)...", fg="orange")
        else:
            self.status_label.config(text="Bot running...", fg="green")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def stop_bot(self):
        if not self._is_bot_running():
            self._refresh_mode()
            return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="Stopping bot...", fg="orange")

        threading.Thread(
            target=self._stop_bot_background,
            daemon=True,
        ).start()


# ===============================
# ENTRY POINT
# ===============================
if __name__ == "__main__":
    root = tk.Tk()
    app = TradingBotGUI(root)
    root.mainloop()





