# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import logging
import threading
import socket
from typing import Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("tasmota_discovery")

# ---------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------
CONFIG_PATH           = os.environ.get("TASMOTA_CONFIG", "/data/tasmota_config.json")
CONFIG_WATCH_INTERVAL = int(os.environ.get("TASMOTA_CONFIG_WATCH", "10"))


class ConfigManager:
    """Watches tasmota_config.json and notifies on change.

    The only thing the config does is supply a ``state_relay_map`` per device.
    If present the device registers as a three-state switch; otherwise it's
    a plain toggle.  Everything else (name, group, icon) stays in the GUI.

    Config examples::

        # Single-relay three-state switch (simplest form)
        {
          "devices": {
            "borehole_0": {
              "three_state": true,
              "labels": ["Off", "On", "Auto"]
            }
          }
        }

        # Multi-relay three-state switch (explicit relay combinations)
        {
          "devices": {
            "tasmota_AABBCC": {
              "state_relay_map": {
                "0": {"POWER1": "OFF", "POWER2": "OFF"},
                "1": {"POWER1": "ON",  "POWER2": "OFF"},
                "2": {"POWER1": "ON",  "POWER2": "ON"}
              }
            }
          }
        }
    """

    def __init__(self, path: str):
        self.path             = path
        self._lock            = threading.Lock()
        self._devices: dict   = {}
        self._mtime: float    = 0.0
        self._callbacks: list = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            if self._devices:
                log.info("Config removed - reverting devices to toggle defaults")
                old = set(self._devices)
                with self._lock:
                    self._devices = {}
                    self._mtime   = 0.0
                self._fire(set(), old, set())
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                new_devs = json.load(f).get("devices", {})
        except Exception as exc:
            log.warning("Config parse error (%s) - keeping previous config", exc)
            return
        old_devs = self._devices
        added    = set(new_devs) - set(old_devs)
        removed  = set(old_devs) - set(new_devs)
        modified = {d for d in set(new_devs) & set(old_devs) if new_devs[d] != old_devs[d]}
        with self._lock:
            self._devices = new_devs
            self._mtime   = mtime
        if added or removed or modified:
            log.info("Config reloaded - added=%s removed=%s modified=%s", added, removed, modified)
            self._fire(added, removed, modified)

    def _fire(self, added, removed, modified):
        for cb in self._callbacks:
            try:
                cb(added, removed, modified)
            except Exception as exc:
                log.exception("Config callback error: %s", exc)

    def register_callback(self, fn):
        self._callbacks.append(fn)

    def start_watcher(self):
        def _loop():
            while True:
                time.sleep(CONFIG_WATCH_INTERVAL)
                self._load()
        threading.Thread(target=_loop, daemon=True, name="config-watcher").start()
        log.info("Config watcher started (polling %s every %ds)", self.path, CONFIG_WATCH_INTERVAL)

    def relay_map(self, device_id: str) -> dict:
        """Return the effective relay map for a device.

        If an explicit ``state_relay_map`` is present in the config it is used
        as-is (multi-relay / custom behaviour).

        If ``"three_state": true`` is set (and no explicit map), a standard
        single-relay three-state map is generated automatically:
            0 -> POWER OFF
            1 -> POWER ON
            2 -> POWER ON  (Auto - physically identical to On)

        Returns an empty dict for plain toggle devices.
        """
        with self._lock:
            dev = self._devices.get(device_id, {})
        explicit = dev.get("state_relay_map")
        if explicit:
            return explicit
        if dev.get("three_state"):
            return {
                "0": {"POWER": "OFF"},
                "1": {"POWER": "ON"},
                "2": {"POWER": "ON"},
            }
        return {}

    def reverse_relay_map(self, device_id: str) -> dict:
        """Return a mapping from frozenset of (key,val) pairs -> state int.

        Allows looking up the state value from a set of observed relay states.
        Example: {frozenset({("POWER1","ON"),("POWER2","OFF")}): 1}

        When multiple states share the same relay combination (e.g. state 1 "On"
        and state 2 "Auto" both map to POWER=ON on a single-relay device), the
        lowest state number wins.  Higher states like "Auto" can only be set
        explicitly by a Node-RED write - they must never be inferred from a
        relay report that is physically identical to a lower state.
        """
        result = {}
        for state_str, commands in self.relay_map(device_id).items():
            key       = frozenset((k.upper(), v.upper()) for k, v in commands.items())
            state_val = int(state_str)
            if key not in result or state_val < result[key]:
                result[key] = state_val
        return result

    def is_three_state(self, device_id: str) -> bool:
        """True when the device should be registered as a three-state switch."""
        with self._lock:
            dev = self._devices.get(device_id, {})
        return bool(dev.get("three_state") or dev.get("state_relay_map"))

    def auto_mode(self, device_id: str) -> int:
        """Return 0 (manual/user controls UI) or 1 (auto/driver controls UI)."""
        with self._lock:
            return int(self._devices.get(device_id, {}).get("auto", 0))

    def labels(self, device_id: str) -> list:
        """Return the three state labels, defaulting to Off/On/Auto."""
        with self._lock:
            return self._devices.get(device_id, {}).get("labels", ["Off", "On", "Auto"])

    def group(self, device_id: str) -> str:
        """Return the stored group name, defaulting to 'Tasmota'."""
        with self._lock:
            return self._devices.get(device_id, {}).get("group", "Tasmota")

    def custom_name(self, device_id: str, channel: int, fallback: str) -> str:
        """Return the persisted custom name for a channel, or fallback if not set."""
        key = f"custom_name_{channel}"
        with self._lock:
            return self._devices.get(device_id, {}).get(key, fallback)

    def assign_instance(self, device_id: str) -> int:
        """Return this device's instance number, assigning the next sequential one if needed.

        Assignment is in-memory only; the number is persisted when register_device
        writes the stub to disk.  On subsequent startups the value is read back
        from the config file, so the same device always gets the same number.
        """
        with self._lock:
            val = self._devices.get(device_id, {}).get("instance")
            if val is not None:
                return int(val)
            used = {
                int(d["instance"])
                for d in self._devices.values()
                if d.get("instance") is not None
            }
            n = 0
            while n in used:
                n += 1
            self._devices.setdefault(device_id, {})["instance"] = n
            return n

    def register_device(self, device_id: str, friendly_name: str, ip: str, channels: int):
        """Add a discovered device to the config file if not already present.

        Existing entries are never touched, so any state_relay_map you have
        added by hand is preserved.
        """
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
        else:
            raw = {}

        devices = raw.setdefault("devices", {})
        if device_id in devices:
            return  # already listed, don't overwrite

        devices[device_id] = {
            "_name":      friendly_name,
            "_ip":        ip,
            "_channels":  channels,
            "instance":   self.assign_instance(device_id),
            # Set to true (or write 1 to /Settings/ThreeState on D-Bus) to make this
            # a three-state switch (Off / On / Auto) for a single-relay device.
            "three_state": False,
            # For multi-relay devices where each state drives a different relay
            # combination, use the explicit map instead of three_state:
            #   "state_relay_map": {
            #     "0": {"POWER1": "OFF", "POWER2": "OFF"},
            #     "1": {"POWER1": "ON",  "POWER2": "OFF"},
            #     "2": {"POWER1": "ON",  "POWER2": "ON"}
            #   }
            #
            # Optional: override the three state labels (default: Off / On / Auto)
            #   "labels": ["Off", "On", "Auto"],
            #
            # Optional: start in auto mode (Node-RED controls /State, user cannot override)
            #   "auto": 0
        }

        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
            log.info("Added %s to config file (%s)", device_id, self.path)
            # Update mtime so the watcher doesn't re-fire for this write
            with self._lock:
                self._devices.setdefault(device_id, devices[device_id])
                self._mtime = os.path.getmtime(self.path)
        except Exception as exc:
            log.warning("Could not write config file: %s", exc)

    def update_device_field(self, device_id: str, key: str, value, notify: bool = True):
        """Update a single field in a device's config entry and persist to disk.

        notify=True (default) fires config-change callbacks so the device's
        D-Bus service re-initialises.  Pass notify=False for cosmetic-only
        changes (e.g. label renames) where a re-init is not needed.
        """
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}
        else:
            raw = {}

        raw.setdefault("devices", {}).setdefault(device_id, {})[key] = value

        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
            log.info("Config: set %s.%s = %r", device_id, key, value)
        except Exception as exc:
            log.warning("Could not write config file: %s", exc)
            return

        with self._lock:
            old_val = self._devices.get(device_id, {}).get(key)
            self._devices.setdefault(device_id, {})[key] = value
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                pass

        if notify and old_val != value:
            self._fire(set(), set(), {device_id})


