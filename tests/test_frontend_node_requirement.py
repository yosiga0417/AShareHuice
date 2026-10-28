"""在 CI 等必须执行前端逻辑的环境里，node 缺失不能变成「测试全绿但一条没跑」。

本地开发默认允许跳过（未安装 node 时 K 线视图测试会 skip）；CI 或显式设置
BACKTEST_REQUIRE_NODE=1 时，本测试会失败并给出明确提示。
"""
from __future__ import annotations

import os
import shutil
import unittest


class NodeRequirementTests(unittest.TestCase):
    def test_node_available_when_required(self) -> None:
        require_node = os.environ.get("BACKTEST_REQUIRE_NODE") == "1" or \
            os.environ.get("CI", "").lower() in ("1", "true", "yes")
        if not require_node:
            self.skipTest("未要求 node（本地开发可跳过前端测试）")
        self.assertIsNotNone(
            shutil.which("node"),
            "当前环境要求执行前端 K 线测试，但没有安装 node；"
            "请安装 node，或取消 CI/BACKTEST_REQUIRE_NODE 后再运行。",
        )


if __name__ == "__main__":
    unittest.main()
