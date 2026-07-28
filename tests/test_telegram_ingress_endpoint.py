from behelink.config import Settings


def test_telegram_settings_have_documented_defaults():
    settings = Settings(_env_file=None)
    assert settings.public_base_url == "https://link.behemotion.com"
    assert settings.telegram_relay_enabled is True
    assert settings.telegram_queue_max_depth == 1000
    assert settings.telegram_queue_ttl_seconds == 3600
    assert settings.telegram_updates_wait_max_seconds == 25.0
    assert settings.telegram_updates_max_batch == 100
    assert settings.telegram_webhook_rate_per_minute == 120
    assert settings.telegram_max_payload_bytes == 1048576
