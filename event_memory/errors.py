class MemoryError(Exception):
    """可向用户展示的业务错误，不携带密钥或完整请求。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class InvalidOutput(MemoryError):
    def __init__(self, message: str):
        super().__init__("invalid_output", message)
