import socket

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tests must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def request_data():
    return dict(product_name="Cup", category="kitchen", platforms=["taobao"],
                output_types=["main_image"], materials=[dict(material_id="m1", source={"data": b"x"})])