CFG = ConfigManager(CONFIG_PATH)

# ---------------------------------------------------------------------
# Venus OS detection
# ---------------------------------------------------------------------
VENUS_OS = os.path.exists("/opt/victronenergy")

# velib_python path on Venus OS
VELIB_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"

# ---------------------------------------------------------------------
# Venus OS / GLib imports
# ---------------------------------------------------------------------
try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    log.info("dbus/GLib imports successful")

except Exception as e:
    log.exception("Failed loading dbus/GLib: %s", e)

    dbus = None
    DBusGMainLoop = None
    GLib = None

# ---------------------------------------------------------------------
# Zeroconf imports
# ---------------------------------------------------------------------
try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

    ZEROCONF_AVAILABLE = True
    log.info("zeroconf available")

except Exception:
    ZEROCONF_AVAILABLE = False
    log.info("zeroconf not installed")

# ---------------------------------------------------------------------
# MQTT config
# ---------------------------------------------------------------------
MQTT_HOST     = os.environ.get("TASMOTA_MQTT_HOST", "127.0.0.1")
MQTT_PORT     = int(os.environ.get("TASMOTA_MQTT_PORT", "1883"))
MQTT_USER     = os.environ.get("TASMOTA_MQTT_USER", "")
MQTT_PASS     = os.environ.get("TASMOTA_MQTT_PASS", "")
POLL_INTERVAL = int(os.environ.get("TASMOTA_POLL_INTERVAL", "30"))

