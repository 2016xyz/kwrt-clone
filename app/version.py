"""版本号 —— 单一真源。

★ 版本只在这里（以及仓库根的同名 VERSION 文件）定义一次：
  - Python 读 app/version.py 的 VERSION
  - PHP 读仓库根的 VERSION 文件（php/src/Version.php）
  两版必须显示同一个数字，否则「后台显示的版本」这件事本身就不可信。

发版流程：改仓库根的 VERSION 文件即可，两版同时生效（不必改两处代码）。
"""
from __future__ import annotations

import os

#: 当前版本。与仓库根 VERSION 文件保持同步（tests 会校验两者一致）。
VERSION = "1.0.7"

#: 展示用（后台/页脚/API 统一用这个格式）
def display() -> str:
    return "v" + VERSION


def version_file() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
