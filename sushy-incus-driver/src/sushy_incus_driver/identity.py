from __future__ import annotations
import os
import re
import uuid
from typing import Optional, Dict

# Regex pour trouver uuid=... dans raw.qemu/raw.qemu.conf
_UUID_RE = re.compile(
    r'\buuid=([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})\b'
)

# Namespace UUIDv5 (changeable via env SUSHY_EMULATOR_INCUS_NS). Par défaut : namespace DNS standard.
_UUIDV5_NAMESPACE_DEFAULT = os.getenv(
    "SUSHY_EMULATOR_INCUS_NS",
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
)

def _canonical_uuid(u: str) -> str:
    """Normalise un UUID en lowercase avec tirets (8-4-4-4-12)."""
    s = (u or "").replace("-", "").lower()
    if len(s) != 32:
        return u
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"

def uuidv5_from_name(name: Optional[str], namespace: Optional[str] = None) -> Optional[str]:
    """Calcule un UUIDv5 déterministe à partir du nom (et namespace)."""
    if not name:
        return None
    ns = uuid.UUID(namespace or _UUIDV5_NAMESPACE_DEFAULT)
    return str(uuid.uuid5(ns, name))

def parse_smbios_uuid_from_raw_qemu(raw_qemu: Optional[str]) -> Optional[str]:
    """Extrait '-smbios type=1,uuid=...' depuis raw.qemu/raw.qemu.conf."""
    if not raw_qemu:
        return None
    m = _UUID_RE.search(raw_qemu)
    if not m:
        return None
    return _canonical_uuid(m.group(1))

def resolve_system_uuid(instance: Dict, strategy: str = "user-first") -> Optional[str]:
    """
    Résout l'UUID Redfish pour une instance Incus selon la stratégie.

    Strategies:
      - 'user-first'   : user.redfish.uuid -> SMBIOS(raw.qemu) -> UUIDv5(name) -> volatile.uuid
      - 'name-first'   : UUIDv5(name)      -> SMBIOS(raw.qemu) -> user.redfish.uuid -> volatile.uuid
      - 'smbios-first' : SMBIOS(raw.qemu)  -> user.redfish.uuid -> UUIDv5(name)     -> volatile.uuid
    """
    cfg = instance.get("config", {}) or {}
    name = instance.get("name") or instance.get("Name")

    user_uuid = cfg.get("user.redfish.uuid")
    smbios_uuid = (
        parse_smbios_uuid_from_raw_qemu(cfg.get("raw.qemu")) or
        parse_smbios_uuid_from_raw_qemu(cfg.get("raw.qemu.conf"))
    )
    name_uuid = uuidv5_from_name(name)
    vol_uuid = cfg.get("volatile.uuid")  # dernier recours (interne Incus)

    strategies = {
        "user-first":   [user_uuid,  smbios_uuid, name_uuid, vol_uuid],
        "name-first":   [name_uuid,  smbios_uuid, user_uuid,  vol_uuid],
        "smbios-first": [smbios_uuid, user_uuid, name_uuid,   vol_uuid],
    }
    for cand in strategies.get(strategy, strategies["user-first"]):
        if cand:
            return _canonical_uuid(cand)
    return None
