class TrajectoryError(Exception):
    """An actionable conversion, storage, or cleanup failure."""

    def __init__(self, message: str, *, code: int = 10, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