# How long to wait for a Status 11 probe response before discarding
# the pending device (seconds).
PROBE_TIMEOUT = int(os.environ.get("TASMOTA_PROBE_TIMEOUT", "15"))

# ---------------------------------------------------------------------
# Dry-run shim
# ---------------------------------------------------------------------
class _Shim:
    def __init__(self, name, **kw):
        self._name  = name
        self._store = {}

        log.info("[DRY-RUN] Register D-Bus service: %s", name)

    def add_path(self, path, value, writeable=False, onchangecallback=None, **kw):
        self._store[path] = value
        log.debug("[DRY-RUN] add %s %s = %r", self._name, path, value)

    def __setitem__(self, path, value):
        self._store[path] = value
        log.debug("[DRY-RUN] set %s %s = %r", self._name, path, value)

    def __getitem__(self, path):
        return self._store.get(path)

# ---------------------------------------------------------------------
# VeDbusService loading
# ---------------------------------------------------------------------
if VENUS_OS:
    sys.path.insert(1, VELIB_PATH)

    try:
        from vedbus import VeDbusService

        log.info("velib_python loaded from %s", VELIB_PATH)

    except ImportError:
        log.warning("velib_python not found at %s - using shim", VELIB_PATH)
        VeDbusService = _Shim

else:
    VeDbusService = _Shim

# Switch type constants
TYPE_TOGGLE      = 1
TYPE_THREE_STATE = 9

STATUS_OFF       = 0x00
STATUS_ON        = 0x09
MODULE_CONNECTED = 0x100

# ValidTypes bitmask: each bit position corresponds to the type enum value.
# bit 1 (value 2)   = toggle (type 1)
# bit 9 (value 512) = three-state (type 9)
# FIX: three-state devices should only advertise bit 9, not bit 1 as well,
# so the GUI doesn't offer "toggle" as a selectable type for three-state devices.
VALID_TYPES_TOGGLE      = (1 << TYPE_TOGGLE)       # 0b0000000010 = 2
VALID_TYPES_THREE_STATE = (1 << TYPE_THREE_STATE)  # 0b1000000000 = 512


