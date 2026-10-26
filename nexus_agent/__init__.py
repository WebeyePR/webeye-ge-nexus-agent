import sys
try:
    import lark_agent  # noqa: F401  有意保留：旧包名兼容 shim
except ImportError:
    sys.modules['lark_agent'] = sys.modules[__name__]

from .agent import root_agent

__all__ = ["root_agent"]

