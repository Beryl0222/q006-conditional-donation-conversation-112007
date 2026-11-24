"""有条件捐赠承诺的会话确认与交接引擎。

模块分工：
- model:    结构化数据类型与状态常量
- parser:   中文群消息/邮件解析，简称、引用、省略落到具体前文
- analyzer: 按发送顺序重核承诺、条件、授权、交接单与争议
- views:    按各方权限过滤消息引用后的交接视图
- cli:      命令行入口
"""

from .analyzer import Analyzer, Evaluation
from .model import Act, Condition, ItemMention, Offer, ParsedMessage
from .parser import parse_message

__all__ = [
    "Analyzer",
    "Evaluation",
    "Act",
    "Condition",
    "ItemMention",
    "Offer",
    "ParsedMessage",
    "parse_message",
]
