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
