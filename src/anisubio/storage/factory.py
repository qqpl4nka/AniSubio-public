from anisubio.config import Settings
from anisubio.storage.telegram import BotApiTransport, TelegramStorage


def create_telegram_transport(settings: Settings) -> BotApiTransport:
    return BotApiTransport(
        token=settings.telegram_bot_token,
        proxy_url=settings.telegram_proxy_url,
        api_base=settings.telegram_api_base,
        timeout_seconds=max(settings.request_timeout_seconds, 60),
    )


def create_telegram_storage(
    settings: Settings,
    *,
    backup: bool = False,
) -> TelegramStorage:
    channel_id = (
        settings.telegram_backup_channel_id
        if backup
        else settings.telegram_storage_channel_id
    )
    if channel_id is None:
        kind = "backup" if backup else "storage"
        raise ValueError(f"Telegram {kind} channel ID is not configured")
    return TelegramStorage(
        create_telegram_transport(settings),
        channel_id=channel_id,
        upload_interval_seconds=settings.telegram_upload_interval_seconds,
        rate_limit_file=settings.telegram_rate_limit_file,
    )
