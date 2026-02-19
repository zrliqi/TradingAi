import socket

socket.setdefaulttimeout(30)

import os
import ctypes
import time
import sqlite3
import asyncio
import threading
from dataclasses import dataclass

import requests
from playsound import playsound

from resource_path import resource_path
from dataframe.db_dataframe import GetDbDataframe
from api_callling.api_calling import APICall
from order_book.market_order import MarketOrder
from order_book.long_stop_loss import LongStopLoss
from order_book.short_stop_loss import ShortStopLoss
from risk_management.progressive_trailing_stop import ProgressiveTrailingStop
from risk_management.safe_entry import SafeEntry
from all_variable import Variable
from sounds.sound_engine import SoundEngine
from app.feature_flags import FeatureFlags
from app.db_manager import DatabaseManager
from app.profit_share import ProfitShareLedger, ProfitShareMonitor, ProfitSharePolicy

Signal = Variable.ENTRY_SIGNAL
laverage = Variable.LAVARAGE

sound = SoundEngine()
LAST_SPOKEN_SIGNAL = None


@dataclass(frozen=True)
class SignalConfig:
    timelines: list[int]
    lookback_minutes: int
    refresh_db: bool = True


class TradingBot:
    PUBLIC_PRICE_ENDPOINTS = (
        "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://fapi1.binance.com/fapi/v1/ticker/price",
        "https://fapi2.binance.com/fapi/v1/ticker/price",
        "https://fapi3.binance.com/fapi/v1/ticker/price",
    )

    def __init__(
        self,
        on_signal=None,
        on_price=None,
        on_alert=None,
        features: FeatureFlags | None = None,
        signal_config: SignalConfig | None = None,
    ):
        if os.name == "nt":
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )

        self.TRADE_ACTIVE = False
        self.LAST_SPOKEN_PRICE = None
        self.LAST_SPOKEN_SIGNAL = None

        if features is None:
            features = FeatureFlags(
                database_unlocked=True,
                live_trading_enabled=True,
                api_section_visible=True,
                automation_enabled=True,
                auto_profit_share_enabled=False,
            )
        self.features = features

        if signal_config is None:
            signal_config = SignalConfig(
                timelines=[5, 15, 30, 60, 240, 1440, 10080],
                lookback_minutes=1440 * 30,
                refresh_db=True,
            )
        self.signal_config = signal_config

        self.sound = SoundEngine()

        self.api = None
        self.client = None
        self.safe_entry = None
        self.long_sl = None
        self.short_sl = None
        self.trader = None
        self.trailing_engine = None
        self.profit_share_monitor = None

        if self.features.live_trading_enabled or self.features.auto_profit_share_enabled:
            self.api = APICall()
            self.client = self.api.client
            self.safe_entry = SafeEntry(client=self.client)

            self.long_sl = LongStopLoss(self.client)
            self.short_sl = ShortStopLoss(self.client)

            self.trader = MarketOrder(
                self.client,
                self.long_sl,
                self.short_sl,
                on_alert=on_alert,
            )
            self.trailing_engine = ProgressiveTrailingStop(self.client)

            if self.features.auto_profit_share_enabled:
                ledger = ProfitShareLedger()
                policy = ProfitSharePolicy()
                self.profit_share_monitor = ProfitShareMonitor(
                    client=self.client,
                    symbol="BTCUSDT",
                    ledger=ledger,
                    policy=policy,
                )

        self.database = os.path.abspath(Variable.DATABASE)
        self.db_manager = DatabaseManager(self.database)

        self.target_symbol = "BTCUSDT"
        self.timelines = list(self.signal_config.timelines)
        self.lookback = int(self.signal_config.lookback_minutes)

        self.on_signal = on_signal
        self.on_price = on_price
        self.on_alert = on_alert
        self._db_not_ready_logged = False
        self._db_not_ready_alerted = False
        self._last_known_price = None
        self._stop_event = threading.Event()

        if self.features.database_unlocked:
            self.db_manager.initialize_full_async(self.target_symbol)
        else:
            self.db_manager.ensure_lightweight()

    def wait_safe_entry(self) -> bool:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._wait())
        finally:
            loop.close()

    async def _wait(self):
        while (
            not self._stop_event.is_set()
            and self.safe_entry.active
            and not self.safe_entry.confirmed
            and not self.safe_entry.timed_out
        ):
            await asyncio.sleep(0.1)
        return self.safe_entry.confirmed

    @staticmethod
    def flatten_indicators(row):
        result = []
        for item in row:
            if isinstance(item, list):
                result.extend(item)
        return result

    def _read_latest_price_from_db(self):
        conn = None
        try:
            conn = sqlite3.connect(self.database)
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='asset_1'"
            )
            if cur.fetchone() is None:
                return None, None

            db = GetDbDataframe(conn)
            price_data = db.get_minute_data(self.target_symbol, 1, 1)
            if price_data.empty:
                return None, None

            last_close = float(price_data["Close"].iloc[-1])
            last_close_time = price_data.index[-1]
            return last_close, last_close_time
        except Exception:
            return None, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_live_price(self):
        if self.client is not None:
            try:
                mark = self.client.futures_mark_price(symbol=self.target_symbol)
                return float(mark.get("markPrice"))
            except Exception:
                pass

        for endpoint in self.PUBLIC_PRICE_ENDPOINTS:
            try:
                resp = requests.get(
                    endpoint,
                    params={"symbol": self.target_symbol},
                    timeout=6,
                )
                resp.raise_for_status()
                payload = resp.json()
                return float(payload.get("price"))
            except Exception:
                continue

        return None

    def _resolve_latest_price(self):
        last_close, last_close_time = self._read_latest_price_from_db()
        if last_close is not None:
            self._last_known_price = last_close
            return last_close, last_close_time

        live_price = self._read_live_price()
        if live_price is not None:
            self._last_known_price = live_price
            return live_price, None

        if self._last_known_price is not None:
            return self._last_known_price, None

        return None, None

    def _emit_price(self, last_close, last_close_time):
        if last_close is None:
            print("BTCUSDT Price -> unavailable")
            return

        if last_close_time is not None and hasattr(last_close_time, "strftime"):
            close_time_str = last_close_time.strftime("%Y-%m-%d %H:%M")
            print(f"BTCUSDT Price -> {last_close:.2f} | Close: {close_time_str}")
        else:
            print(f"BTCUSDT Price -> {last_close:.2f}")

        if self.on_price:
            self.on_price(last_close)

    def _run_cycle(self):
        last_close, last_close_time = self._resolve_latest_price()

        if not self.db_manager.has_required_tables(self.timelines):
            if not self._db_not_ready_logged:
                print("[WARN] Database tables not ready for signals. Waiting...", flush=True)
                self._db_not_ready_logged = True
            if self.on_alert and not self._db_not_ready_alerted:
                self.on_alert("Database not ready for signals.", "orange")
                self._db_not_ready_alerted = True
            self._emit_price(last_close, last_close_time)
            return

        self._db_not_ready_logged = False
        self._db_not_ready_alerted = False

        if self.signal_config.refresh_db and self.features.database_unlocked:
            from database.missing_data_single_symbol import MissingDataCollection

            MissingDataCollection(database=self.database).collect_missing_data_single_symbols(
                self.target_symbol
            )

        final_signal = 0
        indicator_frames = []

        for timeline in self.timelines:
            conn = sqlite3.connect(self.database)
            try:
                db = GetDbDataframe(conn)
                data = db.get_minute_data(self.target_symbol, timeline, self.lookback)
                df = db.get_all_indicators(self.target_symbol, timeline, self.lookback)
            finally:
                conn.close()

            if data.empty or df.empty:
                continue

            if last_close is None:
                last_close = float(data["Close"].iloc[-1])
                last_close_time = data.index[-1]

            df.index = data.index
            df = df.add_prefix(f"{timeline}_")

            data[f"Sum_{timeline}m"] = df.sum(axis=1)
            last_sum = data[f"Sum_{timeline}m"].iloc[-1]
            final_signal += int(last_sum)

            indicator_frames.append(data.tail(1)[[f"Sum_{timeline}m"]])

        global LAST_SPOKEN_SIGNAL

        print(f"FINAL Signal Sum -> {final_signal}")
        self._emit_price(last_close, last_close_time)

        if self.on_signal:
            self.on_signal(final_signal)

        if LAST_SPOKEN_SIGNAL != final_signal:
            sound.voice_alert(f"Signal {final_signal}")
            LAST_SPOKEN_SIGNAL = final_signal

        if (
            self.features.live_trading_enabled
            and self.safe_entry
            and not self.safe_entry.active
            and not self.TRADE_ACTIVE
            and final_signal >= Signal
        ):
            print("LONG signal")
            self.safe_entry.long()
            if self.wait_safe_entry():
                try:
                    self.trader.long("BTCUSDT", 1, laverage)
                    self.trailing_engine.start()
                    playsound(resource_path(os.path.join("sounds", "Bullish.wav")))
                    self.TRADE_ACTIVE = True
                except Exception as e:
                    print(f"LONG order failed: {e}", flush=True)

        elif (
            self.features.live_trading_enabled
            and self.safe_entry
            and not self.safe_entry.active
            and not self.TRADE_ACTIVE
            and final_signal <= -(int(Signal))
        ):
            print("SHORT signal")
            self.safe_entry.short()
            if self.wait_safe_entry():
                try:
                    self.trader.short("BTCUSDT", 1, laverage)
                    self.trailing_engine.start()
                    playsound(resource_path(os.path.join("sounds", "Bearish.wav")))
                    self.TRADE_ACTIVE = True
                except Exception as e:
                    print(f"SHORT order failed: {e}", flush=True)

    def stop(self):
        self._stop_event.set()

        if self.safe_entry:
            self.safe_entry.active = False

        if self.profit_share_monitor:
            try:
                self.profit_share_monitor.stop()
            except Exception:
                pass

        if self.trailing_engine is not None and hasattr(self.trailing_engine, "running"):
            self.trailing_engine.running = False

    def run_trading_bot(self):
        print("Trading Bot Started")
        self._stop_event.clear()
        start = time.monotonic()
        if self.profit_share_monitor:
            self.profit_share_monitor.start()

        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            self._run_cycle()

            elapsed = time.monotonic() - loop_start
            sleep_time = max(1, 60 - elapsed)

            print(f"Loop: {elapsed:.2f}s | Sleep: {sleep_time:.2f}s")
            if self._stop_event.wait(sleep_time):
                break

            runtime = (time.monotonic() - start) / 60
            print(f"Runtime: {runtime:.1f} minutes")

        print("Trading Bot Stopped")
