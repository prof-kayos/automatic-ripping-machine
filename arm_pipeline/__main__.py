"""python3 -m arm_pipeline daemon|status|approve|reject|identify|retry|skip ...

Secrets are read from the environment; for interactive use the owner's ~/.env is loaded if present (never printed).
"""
from __future__ import annotations

import json
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


def _touch(cfg, kind, fp):
    d = os.path.join(cfg.state_path, kind)
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, fp), "w"):
        pass


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
        print("usage: arm_pipeline daemon | status | approve FP | reject FP | identify FP \"Title (Year)\" | retry FP | skip FP")
        return 2
    fp = argv[1]
    import re
    if not re.match(r"^[0-9a-f]{16}$", fp):
        print("that is not a disc fingerprint (16 hex characters); run `status` to list them")
        return 2
    rec = led.get(fp)
    if cmd in ("approve", "reject"):
        if rec is None or rec.state != "awaiting_approval":
            print(f"nothing to {cmd}: that disc is not waiting for approval (state: {rec.state if rec else 'unknown'})")
            return 2
        _touch(cfg, cmd, fp)
    elif cmd == "retry":
        led.retry(fp)
    elif cmd == "skip":
        if rec is None or rec.state != "awaiting_identity":
            print("skip only applies to a disc waiting for an answer (awaiting_identity)")
            return 2
        led.set_state(fp, "skipped", "skipped by owner")
        from . import drive
        if drive.disc_present(cfg.sr_dev):
            print("ejected:", drive.eject(cfg))
    elif cmd == "identify":
        import re
        m = re.match(r"^\s*(.+?)\s*\((\d{4})\)\s*$", argv[2] if len(argv) > 2 else "")
        if not m:
            print('give the movie as "Title (Year)"')
            return 2
        title, year = m.group(1), int(m.group(2))
        os.makedirs(os.path.join(cfg.state_path, "identify"), mode=0o700, exist_ok=True)
        with open(os.path.join(cfg.state_path, "identify", fp + ".json"), "w") as fh:
            json.dump({"title": title, "year": year}, fh)
    else:
        print("unknown command", cmd)
        return 2
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
