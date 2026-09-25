"""qe.ai test guards: no test may reach a network or construct an AWS client.

Every qe.ai test runs against the deterministic FakeLLM. These autouse patches
make an accidental real call fail loudly instead of spending money.
"""

import socket

import pytest


def _refuse(*_args, **_kwargs):
    raise RuntimeError("network / AWS access is forbidden in qe.ai tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    try:
        import boto3
    except ImportError:
        return
    monkeypatch.setattr(boto3, "client", _refuse)
