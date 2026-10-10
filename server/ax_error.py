#!/usr/bin/env python3
"""AX 错误的类型与语义名 —— 单独一个极小的模块。

工具服务器每次调用都要 `except AXBridgeError`，但那是不是需要整套 ctypes/AX 实现；
把类型放这儿，启动时只需要 import 这个文件（几百字节），真正的 ax_bridge 推迟到
第一个用元素路径的工具才加载。
"""


class AXBridgeError(Exception):
    """带语义错误码的桥接异常；str() 直接就是给模型看的 `ERROR[CODE]: 说明`。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"ERROR[{code}]: {detail}" if detail else f"ERROR[{code}]")
        self.code, self.detail = code, detail


ERROR_NAMES = {
    -25200: "STALE_STATE",
    -25201: "ELEMENT_UNAVAILABLE",
    -25202: "STALE_STATE",
    -25204: "CANNOT_COMPLETE",
    -25205: "NOT_IMPLEMENTED",
    -25206: "NOTIFICATION_UNSUPPORTED",
    -25207: "NOTIFICATION_ALREADY_REGISTERED",
    -25208: "ATTRIBUTE_UNSUPPORTED",
    -25209: "ACTION_UNSUPPORTED",
    -25210: "NOT_ENOUGH_PRECISION",
    -25211: "ACTION_UNSUPPORTED",
    1: "AX_FAILURE",
    2: "INVALID_ARGUMENT",
}


def err_name(err: int) -> str:
    return ERROR_NAMES.get(err, f"AX_ERROR_{err}")
