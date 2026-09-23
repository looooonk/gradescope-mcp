class GradescopeError(Exception):
    def __init__(self, message, code="invalid_request", http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


OPERATIONS = {}
