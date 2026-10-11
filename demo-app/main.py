import importlib
import os
import signal
import sys

ROLES = {
    "api": "app.api",
    "worker": "app.worker",
    "payments-gw": "app.payments",
    "loadgen": "app.loadgen",
    "slo-probe": "app.probe",
    "wait": "app.wait",
}


def main():
    role = os.environ.get("ROLE", "")
    if role not in ROLES:
        sys.exit(f"unknown ROLE {role!r}; expected one of: {', '.join(ROLES)}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    importlib.import_module(ROLES[role]).run()


if __name__ == "__main__":
    main()
