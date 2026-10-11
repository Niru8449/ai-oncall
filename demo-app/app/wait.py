import os
import socket
import sys
import time

from app.common import setup_logging

log = setup_logging("wait")


def run():
    target = os.environ.get("WAIT_FOR", "")
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        sys.exit(f"WAIT_FOR must be host:port, got {target!r}")
    while True:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                log.info("%s is reachable", target)
                return
        except OSError as exc:
            log.info("waiting for %s: %s", target, exc)
            time.sleep(2)
