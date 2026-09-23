"""Simulate a machine with no internet: block every non-loopback socket.

Proves the upload + diff path never attempts an outbound connection.
"""

import socket

_orig_connect = socket.socket.connect
_orig_create = socket.create_connection


def _allowed(host: str) -> bool:
    return host.startswith("127.") or host in ("localhost", "::1", "0.0.0.0")


def guard(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else str(address)
    if not _allowed(host):
        raise OSError(f"NETWORK BLOCKED: tried to connect to {host}")
    return _orig_connect(self, address, *args, **kwargs)


def guard_create(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else str(address)
    if not _allowed(host):
        raise OSError(f"NETWORK BLOCKED: tried to connect to {host}")
    return _orig_create(address, *args, **kwargs)


def guard_dns(*args, **kwargs):
    raise OSError("DNS BLOCKED")


socket.socket.connect = guard
socket.create_connection = guard_create
socket.getaddrinfo = guard_dns

from airshop.ndc.catalog import MessageCatalog, review_update  # noqa: E402

catalog = MessageCatalog("/tmp/offlinetest/msgs")
catalog.register("baseline_rq", "/tmp/offlinetest/airshopping_rq.xml")
catalog.register("baseline_rs", "/tmp/offlinetest/airshopping_rs_baseline.xml")
catalog.register("second_rs", "/tmp/offlinetest/airshopping_rs_v2.xml")

review = review_update(
    catalog.resolve("baseline_rs"),
    catalog.resolve("second_rs"),
    catalog.resolve("baseline_rq"),
)
print(review.render())
print()
print("=== OFFLINE TEST PASSED: no outbound connection attempted ===")
