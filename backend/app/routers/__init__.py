"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, rollback

# rollback 提供 /api/rollback/* 固定路径，放在通用资源路由之前注册，
# 避免被 /api/{module} 动态路径抢先匹配。
ROUTERS = [rollback, resource]
