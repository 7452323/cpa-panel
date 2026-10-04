"""Web 层（内置 HTTP 服务 + 静态前端）。"""

from .server import PanelApp, PanelHandler, run_forever, serve

__all__ = ["PanelApp", "PanelHandler", "serve", "run_forever"]
