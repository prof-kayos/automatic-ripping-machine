"""Optical drive helpers: tray state via ioctl and a LXC-safe eject via SCSI (sg3-utils)."""
from __future__ import annotations

import fcntl
import os
import subprocess
import time

from .errors import DriveError

CDROM_DRIVE_STATUS = 0x5326
_STATUS = {0: "NO_INFO", 1: "NO_DISC", 2: "TRAY_OPEN", 3: "NOT_READY", 4: "DISC_OK"}


def tray_status(dev: str) -> str:
    """Ask the kernel for the drive state. (Never use sysfs size: it goes stale.)"""
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise DriveError(f"cannot open {dev}: {e.strerror}") from e
    try:
        return _STATUS.get(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0), "UNKNOWN")
    except OSError:
        return "UNKNOWN"
    finally:
        os.close(fd)


def disc_present(dev: str) -> bool:
    return tray_status(dev) == "DISC_OK"


def _run(runner, cmd):
    """Run a command; returns returncode, or None when the binary is missing."""
    try:
        return runner(cmd, capture_output=True, text=True, timeout=60).returncode
    except FileNotFoundError:
        return None
    except subprocess.SubprocessError:
        return 1


def eject(cfg, runner=subprocess.run, sleep=time.sleep, tries: int = 10) -> tuple[bool, str]:
    """Eject the disc. In an unprivileged LXC plain `eject` fails ("mounted on"), so use
    SCSI commands on the sg node, falling back to `eject -m` if sg_start is unavailable."""
    _run(runner, ["sg_prevent", "--allow", cfg.sg_dev])        # unlock the tray; best effort
    rc = _run(runner, ["sg_start", "--eject", cfg.sg_dev])
    if rc != 0:                                                # missing tool or command failed
        _run(runner, ["eject", "-m", cfg.sr_dev])
    status = "UNKNOWN"
    for _ in range(tries):
        try:
            status = tray_status(cfg.sr_dev)
        except DriveError:
            status = "UNKNOWN"
        if status in ("TRAY_OPEN", "NO_DISC"):
            return True, status
        sleep(1)
    return False, status
