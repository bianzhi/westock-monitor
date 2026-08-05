"""Supabase 客户端工厂 — westock-monitor 通用。

所有需要 Supabase 客户端的代码应从此模块获取，而不是各自 create_client。
- API 后端：使用 create_admin_client()（service_role key，绕过 RLS）
- 脚本/定时任务：同理
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supabase import Client

logger = logging.getLogger(__name__)

# 内置 anon 凭据（可通过环境变量 SUPABASE_URL / SUPABASE_KEY 覆盖）
SUPABASE_ANON_URL = ""
SUPABASE_ANON_KEY = ""


def _resolve_credentials() -> tuple[str, str]:
    """解析 Supabase URL 和 Key，统一回退链：环境变量 → 内置 anon key。"""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if url and key:
        return url, key
    url = url or SUPABASE_ANON_URL
    key = key or SUPABASE_ANON_KEY
    return url, key


def create_admin_client():
    """Service-role 客户端（写库用，不经过 RLS）。

    优先读 SUPABASE_SERVICE_ROLE_KEY，回退到通用凭据链。
    """
    from supabase import create_client

    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and service_key:
        return create_client(url, service_key)
    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_KEY 未配置")
    return create_client(url, key)


def create_anon_client():
    """Anon-key 客户端（RLS 保护）。"""
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("Missing Supabase credentials.")
    return create_client(url, key)


def create_user_client(access_token: str):
    """用用户 JWT 创建客户端（通过 RLS）。"""
    from supabase import create_client

    url, key = _resolve_credentials()
    if not url or not key:
        raise ValueError("SUPABASE_URL / SUPABASE_KEY 未配置")
    client = create_client(url, key)
    client.postgrest.auth(access_token)
    return client


def is_configured() -> bool:
    """检查是否存在显式 Supabase 凭据。"""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.getenv("SUPABASE_KEY", "").strip()
    return bool(url and key)
