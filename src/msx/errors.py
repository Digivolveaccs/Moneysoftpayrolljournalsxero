"""Exceptions shared across the pipeline."""


class Hold(Exception):
    """Routine outcome: this client-period needs a human, not a journal.

    A Hold is never a bug. It is raised whenever a figure cannot be proven,
    a mapping is missing, or the world is not in the state the pipeline
    expects. The message must say exactly what a human should do next.
    """

    def __init__(self, message, *, stage=None, details=None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.details = details or {}

    def __str__(self):
        return self.message


class Skip(Exception):
    """Nothing to do for this client-period (nil payroll, already posted)."""

    def __init__(self, message, *, reason=None):
        super().__init__(message)
        self.message = message
        self.reason = reason or message

    def __str__(self):
        return self.message
