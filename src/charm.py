import logging
import os
import subprocess
from typing import Dict, List, Tuple, Union

from ops.charm import (
    ActionEvent,
    CharmBase,
    ConfigChangedEvent,
    InstallEvent,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationCreatedEvent,
    RelationDepartedEvent,
    RelationJoinedEvent,
    StartEvent,
)
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    RelationData,
)

logger = logging.getLogger(__name__)


class MaasMicrocloudCharmCharm(CharmBase):
    """Microcloud charm class."""

    _stored = StoredState()

    def __init__(self, *args):
        """Initialize charm's variable"""
        super().__init__(*args)

        # Initialize the persistent storage if needed
        self._stored.set_default(
            addresses={},
            config={},
            inside_container=False,
            microcloud_binary_path="",
            microcloud_initialized=False,
            microcloud_snap_path="",
            reboot_required=False,
        )

        # Main event handlers
        self.framework.observe(self.on.config_changed, self._on_charm_config_changed)
        self.framework.observe(self.on.install, self._on_charm_install)
        self.framework.observe(self.on.start, self._on_charm_start)

    def _on_charm_install(self, event: InstallEvent) -> None:
        logger.info("Installing the Microcloud charm")
        # Confirm that the config is valid
        if not self.config_is_valid():
            return

        # Install LXD itself
        try:
            self.snap_install_microcloud()
            logger.info("Microcloud installed successfully")
        except RuntimeError:
            logger.error("Failed to install Microcloud")
            event.defer()
            return

        # Detect if running inside a container
        c = subprocess.run(
            ["systemd-detect-virt", "--quiet", "--container"],
            check=False,
            timeout=600,
        )
        if c.returncode == 0:
            logger.debug(
                "systemd-detect-virt detected the run-time environment as being of container type"
            )
            self._stored.inside_container = True

        # Apply various configs
        self.snap_config_set()
        self.kernel_sysctl()

        # Initial configuration
        try:
            self.microcloud_init()
            self._stored.microcloud_initialized = True
            logger.info("Microcloud initialized successfully")
        except RuntimeError:
            logger.error("Failed to initialize Microcloud")
            event.defer()
            return

        # Apply sideloaded resources attached at deploy time
        self.resource_sideload()

        # All done
        self.unit_active()

    def _on_charm_start(self, event: StartEvent) -> None:
        logger.info("Starting the Microcloud charm")

        if not self._stored.microcloud_initialized:
            logger.debug("Microcloud is not initialized yet, not starting the charm")
            return

        if not self._stored.reboot_required and isinstance(self.unit.status, BlockedStatus):
            self.unit_active("Pending configuration changes were applied during the last reboot")

        # Apply pending config changes (those were likely queued up while the unit was
        # down/rebooting)
        if self.config_changed():
            logger.debug("Pending config changes detected")
            self._on_charm_config_changed(event)

    def _on_charm_config_changed(self, event: Union[ConfigChangedEvent, StartEvent]) -> None:
        """React to configuration changes.

        Some configuration items can be set only once
        while others are changeable, sometimes requiring
        a service reload or even a machine reboot.
        """
        logger.info("Updating charm config")

        error = False

        # Confirm that the config is valid
        if not self.config_is_valid():
            return

        # Get all the configs that changed
        changed = self.config_changed()
        if not changed:
            logger.debug("No configuration changes to apply")
            return

        # Apply all the configs that changed
        try:
            if "snap-channel-lxd" in changed or "snap-channel-microcloud" in changed or "snap-channel-microceph" in changed or "snap-channel-microovn" in changed:
                self.snap_install_microcloud()
        except RuntimeError:
            msg = "Failed to apply some configuration change(s): %s" % ", ".join(changed)
            self.unit_blocked(msg)
            event.defer()
            return

        # All done
        if error:
            msg = "Some configuration change(s) didn't apply successfully"
        else:
            msg = "Configuration change(s) applied successfully"
        self.unit_active(msg)

    def config_changed(self) -> Dict:
        """Figure out what changed."""
        new_config = self.config
        old_config = self._stored.config
        apply_config = {}
        for k, v in new_config.items():
            if k not in old_config:
                apply_config[k] = v
            elif v != old_config[k]:
                apply_config[k] = v

        return apply_config

    def config_is_valid(self) -> bool:
        """Validate the config."""
        config_changed = self.config_changed()

        # If nothing changed and we were blocked due to a lxd- key
        # change (post-init), we can assume the change was reverted thus unblocking us
        if (
            not config_changed
            and isinstance(self.unit.status, BlockedStatus)
            and "Can't modify microcloud- keys after initialization:" in self.unit.status.message
        ):
            self.unit_active("Unblocking as the microcloud- keys were reset to their initial values")

        for k in config_changed:
            if k == "mode" and self._stored.microcloud_initialized:
                self.unit_blocked("Can't modify mode after initialization")
                return False

        return True

    def snap_install_microcloud(self) -> None:
        """Install Microcloud from snap."""
        lxd_channel = self.config["snap-channel-lxd"]
        if lxd_channel:
            lxd_channel_name = microcloud_channel
        else:
            lxd_channel_name = "latest/stable"
        self.unit_maintenance(f"Installing LXD snap (channel={lxd_channel_name})")

        microcloud_channel = self.config["snap-channel-microcloud"]
        if microcloud_channel:
            microcloud_channel_name = microcloud_channel
        else:
            microcloud_channel_name = "latest/stable"
        self.unit_maintenance(f"Installing Microcloud snap (channel={microcloud_channel_name})")

        microceph = self.config["microceph"]
        if microceph:
            microceph_channel = self.config["snap-channel-microceph"]
            if microceph_channel:
                microceph_channel_name = microceph_channel
            else:
                microceph_channel_name = "latest/stable"
            self.unit_maintenance(f"Installing Microceph snap (channel={microceph_channel_name})")

        microovn = self.config["microovn"]
        if microovn:
            microovn_channel = self.config["snap-channel-microovn"]
            if microovn_channel:
                microovn_channel_name = microovn_channel
            else:
                microovn_channel_name = "latest/stable"
            self.unit_maintenance(f"Installing MicroOVN snap (channel={microovn_channel_name})")

        cohort = ["--cohort=+"]
        try:
            # LXD
            subprocess.run(
                ["snap", "install", "lxd", f"--channel={lxd_channel}"] + cohort,
                capture_output=True,
                check=True,
                timeout=600,
            )
            subprocess.run(
                ["snap", "refresh", "lxd", f"--channel={lxd_channel}"] + cohort,
                capture_output=True,
                check=True,
                timeout=600,
            )
            if os.path.exists("/var/lib/lxd"):
                subprocess.run(
                    ["lxd.migrate", "-yes"], capture_output=True, check=True, timeout=600
                )

            # Microcloud
            subprocess.run(
                ["snap", "install", "microcloud", f"--channel={microcloud_channel}"] + cohort,
                capture_output=True,
                check=True,
                timeout=600,
            )
            subprocess.run(
                ["snap", "refresh", "microcloud", f"--channel={microcloud_channel}"] + cohort,
                capture_output=True,
                check=True,
                timeout=600,
            )

            # MicroCeph
            if microceph:
                subprocess.run(
                    ["snap", "install", "microceph", f"--channel={microceph_channel}"] + cohort,
                    capture_output=True,
                    check=True,
                    timeout=600,
                )
                subprocess.run(
                    ["snap", "refresh", "microceph", f"--channel={microceph_channel}"] + cohort,
                    capture_output=True,
                    check=True,
                    timeout=600,
                )

            # MicroOVN
            if microovn:
                subprocess.run(
                    ["snap", "install", "microovn", f"--channel={microovn_channel}"] + cohort,
                    capture_output=True,
                    check=True,
                    timeout=600,
                )
                subprocess.run(
                    ["snap", "refresh", "microovn", f"--channel={microovn_channel}"] + cohort,
                    capture_output=True,
                    check=True,
                    timeout=600,
                )
        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

        # Done with the snap installation
        self._stored.config["snap-channel-lxd"] = lxd_channel
        self._stored.config["snap-channel-microcloud"] = microcloud_channel
        if microceph:
            self._stored.config["snap-channel-microceph"] = microceph_channel
        if microovn:
            self._stored.config["snap-channel-microovn"] = microovn_channel

    def unit_active(self, msg: str = "") -> None:
        """Set the unit's status to active and log the provided message, if any."""
        self.unit.status = ActiveStatus()
        if msg:
            logger.debug(msg)

    def unit_blocked(self, msg: str) -> None:
        """Set the unit's status to blocked and log the provided message."""
        self.unit.status = BlockedStatus(msg)
        logger.error(msg)

    def unit_maintenance(self, msg: str) -> None:
        """Set the unit's status to maintenance and log the provided message."""
        self.unit.status = MaintenanceStatus(msg)
        logger.info(msg)


if __name__ == "__main__":
    main(MaasMicrocloudCharmCharm)
