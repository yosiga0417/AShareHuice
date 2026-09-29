"""静态前端路由：让 `python3 backend.py` 启动后直接访问 / 就能打开页面。"""
from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.routing import Mount

from backend.api import FRONTEND_DIR, _frontend_file, app, health

EXPECTED_PATHS = {"/", "/index.html", "/app.js", "/styles.css"}


class FrontendStaticRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = {route.path for route in app.routes if hasattr(route, "path")}

    def test_frontend_routes_registered(self) -> None:
        self.assertTrue(
            EXPECTED_PATHS.issubset(self.paths),
            f"缺少静态前端路由：{EXPECTED_PATHS - self.paths}",
        )

    def test_api_routes_not_shadowed(self) -> None:
        self.assertIn("/api/health", self.paths)
        self.assertIn("/api/backtest/tasks", self.paths)

    def test_health_identifies_frontend_service(self) -> None:
        payload = health()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data_source"], "AKShare")
        self.assertIs(payload["frontend_served"], True)

    def test_vendor_mounted(self) -> None:
        mounts = {
            route.path: route
            for route in app.routes
            if isinstance(route, Mount)
        }
        self.assertIn("/vendor", mounts)
        self.assertTrue((FRONTEND_DIR / "vendor" / "echarts.min.js").is_file())

    def test_frontend_dir_points_at_project_root(self) -> None:
        self.assertTrue((FRONTEND_DIR / "index.html").is_file())
        self.assertTrue((FRONTEND_DIR / "backend" / "api.py").is_file())

    def test_assets_resolve_to_local_files(self) -> None:
        for name in ("index.html", "app.js", "styles.css"):
            response = _frontend_file(name)
            self.assertIsInstance(response, FileResponse)
            self.assertEqual(Path(response.path).resolve(), (FRONTEND_DIR / name).resolve())

    def test_missing_asset_raises_404(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _frontend_file("does-not-exist.txt")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_source_and_cache_are_not_exposed(self) -> None:
        # 仅注册前端白名单文件，backend/ 源码与 .cache/ 数据不能通过 HTTP 访问。
        for leaked in ("/backend/api.py", "/requirements.txt", "/.cache", "/start.sh"):
            self.assertNotIn(leaked, self.paths)


if __name__ == "__main__":
    unittest.main()
