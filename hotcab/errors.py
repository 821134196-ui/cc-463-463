class HotCabError(Exception):
    """业务错误基类，映射为 HTTP 400。"""


class NotFoundError(HotCabError):
    """资源不存在，HTTP 404。"""


class ValidationError(HotCabError):
    """参数不合法。"""


class StateError(HotCabError):
    """当前批次状态不允许该操作（含已报损、超卖等），HTTP 409。"""


class ConflictError(HotCabError):
    """乐观锁冲突：提交基于旧版本，HTTP 409。"""

    def __init__(self, message="批次已被他人修改，请刷新后基于最新版本提交", expected_version=None):
        super().__init__(message)
        self.expected_version = expected_version


class AuthError(HotCabError):
    """权限不足（店员越权使用店长例外价等），HTTP 403。"""
