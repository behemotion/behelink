from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEHELINK_")

    database_path: str = "behelink.db"
    heartbeat_ttl_seconds: int = 180
    registration_rate_per_hour: int = 10
    host: str = "127.0.0.1"
    port: int = 47150
    reflector_host: str = "0.0.0.0"
    reflector_port: int = 47151
    reflector_rate_per_minute: int = 20
    reflector_max_payload_bytes: int = 512
    request_connect_rate_per_minute: int = 10
    pending_connect_ttl_seconds: float = 10.0
    pending_connect_wait_max_seconds: float = 25.0
