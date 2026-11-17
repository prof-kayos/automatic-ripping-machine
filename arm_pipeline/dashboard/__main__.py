"""python3 -m arm_pipeline.dashboard -- run the web dashboard (token from DASHBOARD_TOKEN)."""
from __future__ import annotations

import logging
import sys

from ..__main__ import _load_env_file
from ..config import Config
from .auth import MIN_TOKEN_LEN


def main() -> int:
    _load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if len(cfg.dashboard_token) < MIN_TOKEN_LEN:
        print(f"refusing to start: DASHBOARD_TOKEN must be set to at least {MIN_TOKEN_LEN} characters",
              file=sys.stderr)
        return 2
    from .server import serve
    try:
        serve(cfg)
    except ValueError as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
