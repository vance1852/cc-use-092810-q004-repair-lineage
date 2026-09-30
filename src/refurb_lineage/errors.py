"""谱系服务向 API 和 CLI 暴露的稳定错误。"""


class LineageError(RuntimeError):
    code = "lineage_error"
    status = 400


class NotFound(LineageError):
    code = "not_found"
    status = 404


class Conflict(LineageError):
    code = "conflict"
    status = 409


class Forbidden(LineageError):
    code = "forbidden"
    status = 403


class InvalidState(LineageError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LineageError):
    code = "validation_failed"
    status = 422
