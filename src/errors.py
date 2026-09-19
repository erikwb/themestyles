"""Stable error categories shared by the CLI, workers and adapters."""

class StylesError(Exception):
    pass


class GenerationError(StylesError):
    def __init__(self, message, code="generation_failed"):
        super().__init__(message)
        self.code = code


class ProcessCancelled(StylesError):
    code = "cancelled"


class ProcessTimedOut(StylesError):
    code = "timeout"


class ProcessFailed(StylesError):
    code = "process_failed"


class ProcessOutputLimit(StylesError):
    code = "output_limit"


class RecordError(StylesError):
    code = "invalid_record"
