class RateLimitError(Exception):
    """Provider rate limit hit (HTTP 429 or equivalent).

    retry_after: seconds to wait before retrying, if provided by the response.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