# =====================================================================
# TasmotaDevice
# =====================================================================
class TasmotaDevice:

    def __init__(self, device_id: str, friendly_name: str, ip: str, channels: int = 1):
        self.device_id     = device_id
        self.friendly_name = friendly_name
        self.ip            = ip
        self.channels      = channels

        self._svc          = None
        self._lock         = threading.Lock()
        self._mqtt_publish = None
        self._relay_state: dict = {}  # tracks latest POWER key states for 3-state reverse lookup

        self._init_service()

    def _init_service(self):
        three_state = CFG.is_three_state(self.device_id)
        sw_type     = TYPE_THREE_STATE if three_state else TYPE_TOGGLE
        # FIX: use separate bitmasks so three-state devices don't also show "toggle"
        # as an option in the GUI type selector.
        valid_types = VALID_TYPES_THREE_STATE if three_state else VALID_TYPES_TOGGLE

        svc_name = f"com.victronenergy.switch.tasmota_{self.device_id}"

        kwargs = {}

        if VENUS_OS and dbus:
            # Use a private D-Bus connection per service to avoid
            # path collision when multiple services register '/'.
            kwargs["bus"]      = dbus.SystemBus(private=True)
            kwargs["register"] = False  # add paths first, then register

        svc = VeDbusService(svc_name, **kwargs)

        svc.add_path("/Mgmt/ProcessName",    __file__)
        svc.add_path("/Mgmt/ProcessVersion", "1.4.1")
        svc.add_path("/Mgmt/Connection",     f"MQTT {MQTT_HOST}")

        svc.add_path("/ProductName", f"Tasmota ({self.friendly_name})")
        svc.add_path("/ProductId",   0xB040)

        svc.add_path("/DeviceInstance", self._stable_instance())

        svc.add_path("/Connected", 0)

        svc.add_path("/FirmwareVersion", "")
        svc.add_path("/HardwareVersion", "")

        svc.add_path("/Serial",     self.device_id)
        svc.add_path("/CustomName", self.friendly_name)

        # Module-level state: initialise as Connected; will be cleared on offline.
        svc.add_path("/State", MODULE_CONNECTED)

        for ch in range(self.channels):

            ch_name = f"CH{ch + 1}" if self.channels > 1 else "Relay"

            svc.add_path(f"/Channel/{ch}/Direction", 0)

            svc.add_path(
                f"/SwitchableOutput/{ch}/State",
                0,
                writeable=True,
                onchangecallback=lambda path, val, c=ch: self._on_state_write(c, val),
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Status",
                STATUS_OFF
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Name",
                ch_name
            )

            # ThreeState is writable on every device so a Node-RED flow or the GUI
            # can promote a toggle device to three-state without editing the JSON.
            # Writing 1 persists "three_state": true to the config and triggers a
            # service re-init via the config-change callback.
            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/ThreeState",
                1 if three_state else 0,
                writeable=True,
                onchangecallback=lambda _, val, c=ch: self._on_three_state_write(c, val),
            )

            if three_state:
                auto_mode = CFG.auto_mode(self.device_id)
                svc.add_path(
                    f"/SwitchableOutput/{ch}/Auto",
                    auto_mode,
                    writeable=True,
                )
                labels = CFG.labels(self.device_id)
                # /Settings/Labels is a Venus OS GUI extension (not in the official spec)
                # but recognised by gui-v2 for three-state switch label customisation.
                # Writing a new JSON array here persists the labels to the config file.
                svc.add_path(
                    f"/SwitchableOutput/{ch}/Settings/Labels",
                    json.dumps(labels),
                    writeable=True,
                    onchangecallback=lambda _, val, c=ch: self._on_labels_write(c, val),
                )

            default_name = f"{self.friendly_name} {ch_name}" if self.channels > 1 else self.friendly_name
            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/CustomName",
                CFG.custom_name(self.device_id, ch, default_name),
                writeable=True,
                onchangecallback=lambda _, val, c=ch: self._on_custom_name_write(c, val),
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/Type",
                sw_type,
                writeable=True
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/ValidTypes",
                valid_types
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/Function",
                2  # Manual
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/ValidFunctions",
                0b0000100
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/Group",
                CFG.group(self.device_id),
                writeable=True,
                onchangecallback=lambda _, val, c=ch: self._on_group_write(c, val),
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/ShowUIControl",
                1,
                writeable=True
            )

            svc.add_path(
                f"/SwitchableOutput/{ch}/Settings/Adjustable",
                1
            )

        # Register after all paths are added (new velib API)
        if VENUS_OS and dbus and hasattr(svc, "register"):
            svc.register()

        self._svc = svc

        log.info(
            "Registered %s (%d channel(s), type=%s)",
            svc_name,
            self.channels,
            "three-state" if three_state else "toggle"
        )

    def _stable_instance(self) -> int:
        return CFG.assign_instance(self.device_id)

    def set_online(self, online: bool):

        with self._lock:
            self._svc["/Connected"] = 1 if online else 0
            # FIX: reflect module state accurately in both directions.
            # When offline, set /State to invalid (None) so the GUI doesn't
            # show "Connected" for a device that has gone away.
            self._svc["/State"] = MODULE_CONNECTED if online else None

        log.info(
            "%s -> %s",
            self.device_id,
            "ONLINE" if online else "OFFLINE"
        )

    def set_power(self, channel: int, state_str: str):
        """Update D-Bus state from an incoming Tasmota POWER report.

        For plain toggle devices: maps ON->1, OFF->0 as before.
        For three-state devices: accumulates per-relay state then
        reverse-looks up the matching state value (0/1/2) from the
        relay_map.  All channels share the same logical three-state
        value so all /SwitchableOutput/<ch>/State paths are updated.
        """
        power_key   = "POWER" if self.channels == 1 else f"POWER{channel}"
        reverse_map = CFG.reverse_relay_map(self.device_id)

        with self._lock:
            if reverse_map:
                # Three-state path: accumulate relay state and resolve
                self._relay_state[power_key] = state_str.upper()
                observed  = frozenset(self._relay_state.items())
                state_val = reverse_map.get(observed)
                if state_val is None:
                    # Partial update - not all relays reported yet; skip
                    log.debug(
                        "%s waiting for more relay updates (%s)",
                        self.device_id,
                        self._relay_state
                    )
                    return
                # Don't overwrite a higher explicitly-set state (e.g. Auto=2) with a
                # lower inferred one (e.g. On=1) when the relay combination is ambiguous.
                # 'Auto' can only be cleared by an explicit write, not by a relay report.
                current = self._svc[f"/SwitchableOutput/0/State"]
                if current is not None and current > state_val:
                    log.debug(
                        "%s keeping explicit state %s, ignoring inferred %s from relay",
                        self.device_id, current, state_val
                    )
                    return
                on = state_val > 0
                log.debug(
                    "%s three-state relay=%s -> state %d",
                    self.device_id, self._relay_state, state_val
                )
                for ch in range(self.channels):
                    self._svc[f"/SwitchableOutput/{ch}/State"]  = state_val
                    self._svc[f"/SwitchableOutput/{ch}/Status"] = STATUS_ON if on else STATUS_OFF
            else:
                # Plain toggle path
                ch_index = channel - 1
                on       = state_str.upper() == "ON"
                self._svc[f"/SwitchableOutput/{ch_index}/State"]  = 1 if on else 0
                self._svc[f"/SwitchableOutput/{ch_index}/Status"] = STATUS_ON if on else STATUS_OFF
                log.debug(
                    "%s CH%d -> %s",
                    self.device_id,
                    channel,
                    state_str
                )

    def set_firmware(self, version: str):

        with self._lock:
            self._svc["/FirmwareVersion"] = version

    def update_name(self, friendly_name: str):

        with self._lock:
            self.friendly_name = friendly_name
            self._svc["/CustomName"] = friendly_name

    def _on_state_write(self, ch_index: int, new_value):
        """Called by Venus OS when the user writes to /SwitchableOutput/x/State.

        Three-state device (type 9): new_value is 0 (Off), 1 (On) or 2 (Auto).
        Each state maps to a set of relay commands via the relay_map in the config.

        Plain toggle device (type 1): new_value is 0 or 1.

        Must return True to accept the write, False to reject it.
        Returning None (implicit) causes velib_python to silently reject the write,
        so all GUI interactions would appear to do nothing.
        """
        relay_map = CFG.relay_map(self.device_id)

        if relay_map:
            # Three-state: look up relay commands for this state value (0/1/2)
            state_key = str(int(new_value))
            commands  = relay_map.get(state_key)
            if commands:
                label      = CFG.labels(self.device_id)
                state_name = label[int(new_value)] if int(new_value) < len(label) else state_key
                log.info("GUI->MQTT state=%s (%s) %s", state_key, state_name, commands)
                for power_key, cmd in commands.items():
                    topic = f"cmnd/{self.device_id}/{power_key}"
                    if self._mqtt_publish:
                        self._mqtt_publish(topic, cmd)
                    log.debug("  publish %s = %s", topic, cmd)
            else:
                log.warning("%s: no relay_map entry for state %s", self.device_id, state_key)
        else:
            # Plain toggle
            cmd       = "ON" if new_value else "OFF"
            power_key = "POWER" if self.channels == 1 else f"POWER{ch_index + 1}"
            topic     = f"cmnd/{self.device_id}/{power_key}"
            if self._mqtt_publish:
                self._mqtt_publish(topic, cmd)
            log.info("GUI->MQTT %s %s", topic, cmd)

        # FIX: return True so velib_python accepts the write and updates /State
        # on the D-Bus. Without this the GUI toggle appears to do nothing because
        # velib_python interprets a falsy return value as "reject the write".
        return True

    def _on_three_state_write(self, _: int, new_value):
        """Called when the GUI writes to /Settings/ThreeState.

        Persists the change to the config file and triggers a service re-init
        via the config-change callback so the device picks up the new type
        (toggle <-> three-state) without restarting the script.
        """
        enable = bool(new_value)
        log.info("%s: ThreeState -> %s (via D-Bus write)", self.device_id, enable)
        CFG.update_device_field(self.device_id, "three_state", enable, notify=True)
        return True

    def _on_custom_name_write(self, ch_index: int, new_value):
        """Called when the GUI writes to /Settings/CustomName. Persists to config."""
        if not isinstance(new_value, str) or not new_value.strip():
            return False
        log.info("%s CH%d: CustomName -> %r (via D-Bus write)", self.device_id, ch_index, new_value)
        CFG.update_device_field(self.device_id, f"custom_name_{ch_index}", new_value, notify=False)
        return True

    def _on_group_write(self, _: int, new_value):
        """Called when the GUI writes to /Settings/Group. Persists to config."""
        if not isinstance(new_value, str) or not new_value.strip():
            return False
        log.info("%s: Group -> %r (via D-Bus write)", self.device_id, new_value)
        CFG.update_device_field(self.device_id, "group", new_value, notify=False)
        return True

    def _on_labels_write(self, _: int, new_value):
        """Called when the GUI writes to /Settings/Labels.

        Expects a JSON-encoded list of three strings, e.g. '["Off","On","Auto"]'.
        Persists to the config file without triggering a full re-init.
        """
        try:
            labels = json.loads(new_value) if isinstance(new_value, str) else list(new_value)
            if not isinstance(labels, list) or len(labels) != 3:
                log.warning("%s: Labels must be a JSON array of 3 strings", self.device_id)
                return False
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("%s: invalid Labels value: %r", self.device_id, new_value)
            return False
        log.info("%s: Labels -> %s (via D-Bus write)", self.device_id, labels)
        CFG.update_device_field(self.device_id, "labels", labels, notify=False)
        return True

    def inject_mqtt_publish(self, fn):
        self._mqtt_publish = fn


