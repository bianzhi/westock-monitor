"""Supabase 用户数据 CRUD — 千人千面核心。

提供：
  - 用户自选板块 watchlist（增删查）
  - 用户告警阈值 user_alerts
  - 用户看板偏好 user_prefs

所有写操作使用 admin client（绕过 RLS），读操作可按需用 user client（RLS 保护）。
未配置 Supabase 时自动降级：返回空列表 / 默认值，不抛错。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from supabase_base import create_admin_client, is_configured

logger = logging.getLogger(__name__)

TABLE_USER_WATCHLIST = "user_watchlist"
TABLE_USER_ALERTS = "user_alerts"
TABLE_USER_PREFS = "user_prefs"


# ============================================================
# 自选板块
# ============================================================
def get_user_watchlist(user_id: str) -> list[str]:
    """获取用户自选板块代码列表。

    Returns:
        [code1, code2, ...]，未配置或无数据返回空列表
    """
    if not is_configured():
        return []
    try:
        client = create_admin_client()
        resp = (
            client.table(TABLE_USER_WATCHLIST)
            .select("code")
            .eq("user_id", user_id)
            .order("created_at", desc=False)
            .execute()
        )
        return [r["code"] for r in (resp.data or [])]
    except Exception as e:
        logger.warning("get_user_watchlist failed: %s", e)
        return []


def add_user_watchlist(user_id: str, codes: list[str]) -> int:
    """批量添加自选板块（去重，不删除已有）。

    Returns:
        新增数量
    """
    if not is_configured() or not codes:
        return 0
    rows = [{"user_id": user_id, "code": c} for c in codes]
    try:
        client = create_admin_client()
        resp = client.table(TABLE_USER_WATCHLIST).upsert(
            rows, on_conflict="user_id,code"
        ).execute()
        return len(resp.data or [])
    except Exception as e:
        logger.warning("add_user_watchlist failed: %s", e)
        return 0


def remove_user_watchlist(user_id: str, codes: list[str]) -> int:
    """批量删除自选板块。

    Returns:
        删除数量
    """
    if not is_configured() or not codes:
        return 0
    try:
        client = create_admin_client()
        resp = (
            client.table(TABLE_USER_WATCHLIST)
            .delete()
            .eq("user_id", user_id)
            .in_("code", codes)
            .execute()
        )
        return len(resp.data or [])
    except Exception as e:
        logger.warning("remove_user_watchlist failed: %s", e)
        return 0


# ============================================================
# 用户告警阈值
# ============================================================
def get_user_alerts(user_id: str) -> dict[str, Any]:
    """获取用户告警阈值配置。

    Returns:
        {"strength_up_threshold": 2, "strength_down_threshold": -2, ...}
    """
    if not is_configured():
        return {}
    try:
        client = create_admin_client()
        resp = (
            client.table(TABLE_USER_ALERTS)
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return resp.data or {}
    except Exception as e:
        logger.warning("get_user_alerts failed: %s", e)
        return {}


def save_user_alerts(user_id: str, alerts: dict[str, Any]) -> bool:
    """保存用户告警阈值。"""
    if not is_configured():
        return False
    row = {"user_id": user_id, **alerts}
    try:
        client = create_admin_client()
        client.table(TABLE_USER_ALERTS).upsert(
            row, on_conflict="user_id"
        ).execute()
        return True
    except Exception as e:
        logger.warning("save_user_alerts failed: %s", e)
        return False


# ============================================================
# 用户看板偏好
# ============================================================
def get_user_prefs(user_id: str) -> dict[str, Any]:
    """获取用户看板偏好（列排序、隐藏、页面大小等）。

    Returns:
        {"columns_visible": [...], "page_size": 50, ...}
    """
    if not is_configured():
        return {}
    try:
        client = create_admin_client()
        resp = (
            client.table(TABLE_USER_PREFS)
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return resp.data or {}
    except Exception as e:
        logger.warning("get_user_prefs failed: %s", e)
        return {}


def save_user_prefs(user_id: str, prefs: dict[str, Any]) -> bool:
    """保存用户看板偏好。"""
    if not is_configured():
        return False
    row = {"user_id": user_id, **prefs}
    try:
        client = create_admin_client()
        client.table(TABLE_USER_PREFS).upsert(
            row, on_conflict="user_id"
        ).execute()
        return True
    except Exception as e:
        logger.warning("save_user_prefs failed: %s", e)
        return False
