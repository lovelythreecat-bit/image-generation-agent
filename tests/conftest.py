import socket
import sys

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original_connect = socket.socket.connect

    def denied(sock, *args, **kwargs):
        caller = sys._getframe(1)
        if caller.f_globals.get("__name__") == "socket" and "socketpair" in caller.f_code.co_name:
            return original_connect(sock, *args, **kwargs)
        raise AssertionError("Tests must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def request_data():
    return dict(
        product_name="Cup",
        category="kitchen",
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[dict(material_id="m1", source={"data": b"x"})],
    )
