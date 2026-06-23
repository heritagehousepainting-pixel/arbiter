"""`python -m mirofish` — launch the A2 service on the configured host/port.

Enforces the loopback guard BEFORE binding (a non-loopback host aborts with a
clear error) and starts uvicorn against the FastAPI app built from the env
config. Run the offline `--fake-llm` mode with:

    MIROFISH_FAKE_LLM=1 python -m mirofish

ISOLATION: never imports arbiter.
"""
from __future__ import annotations

import sys

from mirofish.config import LOOPBACK_HOSTS, Config


def main() -> int:
    config = Config.from_env()

    if config.host not in LOOPBACK_HOSTS:
        sys.stderr.write(
            f"refusing to bind non-loopback host {config.host!r}: MiroFish A2 "
            f"must bind loopback only (one of {sorted(LOOPBACK_HOSTS)}).\n"
        )
        return 2

    import uvicorn

    from mirofish.app import create_app

    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
