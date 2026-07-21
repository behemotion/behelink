import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI

from behelink import db
from behelink.config import Settings
from behelink.errors import install_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="behelink", docs_url=None, redoc_url=None)
    app.state.settings = settings
    install_handlers(app)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    return app
