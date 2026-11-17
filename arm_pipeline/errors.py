"""Exception types shared by the pipeline modules."""


class PipelineError(Exception):
    """Base class. `step` names the pipeline step for the Slack failure message."""
    step = "pipeline"

    def __init__(self, message: str, step: str | None = None):
        super().__init__(message)
        if step:
            self.step = step


class VerifyError(PipelineError):
    step = "verify"


class TransferError(PipelineError):
    step = "transfer"


class SwapError(PipelineError):
    step = "swap"


class ServerError(PipelineError):
    step = "media-server"


class MetadataError(PipelineError):
    step = "metadata"


class DriveError(PipelineError):
    step = "drive"


def cli(sub: str, fp: str = "", extra: str = "") -> str:
    """A command line the owner can paste in a shell on the ripper and it will work: the package is only importable from its
    install directory (the unit sets PYTHONPATH=/opt/arm), and the settings come from ~/.env. A bare `python3 -m arm_pipeline ...`
    fails with 'No module named arm_pipeline'."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tail = " ".join(x for x in (sub, fp, extra) if x)
    return f"cd {root} && set -a && . ~/.env && set +a && python3 -m arm_pipeline {tail}"
