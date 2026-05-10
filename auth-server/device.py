"""Device fingerprint validation and binding enforcement."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.models import Device, User

log = logging.getLogger("animora.device")

PLAN_DEVICE_LIMITS = {
    "trial": settings.devices_trial,
    "standard": settings.devices_standard,
    "studio": settings.devices_studio,
}


async def get_or_create_device(
    db: AsyncSession,
    user: User,
    fingerprint_hash: str,
    platform: str = "",
) -> tuple[Device, bool]:
    """Return (device, is_new). Raises ValueError if device limit exceeded."""
    # Check existing device for this fingerprint
    result = await db.execute(
        select(Device).where(
            Device.user_id == user.id,
            Device.fingerprint_hash == fingerprint_hash,
            Device.revoked == False,  # noqa: E712
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False

    # Check device count against plan limit
    result = await db.execute(
        select(Device).where(Device.user_id == user.id, Device.revoked == False)  # noqa: E712
    )
    active_devices = result.scalars().all()
    limit = PLAN_DEVICE_LIMITS.get(user.plan, 1)

    if len(active_devices) >= limit:
        raise ValueError(
            f"Device limit reached for {user.plan} plan ({limit} device{'s' if limit > 1 else ''}). "
            "Remove a device from your dashboard to add a new one."
        )

    device = Device(
        user_id=user.id,
        fingerprint_hash=fingerprint_hash,
        platform=platform,
    )
    db.add(device)
    await db.flush()
    log.info("New device registered for user %s (platform=%s)", user.id, platform)
    return device, True


async def check_fingerprint_abuse(db: AsyncSession, fingerprint_hash: str, max_accounts: int = 2) -> bool:
    """Return True if fingerprint appears in too many accounts (abuse signal)."""
    result = await db.execute(
        select(Device.user_id)
        .where(Device.fingerprint_hash == fingerprint_hash, Device.revoked == False)  # noqa: E712
        .distinct()
    )
    user_ids = result.scalars().all()
    return len(user_ids) >= max_accounts
