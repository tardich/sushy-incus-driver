# sushy_incus_driver/incusdriver.py
import json
import time
from typing import Dict, List, Optional, Tuple
import requests

from sushy_tools.emulator.resources.systems.base import AbstractSystemsDriver
from sushy_tools.emulator import constants
from sushy_tools import error


def _rf_power_from_incus(status: str) -> Optional[str]:
    # Map état Incus -> Redfish ("On"/"Off")
    if status.lower() in ("running", "frozen"):
        return "On"
    if status.lower() in ("stopped", "stopping"):
        return "Off"
    return None


class IncusRest:
    """Client REST minimal pour Incus (HTTPS)."""
    def __init__(self, base_url: str, cert: Optional[str], key: Optional[str], verify=True, timeout=20):
        # base_url ex: "https://incus.example:8443"
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if cert and key:
            self.session.cert = (cert, key)
        self.session.verify = verify
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str):
        r = self.session.get(self._url(path), timeout=self.timeout)
        r.raise_for_status()
        return r

    def put(self, path: str, data: dict):
        r = self.session.put(self._url(path), json=data, timeout=self.timeout)
        r.raise_for_status()
        return r

    def post(self, path: str, data: dict):
        r = self.session.post(self._url(path), json=data, timeout=self.timeout)
        r.raise_for_status()
        return r

    # Helpers Incus
    def list_instances(self, recursion: int = 2) -> List[dict]:
        r = self.get(f"/1.0/instances?recursion={recursion}")
        # API Incus: type=sync => metadata contient la liste
        return r.json().get("metadata", [])  # [2](https://linuxcontainers.org/incus/docs/main/howto/instances_manage/)

    def instance_state(self, name: str) -> dict:
        r = self.get(f"/1.0/instances/{name}/state")
        return r.json().get("metadata", {})  # [3](https://main.servers.andremor.dev:8443/documentation/howto/instances_manage/)

    def set_instance_state(self, name: str, action: str, force: bool = False, timeout: int = 30):
        payload = {"action": action, "force": force, "timeout": timeout}
        r = self.put(f"/1.0/instances/{name}/state", payload)  # async op
        # On peut attendre l'opération si nécessaire, mais pour sushy-tools,
        # l'ack async suffit. [3](https://main.servers.andremor.dev:8443/documentation/howto/instances_manage/)
        return r.json()


class IncusDriver(AbstractSystemsDriver):
    """Driver Systems Redfish pour Incus."""

    @classmethod
    def initialize(cls, config, logger, *, base_url: str, cert: Optional[str],
                   key: Optional[str], verify=True, only_vms: bool = True):
        cls._config = config
        cls._logger = logger
        cls._client = IncusRest(base_url, cert, key, verify=verify)
        cls._only_vms = only_vms
        return cls

    def __init__(self):
        super().__init__()
        self._systems_by_uuid: Dict[str, dict] = {}
        self._name_to_uuid: Dict[str, str] = {}
        self._refresh()

    # --------- utils internes ----------
    def _refresh(self):
        systems = []
        try:
            items = self._client.list_instances(recursion=2)
            for it in items:
                if self._only_vms and it.get("type") != "virtual-machine":
                    continue
                # On utilise le "name" Incus comme identity Redfish ; UUID fallback = name
                uuid = it.get("config", {}).get("volatile.uuid") or it.get("name")
                entry = {
                    "uuid": uuid,
                    "name": it.get("name"),
                    "raw": it,
                }
                    # NB: Incus ne garantit pas un UUID stable côté instance, donc fallback.
                    # Le contrat AbstractSystemsDriver permet de renvoyer l'identity si pas d'UUID. [8](https://opendev.org/openstack/sushy-tools/src/branch/master/sushy_tools/emulator/resources/systems/base.py)
                systems.append(entry)
        except requests.HTTPError as e:
            raise error.FishyError(f"Incus API error while listing instances: {e}")

        self._systems_by_uuid = {s["uuid"]: s for s in systems}
        self._name_to_uuid = {s["name"]: s["uuid"] for s in systems}

    def _get(self, identity: str) -> dict:
        # identity peut être name ou uuid ; on normalise
        if identity in self._systems_by_uuid:
            return self._systems_by_uuid[identity]

        if identity in self._name_to_uuid:
            return self._systems_by_uuid[self._name_to_uuid[identity]]

        # On tente un refresh (noms nouvellement créés)
        self._refresh()
        if identity in self._systems_by_uuid:
            return self._systems_by_uuid[identity]
        if identity in self._name_to_uuid:
            return self._systems_by_uuid[self._name_to_uuid[identity]]

        raise error.NotFound(f"Incus system '{identity}' was not found")

    # --------- API AbstractSystemsDriver ----------
    @property
    def driver(self) -> str:
        return "Incus"

    @property
    def systems(self) -> List[str]:
        # Liste des UUIDs
        self._refresh()
        return list(self._systems_by_uuid.keys())

    def uuid(self, identity: str) -> str:
        return self._get(identity)["uuid"]

    def name(self, identity: str) -> str:
        return self._get(identity)["name"]

    def get_power_state(self, identity: str) -> Optional[str]:
        name = self.name(identity)
        state = self._client.instance_state(name)
        return _rf_power_from_incus(state.get("status"))

    def set_power_state(self, identity: str, state: str):
        """
        Redfish ResetType -> Incus action :
          On/ForceOn          -> start
          GracefulShutdown    -> stop (force=false)
          ForceOff            -> stop (force=true)
          GracefulRestart     -> restart (force=false)
          ForceRestart        -> restart (force=true)
          Nmi                 -> non supporté
        """
        name = self.name(identity)
        state_lower = state.lower()
        if state_lower in ("on", "forceon"):
            action, force = "start", False
        elif state_lower == "gracefulshutdown":
            action, force = "stop", False
        elif state_lower == "forceoff":
            action, force = "stop", True
        elif state_lower == "gracefulrestart":
            action, force = "restart", False
        elif state_lower == "forcerestart":
            action, force = "restart", True
        else:
            raise error.FishyError(f"Unsupported power state for Incus: {state}")

        try:
            self._client.set_instance_state(name, action=action, force=force)
        except requests.HTTPError as e:
            raise error.FishyError(f"Incus API error while setting power state: {e}")

    # Boot device – v1 lecture best-effort (retourne Pxe si NIC prioritaire, sinon Hdd)
    def get_boot_device(self, identity: str) -> Optional[str]:
        raw = self._get(identity)["raw"]
        devices = raw.get("devices", {}) or {}
        # Examine boot.priority si présent (voir doc Incus pour boot.priority devices) [4](https://linuxcontainers.org/incus/docs/main/config-options/)
        best = ("Hdd", 0)
        for name, dev in devices.items():
            if dev.get("type") == "nic":
                prio = int(dev.get("boot.priority", "0"))
                if prio > best[1]:
                    best = ("Pxe", prio)
            if dev.get("type") == "disk":
                prio = int(dev.get("boot.priority", "0"))
                if prio > best[1]:
                    # Heuristique : si disk avec source iso/ceph/… on pourrait renvoyer "Cd"
                    best = ("Hdd", prio)
        return best[0] if best[1] > 0 else None

    def set_boot_device(self, identity: str, boot_source: str):
        # v1: non implémenté pour éviter les mises à jour ETag devices complètes.
        # (On pourra utiliser PUT /1.0/instances/<name> avec If-Match et ajuster boot.priority) [5](https://linuxcontainers.org/incus/docs/main/rest-api/)
        raise error.FishyError("Setting boot device not implemented in Incus driver v1")