# sitecustomize.py
def _patch_sushy_application():
    try:
        from sushy_tools.emulator import main as sushy_main
        from sushy_incus_driver.incusdriver import IncusDriver
        from functools import wraps
    except Exception:
        return # sushy-tools not imported, nothing to do
    
    # Keep original version property
    original_systems_prop = sushy_main.Application.systems

    @property
    @wraps(original_systems_prop.fget)
    def systems_with_incus(self) :
        # If Incus URL given, use the INcus driver
        incus_url = self.config.get("SUSHY_EMULATOR_INCUS_URL")
        if incus_url:
            cert = self.config.get("SUSHY_EMULATOR_INCUS_CERT")
            key = self.config.get("SUSHY_EMULATOR_INCUS_KEY")
            verify = self.config.get("SUSHY_EMULATOR_INCUS_VERIFY", True)
            only_vms = self.config.get("SUSHY_EMULATOR_INCUS_ONLY_VMS", True)
            drv = IncusDriver.initialize(self.config, self.logger, base_url=incus_url,
                                         cert=cert, key=key, verify=verify,
                                         only_vms=only_vms) ()
            self.logger.debug("Initialized system resource backend by Incus driver")
            return drv
        # Otherwise, back to normal behavior (libvirt, OPenstack, Ironic...)
        return original_systems_prop.fget(self)
    # Clean replace of perperty
    sushy_main.Application.systems = systems_with_incus
_patch_sushy_application()