# =====================================================================
# TasmotaDiscovery
# =====================================================================
class TasmotaDiscovery:

    def __init__(self):

        self._devices: dict[str, TasmotaDevice] = {}
        self._lock = threading.Lock()
        self._mqttc = None

        # Devices seen via LWT/mDNS but not yet confirmed as switches.
        # Format: { device_id: threading.Timer }
        # The Timer fires PROBE_TIMEOUT seconds after the probe is sent
        # and discards the entry if no STATUS11 response has arrived.
        self._pending: dict[str, threading.Timer] = {}
        self._pending_lock = threading.Lock()

        CFG.register_callback(self._on_config_change)

    # -----------------------------------------------------------------
    # Config change handler
    # -----------------------------------------------------------------
    def _on_config_change(self, added: set, removed: set, modified: set):
        """Re-init D-Bus service for any registered device whose config changed.

        Runs in a background thread so this is safe to call from within a
        D-Bus write callback (e.g. ThreeState toggle from the GUI).
        """
        reinit = (added | modified | removed) & set(self._devices)
        if not reinit:
            return
        log.info("Config change - re-initing: %s", reinit)

        def _do():
            # Snapshot device references without holding the lock during re-init.
            with self._lock:
                devs = [self._devices[d] for d in reinit if d in self._devices]
            for dev in devs:
                dev._init_service()

        threading.Thread(target=_do, daemon=True, name="config-reinit").start()

    # -----------------------------------------------------------------
    # MQTT setup
    # -----------------------------------------------------------------
    def start_mqtt(self):

        self._mqttc = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id="venus-tasmota-discovery",
            clean_session=True
        )

        if MQTT_USER:
            self._mqttc.username_pw_set(MQTT_USER, MQTT_PASS)

        self._mqttc.on_connect    = self._on_connect
        self._mqttc.on_disconnect = self._on_disconnect
        self._mqttc.on_message    = self._on_message

        self._mqttc.connect_async(
            MQTT_HOST,
            MQTT_PORT,
            keepalive=60
        )

        self._mqttc.loop_start()

        log.info(
            "MQTT connecting to %s:%d",
            MQTT_HOST,
            MQTT_PORT
        )

    def _mqtt_publish(self, topic: str, payload: str):

        if self._mqttc:
            self._mqttc.publish(topic, payload, retain=False)

    def _on_connect(self, client, userdata, flags, rc):

        if rc != 0:
            log.error("MQTT connect failed rc=%d", rc)
            return

        log.info("MQTT connected")

        for topic in [
            "tele/+/LWT",
            "tele/+/INFO1",
            "tele/+/INFO3",         # carries IP address on modern firmware
            "stat/+/STATUS",
            "stat/+/STATUS11",      # probe response: contains POWER keys if switch
            "stat/+/POWER",
            "stat/+/POWER1",
            "stat/+/POWER2",
            "stat/+/POWER3",
            "stat/+/POWER4",
        ]:
            client.subscribe(topic)

        log.info("Subscribed to Tasmota wildcard topics")

        threading.Timer(2, self._poll_all).start()

    def _on_disconnect(self, client, userdata, rc):

        log.warning(
            "MQTT disconnected (rc=%d) - reconnecting",
            rc
        )

    def _on_message(self, client, userdata, msg):

        try:
            self._dispatch(
                msg.topic,
                msg.payload.decode(
                    "utf-8",
                    errors="replace"
                ).strip()
            )

        except Exception as exc:
            log.exception(
                "Error handling message %s: %s",
                msg.topic,
                exc
            )

    def _dispatch(self, topic: str, payload: str):

        parts = topic.split("/")

        if len(parts) < 3:
            return

        device_id = parts[1]
        subtopic  = parts[-1]

        # -------------------------------------------------------------
        # LWT
        # -------------------------------------------------------------
        if subtopic == "LWT":

            online = payload.lower() == "online"

            if online:
                # Don't register yet  -  probe first to confirm it has relays.
                self._probe_device(device_id)

            else:
                # Device going offline: mark existing registered device offline.
                # If still pending (probe not yet answered), cancel and discard.
                self._cancel_probe(device_id)

                dev = self._get(device_id)

                if dev:
                    dev.set_online(False)

            return

        # -------------------------------------------------------------
        # STATUS11  -  probe response
        # Only register the device if StatusSTS contains a POWER key.
        # -------------------------------------------------------------
        if subtopic == "STATUS11":

            # Only act on devices we're actively probing.
            if not self._is_pending(device_id):
                # Already registered  -  ignore (can arrive during normal polls).
                return

            try:
                data = json.loads(payload)
                sts  = data.get("StatusSTS", {})

            except (json.JSONDecodeError, TypeError):
                log.warning(
                    "%s STATUS11 parse error  -  treating as non-switch",
                    device_id
                )
                self._cancel_probe(device_id)
                return

            has_relay = any(
                k.upper().startswith("POWER")
                for k in sts.keys()
            )

            if not has_relay:
                log.info(
                    "%s has no POWER keys in STATUS11  -  sensor device, skipping",
                    device_id
                )
                self._cancel_probe(device_id)
                return

            # It's a switch. Cancel the timeout timer and promote to registered.
            log.info(
                "%s confirmed as switch device (STATUS11 keys: %s)",
                device_id,
                [k for k in sts.keys() if k.upper().startswith("POWER")]
            )

            self._cancel_probe(device_id, confirmed=True)

            # Create the device entry with a preliminary name.
            dev = self._ensure_device(device_id, device_id, "unknown")
            dev.set_online(True)

            # Request full status to populate friendly name and channel count.
            self._mqtt_publish(f"cmnd/{device_id}/Status", "0")

            # Seed initial relay states from the STATUS11 response.
            self._apply_status_sts(dev, sts)

            return

        # -------------------------------------------------------------
        # INFO1
        # -------------------------------------------------------------
        if subtopic == "INFO1":

            try:
                info = json.loads(payload)

            except json.JSONDecodeError:
                return

            # Only update already-registered devices from INFO1.
            # If still pending, the device has not been confirmed as a switch yet.
            dev = self._get(device_id)

            if dev:
                dev.set_firmware(info.get("Version", ""))
                dev.set_online(True)

                fname = info.get("FriendlyName1", "")
                if fname and fname != device_id:
                    dev.update_name(fname)

            return

        # -------------------------------------------------------------
        # INFO3  -  carries IP address on modern Tasmota firmware
        # -------------------------------------------------------------
        if subtopic == "INFO3":

            try:
                info = json.loads(payload)
                ip   = info.get("IPAddress", "")

            except (json.JSONDecodeError, TypeError):
                return

            dev = self._get(device_id)

            if dev and ip:
                with dev._lock:
                    dev.ip = ip

            return

        # -------------------------------------------------------------
        # STATUS (Status 0 response  -  friendly name + channel count)
        # -------------------------------------------------------------
        if subtopic == "STATUS":

            try:
                data = json.loads(payload)

                status = data.get("Status", {})

                names = status.get(
                    "FriendlyName",
                    [device_id]
                )

                fname    = names[0] if names else device_id
                channels = max(1, len(names))

            except (json.JSONDecodeError, TypeError):
                return

            # Only update already-registered (confirmed) devices.
            dev = self._get(device_id)

            if dev:
                if fname and fname != device_id:
                    dev.update_name(fname)

                dev.set_online(True)

                # Re-init service if channel count has changed.
                # This is rare but handles multi-channel devices whose
                # channel count wasn't known at initial registration.
                if channels != dev.channels:
                    log.info(
                        "%s channel count changed %d -> %d, re-registering",
                        device_id,
                        dev.channels,
                        channels
                    )
                    dev.channels = channels
                    dev._init_service()

            return

        # -------------------------------------------------------------
        # POWER
        # -------------------------------------------------------------
        if subtopic.startswith("POWER"):

            channel = 1

            if len(subtopic) > 5:

                try:
                    channel = int(subtopic[5:])

                except ValueError:
                    pass

            dev = self._get(device_id)

            if dev:
                dev.set_power(channel, payload)

            return

    # -----------------------------------------------------------------
    # Probe management
    # -----------------------------------------------------------------
    def _probe_device(self, device_id: str):
        """Send a Status 11 probe to check whether device has relays."""

        with self._pending_lock:

            if device_id in self._pending:
                # Already probing  -  reset the timeout.
                self._pending[device_id].cancel()

            # Register a timeout that discards the probe if no response.
            timer = threading.Timer(
                PROBE_TIMEOUT,
                self._probe_timeout,
                args=(device_id,)
            )
            timer.daemon = True
            timer.start()

            self._pending[device_id] = timer

        log.debug("Probing %s (Status 11)", device_id)

        self._mqtt_publish(f"cmnd/{device_id}/Status", "11")

    def _probe_timeout(self, device_id: str):
        """Called when a probed device never replied  -  discard it."""

        with self._pending_lock:

            if device_id not in self._pending:
                return  # already resolved

            del self._pending[device_id]

        log.info(
            "%s probe timed out after %ds  -  no STATUS11 response, ignoring",
            device_id,
            PROBE_TIMEOUT
        )

    def _cancel_probe(self, device_id: str, confirmed: bool = False):
        """Cancel the probe timer. confirmed=True means we got a valid response."""

        with self._pending_lock:

            timer = self._pending.pop(device_id, None)

            if timer:
                timer.cancel()

        if not confirmed:
            log.debug("Probe cancelled for %s", device_id)

    def _is_pending(self, device_id: str) -> bool:

        with self._pending_lock:
            return device_id in self._pending

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _apply_status_sts(self, dev: TasmotaDevice, sts: dict):
        """Seed relay states from the StatusSTS block in a STATUS11 response."""

        for key, val in sts.items():

            upper = key.upper()

            if not upper.startswith("POWER"):
                continue

            if upper == "POWER":
                channel = 1
            else:
                try:
                    channel = int(upper[5:])
                except ValueError:
                    continue

            dev.set_power(channel, str(val))

    # -----------------------------------------------------------------
    # Device registry
    # -----------------------------------------------------------------
    def _ensure_device(
        self,
        device_id: str,
        friendly_name: str,
        ip: str,
        channels: int = 1
    ) -> TasmotaDevice:

        with self._lock:

            if device_id not in self._devices:

                log.info(
                    "New device: %s name=%r ip=%s channels=%d",
                    device_id,
                    friendly_name,
                    ip,
                    channels
                )

                dev = TasmotaDevice(
                    device_id,
                    friendly_name,
                    ip,
                    channels
                )

                dev.inject_mqtt_publish(self._mqtt_publish)

                self._devices[device_id] = dev

                CFG.register_device(device_id, friendly_name, ip, channels)

            else:
                # Device already registered  -  update name only if we now
                # have a real name and previously only had the device_id
                # placeholder. Channel count changes are handled in STATUS.
                dev = self._devices[device_id]

                if (
                    friendly_name != device_id
                    and dev.friendly_name == device_id
                ):
                    dev.update_name(friendly_name)
                    CFG.register_device(device_id, friendly_name, ip, dev.channels)

        return self._devices[device_id]

    def _get(self, device_id: str) -> Optional[TasmotaDevice]:

        with self._lock:
            return self._devices.get(device_id)

    # -----------------------------------------------------------------
    # Polling
    # -----------------------------------------------------------------
    def _poll_all(self):
        """Send a power query to all already-registered devices."""

        with self._lock:
            ids = list(self._devices.keys())

        for did in ids:
            self._mqttc.publish(
                f"cmnd/{did}/Power",
                ""
            )

        log.debug(
            "Polled %d device(s)",
            len(ids)
        )

    def _start_poll_timer(self):

        def _loop():

            while True:
                time.sleep(POLL_INTERVAL)
                self._poll_all()

        threading.Thread(
            target=_loop,
            daemon=True,
            name="poll-timer"
        ).start()

    # -----------------------------------------------------------------
    # mDNS
    # -----------------------------------------------------------------
    def _start_mdns(self):

        if not ZEROCONF_AVAILABLE:
            log.info("zeroconf not installed - mDNS disabled")
            return

        def _on_service(zeroconf, service_type, name, state_change):

            if state_change is not ServiceStateChange.Added:
                return

            info = zeroconf.get_service_info(service_type, name)

            if not info:
                return

            server = info.server.rstrip(".")

            if "tasmota" not in server.lower():
                return

            ip = (
                socket.inet_ntoa(info.addresses[0])
                if info.addresses
                else "unknown"
            )

            device_id = (
                server
                .replace(".local", "")
                .replace(".", "_")
            )

            log.info(
                "mDNS found: %s @ %s  -  probing",
                device_id,
                ip
            )

            # Use the same probe path as LWT: don't register until
            # STATUS11 confirms it has relay outputs.
            self._probe_device(device_id)

        zc = Zeroconf()

        ServiceBrowser(
            zc,
            "_http._tcp.local.",
            handlers=[_on_service]
        )

        log.info("mDNS browser started")

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------
    def run(self):

        if VENUS_OS and DBusGMainLoop:
            DBusGMainLoop(set_as_default=True)

        CFG.start_watcher()

        self.start_mqtt()

        self._start_mdns()

        self._start_poll_timer()

        if VENUS_OS and GLib:

            log.info("Running GLib main loop")

            GLib.MainLoop().run()

        else:

            log.info("Dry-run mode - Ctrl+C to stop")

            try:
                while True:
                    time.sleep(1)

            except KeyboardInterrupt:
                log.info("Stopped")


# =====================================================================
# Main entry
# =====================================================================
if __name__ == "__main__":

    try:
        log.info("Creating discovery instance")

        discovery = TasmotaDiscovery()

        log.info("Starting discovery.run()")

        discovery.run()

    except KeyboardInterrupt:
        log.info("Stopped by user")

    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
