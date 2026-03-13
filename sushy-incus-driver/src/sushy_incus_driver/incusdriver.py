# sushy_incus_driver/incusdriver.py
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple
import requests

from sushy_tools.emulator.resources.systems.base import AbstractSystemsDriver
from sushy_tools.emulator import constants
from sushy_tools import error

# imports identity helper
from .identity import resolve_system_uuid

def _rf_power_from_incus(status: str) -> Optional[str]:
    """
    Map état Incus -> Redfish ("On"/"Off")
    """
    if not status:
        return None
    s = status.lower()
    if s in ("running", "frozen"):
        return "On"
    if s in ("stopped", "stopping"):
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

    # --- Helpers Instance -------------------------------------------------
    def get_instance(self, name: str) -> dict:
        r = self.get(f"/1.0/instances/{name}")
        return r.json().get("metadata", {})  # full instance object

    def update_instance_merge(
        self,
        name: str,
        *,
        merge_devices: Optional[dict] = None,
        merge_config: Optional[dict] = None,
    ) -> dict:
        """
        Merge 'devices' et/ou 'config' dans la définition de l'instance (read-modify-put).
        """
        cur = self.get_instance(name)
        new_obj = dict(cur)
        if merge_devices is not None:
            devices = dict(cur.get("devices", {}) or {})
            devices.update(merge_devices)
            new_obj["devices"] = devices
        if merge_config is not None:
            cfg = dict(cur.get("config", {}) or {})
            cfg.update(merge_config)
            new_obj["config"] = cfg
        r = self.put(f"/1.0/instances/{name}", new_obj)
        return r.json()

    def replace_instance_devices(self, name: str, new_devices: dict) -> dict:
        """
        Remplace l'ensemble 'devices' en conservant les autres clés.
        """
        cur = self.get_instance(name)
        new_obj = dict(cur)
        new_obj["devices"] = new_devices or {}
        r = self.put(f"/1.0/instances/{name}", new_obj)
        return r.json()

    # Helpers Incus (listes, état, alimentation)
    def list_instances(self, recursion: int = 2) -> List[dict]:
        r = self.get(f"/1.0/instances?recursion={recursion}")
        # API Incus: type=sync => metadata contient la liste
        return r.json().get("metadata", [])

    def instance_state(self, name: str) -> dict:
        r = self.get(f"/1.0/instances/{name}/state")
        return r.json().get("metadata", {})

    def set_instance_state(self, name: str, action: str, force: bool = False, timeout: int = 30):
        payload = {"action": action, "force": force, "timeout": timeout}
        r = self.put(f"/1.0/instances/{name}/state", payload)  # async op
        # Pour sushy-tools, l'ack async suffit.
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
        # Dossier où déposer les ISO téléchargées (surchargeable via conf Flask)
        self._iso_dir = (self._config.get("SUSHY_EMULATOR_INCUS_ISO_DIR")
                         or "/var/lib/sushy-incus/isos")
        Path(self._iso_dir).mkdir(parents=True, exist_ok=True)

    # --------- utils internes ----------
    def _resolve_strategy(self) -> str:
        # Permet de changer la strategie a chaud via l'env
        return os.getenv("SUSHY_EMULATOR_INCUS_UUID_STRATEGY", "user-first")

    def _refresh(self):
        systems = []
        try:
            items = self._client.list_instances(recursion=2)
            for it in items:
                if self._only_vms and it.get("type") != "virtual-machine":
                    continue

                # UUID Redfish resolu via identity.py
                sys_uuid = resolve_system_uuid(it, strategy=self._resolve_strategy())
                if not sys_uuid:
                    # Ultime fallback: utiliser le nom comme identity interne (peu recommande)
                    sys_uuid = it.get("name")

                entry = {
                    "uuid": sys_uuid,
                    "name": it.get("name"),
                    "raw": it,
                }
                systems.append(entry)
        except requests.HTTPError as e:
            raise error.FishyError(f"Incus API error while listing instances: {e}")

        # Index par UUID, et mapping name -> uuid
        self._systems_by_uuid = {s["uuid"]: s for s in systems if s.get("uuid")}
        self._name_to_uuid = {s["name"]: s["uuid"] for s in systems if s.get("name") and s.get("uuid")}

    def _get(self, identity: str) -> dict:
        # identity peut être name ou uuid ; on normalise
        if identity in self._systems_by_uuid:
            return self._systems_by_uuid[identity]
        if identity in self._name_to_uuid:
            return self._systems_by_uuid[self._name_to_uuid[identity]]
        # On tente un refresh
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
        s = state.lower()
        if s in ("on", "forceon"):
            action, force = "start", False
        elif s == "gracefulshutdown":
            action, force = "stop", False
        elif s == "forceoff":
            action, force = "stop", True
        elif s == "gracefulrestart":
            action, force = "restart", False
        elif s == "forcerestart":
            action, force = "restart", True
        else:
            raise error.FishyError(f"Unsupported power state for Incus: {state}")

        try:
            self._client.set_instance_state(name, action=action, force=force)
            # Boot-once : restauration après start/restart
            if action in ("start", "restart"):
                self._restore_boot_once_if_needed(identity)
        except requests.HTTPError as e:
            raise error.FishyError(f"Incus API error while setting power state: {e}")

    # Boot device – lecture best-effort (retourne Pxe si NIC prioritaire, sinon Hdd)
    def get_boot_device(self, identity: str) -> Optional[str]:
        raw = self._get(identity)["raw"]
        devices = raw.get("devices", {}) or {}
        # Examine boot.priority si présent
        best = ("Hdd", 0)
        for name, dev in devices.items():
            if dev.get("type") == "nic":
                prio = int(dev.get("boot.priority", "0"))
                if prio > best[1]:
                    best = ("Pxe", prio)
            if dev.get("type") == "disk":
                prio = int(dev.get("boot.priority", "0"))
                if prio > best[1]:
                    # Heuristique : si disk avec source iso/ceph/... on pourrait renvoyer "Cd"
                    best = ("Hdd", prio)
        return best[0] if best[1] > 0 else None

    def set_boot_device(self, identity: str, boot_source: str):
        """
        Implémentation 'boot once' :
         - PXE  -> priorité haute sur les NIC (boot.priority=10)
         - CD   -> priorité haute sur cdrom0 (doit exister)
         - HDD  -> priorité sur un disk non-cdrom (best-effort)
        Sauvegarde l'état précédent dans config user.sushy.* puis restauration
        automatique après le prochain power on/restart.
        """
        sys_entry = self._get(identity)
        name = sys_entry["name"]
        instance = self._client.get_instance(name)
        devices = dict(instance.get("devices", {}) or {})

        # Sauvegarde des priorités actuelles
        prev = {d: dev["boot.priority"] for d, dev in devices.items() if "boot.priority" in dev}

        # Reset priorités
        for dev in devices.values():
            dev.pop("boot.priority", None)

        def bump(dname: str):
            dev = devices.get(dname) or {}
            dev["boot.priority"] = "10"
            devices[dname] = dev

        target = boot_source.lower()
        if target in ("pxe", "network"):
            for dname, dev in devices.items():
                if dev.get("type") == "nic":
                    bump(dname)
        elif target in ("cd", "cdrom", "dvd"):
            if "cdrom0" in devices and devices["cdrom0"].get("type") == "disk":
                bump("cdrom0")
            else:
                raise error.FishyError("No virtual media attached (cdrom0 not present)")
        elif target in ("hdd", "disk"):
            candidates = [n for n, d in devices.items() if d.get("type") == "disk" and n != "cdrom0"]
            if candidates:
                bump(candidates[0])
        else:
            raise error.FishyError(f"Unsupported boot source: {boot_source}")

        self._client.replace_instance_devices(name, devices)
        meta = {"user.sushy.bootonce": "true", "user.sushy.bootonce.prev": json.dumps(prev)}
        self._client.update_instance_merge(name, merge_config=meta)
        self._refresh()
        return

    # --------- EthernetInterfaces (lecture) -------------------------------
    def get_nics(self, identity: str) -> List[dict]:
        """
        Retourne une liste d'interfaces:
         [{"id":"<mac>","mac":"<mac>","name":"<if>","state":"up/down","ipv4":[...],"ipv6":[...]}]
        - Source live: /instances/<name>/state
        - Fallback: devices 'nic' (hwaddr) quand VM arrêtée
        """
        sys_entry = self._get(identity)
        name = sys_entry["name"]
        nics: List[dict] = []
        # 1) état live
        try:
            st = self._client.instance_state(name)
            net = (st or {}).get("network", {}) or {}
            for ifname, attrs in net.items():
                mac = (attrs or {}).get("hwaddr")
                if not mac or ifname == "lo":
                    continue
                ipv4, ipv6 = [], []
                for addr in (attrs or {}).get("addresses", []) or []:
                    fam = addr.get("family"); ip = addr.get("address"); scope = addr.get("scope")
                    if not ip:
                        continue
                    if fam == "inet":
                        ipv4.append({"address": ip, "scope": scope})
                    elif fam == "inet6":
                        ipv6.append({"address": ip, "scope": scope})
                nics.append({
                    "id": mac.lower(),
                    "mac": mac.lower(),
                    "name": ifname,
                    "state": (attrs or {}).get("state"),
                    "ipv4": ipv4,
                    "ipv6": ipv6
                })
        except requests.HTTPError:
            pass
        # 2) fallback config
        if not nics:
            raw = sys_entry["raw"] or {}
            devices = raw.get("devices", {}) or {}
            for dev_name, dev in devices.items():
                if (dev or {}).get("type") != "nic":
                    continue
                mac = (dev or {}).get("hwaddr")
                if not mac:
                    continue
                nics.append({
                    "id": mac.lower(),
                    "mac": mac.lower(),
                    "name": dev_name,
                    "state": None,
                    "ipv4": [],
                    "ipv6": []
                })
        nics.sort(key=lambda x: (x.get("name") or "", x["mac"]))
        return nics

    def get_nic(self, identity: str, nic_id: str) -> dict:
        target = nic_id.lower()
        for nic in self.get_nics(identity):
            if nic.get("id", "").lower() == target:
                return nic
        raise error.NotFound(f"NIC '{nic_id}' not found on Incus system '{identity}'")

    # --------- Virtual Media (Insert/Eject + download) --------------------
    def _download_iso_if_needed(self, image: str) -> str:
        """
        Retourne un chemin local vers l'ISO :
          - http(s): télécharge dans self._iso_dir (si absent)
          - file:/// ou absolu: renvoie tel quel
          - relatif: dans self._iso_dir
        """
        parsed = urlparse(image)
        if parsed.scheme in ("http", "https"):
            fname = os.path.basename(parsed.path) or "image.iso"
            dest = Path(self._iso_dir) / fname
            if not dest.exists():
                with self._client.session.get(image, stream=True, timeout=self._client.timeout) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
            return str(dest)
        if parsed.scheme == "file":
            return parsed.path
        if os.path.isabs(image):
            return image
        return str(Path(self._iso_dir) / image)

    def attach_virtual_media(self, identity: str, image: str, *, boot_once: bool = True):
        """
        Insert d'un ISO via device 'disk' (cdrom0). Si boot_once:
         - boot.priority=10
         - sauvegarde priorités et restauration après prochain start/restart.
        """
        sys_entry = self._get(identity)
        name = sys_entry["name"]
        local_iso = self._download_iso_if_needed(image)

        inst = self._client.get_instance(name)
        devices = dict(inst.get("devices", {}) or {})
        prev = {d: dev["boot.priority"] for d, dev in devices.items() if "boot.priority" in dev}

        devices["cdrom0"] = {"type": "disk", "readonly": "true", "source": local_iso}
        if boot_once:
            devices["cdrom0"]["boot.priority"] = "10"

        self._client.replace_instance_devices(name, devices)
        meta = {"user.sushy.bootonce": "true", "user.sushy.bootonce.prev": json.dumps(prev)}
        self._client.update_instance_merge(name, merge_config=meta)
        self._refresh()
        return

    def eject_virtual_media(self, identity: str):
        """
        Ejecte 'cdrom0' et nettoie les marqueurs bootonce.
        """
        sys_entry = self._get(identity)
        name = sys_entry["name"]
        inst = self._client.get_instance(name)
        devices = dict(inst.get("devices", {}) or {})
        if "cdrom0" in devices:
            devices.pop("cdrom0", None)
            self._client.replace_instance_devices(name, devices)
        cfg = dict(inst.get("config", {}) or {})
        for k in ("user.sushy.bootonce", "user.sushy.bootonce.prev"):
            cfg.pop(k, None)
        self._client.update_instance_merge(name, merge_config=cfg)
        self._refresh()
        return

    # --------- Boot once: restauration interne ----------------------------
    def _restore_boot_once_if_needed(self, identity: str):
        """
        Si user.sushy.bootonce=true: restaurer les boot.priority précédentes,
        puis supprimer les marqueurs.
        """
        sys_entry = self._get(identity)
        name = sys_entry["name"]
        inst = self._client.get_instance(name)
        cfg = dict(inst.get("config", {}) or {})
        if cfg.get("user.sushy.bootonce") != "true":
            return
        prev_raw = cfg.get("user.sushy.bootonce.prev")
        prev = {}
        if prev_raw:
            try:
                prev = json.loads(prev_raw)
            except Exception:
                prev = {}
        devices = dict(inst.get("devices", {}) or {})
        for dev in devices.values():
            dev.pop("boot.priority", None)
        for dname, pr in (prev or {}).items():
            if dname in devices:
                devices[dname]["boot.priority"] = pr
        self._client.replace_instance_devices(name, devices)
        for k in ("user.sushy.bootonce", "user.sushy.bootonce.prev"):
            cfg.pop(k, None)
        self._client.update_instance_merge(name, merge_config=cfg)
        self._refresh()
        return
