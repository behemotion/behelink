import httpx
import pytest

from behelink.config import Settings
from behelink.main import create_app

CLIENT_IP = "203.0.113.10"


@pytest.fixture
def app(tmp_path):
    settings = Settings(database_path=str(tmp_path / "behelink.db"))
    return create_app(settings)


def make_client(app, ip: str = CLIENT_IP) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 41000))
    return httpx.AsyncClient(transport=transport, base_url="http://behelink.test")


@pytest.fixture
async def client(app):
    async with make_client(app) as c:
        yield c
