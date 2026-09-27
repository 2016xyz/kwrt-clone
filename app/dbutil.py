"""SQLite 连接收口 —— 让 `with db() as c:` 真正**提交并关闭**。

背景（本轮审计实测的 DoS）
--------------------------
各模块原先的 `db()` 都是：

    def db():
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        return c

调用方统一写 `with db() as c:`。问题在于 **sqlite3.Connection 的
上下文管理器只负责 commit/rollback，并不关闭连接** —— 于是文件描述符
（以及连接持有的锁与内存）要等对象被回收才释放。

实测（Python 3.13，关掉自动 GC 以模拟负载下 GC 尚未运行的窗口）：

    500  次 `with db() as c:` → 泄漏 500  fd
    2000 次 `with db() as c:` → 泄漏 2000 fd   ← 线性，1:1
    gc.collect() 之后才回落

这不是「CPython 引用计数会兜底」的那种纸面问题：连接对象处在**循环引用**里
（connection ⇄ 语句缓存/游标），所以只能靠分代 GC 回收，而 GC 是按分配阈值
触发的 —— 在高并发或密集 DB 操作下，fd 会先涨起来。默认 `ulimit -n` 为
1024 的机器上，几百次请求即可耗尽 fd，之后整个进程无法再打开任何文件
（连数据库都打不开），属于**进程级拒绝服务**。

修法
----
用连接工厂让 `__exit__` 在提交/回滚之后**顺手关闭**连接。
这样 110+ 处既有调用点**一行都不用改**，语义还更正确：

    with db() as c:      # 现在 = 提交 + 关闭
        ...
"""
from __future__ import annotations

import sqlite3


class ClosingConnection(sqlite3.Connection):
    """`with` 退出时先提交/回滚，再关闭连接（原生 Connection 只做前半步）。"""

    def __exit__(self, exc_type, exc, tb):
        try:
            # 原生语义：无异常则 commit，有异常则 rollback
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()
