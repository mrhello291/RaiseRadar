import httpx
import pytest

from raiseradar.db import DB
from raiseradar.http import Fetcher


@pytest.fixture
def db():
    d = DB(":memory:")
    yield d
    d.close()


def make_fetcher(handler, robots=False):
    f = Fetcher("TestBot/1.0", timeout=5, min_delay=0, respect_robots=robots, transport=httpx.MockTransport(handler))
    f.backoff = 0
    return f


def json_response(data, status=200):
    return httpx.Response(status, json=data)


def html_response(html, status=200):
    return httpx.Response(status, text=html, headers={"content-type": "text/html; charset=utf-8"})
