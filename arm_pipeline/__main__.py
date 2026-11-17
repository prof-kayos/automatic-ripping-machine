"""python3 -m arm_pipeline daemon|status|approve|reject|identify|retry|skip ...

Secrets are read from the environment; for interactive use the owner's ~/.env is loaded if present (never printed).
"""
from __future__ import annotations

import logging
import os
import sys

from .config import Config


def _load_env_file(path="~/.env") -> None:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _load_env_file()
    cfg = Config.from_env()
    cmd = argv[0] if argv else "status"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if cmd == "daemon":
        from . import daemon
        daemon.run(cfg)
        return 0
    from .ledger import Ledger
    led = Ledger(cfg.state_path)
    if cmd in ("status", "list"):
        for r in sorted(led.all(), key=lambda r: r.updated):
            print(f"{r.fingerprint}  {r.state:18} {r.title or r.label:40} {r.message[:70]}")
        return 0
    if len(argv) < 2:
        print("usage: arm_pipeline daemon | status | approve FP | reject FP | identify FP \"Title (Year)\" | retry FP (also undoes a skip) | skip FP\nrun from the install dir with the env loaded: cd /opt/arm && set -a && . ~/.env && set +a")
        return 2
    fp = argv[1]
    from . import operator as ops
    if not ops.valid_fp(fp):
        print("that is not a disc fingerprint (16 hex characters); run `status` to list them")
        return 2
    if cmd in ("approve", "reject", "retry", "skip"):
        res = getattr(ops, cmd)(cfg, led, fp)
    elif cmd == "identify":
        import re
        m = re.match(r"^\s*(.+?)\s*\((\d{4})\)\s*$", argv[2] if len(argv) > 2 else "")
        if not m:
            print('give the movie as "Title (Year)"')
            return 2
        res = ops.identify(cfg, led, fp, m.group(1), int(m.group(2)))
    else:
        print("unknown command", cmd)
        return 2
    if not res["ok"]:
        print(res["message"])
        return 2
    if res.get("ejected"):
        print("ejected:", res["ejected"])
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
