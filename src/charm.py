import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from typing import Dict, List, Union

from ops.charm import (
    CharmBase,
    ConfigChangedEvent,
    InstallEvent,
    RelationChangedEvent,
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
    WaitingStatus,
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

        # Relation event handlers
        self.framework.observe(self.on.cluster_relation_created, self._on_cluster_relation_created)
        self.framework.observe(self.on.cluster_relation_joined, self._on_cluster_relation_joined)
        self.framework.observe(
            self.on.cluster_relation_departed, self._on_cluster_relation_departed
        )

    @property
    def peers(self):
        """Fetch the cluster relation."""
        return self.model.get_relation("cluster")

    def get_peer_data_str(self, bag, key: str) -> str:
        """Retrieve a str from the peer data bag."""
        if not self.peers or not bag or not key:
            return ""

        value = self.peers.data[bag].get(key, "")
        if isinstance(value, str):
            return value

        logger.error(f"Invalid data pulled out from {bag.name}.get('{key}')")
        return ""

    def set_peer_data_str(self, bag, key: str, value: str) -> None:
        """Put a str into the peer data bag if not there or different."""
        if not self.peers or not bag or not key:
            return

        old_value: str = self.get_peer_data_str(bag, key)
        if old_value != value:
            self.peers.data[bag][key] = value

    def _on_charm_install(self, event: InstallEvent) -> None:
        logger.info("Installing the Microcloud charm")
        # Confirm that the config is valid
        if not self.config_is_valid():
            return

        # Install Microcloud itself
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

        # Apply sideloaded resources attached at deploy time
        self.resource_sideload()

        # Installation done
        self.set_peer_data_str(self.unit, "ready_to_bootstrap", "True")
        self.unit_waiting(
            "Microcloud installed successfully, waiting for leader node to initialize the cluster"
        )

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
            if (
                "snap-channel-lxd" in changed
                or "snap-channel-microcloud" in changed
                or "snap-channel-microceph" in changed
                or "snap-channel-microovn" in changed
            ):
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

    def _on_cluster_relation_created(self, event: RelationChangedEvent) -> None:
        """We must wait for all units to be ready before initializing Microcloud."""
        if self.get_peer_data_str(self.app, "microcloud_initialized") == "True":
            self._stored.microcloud_initialized = True
            return

        all_units = self.peers.units
        if len(all_units) >= 2 and all(
            [self.get_peer_data_str(unit, "ready_to_bootstrap") == "True" for unit in all_units]
        ):
            try:
                self.microcloud_init()
                self.set_peer_data_str(self.app, "microcloud_initialized", "True")
                self._stored.microcloud_initialized = True
                logger.info("Microcloud initialized successfully")
            except RuntimeError:
                logger.error("Failed to initialize Microcloud")
                event.defer()
                return

    def _on_cluster_relation_joined(self, event: RelationJoinedEvent) -> None:
        """Add a new node to the existing Microcloud cluster"""
        if self.get_peer_data_str(self.app, "microcloud_initialized") != "True":
            logger.error("Can not add a node to a uninitialized Microcloud cluster")
            return

        try:
            self.microcloud_add()
            logger.info("New node successfully added to Microcloud")
        except RuntimeError:
            logger.error("Failed to add a new node to Microcloud")
            event.defer()
            return

    def _on_cluster_relation_departed(self, event: RelationDepartedEvent) -> None:
        """Remove a new node to the existing Microcloud cluster"""
        logger.info("Remove a node from Microcloud")
        # TODO: implement microcloud remove
        return

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
            self.unit_active(
                "Unblocking as the microcloud- keys were reset to their initial values"
            )

        for k in config_changed:
            if k == "mode" and self._stored.microcloud_initialized:
                self.unit_blocked("Can't modify mode after initialization")
                return False

        return True

    def microcloud_init(self) -> None:
        """Apply initial configuration of Microcloud."""
        self.unit_maintenance(f"Initializing Microcloud")

        try:
            subprocess.run(
                ["microcloud", "init", "--auto"],
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

    def microcloud_add(self) -> None:
        """Add a new node to Microcloud."""
        self.unit_maintenance(f"Adding node to Microcloud")

        try:
            subprocess.run(
                ["microcloud", "add", "--auto"],
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

    def microcloud_reload(self) -> None:
        """Reload the microcloud daemon."""
        self.unit_maintenance("Reloading Microcloud")
        try:
            # Avoid occasional race during startup where a reload could cause a failure
            subprocess.run(
                ["microcloud", "waitready", "--timeout=30"], capture_output=True, check=False
            )
            subprocess.run(
                ["systemctl", "reload", "snap.microcloud.daemon.service"],
                capture_output=True,
                check=True,
            )

        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError

    def resource_sideload(self) -> None:
        """Sideload resources."""
        # Multi-arch support
        arch: str = os.uname().machine
        possible_archs: List[str] = [arch]
        if arch == "x86_64":
            possible_archs = ["x86_64", "amd64"]

        # Microcloud snap
        microcloud_snap_resource: str = ""
        fname_suffix: str = ".snap"
        try:
            # Note: self._stored can only store simple data types (int/float/dict/list/etc)
            microcloud_snap_resource = str(self.model.resources.fetch("microcloud-snap"))
        except ModelError:
            pass

        tmp_dir: str = ""
        if microcloud_snap_resource and tarfile.is_tarfile(microcloud_snap_resource):
            logger.debug(f"{microcloud_snap_resource} is a tarball; unpacking")
            tmp_dir = tempfile.mkdtemp()
            tarball = tarfile.open(microcloud_snap_resource)
            valid_names = {f"microcloud_{x}{fname_suffix}" for x in possible_archs}
            for f in valid_names.intersection(tarball.getnames()):
                tarball.extract(f, path=tmp_dir)
                logger.debug(f"{f} was extracted from the tarball")
                self._stored.lxd_snap_path = f"{tmp_dir}/{f}"
                break
            else:
                logger.debug("Missing arch specific snap from tarball.")
            tarball.close()
        else:
            self._stored.microcloud_snap_path = microcloud_snap_resource

        if self._stored.microcloud_snap_path:
            self.snap_sideload_microcloud()
            if tmp_dir:
                os.remove(self._stored.microcloud_snap_path)
                os.rmdir(tmp_dir)

        # Microcloud binary
        microcloud_binary_resource: str = ""
        fname_suffix = ""
        try:
            # Note: self._stored can only store simple data types (int/float/dict/list/etc)
            microcloud_binary_resource = str(self.model.resources.fetch("microcloud-binary"))
        except ModelError:
            pass

        tmp_dir = ""
        if microcloud_binary_resource and tarfile.is_tarfile(microcloud_binary_resource):
            logger.debug(f"{microcloud_binary_resource} is a tarball; unpacking")
            tmp_dir = tempfile.mkdtemp()
            tarball = tarfile.open(microcloud_binary_resource)
            valid_names = {f"microcloud_{x}{fname_suffix}" for x in possible_archs}
            for f in valid_names.intersection(tarball.getnames()):
                tarball.extract(f, path=tmp_dir)
                logger.debug(f"{f} was extracted from the tarball")
                self._stored.microcloud_binary_path = f"{tmp_dir}/{f}"
                break
            else:
                logger.debug("Missing arch specific binary from tarball.")
            tarball.close()
        else:
            self._stored.microcloud_binary_path = microcloud_binary_resource

        if self._stored.microcloud_binary_path:
            self.snap_sideload_microcloud_binary()
            if tmp_dir:
                os.remove(self._stored.microcloud_binary_path)
                os.rmdir(tmp_dir)

    def snap_sideload_microcloud(self) -> None:
        """Sideload Microcloud snap resource."""
        logger.debug("Applying Microcloud snap sideload changes")

        cmd: List[str] = []
        alias: List[str] = []
        enable: List[str] = []

        # A 0 byte file will unload the resource
        if os.path.getsize(self._stored.microcloud_snap_path) == 0:
            logger.debug("Reverting to Microcloud snap from snapstore")
            channel: str = self._stored.config["snap-channel-microcloud"]
            cmd = ["snap", "refresh", "microcloud", f"--channel={channel}", "--amend"]
        else:
            logger.debug("Sideloading Microcloud snap")
            cmd = ["snap", "install", "--dangerous", self._stored.microcloud_snap_path]
            # Since the sideloaded snap doesn't have an assertion, some things need
            # to be done manually
            enable = ["systemctl", "enable", "--now", "snap.microcloud.daemon.unix.socket"]

        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=600)
            if alias:
                subprocess.run(alias, capture_output=True, check=True, timeout=600)
            if enable:
                subprocess.run(enable, capture_output=True, check=True, timeout=600)
        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

    def snap_sideload_microcloud_binary(self) -> None:
        """Sideload Microcloud binary resource."""
        logger.debug("Applying Microcloud binary sideload changes")
        microcloud_debug: str = "/var/snap/microcloud/common/microcloud.debug"

        # A 0 byte file will unload the resource
        if os.path.getsize(self._stored.lxd_binary_path) == 0:
            logger.debug("Unloading sideloaded Microcloud binary")
            if os.path.exists(microcloud_debug):
                os.remove(microcloud_debug)
        else:
            logger.debug("Sideloading Microcloud binary")
            # Avoid "Text file busy" error
            if os.path.exists(microcloud_debug):
                logger.debug("Removing old sideloaded LXD binary")
                os.remove(microcloud_debug)
            shutil.copyfile(self._stored.microcloud_binary_path, microcloud_debug)
            os.chmod(microcloud_debug, 0o755)

        self.microcloud_reload()

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

    def unit_waiting(self, msg: str) -> None:
        """Set the unit's status to waiting and log the provided message."""
        self.unit.status = WaitingStatus(msg)
        logger.info(msg)


if __name__ == "__main__":
    main(MaasMicrocloudCharmCharm)
