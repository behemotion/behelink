from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEHELINK_")

    database_path: str = "behelink.db"
    heartbeat_ttl_seconds: int = 180
    registration_rate_per_hour: int = 10
    host: str = "127.0.0.1"
    port: int = 47150
