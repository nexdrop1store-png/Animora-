"""
Abuse detection: disposable email, VPN/proxy detection, burst trial creation.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import httpx

from .config import settings

log = logging.getLogger("animora.abuse")


@lru_cache(maxsize=1)
def _disposable_domains() -> set[str]:
    try:
        resp = httpx.get(settings.disposable_domain_list_url, timeout=10)
        return set(resp.text.strip().splitlines())
    except Exception as exc:
        log.warning("Could not load disposable domain list: %s", exc)
        return set()


def is_disposable_email(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    return domain in _disposable_domains()


async def is_proxy_or_vpn(ip_address: str) -> bool:
    if not settings.ipqs_api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"https://ipqualityscore.com/api/json/ip/{settings.ipqs_api_key}/{ip_address}",
                params={"strictness": 1},
            )
            data = resp.json()
            return data.get("proxy", False) or data.get("vpn", False) or data.get("tor", False)
    except Exception as exc:
        log.warning("IPQS check failed: %s", exc)
        return False
