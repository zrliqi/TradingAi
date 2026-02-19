from app.feature_flags import build_feature_flags
from app.license_service import LicenseService
from app.mode_manager import ModeManager
from app.settings import load_settings
from bot.trading_bot import SignalConfig, TradingBot

settings = load_settings()
license_service = LicenseService()
mode_ctx = ModeManager(license_service, settings).resolve()
features = build_feature_flags(mode_ctx)

if mode_ctx.mode == Mode.TRIAL:
    signal_config = SignalConfig(
        timelines=list(settings.trial_timelines),
        lookback_minutes=int(settings.trial_lookback_minutes),
        refresh_db=False,
    )
else:
    signal_config = SignalConfig(
        timelines=[5, 15, 30, 60, 240, 1440, 10080],
        lookback_minutes=1440 * 30,
        refresh_db=True,
    )

bot = TradingBot(features=features, signal_config=signal_config)
bot.run_trading_bot()
