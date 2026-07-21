import uvicorn

from behelink.config import Settings
from behelink.main import create_app


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Caddy fronts behelink on the same host; trust its X-Forwarded-For so
        # the observed client IP is the real public address, not 127.0.0.1.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
