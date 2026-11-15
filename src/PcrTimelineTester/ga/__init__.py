"""语义 UB 遗传算法模式。

三个模块各管一块：`semantics` 是五条角色 UB 序列的基因编码与搜索池，
`fingerprint` 是 ActionProgress 行为指纹与距离，`engine` 是多岛屿遗传算法。
三者都是同步实现（客户端用同步 httpx，服务器本身并行算），
基因编码、搜索池、行为去重、岛屿 GA 的规则互相独立、可以单独测试。
"""

from __future__ import annotations

__all__ = ["engine", "evaluator", "fingerprint", "settings", "semantics"]
