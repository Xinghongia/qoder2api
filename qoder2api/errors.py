"""qoder2api.errors —— 由 qoder_proxy.py 拆分而来。"""






class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""

    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length


class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""


class UpstreamStatus(Exception):
    """Raised when the upstream SSE envelope carries a non-200 status."""

    def __init__(self, status, detail=""):
        super(UpstreamStatus, self).__init__("upstream status %s" % status)
        self.status = status
        self.detail = str(detail or "")
