# -*- coding: utf-8 -*-
"""Tasmota <-> Victron Venus OS bridge.

Exposes Tasmota relays as ``com.victronenergy.switch`` services and, for
Tasmota devices with energy monitoring, as an AC meter service
(``com.victronenergy.acload`` by default).

The D-Bus surface follows the official specification:
https://github.com/victronenergy/venus/wiki/dbus#switch

Notes on the three-state switch (Settings/Type == 9)
----------------------------------------------------
Per the spec, ``/State`` is *always* the plain on/off state of the channel.
The third position of a three-state switch is the separate ``/Auto`` path:

    /SwitchableOutput/x/State  0 = Off, 1 = On
    /SwitchableOutput/x/Auto   0 = Manual (user drives /State from the UI)
                               1 = Auto   (driver / Node-RED drives /State,
                                           UI control is locked out)

Earlier versions of this driver encoded "Auto" as ``/State == 2``, which the
GUI never writes and never renders, so the third position appeared dead.

Notes on the momentary button (Settings/Type == 0)
--------------------------------------------------
A momentary channel is a push button rather than a latch: a write of 1 to
``/State`` closes the relay for ``pulse_ms`` (default and minimum 600ms) and
then releases it again.  A write of 0 is the button coming back up and
deliberately does *not* cut the pulse short - a gate or garage-door
controller needs a solid contact, and a quick tap in the GUI would otherwise
produce a pulse a few milliseconds long.

Per the spec the output is also forced back to its inactive state whenever a
channel becomes momentary, so a switch that happened to be on cannot sit
there energised underneath a button that reads as released.

Design rules that keep this driver stable
-----------------------------------------
* A device's D-Bus service is created exactly once and then only ever
  updated in place.  Nothing in normal operation - changing type, group,
  labels, custom name or auto mode - re-registers a service.  Re-registering
  a well-known bus name while the old connection still owns it is what
  produced duplicate/ghost ``com.victronenergy.switch.tasmota_*`` services.
* All D-Bus access happens on the GLib main-loop thread.  Value updates are
  posted (fire and forget) so a busy or wedged main loop can never stall the
  MQTT network thread; only the rare service-creation path waits.
* A whole-process flock guard makes it impossible for two copies of this
  script to register the same names.
"""

from __future__ import annotations

import os
import sys
import json
import time
import fcntl
import signal
import inspect
import logging
import tempfile
import threading
import socket
from typing import Optional, List, Dict, Any

import paho.mqtt.client as mqtt

VERSION = "2.1.0"

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger("tasmota_discovery")


def _env_int(name: str, default: int) -> int:
    """int(os.environ[name]) that never kills the process on a bad value."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer - using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
CONFIG_PATH           = os.environ.get("TASMOTA_CONFIG", "/data/tasmota_config.json")
CONFIG_WATCH_INTERVAL = _env_int("TASMOTA_CONFIG_WATCH", 10)

MQTT_HOST     = os.environ.get("TASMOTA_MQTT_HOST", "192.168.10.80")
MQTT_PORT     = _env_int("TASMOTA_MQTT_PORT", 1883)
MQTT_USER     = os.environ.get("TASMOTA_MQTT_USER", "")
MQTT_PASS     = os.environ.get("TASMOTA_MQTT_PASS", "")

# A client id must be unique on the broker.  Two clients sharing one are
# disconnected by the broker in turn, each immediately reconnecting and
# evicting the other - which presents as every device flapping between
# connected and disconnected forever.  The pid suffix means a stale copy of
# this driver (or of an older version, whose lock file lives elsewhere)
# cannot fight this one.  Nothing is stored per session - clean_session is
# on - so the changing id costs nothing.
MQTT_CLIENT_ID = os.environ.get(
    "TASMOTA_MQTT_CLIENT_ID",
    f"venus-tasmota-discovery-{os.getpid()}",
)

# How often registered devices are re-queried (seconds).
POLL_INTERVAL = _env_int("TASMOTA_POLL_INTERVAL", 30)

# How long to wait for a `Status 0` probe response before discarding a
# candidate device (seconds).
PROBE_TIMEOUT = _env_int("TASMOTA_PROBE_TIMEOUT", 15)

# Minimum gap between probes of the same device, so a device whose LWT is
# flapping cannot spawn a probe storm (seconds).
PROBE_MIN_INTERVAL = _env_int("TASMOTA_PROBE_MIN_INTERVAL", 120)

# Upper bound on how long a worker thread waits for a D-Bus op that must run
# on the GLib main thread.  Only service creation uses the blocking path.
MAIN_THREAD_TIMEOUT = _env_int("TASMOTA_MAIN_THREAD_TIMEOUT", 30)

# Momentary channels: how long one press holds the relay closed.  The floor
# is deliberate - a shorter contact is not reliably registered by the gate,
# garage-door or doorbell controller on the other side of the relay.
DEFAULT_PULSE_MS = _env_int("TASMOTA_PULSE_MS", 600)
MIN_PULSE_MS     = 600
MAX_PULSE_MS     = 60_000

# Optionally kick discovery by asking the Tasmota group topic for status.
# Off by default: Tasmota publishes LWT retained, so simply subscribing
# already reveals every device that is online.
GROUP_PROBE = _env_bool("TASMOTA_GROUP_PROBE", False)
GROUP_TOPIC = os.environ.get("TASMOTA_GROUP_TOPIC", "tasmotas")

# Create an AC meter service for devices that report an ENERGY block.
METER_ENABLED      = _env_bool("TASMOTA_METER", True)
METER_DEFAULT_ROLE = os.environ.get("TASMOTA_METER_ROLE", "acload")

VALID_METER_ROLES = ("acload", "grid", "pvinverter", "genset", "heatpump")

# ---------------------------------------------------------------------
# Venus OS detection
# ---------------------------------------------------------------------
VENUS_OS   = os.path.exists("/opt/victronenergy")
VELIB_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"

try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    log.info("dbus/GLib imports successful")

except Exception as exc:  # pragma: no cover - depends on host
    log.warning("dbus/GLib not available (%s) - running without D-Bus", exc)

    dbus          = None
    DBusGMainLoop = None
    GLib          = None

try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

    ZEROCONF_AVAILABLE = True
    log.info("zeroconf available")

except Exception:
    ZEROCONF_AVAILABLE = False
    log.info("zeroconf not installed - mDNS discovery disabled")


# =====================================================================
# D-Bus specification constants
# https://github.com/victronenergy/venus/wiki/dbus#switch
# =====================================================================
class SwitchType:
    """/SwitchableOutput/x/Settings/Type"""
    MOMENTARY            = 0
    TOGGLE               = 1
    DIMMABLE             = 2
    TEMPERATURE_SETPOINT = 3
    STEPPED              = 4
    SLAVE                = 5
    DROPDOWN             = 6
    BASIC_SLIDER         = 7
    NUMERIC_INPUT        = 8
    THREE_STATE          = 9
    BILGE_PUMP           = 10
    RGB                  = 11
    CCT                  = 12
    RGBW                 = 13


class SwitchStatus:
    """/SwitchableOutput/x/Status (bit flags)."""
    OFF              = 0x00
    POWERED          = 0x01
    TRIPPED          = 0x02
    OVER_TEMPERATURE = 0x04
    OUTPUT_FAULT     = 0x08
    ON               = 0x09   # per spec: the "On" value, output-fault bit included
    SHORT_FAULT      = 0x10
    DISABLED         = 0x20
    BYPASSED         = 0x40
    EXT_CONTROL      = 0x80


class ModuleState:
    """/State - module wide state, offset by 0x100."""
    CONNECTED           = 0x100
    OVER_TEMPERATURE    = 0x101
    TEMPERATURE_WARNING = 0x102
    CHANNEL_FAULT       = 0x103
    CHANNEL_TRIPPED     = 0x104
    UNDER_VOLTAGE       = 0x105


class SwitchFunction:
    """/SwitchableOutput/x/Settings/Function"""
    ALARM               = 0
    GENERATOR_START     = 1
    MANUAL              = 2
    TANK_PUMP           = 3
    TEMPERATURE         = 4
    GENSET_HELPER_RELAY = 5
    OPPORTUNITY_LOAD    = 6


# /Channel/x/Direction
DIRECTION_OUTPUT = 0

# ValidTypes / ValidFunctions are bit fields: bit N corresponds to enum
# value N of the matching Type / Function path.  Advertising momentary,
# toggle and three-state together is what lets the mode be picked from the
# GUI's switch settings instead of by hand-editing the config file.
VALID_TYPES = (
    (1 << SwitchType.MOMENTARY)
    | (1 << SwitchType.TOGGLE)
    | (1 << SwitchType.THREE_STATE)
)                                                                            # 515
VALID_FUNCTIONS = (1 << SwitchFunction.MANUAL)                               # 4

# Types this driver knows how to drive over MQTT.
SUPPORTED_TYPES = (SwitchType.MOMENTARY, SwitchType.TOGGLE, SwitchType.THREE_STATE)

TYPE_NAMES = {
    SwitchType.MOMENTARY:   "momentary",
    SwitchType.TOGGLE:      "toggle",
    SwitchType.THREE_STATE: "three-state",
}

# /Settings/ShowUIControl - 0bxx1 == show in every UI.
SHOW_UI_CONTROL = 1

# /Settings/Group and /Settings/CustomName are capped at 32 bytes of utf-8.
MAX_LABEL_BYTES = 32

PRODUCT_ID = 0xB040

DEFAULT_LABELS = ["Off", "On", "Auto"]


def _clip_utf8(text: str, limit: int = MAX_LABEL_BYTES) -> str:
    """Trim a string so its utf-8 encoding fits within `limit` bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


# =====================================================================
# ConfigManager
# =====================================================================
class ConfigManager:
    """Owns tasmota_config.json.

    Every device the driver has seen gets an entry.  Only a handful of keys
    are meaningful; the rest are informational and written once at discovery.

    Per-device keys::

        three_state       bool   register the channel as switch type 9
        momentary         bool   register the channel as switch type 0, a
                                 push button that pulses the relay
        pulse_ms          int    momentary hold time in ms, default and
                                 minimum 600
        auto              0|1    three-state Auto position (see /Auto)
        labels            [str]  three UI labels, default Off / On / Auto
        group             str    /Settings/Group (groups switches on one card)
        custom_name_<ch>  str    /Settings/CustomName for that channel
        instance          int    stable /DeviceInstance for the switch service
        state_relay_map   dict   multi-relay devices driven as one logical
                                 switch: which relays make up Off (0) and
                                 On (1), e.g.
                                 {"0": {"POWER1": "OFF", "POWER2": "OFF"},
                                  "1": {"POWER1": "ON",  "POWER2": "ON"}}
        meter             bool   expose an AC meter service (default: yes,
                                 when the device reports ENERGY telemetry)
        meter_role        str    acload | grid | pvinverter | genset
        meter_instance    int    stable /DeviceInstance for the meter service
        meter_position    int    pvinverter only: 0 = AC in 1, 1 = AC out,
                                 2 = AC in 2
        meter_phases      list   which phases to publish, e.g. [1, 3] or
                                 "L1,L3".  Unset = every phase the device
                                 reports.
        meter_split       bool   expose each phase as its own single-phase
                                 meter device instead of one three-phase
                                 device
        meter_names       dict   split mode display names, e.g.
                                 {"L1": "Kitchen", "L3": "Geyser"}

    All disk writes are atomic (temp file + rename) and serialised, so two
    config updates can never interleave and lose keys.
    """

    def __init__(self, path: str):
        self.path     = path
        self._lock    = threading.RLock()   # guards _devices / _mtime
        self._io_lock = threading.RLock()   # serialises read-modify-write
        self._devices: Dict[str, dict] = {}
        self._mtime   = 0.0
        self._callbacks: List[Any] = []
        self._load()

    # -- disk ---------------------------------------------------------
    def _read_raw(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            log.warning("Config read error (%s)", exc)
            return {}

    def _write_raw(self, raw: dict) -> bool:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tasmota_config.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            log.warning("Could not write config file: %s", exc)
            return False

        # Remember the mtime we just produced so the watcher does not treat
        # our own write as an external edit and fire callbacks for it.
        with self._lock:
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                pass
        return True

    def _load(self):
        """Pick up external edits to the config file."""
        if not os.path.exists(self.path):
            with self._lock:
                had = set(self._devices)
                self._devices = {}
                self._mtime   = 0.0
            if had:
                log.info("Config file removed - reverting to defaults")
                self._fire(had)
            return

        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        with self._lock:
            if mtime == self._mtime:
                return

        raw      = self._read_raw()
        new_devs = raw.get("devices", {})
        if not isinstance(new_devs, dict):
            log.warning("Config 'devices' is not an object - ignoring this revision")
            return

        with self._lock:
            old_devs = self._devices
            changed = {
                d for d in set(new_devs) | set(old_devs)
                if new_devs.get(d) != old_devs.get(d)
            }
            self._devices = new_devs
            self._mtime   = mtime

        if changed:
            log.info("Config reloaded - changed: %s", sorted(changed))
            self._fire(changed)

    def _fire(self, changed: set):
        for cb in list(self._callbacks):
            try:
                cb(changed)
            except Exception:
                log.exception("Config callback error")

    def register_callback(self, fn):
        self._callbacks.append(fn)

    def start_watcher(self):
        def _loop():
            while True:
                time.sleep(CONFIG_WATCH_INTERVAL)
                try:
                    self._load()
                except Exception:
                    log.exception("Config watcher error")

        threading.Thread(target=_loop, daemon=True, name="config-watcher").start()
        log.info("Config watcher started (%s every %ds)", self.path, CONFIG_WATCH_INTERVAL)

    # -- accessors ----------------------------------------------------
    def _dev(self, device_id: str) -> dict:
        with self._lock:
            dev = self._devices.get(device_id, {})
            return dev if isinstance(dev, dict) else {}

    def is_three_state(self, device_id: str) -> bool:
        return bool(self._dev(device_id).get("three_state"))

    def is_momentary(self, device_id: str) -> bool:
        return bool(self._dev(device_id).get("momentary"))

    def switch_type(self, device_id: str) -> int:
        """Resolve the configured /Settings/Type.

        three_state wins when both flags are set: a relay map describes
        behaviour a push button cannot express.
        """
        if self.is_three_state(device_id):
            return SwitchType.THREE_STATE
        if self.is_momentary(device_id):
            return SwitchType.MOMENTARY
        return SwitchType.TOGGLE

    def pulse_ms(self, device_id: str) -> int:
        """Momentary hold time, clamped to MIN_PULSE_MS..MAX_PULSE_MS.

        Anything shorter is not reliably seen by the hardware being pulsed;
        anything longer has stopped being a push button.
        """
        raw = self._dev(device_id).get("pulse_ms", DEFAULT_PULSE_MS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            log.warning("%s: invalid pulse_ms %r - using %d",
                        device_id, raw, DEFAULT_PULSE_MS)
            return DEFAULT_PULSE_MS
        if value < MIN_PULSE_MS:
            log.warning("%s: pulse_ms %d is below the %dms minimum - using %dms",
                        device_id, value, MIN_PULSE_MS, MIN_PULSE_MS)
        return max(MIN_PULSE_MS, min(value, MAX_PULSE_MS))

    def auto_mode(self, device_id: str) -> int:
        try:
            return 1 if int(self._dev(device_id).get("auto", 0)) else 0
        except (TypeError, ValueError):
            return 0

    def labels(self, device_id: str) -> List[str]:
        labels = self._dev(device_id).get("labels")
        if (isinstance(labels, list) and len(labels) == 3
                and all(isinstance(x, str) for x in labels)):
            return labels
        return list(DEFAULT_LABELS)

    def group(self, device_id: str) -> str:
        value = self._dev(device_id).get("group", "Tasmota")
        return _clip_utf8(value if isinstance(value, str) else "Tasmota")

    def custom_name(self, device_id: str, channel: int, fallback: str) -> str:
        value = self._dev(device_id).get(f"custom_name_{channel}")
        if not isinstance(value, str) or not value.strip():
            value = fallback
        return _clip_utf8(value)

    def relay_map(self, device_id: str) -> Dict[str, Dict[str, str]]:
        """Relay combinations that make up the Off (0) and On (1) states.

        Only meaningful for multi-relay devices that must be driven as a
        single logical switch.  Returns {} for ordinary devices, where each
        Tasmota relay maps one-to-one onto a D-Bus channel.

        Configs written by version 1.x may contain a "2" entry for the old
        (non-spec) "/State == 2 means Auto" encoding.  Auto is now the
        separate /Auto path, so that entry is ignored.
        """
        raw = self._dev(device_id).get("state_relay_map")
        if not isinstance(raw, dict):
            return {}

        result: Dict[str, Dict[str, str]] = {}
        for key, commands in raw.items():
            if not isinstance(commands, dict):
                continue
            try:
                state = int(key)
            except (TypeError, ValueError):
                continue
            if state not in (0, 1):
                continue
            result[str(state)] = {
                str(k).upper(): str(v).upper() for k, v in commands.items()
            }
        return result

    def reverse_relay_map(self, device_id: str) -> Dict[frozenset, int]:
        """frozenset of (relay, value) pairs -> 0 / 1."""
        reverse: Dict[frozenset, int] = {}
        for state, commands in self.relay_map(device_id).items():
            reverse[frozenset(commands.items())] = int(state)
        return reverse

    def meter_enabled(self, device_id: str) -> bool:
        if not METER_ENABLED:
            return False
        return bool(self._dev(device_id).get("meter", True))

    def meter_role(self, device_id: str) -> str:
        role = self._dev(device_id).get("meter_role", METER_DEFAULT_ROLE)
        if role not in VALID_METER_ROLES:
            log.warning("%s: unknown meter_role %r - using acload", device_id, role)
            return "acload"
        return role

    def meter_position(self, device_id: str) -> int:
        try:
            return int(self._dev(device_id).get("meter_position", 0))
        except (TypeError, ValueError):
            return 0

    def meter_split(self, device_id: str) -> bool:
        """True: one single-phase meter device per phase instead of one
        three-phase device."""
        return bool(self._dev(device_id).get("meter_split", False))

    def meter_phases(self, device_id: str, available: int) -> List[int]:
        """Which phases to publish, as 1-based numbers.

        ``meter_phases`` accepts [1, 3] / ["L1", "L3"] / "L1,L3" / "1,3".
        Unset means every phase the device actually reports.  Entries the
        device does not report are dropped; if nothing is left the filter is
        ignored rather than silently producing a meter with no data.
        """
        available = max(1, min(3, available))
        every     = list(range(1, available + 1))

        raw = self._dev(device_id).get("meter_phases")
        if raw is None:
            return every

        items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        chosen: List[int] = []
        for item in items:
            text = str(item).strip().upper().lstrip("L")
            try:
                number = int(text)
            except ValueError:
                continue
            if 1 <= number <= available and number not in chosen:
                chosen.append(number)

        if not chosen:
            log.warning(
                "%s: meter_phases=%r selects no phase this device reports "
                "(it has %d) - using all of them",
                device_id, raw, available,
            )
            return every
        return sorted(chosen)

    def meter_name(self, device_id: str, phase: Optional[int], fallback: str) -> str:
        """Per-phase display name, e.g. {"L1": "Kitchen", "L3": "Geyser"}."""
        names = self._dev(device_id).get("meter_names")
        if isinstance(names, dict) and phase is not None:
            for key in (f"L{phase}", str(phase)):
                value = names.get(key)
                if isinstance(value, str) and value.strip():
                    return _clip_utf8(value)
        return _clip_utf8(fallback)

    # -- instance allocation ------------------------------------------
    def assign_instance(self, device_id: str, key: str = "instance",
                        namespace: Optional[str] = None) -> int:
        """Return this device's stable /DeviceInstance for a service class.

        Instances only have to be unique per service class, so switches and
        meters are numbered independently - hence `namespace`, which is the
        set of config keys that compete for the same numbers.  Every meter
        key shares the "meter_instance" namespace, because a plug split into
        three single-phase services still produces three com.victronenergy
        .acload names that must not collide.

        The number is persisted, so a device keeps the same instance - and
        therefore the same position and settings in the GUI - across
        restarts.
        """
        namespace = namespace or key
        with self._io_lock:
            with self._lock:
                existing = self._devices.get(device_id, {}).get(key)
                if existing is not None:
                    try:
                        return int(existing)
                    except (TypeError, ValueError):
                        pass
                used = set()
                for dev in self._devices.values():
                    if not isinstance(dev, dict):
                        continue
                    for name, value in dev.items():
                        if name != namespace and not name.startswith(namespace + "_"):
                            continue
                        try:
                            used.add(int(value))
                        except (TypeError, ValueError):
                            continue
                number = 0
                while number in used:
                    number += 1
                self._devices.setdefault(device_id, {})[key] = number

            self.set(device_id, key, number)
            return number

    # -- mutation -----------------------------------------------------
    def set(self, device_id: str, key: str, value, notify: bool = False):
        """Persist one field. Serialised against every other config write."""
        old = None
        with self._io_lock:
            raw     = self._read_raw()
            devices = raw.setdefault("devices", {})
            if not isinstance(devices, dict):
                devices = raw["devices"] = {}
            entry = devices.setdefault(device_id, {})
            if not isinstance(entry, dict):
                entry = devices[device_id] = {}

            if entry.get(key) == value:
                # Already on disk; just make sure memory agrees.
                with self._lock:
                    self._devices.setdefault(device_id, {})[key] = value
                return

            entry[key] = value
            if not self._write_raw(raw):
                return

            with self._lock:
                old = self._devices.get(device_id, {}).get(key)
                self._devices.setdefault(device_id, {})[key] = value

        log.info("Config: %s.%s = %r", device_id, key, value)
        if notify and old != value:
            self._fire({device_id})

    def register_device(self, device_id: str, friendly_name: str, ip: str, channels: int):
        """Create the config stub for a newly discovered device.

        Existing entries are never replaced, so hand-written keys such as
        state_relay_map survive rediscovery.
        """
        with self._io_lock:
            raw     = self._read_raw()
            devices = raw.setdefault("devices", {})
            if not isinstance(devices, dict):
                devices = raw["devices"] = {}

            entry = devices.get(device_id)
            if isinstance(entry, dict) and entry:
                # Only refresh the informational fields.
                entry["_name"]     = friendly_name
                entry["_ip"]       = ip
                entry["_channels"] = channels
            else:
                devices[device_id] = {
                    "_name":       friendly_name,
                    "_ip":         ip,
                    "_channels":   channels,
                    # Set to true - or pick "Three-state switch" in the GUI's
                    # switch settings - to use Off / On / Auto.
                    "three_state": False,
                    # Set to true - or pick "Momentary" in the GUI's switch
                    # settings - to turn this into a push button that pulses
                    # the relay for "pulse_ms" (default and minimum 600ms).
                    "momentary":   False,
                }

            if not self._write_raw(raw):
                return
            with self._lock:
                self._devices[device_id] = devices[device_id]

        log.debug("Config stub written for %s", device_id)


CFG = ConfigManager(CONFIG_PATH)


# =====================================================================
# GLib main-thread marshalling
# =====================================================================
def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def dbus_post(fn):
    """Run fn() on the GLib main thread; do not wait for the result.

    Used for every property update.  GLib runs idle callbacks in submission
    order, so updates stay ordered relative to each other and relative to the
    service creation that precedes them.  Never blocking here is what keeps a
    slow main loop from backing up the MQTT network thread.
    """
    if GLib is None or _on_main_thread():
        try:
            fn()
        except Exception:
            log.exception("D-Bus update failed")
        return

    def _wrapper():
        try:
            fn()
        except Exception:
            log.exception("D-Bus update failed")
        return False  # one-shot

    GLib.idle_add(_wrapper)


def dbus_sync(fn):
    """Run fn() on the GLib main thread and return its result.

    Only used where the caller genuinely needs the outcome (service creation
    and teardown).  Bounded by MAIN_THREAD_TIMEOUT so a wedged main loop
    fails the operation instead of hanging the calling thread forever.
    """
    if GLib is None or _on_main_thread():
        return fn()

    result: Dict[str, Any] = {}
    done = threading.Event()

    def _wrapper():
        try:
            result["value"] = fn()
        except Exception as exc:
            result["exc"] = exc
        finally:
            done.set()
        return False

    GLib.idle_add(_wrapper)
    if not done.wait(timeout=MAIN_THREAD_TIMEOUT):
        log.error(
            "D-Bus op did not run within %ds - GLib main loop appears wedged",
            MAIN_THREAD_TIMEOUT,
        )
        raise TimeoutError("GLib main loop unresponsive")

    if "exc" in result:
        raise result["exc"]
    return result.get("value")


# =====================================================================
# VeDbusService loading (+ dry-run shim)
# =====================================================================
class _Shim:
    """Stand-in for VeDbusService when there is no D-Bus (development host)."""

    def __init__(self, name, bus=None, register=True):
        self._name = name
        self._store: Dict[str, Any] = {}
        self._cbs: Dict[str, Any]   = {}
        log.info("[DRY-RUN] service %s", name)

    def add_path(self, path, value, description="", writeable=False,
                 onchangecallback=None, gettextcallback=None, valuetype=None):
        self._store[path] = value
        if onchangecallback:
            self._cbs[path] = onchangecallback
        log.debug("[DRY-RUN] %s add %s = %r", self._name, path, value)

    def register(self):
        log.info("[DRY-RUN] register %s", self._name)

    def __setitem__(self, path, value):
        self._store[path] = value
        log.debug("[DRY-RUN] %s set %s = %r", self._name, path, value)

    def __getitem__(self, path):
        return self._store.get(path)

    def __contains__(self, path):
        return path in self._store


if VENUS_OS and dbus is not None:
    sys.path.insert(1, VELIB_PATH)
    try:
        from vedbus import VeDbusService

        log.info("velib_python loaded from %s", VELIB_PATH)
    except ImportError:
        log.warning("velib_python not found at %s - using dry-run shim", VELIB_PATH)
        VeDbusService = _Shim
else:
    VeDbusService = _Shim

# velib gained an explicit two-phase register() (add all paths, then claim
# the bus name).  Detect it rather than guessing, so this file works on both
# old and new Venus OS images.
try:
    _VEDBUS_TWO_PHASE = "register" in inspect.signature(VeDbusService.__init__).parameters
except (TypeError, ValueError):
    _VEDBUS_TWO_PHASE = False

REAL_DBUS = VeDbusService is not _Shim and dbus is not None


# =====================================================================
# DbusServiceBase
# =====================================================================
class DbusServiceBase:
    """One D-Bus service on its own private bus connection.

    A private connection per service avoids the object-path collision that
    happens when several VeDbusService instances export '/' on one shared
    connection.  Owning the connection also means the service can be given
    up cleanly: release the well-known name, then close the socket.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._svc = None
        self._bus = None

    # -- lifecycle ----------------------------------------------------
    def _create(self, build) -> bool:
        """Create the service on the main thread. Returns True on success."""
        if self._svc is not None:
            return True

        def _do():
            if REAL_DBUS:
                bus = dbus.SystemBus(private=True)
                # Never fight another process for a name.  If it is already
                # owned, something else - typically a stale copy of this
                # driver - is serving it, and taking it over would leave two
                # half-live services behind.
                try:
                    if bus.name_has_owner(self.service_name):
                        log.error(
                            "%s is already owned on the system bus - refusing to "
                            "register a second copy",
                            self.service_name,
                        )
                        bus.close()
                        return False
                except Exception:
                    log.debug("name_has_owner check failed for %s", self.service_name)
            else:
                bus = None

            kwargs: Dict[str, Any] = {}
            if bus is not None:
                kwargs["bus"] = bus
            if _VEDBUS_TWO_PHASE:
                kwargs["register"] = False

            try:
                svc = VeDbusService(self.service_name, **kwargs)
                build(svc)
                if _VEDBUS_TWO_PHASE and hasattr(svc, "register"):
                    svc.register()
            except Exception:
                log.exception("Could not register %s", self.service_name)
                if bus is not None:
                    try:
                        bus.close()
                    except Exception:
                        pass
                return False

            self._svc = svc
            self._bus = bus
            return True

        try:
            ok = bool(dbus_sync(_do))
        except TimeoutError:
            return False

        if ok:
            log.info("Registered %s", self.service_name)
        return ok

    def destroy(self):
        """Release the bus name and close the connection.

        Releasing the name explicitly - rather than dropping the reference
        and hoping for garbage collection - is what guarantees a replacement
        can claim the same name without a collision.
        """
        if self._svc is None and self._bus is None:
            return

        def _do():
            svc, bus = self._svc, self._bus
            self._svc = None
            self._bus = None
            if bus is None:
                return
            try:
                bus.release_name(self.service_name)
            except Exception:
                log.debug("release_name(%s) failed", self.service_name)
            try:
                bus.close()
            except Exception:
                log.debug("close() failed for %s", self.service_name)
            del svc

        try:
            dbus_sync(_do)
            log.info("Released %s", self.service_name)
        except TimeoutError:
            log.warning("Could not release %s - main loop unresponsive", self.service_name)

    # -- value access -------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._svc is not None

    def set(self, path: str, value):
        """Queue a single value update on the main thread."""
        self.set_many({path: value})

    def set_many(self, values: Dict[str, Any]):
        """Queue several updates as one main-loop callback."""
        def _do():
            svc = self._svc
            if svc is None:
                return
            for path, value in values.items():
                try:
                    svc[path] = value
                except Exception:
                    log.exception("Failed writing %s%s", self.service_name, path)

        dbus_post(_do)

    @staticmethod
    def add_common(svc, product_name: str, instance: int, serial: str, connection: str):
        svc.add_path("/Mgmt/ProcessName",    os.path.basename(__file__))
        svc.add_path("/Mgmt/ProcessVersion", VERSION)
        svc.add_path("/Mgmt/Connection",     connection)

        svc.add_path("/DeviceInstance",  instance)
        svc.add_path("/ProductId",       PRODUCT_ID)
        svc.add_path("/ProductName",     product_name)
        svc.add_path("/FirmwareVersion", "")
        svc.add_path("/HardwareVersion", "")
        svc.add_path("/Serial",          serial)
        svc.add_path("/Connected",       0)


# =====================================================================
# TasmotaSwitchService - com.victronenergy.switch.tasmota_<id>
# =====================================================================
class TasmotaSwitchService(DbusServiceBase):

    def __init__(self, device_id: str, friendly_name: str, channels: int, publish):
        super().__init__(f"com.victronenergy.switch.tasmota_{device_id}")
        self.device_id     = device_id
        self.friendly_name = friendly_name
        self.channels      = max(1, channels)
        self._publish      = publish

        # Latest known Tasmota relay states, e.g. {"POWER1": "ON"}.  Cleared
        # when the device goes away, since the combination it describes can no
        # longer be trusted.
        self._relays: Dict[str, str] = {}
        # Last switch position published per channel.  Deliberately survives
        # going offline, so coming back can restore a channel's status instead
        # of leaving it stuck on "disabled".
        self._states: Dict[int, int] = {}
        self._online = False

        # In-flight momentary releases, keyed by channel index.
        self._pulses: Dict[int, threading.Timer] = {}
        self._pulse_lock = threading.Lock()

        # Last switch type pushed onto the service, so apply_config can tell
        # a real type change from any other config edit.
        self._sw_type: Optional[int] = None

    # -- construction -------------------------------------------------
    def start(self) -> bool:
        return self._create(self._build)

    def _build(self, svc):
        instance = CFG.assign_instance(self.device_id, "instance")

        self.add_common(
            svc,
            product_name=f"Tasmota ({self.friendly_name})",
            instance=instance,
            serial=self.device_id,
            connection=f"MQTT {MQTT_HOST}:{MQTT_PORT}",
        )
        svc.add_path("/CustomName", self.friendly_name)

        # Module level state.  Invalid (None) while the device is offline so
        # the GUI does not claim a vanished device is connected.
        svc.add_path("/State", None)

        sw_type       = CFG.switch_type(self.device_id)
        three_state   = sw_type == SwitchType.THREE_STATE
        self._sw_type = sw_type

        for ch in range(self.channels):
            self._build_channel(svc, ch, sw_type, three_state)

    def _build_channel(self, svc, ch: int, sw_type: int, three_state: bool):
        ch_name = f"CH{ch + 1}" if self.channels > 1 else "Relay"
        default_name = (
            f"{self.friendly_name} {ch_name}" if self.channels > 1 else self.friendly_name
        )
        base = f"/SwitchableOutput/{ch}"

        # Channel-wide configuration must be set before the channel can
        # identify itself as a switchable output.
        svc.add_path(f"/Channel/{ch}/Direction", DIRECTION_OUTPUT)

        # Operational paths.  /State is on/off for *every* type, including
        # three-state; the third position lives in /Auto.  On a momentary
        # channel a write of 1 is a press rather than a lasting state.
        svc.add_path(
            f"{base}/State", None,
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_state_write(c, v),
        )
        svc.add_path(f"{base}/Status", SwitchStatus.DISABLED)
        svc.add_path(f"{base}/Name",   ch_name)

        # Optional per-channel measurements.  Declared up front and left
        # invalid until the device reports them: velib can only write paths
        # that were added before the service was registered.
        svc.add_path(f"{base}/Voltage",     None)
        svc.add_path(f"{base}/Current",     None)
        svc.add_path(f"{base}/Temperature", None)

        # /Auto is only meaningful for types 9 and 10.  The path always
        # exists so a type change never needs the service to be rebuilt, but
        # it is held invalid while the channel is a plain toggle or a
        # momentary button, which is indistinguishable from absent as far as
        # the GUI is concerned.
        svc.add_path(
            f"{base}/Auto",
            CFG.auto_mode(self.device_id) if three_state else None,
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_auto_write(c, v),
        )

        # Settings.
        svc.add_path(f"{base}/Settings/Adjustable", 1)
        svc.add_path(
            f"{base}/Settings/Group",
            CFG.group(self.device_id),
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_group_write(c, v),
        )
        svc.add_path(
            f"{base}/Settings/CustomName",
            CFG.custom_name(self.device_id, ch, default_name),
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_custom_name_write(c, v),
        )
        svc.add_path(f"{base}/Settings/ShowUIControl", SHOW_UI_CONTROL, writeable=True)
        svc.add_path(
            f"{base}/Settings/Type",
            sw_type,
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_type_write(c, v),
        )
        svc.add_path(f"{base}/Settings/ValidTypes",     VALID_TYPES)
        svc.add_path(f"{base}/Settings/Function",       SwitchFunction.MANUAL, writeable=True)
        svc.add_path(f"{base}/Settings/ValidFunctions", VALID_FUNCTIONS)

        # Not part of the spec: gui-v2 renders fixed Off/On/Auto captions.
        # Kept so labels configured under 1.x remain visible to Node-RED
        # flows and to this driver's logging.
        svc.add_path(
            f"{base}/Settings/Labels",
            json.dumps(CFG.labels(self.device_id)),
            writeable=True,
            onchangecallback=lambda _p, v, c=ch: self._on_labels_write(c, v),
        )

    # -- inbound: MQTT -> D-Bus ---------------------------------------
    def set_online(self, online: bool):
        if online == self._online:
            return
        self._online = online

        values: Dict[str, Any] = {
            "/Connected": 1 if online else 0,
            "/State":     ModuleState.CONNECTED if online else None,
        }
        if not online:
            # A pulse whose release can no longer be delivered must not be
            # left pending: send it now rather than silently dropping it.
            self._release_pulses()
            # Spec: on a module level problem all channels indicate disabled.
            for ch in range(self.channels):
                values[f"/SwitchableOutput/{ch}/Status"] = SwitchStatus.DISABLED
            self._relays.clear()
        else:
            # Undo that disabled status, otherwise the device reads as
            # connected while every channel still says disabled until a POWER
            # report happens to arrive - which looks, in the GUI, exactly like
            # a switch flipping between enabled and disabled.  The last known
            # position is the honest answer; None (unknown) if there is none
            # yet, since the relay may have been operated while we were away.
            for ch in range(self.channels):
                state = self._states.get(ch)
                values[f"/SwitchableOutput/{ch}/Status"] = (
                    None if state is None
                    else SwitchStatus.ON if state else SwitchStatus.OFF
                )

        self.set_many(values)
        log.info("%s -> %s", self.device_id, "ONLINE" if online else "OFFLINE")

    def set_relay(self, power_key: str, value):
        """Apply one Tasmota POWER report, e.g. ("POWER2", "ON")."""
        power_key = str(power_key).upper()
        value     = str(value).upper()

        if value in ("1", "TRUE"):
            value = "ON"
        elif value in ("0", "FALSE"):
            value = "OFF"
        if value not in ("ON", "OFF"):
            # TOGGLE / BLINK and friends carry no resolved state.
            log.debug("%s: ignoring %s=%r", self.device_id, power_key, value)
            return

        self._relays[power_key] = value
        reverse = CFG.reverse_relay_map(self.device_id)

        if reverse:
            # Multi-relay device driven as one logical switch: resolve the
            # combination of relays into a single on/off state.
            observed = frozenset(self._relays.items())
            state    = reverse.get(observed)
            if state is None:
                log.debug(
                    "%s: relay set %s matches no mapped state yet",
                    self.device_id, self._relays,
                )
                return
            self._write_state_all(state)
        else:
            ch = self._channel_of(power_key)
            if ch is None or not 0 <= ch < self.channels:
                return
            self._write_state(ch, 1 if value == "ON" else 0)

    @staticmethod
    def _channel_of(power_key: str) -> Optional[int]:
        if power_key == "POWER":
            return 0
        try:
            return int(power_key[5:]) - 1
        except ValueError:
            return None

    def _power_key(self, ch: int) -> str:
        return "POWER" if self.channels == 1 else f"POWER{ch + 1}"

    def _write_state(self, ch: int, state: int):
        self._states[ch] = state
        self.set_many({
            f"/SwitchableOutput/{ch}/State":  state,
            f"/SwitchableOutput/{ch}/Status": SwitchStatus.ON if state else SwitchStatus.OFF,
        })

    def _write_state_all(self, state: int):
        values: Dict[str, Any] = {}
        for ch in range(self.channels):
            self._states[ch] = state
            values[f"/SwitchableOutput/{ch}/State"]  = state
            values[f"/SwitchableOutput/{ch}/Status"] = (
                SwitchStatus.ON if state else SwitchStatus.OFF
            )
        self.set_many(values)

    def set_firmware(self, version: str):
        if version:
            self.set("/FirmwareVersion", version)

    def update_name(self, friendly_name: str):
        if not friendly_name or friendly_name == self.friendly_name:
            return
        self.friendly_name = friendly_name
        self.set_many({
            "/CustomName":  friendly_name,
            "/ProductName": f"Tasmota ({friendly_name})",
        })

    def set_measurements(self, voltage=None, current=None, temperature=None):
        """Optional per-channel measurements, straight from the spec's
        /SwitchableOutput/x/{Voltage,Current,Temperature}."""
        values: Dict[str, Any] = {}
        base = "/SwitchableOutput/0"
        if voltage is not None:
            values[f"{base}/Voltage"] = voltage
        if current is not None:
            values[f"{base}/Current"] = current
        if temperature is not None:
            values[f"{base}/Temperature"] = temperature
        if values:
            self.set_many(values)

    # -- momentary ----------------------------------------------------
    def _pulse_commands(self, ch: int):
        """(press, release) relay commands for a momentary press.

        Multi-relay devices driven as one logical switch pulse their whole
        state_relay_map combination; everything else pulses its own relay.
        """
        relay_map = CFG.relay_map(self.device_id)
        press     = relay_map.get("1")
        release   = relay_map.get("0")
        if press and release:
            return dict(press), dict(release)
        key = self._power_key(ch)
        return {key: "ON"}, {key: "OFF"}

    def _is_on(self, ch: int) -> bool:
        """Whether the channel's relay(s) are currently reported as closed."""
        reverse = CFG.reverse_relay_map(self.device_id)
        if reverse:
            return reverse.get(frozenset(self._relays.items())) == 1
        return self._relays.get(self._power_key(ch)) == "ON"

    def _pulse(self, ch: int):
        """Close the relay for pulse_ms, then release it again.

        The release is scheduled on a timer so the D-Bus write callback
        returns immediately instead of holding the main loop for the whole
        contact.  Pressing again mid-pulse extends the hold rather than
        releasing on the older timer.
        """
        press, release = self._pulse_commands(ch)
        hold_ms = CFG.pulse_ms(self.device_id)
        holder: Dict[str, Any] = {}

        def _release():
            with self._pulse_lock:
                # A newer press may have replaced this timer - leave it be.
                if self._pulses.get(ch) is not holder.get("timer"):
                    return
                del self._pulses[ch]
            for key, command in release.items():
                self._send(key, command)
            self._write_state(ch, 0)

        timer = threading.Timer(hold_ms / 1000.0, _release)
        timer.daemon = True
        holder["timer"] = timer

        with self._pulse_lock:
            previous = self._pulses.get(ch)
            if previous is not None:
                previous.cancel()
            self._pulses[ch] = timer

        log.info("%s CH%d: momentary press (%dms)", self.device_id, ch + 1, hold_ms)
        for key, command in press.items():
            self._send(key, command)

        timer.start()

    def _release_pulses(self):
        """End every in-flight pulse now, sending the release commands.

        Fail-safe for shutdown and for losing the device: a timer that is
        merely cancelled never sends its OFF, which would leave the relay
        closed for as long as the device stays up.
        """
        with self._pulse_lock:
            pending = list(self._pulses.items())
            self._pulses.clear()

        for ch, timer in pending:
            timer.cancel()
            log.info("%s CH%d: releasing momentary pulse early", self.device_id, ch + 1)
            for key, command in self._pulse_commands(ch)[1].items():
                self._send(key, command)

    def _reset_outputs(self):
        """Force every channel back to its inactive state.

        Spec: "The device should reset the output to its inactive state when
        the type is changed to momentary to prevent the output being in the
        active state while the user is not pressing the button."  Without
        this, making a switch that was left on into a button leaves the relay
        energised underneath a button that reads as released.
        """
        self._release_pulses()
        for ch in range(self.channels):
            if self._is_on(ch):
                log.info("%s CH%d: releasing relay left on by the previous type",
                         self.device_id, ch + 1)
                for key, command in self._pulse_commands(ch)[1].items():
                    self._send(key, command)
            self._write_state(ch, 0)

    # -- config -> D-Bus (in place, no re-registration) ----------------
    def apply_config(self):
        """Push the current config onto the live service.

        Everything a user can change - type, auto, group, name, labels - is
        an in-place property write.  The service is never rebuilt, so bus
        names never churn and duplicate services cannot appear.
        """
        if not self.alive:
            return

        sw_type      = CFG.switch_type(self.device_id)
        three_state  = sw_type == SwitchType.THREE_STATE
        type_changed = sw_type != self._sw_type
        self._sw_type = sw_type

        auto   = CFG.auto_mode(self.device_id) if three_state else None
        group  = CFG.group(self.device_id)
        labels = json.dumps(CFG.labels(self.device_id))

        values: Dict[str, Any] = {}
        for ch in range(self.channels):
            base = f"/SwitchableOutput/{ch}"
            default_name = (
                f"{self.friendly_name} CH{ch + 1}" if self.channels > 1 else self.friendly_name
            )
            values[f"{base}/Settings/Type"]       = sw_type
            values[f"{base}/Auto"]                = auto
            values[f"{base}/Settings/Group"]      = group
            values[f"{base}/Settings/CustomName"] = CFG.custom_name(
                self.device_id, ch, default_name
            )
            values[f"{base}/Settings/Labels"]     = labels

        self.set_many(values)

        # Only on an actual change of type, so an unrelated config edit
        # cannot cut a press that happens to be in flight short.
        if type_changed and sw_type == SwitchType.MOMENTARY:
            self._reset_outputs()

    # -- outbound: D-Bus writes -> MQTT --------------------------------
    def _on_state_write(self, ch: int, value) -> bool:
        """The GUI (or Node-RED) requested a new on/off state.

        Returning True accepts the write; velib treats any falsy return as a
        rejection, which is why a GUI toggle appears to do nothing when the
        callback forgets to return.
        """
        try:
            state = 1 if int(value) else 0
        except (TypeError, ValueError):
            log.warning("%s: rejecting non-numeric State write %r", self.device_id, value)
            return False

        if CFG.switch_type(self.device_id) == SwitchType.MOMENTARY:
            # A push button has no lasting state: 1 is a press, and the
            # release belongs to the pulse timer.  A write of 0 is the button
            # coming back up, which must not cut the contact short.
            if state:
                self._pulse(ch)
            return True

        relay_map = CFG.relay_map(self.device_id)
        if relay_map:
            commands = relay_map.get(str(state))
            if not commands:
                log.warning(
                    "%s: state_relay_map has no entry for state %d", self.device_id, state
                )
                return False
            for power_key, command in commands.items():
                self._send(power_key, command)
        else:
            self._send(self._power_key(ch), "ON" if state else "OFF")

        return True

    def _on_auto_write(self, ch: int, value) -> bool:
        """The GUI moved the three-state switch into or out of Auto.

        In Auto the UI stops driving /State; the driver or an external
        service - typically a Node-RED flow - writes it instead.  Nothing is
        sent to the relay here, only the mode is recorded.
        """
        if CFG.switch_type(self.device_id) != SwitchType.THREE_STATE:
            log.warning(
                "%s: /Auto write rejected - channel is not a three-state switch",
                self.device_id,
            )
            return False
        try:
            auto = 1 if int(value) else 0
        except (TypeError, ValueError):
            return False

        labels = CFG.labels(self.device_id)
        log.info("%s CH%d: %s mode", self.device_id, ch + 1, labels[2] if auto else "Manual")
        CFG.set(self.device_id, "auto", auto)

        # Mirror onto the other channels of a multi-channel device.
        if self.channels > 1:
            self.set_many({
                f"/SwitchableOutput/{c}/Auto": auto
                for c in range(self.channels) if c != ch
            })
        return True

    def _on_type_write(self, ch: int, value) -> bool:
        """The GUI switched the channel between momentary, toggle and three-state.

        This used to tear down and re-register the service.  It no longer
        does: every path a type change touches already exists, so the change
        is a handful of property writes.
        """
        try:
            sw_type = int(value)
        except (TypeError, ValueError):
            return False
        if sw_type not in SUPPORTED_TYPES:
            log.warning(
                "%s: type %s is not supported by this driver "
                "(momentary=%d, toggle=%d, three-state=%d)",
                self.device_id, sw_type, SwitchType.MOMENTARY,
                SwitchType.TOGGLE, SwitchType.THREE_STATE,
            )
            return False

        three_state = sw_type == SwitchType.THREE_STATE
        momentary   = sw_type == SwitchType.MOMENTARY
        log.info("%s: type -> %s", self.device_id, TYPE_NAMES.get(sw_type, sw_type))

        CFG.set(self.device_id, "three_state", three_state)
        CFG.set(self.device_id, "momentary",   momentary)
        self._sw_type = sw_type

        auto = CFG.auto_mode(self.device_id) if three_state else None
        values: Dict[str, Any] = {}
        for c in range(self.channels):
            values[f"/SwitchableOutput/{c}/Auto"] = auto
            if c != ch:
                values[f"/SwitchableOutput/{c}/Settings/Type"] = sw_type
        self.set_many(values)

        if momentary:
            self._reset_outputs()
        return True

    def _on_group_write(self, ch: int, value) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        value = _clip_utf8(value)
        CFG.set(self.device_id, "group", value)
        if self.channels > 1:
            self.set_many({
                f"/SwitchableOutput/{c}/Settings/Group": value
                for c in range(self.channels) if c != ch
            })
        return True

    def _on_custom_name_write(self, ch: int, value) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        CFG.set(self.device_id, f"custom_name_{ch}", _clip_utf8(value))
        return True

    def _on_labels_write(self, ch: int, value) -> bool:
        try:
            labels = json.loads(value) if isinstance(value, str) else list(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("%s: invalid Labels payload %r", self.device_id, value)
            return False
        if (not isinstance(labels, list) or len(labels) != 3
                or not all(isinstance(x, str) for x in labels)):
            log.warning("%s: Labels must be a JSON array of three strings", self.device_id)
            return False
        CFG.set(self.device_id, "labels", labels)
        return True

    def _send(self, power_key: str, command: str):
        topic = f"cmnd/{self.device_id}/{power_key}"
        if self._publish:
            self._publish(topic, command)
        log.info("D-Bus -> MQTT %s = %s", topic, command)

    # -- lifecycle ----------------------------------------------------
    def destroy(self):
        self._release_pulses()
        super().destroy()


# =====================================================================
# TasmotaMeterService - com.victronenergy.<role>.tasmota_<id>
# =====================================================================
class TasmotaMeterService(DbusServiceBase):
    """AC meter built from Tasmota ENERGY telemetry.

    Tasmota reports the ENERGY block on tele/<topic>/SENSOR and in the
    StatusSNS block of `Status 0` / `Status 10`::

        "ENERGY": {"Total": 12.345, "Today": 0.5, "Power": 143,
                   "ApparentPower": 150, "ReactivePower": 44,
                   "Factor": 0.95, "Voltage": 231, "Current": 0.62}

    Three-phase capable firmware reports Power / Voltage / Current (and on
    some meters Total) as lists, indexed by phase.

    One service covers a chosen set of phases.  In *combined* mode a single
    service carries them all on their real paths - phase 3 stays on /Ac/L3,
    with unselected phases left invalid.  In *split* mode there is one
    service per phase and each publishes on /Ac/L1, because a single-phase
    meter that reports only on /Ac/L2 renders as an empty first phase in the
    GUI.  Which physical phase a split service represents is carried by its
    service name and its custom name instead.
    """

    def __init__(self, device_id: str, friendly_name: str, role: str,
                 phases: List[int], device_phases: int = 1,
                 split_phase: Optional[int] = None, owns_total: bool = True):
        suffix = f"_L{split_phase}" if split_phase else ""
        super().__init__(f"com.victronenergy.{role}.tasmota_{device_id}{suffix}")

        self.device_id     = device_id
        self.friendly_name = friendly_name
        self.role          = role
        self.phases        = sorted(phases) or [1]
        self.split_phase   = split_phase
        # How many phases the physical device has, which is not the same as
        # how many this service publishes.  It decides whether a single
        # device-wide kWh counter can honestly be called one phase's energy.
        self.device_phases = max(1, device_phases)
        # Tasmota usually keeps one cumulative energy counter for the whole
        # device.  Exactly one service may claim it, otherwise a split
        # three-phase plug would report its lifetime energy three times.
        self.owns_total    = owns_total

        self._instance_key = (
            f"meter_instance_L{split_phase}" if split_phase else "meter_instance"
        )

    def start(self) -> bool:
        return self._create(self._build)

    def _build(self, svc):
        instance = CFG.assign_instance(
            self.device_id, self._instance_key, namespace="meter_instance"
        )

        self.add_common(
            svc,
            product_name=f"Tasmota meter ({self.friendly_name})",
            instance=instance,
            serial=(
                f"{self.device_id}-meter-L{self.split_phase}"
                if self.split_phase else f"{self.device_id}-meter"
            ),
            connection=f"MQTT {MQTT_HOST}:{MQTT_PORT}",
        )
        svc.add_path("/CustomName", self.display_name)
        svc.add_path("/Role",       self.role)
        svc.add_path("/NrOfPhases", self.nr_of_phases)

        if self.role == "pvinverter":
            # 0 = AC input 1, 1 = AC output, 2 = AC input 2
            svc.add_path("/Position",   CFG.meter_position(self.device_id))
            svc.add_path("/ErrorCode",  0)
            svc.add_path("/StatusCode", 7)  # Running

        svc.add_path("/Ac/Power",          None)
        svc.add_path("/Ac/Energy/Forward", None)
        svc.add_path("/Ac/Frequency",      None)

        # Declare every path up to /NrOfPhases, not just the selected ones:
        # velib can only write paths added before registration, and a
        # consumer that walks L1..LN must find the gaps as invalid rather
        # than missing.
        for number in range(1, self.nr_of_phases + 1):
            svc.add_path(f"/Ac/L{number}/Voltage",        None)
            svc.add_path(f"/Ac/L{number}/Current",        None)
            svc.add_path(f"/Ac/L{number}/Power",          None)
            svc.add_path(f"/Ac/L{number}/Energy/Forward", None)

    @property
    def display_name(self) -> str:
        if self.split_phase:
            return CFG.meter_name(
                self.device_id, self.split_phase,
                f"{self.friendly_name} L{self.split_phase}",
            )
        return self.friendly_name

    @property
    def nr_of_phases(self) -> int:
        """Highest phase this service publishes on.

        Split services always land on L1, so they are single-phase.  A
        combined service that was filtered down to, say, L1 and L3 still has
        to declare 3 so a consumer walking the phases reaches L3.
        """
        return 1 if self.split_phase else max(self.phases)

    def _label_of(self, phase: int) -> str:
        """D-Bus phase label a physical phase publishes under."""
        return "L1" if self.split_phase else f"L{phase}"

    def set_online(self, online: bool):
        if online:
            self.set("/Connected", 1)
            return
        values: Dict[str, Any] = {"/Connected": 0, "/Ac/Power": None}
        for phase in self.phases:
            label = self._label_of(phase)
            values[f"/Ac/{label}/Voltage"] = None
            values[f"/Ac/{label}/Current"] = None
            values[f"/Ac/{label}/Power"]   = None
        self.set_many(values)

    def update(self, energy: dict):
        """Apply one Tasmota ENERGY block."""
        power   = _as_list(energy.get("Power"))
        voltage = _as_list(energy.get("Voltage"))
        current = _as_list(energy.get("Current"))
        total   = _as_list(energy.get("Total"))

        values: Dict[str, Any] = {"/Connected": 1}

        # Meters that report Total per phase let every phase carry its own
        # energy.  A device with one cumulative counter does not: attributing
        # it to a phase would claim that phase consumed the whole device's
        # lifetime energy, so per-phase energy stays invalid unless the
        # device really is single-phase.
        per_phase_energy = len(total) > 1

        powers:   List[float] = []
        energies: List[float] = []

        for phase in self.phases:
            index = phase - 1
            label = self._label_of(phase)

            phase_power = _pick(power, index)
            if per_phase_energy:
                phase_energy = _pick(total, index)
            elif self.device_phases == 1 and total:
                phase_energy = total[0]
            else:
                phase_energy = None

            values[f"/Ac/{label}/Voltage"] = _pick(voltage, index)
            values[f"/Ac/{label}/Current"] = _pick(current, index)
            values[f"/Ac/{label}/Power"]   = phase_power
            values[f"/Ac/{label}/Energy/Forward"] = (
                round(phase_energy, 3) if phase_energy is not None else None
            )

            if phase_power is not None:
                powers.append(phase_power)
            if phase_energy is not None:
                energies.append(phase_energy)

        # Power totals cover only the phases this service publishes, so a
        # filtered or split meter never reports power it does not own.
        values["/Ac/Power"] = float(sum(powers)) if powers else None

        if per_phase_energy:
            values["/Ac/Energy/Forward"] = round(sum(energies), 3) if energies else None
        elif total and self.owns_total:
            # Exactly one service may claim a shared counter, otherwise a
            # split three-phase plug reports its lifetime energy three times.
            values["/Ac/Energy/Forward"] = round(total[0], 3)
        else:
            values["/Ac/Energy/Forward"] = None

        frequency = _as_float(energy.get("Frequency"))
        if frequency is not None:
            values["/Ac/Frequency"] = frequency

        self.set_many(values)
        log.debug(
            "%s: %sW on %s", self.service_name, values["/Ac/Power"],
            ", ".join(f"L{p}" for p in self.phases),
        )


def _as_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value) -> List[Optional[float]]:
    """Normalise a Tasmota scalar-or-list measurement into a list of floats."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_as_float(v) for v in value]
    single = _as_float(value)
    return [] if single is None else [single]


def _pick(values: List[Optional[float]], index: int):
    return values[index] if index < len(values) else None


# =====================================================================
# TasmotaDevice - one physical Tasmota, up to two D-Bus services
# =====================================================================
class TasmotaDevice:

    def __init__(self, device_id: str, friendly_name: str, ip: str,
                 channels: int, publish):
        self.device_id     = device_id
        self.friendly_name = friendly_name or device_id
        self.ip            = ip
        self.channels      = channels
        self._publish      = publish
        self._lock         = threading.Lock()

        self.switch: Optional[TasmotaSwitchService] = None
        self.meters: List[TasmotaMeterService] = []
        self._meters_started = False
        self._online = False

    # -- services -----------------------------------------------------
    def start_switch(self) -> bool:
        if self.channels < 1:
            return False
        svc = TasmotaSwitchService(
            self.device_id, self.friendly_name, self.channels, self._publish
        )
        if not svc.start():
            return False
        self.switch = svc
        return True

    def ensure_meters(self, available_phases: int) -> bool:
        """Build the meter service(s) on the first ENERGY sample.

        Deferred until here because the phase count - and therefore how many
        services to create - is only known once the device has reported.
        """
        with self._lock:
            if self._meters_started:
                return bool(self.meters)
            if not CFG.meter_enabled(self.device_id):
                self._meters_started = True
                return False
            # Claim the slot before releasing the lock so a second sample
            # arriving mid-registration cannot build a duplicate set.
            self._meters_started = True
            role     = CFG.meter_role(self.device_id)
            selected = CFG.meter_phases(self.device_id, available_phases)
            split    = CFG.meter_split(self.device_id)

        if split:
            pending = [
                TasmotaMeterService(
                    self.device_id, self.friendly_name, role,
                    phases=[phase],
                    device_phases=available_phases,
                    split_phase=phase,
                    # Only the first service may claim a device-wide
                    # cumulative energy counter.
                    owns_total=(phase == selected[0]),
                )
                for phase in selected
            ]
        else:
            pending = [
                TasmotaMeterService(
                    self.device_id, self.friendly_name, role,
                    phases=selected, device_phases=available_phases,
                )
            ]

        started = [svc for svc in pending if svc.start()]
        with self._lock:
            self.meters = started

        if not started:
            log.error("%s: no meter service could be registered", self.device_id)
            return False

        log.info(
            "%s: energy monitoring detected - registered %d %s meter(s) for %s%s",
            self.device_id, len(started), role,
            ", ".join(f"L{p}" for p in selected),
            " (split into single-phase devices)" if split else "",
        )
        return True

    def destroy(self):
        for svc in [self.switch, *self.meters]:
            if svc is not None:
                svc.destroy()

    # -- state --------------------------------------------------------
    def set_online(self, online: bool):
        # Guarded here, not just on the switch: every inbound message counts
        # as proof of life, and without this each one would re-write every
        # meter's /Connected.
        if online == self._online:
            return
        self._online = online

        if self.switch:
            self.switch.set_online(online)
        for meter in self.meters:
            meter.set_online(online)

    def set_firmware(self, version: str):
        if self.switch:
            self.switch.set_firmware(version)
        for meter in self.meters:
            meter.set("/FirmwareVersion", version)

    def update_name(self, friendly_name: str):
        if not friendly_name or friendly_name == self.friendly_name:
            return
        self.friendly_name = friendly_name
        if self.switch:
            self.switch.update_name(friendly_name)
        for meter in self.meters:
            meter.friendly_name = friendly_name
            meter.set_many({
                "/CustomName":  meter.display_name,
                "/ProductName": f"Tasmota meter ({friendly_name})",
            })

    def apply_config(self):
        if self.switch:
            self.switch.apply_config()

    # -- MQTT payload handling ----------------------------------------
    def apply_power_keys(self, data: dict):
        """Feed every POWERn key of a Tasmota status/state payload."""
        if not self.switch:
            return
        for key, value in data.items():
            if str(key).upper().startswith("POWER"):
                self.switch.set_relay(key, value)

    def apply_sensor(self, data: dict):
        """Feed a StatusSNS / SENSOR payload (currently the ENERGY block)."""
        energy = data.get("ENERGY")
        if not isinstance(energy, dict):
            return

        phases = max(
            1,
            len(_as_list(energy.get("Power"))),
            len(_as_list(energy.get("Voltage"))),
        )
        if self.ensure_meters(phases):
            for meter in self.meters:
                meter.update(energy)

        # Also surface the readings on the switch channel itself; the spec
        # provides /SwitchableOutput/x/{Voltage,Current} for exactly this.
        if self.switch:
            self.switch.set_measurements(
                voltage=_pick(_as_list(energy.get("Voltage")), 0),
                current=_pick(_as_list(energy.get("Current")), 0),
            )


# =====================================================================
# Probe bookkeeping
# =====================================================================
class _Probe:
    """Everything learned about a candidate device before it is registered."""

    __slots__ = ("timer", "name", "firmware", "ip", "sensor", "power")

    def __init__(self):
        self.timer    = None
        self.name     = None
        self.firmware = None
        self.ip       = None
        self.sensor   = None
        self.power: Dict[str, Any] = {}


# =====================================================================
# TasmotaDiscovery
# =====================================================================
class TasmotaDiscovery:

    # `Status 0` makes a device answer on STATUS, STATUS2, STATUS5, STATUS10
    # and STATUS11 in one go, which is why it is used as the single probe.
    SUBSCRIPTIONS = [
        "tele/+/LWT",
        "tele/+/STATE",       # periodic, carries every POWERn key
        "tele/+/SENSOR",      # periodic, carries the ENERGY block
        "tele/+/INFO1",
        "tele/+/INFO2",
        "tele/+/INFO3",
        "stat/+/RESULT",      # immediate echo of a command
        "stat/+/POWER",
        "stat/+/POWER1",
        "stat/+/POWER2",
        "stat/+/POWER3",
        "stat/+/POWER4",
        "stat/+/POWER5",
        "stat/+/POWER6",
        "stat/+/POWER7",
        "stat/+/POWER8",
        "stat/+/STATUS",      # FriendlyName
        "stat/+/STATUS2",     # firmware version
        "stat/+/STATUS5",     # network / IP
        "stat/+/STATUS8",     # sensors (older firmware)
        "stat/+/STATUS10",    # sensors
        "stat/+/STATUS11",    # relay states
    ]

    def __init__(self):
        self._devices: Dict[str, TasmotaDevice] = {}
        self._lock = threading.Lock()

        self._probes: Dict[str, _Probe] = {}
        self._last_probe: Dict[str, float] = {}
        self._probe_lock = threading.Lock()

        self._mqttc = None
        self._stopping = False

        # Connection churn tracking, so a flapping link is named in the log
        # rather than just scrolling past as reconnects.
        self._connected_at = 0.0
        self._flaps        = 0

        CFG.register_callback(self._on_config_change)

    # -----------------------------------------------------------------
    # Config changes
    # -----------------------------------------------------------------
    def _on_config_change(self, changed: set):
        """Apply an edited config file to the live services.

        This never re-registers anything.  Every setting the config carries
        maps onto a property write on an already-registered service, which is
        what makes `nano tasmota_config.json` safe while the GUI is open.
        """
        with self._lock:
            targets = [self._devices[d] for d in changed if d in self._devices]
        for device in targets:
            device.apply_config()

    # -----------------------------------------------------------------
    # MQTT
    # -----------------------------------------------------------------
    def start_mqtt(self):
        self._mqttc = self._make_client()

        if MQTT_USER:
            self._mqttc.username_pw_set(MQTT_USER, MQTT_PASS)

        self._mqttc.on_connect    = self._on_connect
        self._mqttc.on_disconnect = self._on_disconnect
        self._mqttc.on_message    = self._on_message

        # Keep retrying rather than giving up if the broker is slow to come
        # up during boot.
        self._mqttc.reconnect_delay_set(min_delay=1, max_delay=60)
        self._mqttc.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._mqttc.loop_start()

        log.info("MQTT connecting to %s:%d", MQTT_HOST, MQTT_PORT)

    @staticmethod
    def _make_client():
        """Build a paho client on either the 1.x or 2.x API."""
        callback_api = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api is not None:
            return mqtt.Client(
                callback_api_version=callback_api.VERSION1,
                client_id=MQTT_CLIENT_ID,
                clean_session=True,
            )
        return mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)

    def publish(self, topic: str, payload: str):
        client = self._mqttc
        if client is None:
            return
        try:
            client.publish(topic, payload, retain=False)
        except Exception:
            log.exception("MQTT publish to %s failed", topic)

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc != 0:
            log.error("MQTT connect failed rc=%s", rc)
            return

        log.info("MQTT connected as %s", MQTT_CLIENT_ID)
        self._connected_at = time.monotonic()
        for topic in self.SUBSCRIPTIONS:
            client.subscribe(topic)
        log.info("Subscribed to %d Tasmota topic patterns", len(self.SUBSCRIPTIONS))

        # Re-query everything already known; retained LWT messages bring in
        # anything that is not.
        timer = threading.Timer(2.0, self._poll_all)
        timer.daemon = True
        timer.start()

        if GROUP_PROBE:
            log.info("Broadcasting Status 0 to the %s group topic", GROUP_TOPIC)
            self.publish(f"cmnd/{GROUP_TOPIC}/Status", "0")

    def _on_disconnect(self, client, userdata, rc, *args):
        if self._stopping:
            return

        held = time.monotonic() - self._connected_at if self._connected_at else 0.0
        log.warning("MQTT disconnected (rc=%s) after %.0fs - reconnecting", rc, held)

        # Every device is marked offline on a disconnect, so a link that keeps
        # dropping shows up in the GUI as switches flipping between enabled and
        # disabled.  Say so plainly instead of leaving it to be inferred from a
        # wall of reconnect lines.
        if held < 30:
            self._flaps += 1
            if self._flaps == 3:
                log.error(
                    "MQTT has dropped 3 times within 30s of connecting. Every "
                    "device flips to disabled and back on each drop. The usual "
                    "cause is a second client using the same id (%s) - check for "
                    "another copy of this driver, or an older version still "
                    "running.", MQTT_CLIENT_ID,
                )
        else:
            self._flaps = 0

        with self._lock:
            devices = list(self._devices.values())
        for device in devices:
            device.set_online(False)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            self._dispatch(msg.topic, payload)
        except Exception:
            log.exception("Error handling %s", msg.topic)

    # -----------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------
    @staticmethod
    def _parse_json(payload: str) -> Optional[dict]:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def _dispatch(self, topic: str, payload: str):
        parts = topic.split("/")
        if len(parts) < 3:
            return

        device_id = parts[1]
        subtopic  = parts[-1]

        # Anything other than LWT is proof the device is talking to us.  LWT
        # owns the offline transition; every other topic means alive, so a
        # device whose STATUS11 reply goes missing still comes back rather
        # than waiting for the next poll.
        if subtopic != "LWT":
            device = self._get(device_id)
            if device:
                device.set_online(True)

        handler = {
            "LWT":      self._handle_lwt,
            "STATE":    self._handle_state,
            "RESULT":   self._handle_state,
            "SENSOR":   self._handle_sensor,
            "STATUS":   self._handle_status,
            "STATUS2":  self._handle_status2,
            "STATUS5":  self._handle_status5,
            "STATUS8":  self._handle_status_sns,
            "STATUS10": self._handle_status_sns,
            "STATUS11": self._handle_status11,
            "INFO1":    self._handle_info,
            "INFO2":    self._handle_info,
            "INFO3":    self._handle_info,
        }.get(subtopic)

        if handler:
            handler(device_id, payload)
        elif subtopic.startswith("POWER"):
            self._handle_power(device_id, subtopic, payload)

    # -- LWT ----------------------------------------------------------
    def _handle_lwt(self, device_id: str, payload: str):
        online = payload.lower() == "online"
        device = self._get(device_id)

        if device:
            device.set_online(online)
            if online:
                # Refresh state after a reconnect, without re-probing.
                self.publish(f"cmnd/{device_id}/Status", "11")
            return

        if online:
            self._probe(device_id)
        else:
            self._cancel_probe(device_id)

    # -- bare stat/<id>/POWERn ----------------------------------------
    def _handle_power(self, device_id: str, subtopic: str, payload: str):
        device = self._get(device_id)
        if device and device.switch:
            device.switch.set_relay(subtopic, payload)
            return
        probe = self._get_probe(device_id)
        if probe:
            probe.power[subtopic.upper()] = payload

    # -- STATE / RESULT -----------------------------------------------
    def _handle_state(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        if not data:
            return

        device = self._get(device_id)
        if device:
            device.apply_power_keys(data)
            return

        probe = self._get_probe(device_id)
        if probe:
            for key, value in data.items():
                if str(key).upper().startswith("POWER"):
                    probe.power[str(key).upper()] = value

    # -- SENSOR -------------------------------------------------------
    def _handle_sensor(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        if not data:
            return

        device = self._get(device_id)
        if device:
            device.apply_sensor(data)
            return

        self._stash_sensor(device_id, data)

    def _stash_sensor(self, device_id: str, data: dict):
        """Keep a sensor payload from an unregistered device.

        A pure energy monitor announces itself through telemetry rather than
        through a relay, so seeing an ENERGY block is itself a reason to
        probe.  The payload has to be stashed on the probe it just started,
        otherwise the reading that triggered the probe is thrown away and the
        device is written off as uninteresting when the probe expires.
        """
        probe = self._get_probe(device_id)
        if probe is None:
            if not isinstance(data.get("ENERGY"), dict):
                return
            self._probe(device_id)
            probe = self._get_probe(device_id)
        if probe:
            probe.sensor = data

    def _handle_status_sns(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        sns  = (data or {}).get("StatusSNS")
        if not isinstance(sns, dict):
            return

        device = self._get(device_id)
        if device:
            device.apply_sensor(sns)
            return

        self._stash_sensor(device_id, sns)

    # -- STATUS (friendly name) ---------------------------------------
    def _handle_status(self, device_id: str, payload: str):
        data   = self._parse_json(payload)
        status = (data or {}).get("Status")
        if not isinstance(status, dict):
            return

        names = status.get("FriendlyName")
        name  = names[0] if isinstance(names, list) and names else status.get("DeviceName")
        if not isinstance(name, str) or not name.strip():
            return

        device = self._get(device_id)
        if device:
            device.update_name(name)
            return

        probe = self._get_probe(device_id)
        if probe:
            probe.name = name

    def _handle_status2(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        fwr  = (data or {}).get("StatusFWR")
        if not isinstance(fwr, dict):
            return
        version = fwr.get("Version", "")
        if not version:
            return

        device = self._get(device_id)
        if device:
            device.set_firmware(version)
            return
        probe = self._get_probe(device_id)
        if probe:
            probe.firmware = version

    def _handle_status5(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        net  = (data or {}).get("StatusNET")
        if not isinstance(net, dict):
            return
        self._set_ip(device_id, net.get("IPAddress", ""))

    def _handle_info(self, device_id: str, payload: str):
        """tele/<id>/INFO1..3, published once at boot.

        Tasmota splits the announcement across three messages - INFO1 carries
        Module/Version, INFO2 the hostname and IP, INFO3 the restart reason -
        so all three go through one handler that picks out whatever is there.
        """
        data = self._parse_json(payload)
        if not data:
            return

        self._set_ip(device_id, data.get("IPAddress", ""))

        version = data.get("Version") or ""
        name    = data.get("FriendlyName1") or data.get("FriendlyName")
        if isinstance(name, list):
            name = name[0] if name else None

        device = self._get(device_id)
        if device:
            if version:
                device.set_firmware(version)
            if isinstance(name, str):
                device.update_name(name)
            return

        probe = self._get_probe(device_id)
        if probe:
            probe.firmware = version or probe.firmware
            if isinstance(name, str):
                probe.name = name

    def _set_ip(self, device_id: str, ip: str):
        if not ip:
            return
        device = self._get(device_id)
        if device:
            device.ip = ip
            return
        probe = self._get_probe(device_id)
        if probe:
            probe.ip = ip

    # -- STATUS11: the decision point ---------------------------------
    def _handle_status11(self, device_id: str, payload: str):
        data = self._parse_json(payload)
        sts  = (data or {}).get("StatusSTS")
        if not isinstance(sts, dict):
            return

        device = self._get(device_id)
        if device:
            device.set_online(True)
            device.apply_power_keys(sts)
            return

        probe = self._get_probe(device_id)
        if probe is None:
            return

        for key, value in sts.items():
            if str(key).upper().startswith("POWER"):
                probe.power[str(key).upper()] = value

        self._finish_probe(device_id)

    # -----------------------------------------------------------------
    # Probing
    # -----------------------------------------------------------------
    def _probe(self, device_id: str):
        """Ask a candidate device to describe itself with `Status 0`."""
        now = time.monotonic()
        with self._probe_lock:
            if device_id in self._probes:
                return  # already in flight
            last = self._last_probe.get(device_id, 0.0)
            if last and now - last < PROBE_MIN_INTERVAL:
                log.debug("%s: probed %.0fs ago - skipping", device_id, now - last)
                return

            probe = _Probe()
            probe.timer = threading.Timer(PROBE_TIMEOUT, self._probe_timeout, (device_id,))
            probe.timer.daemon = True
            probe.timer.start()
            self._probes[device_id]     = probe
            self._last_probe[device_id] = now

        log.debug("Probing %s (Status 0)", device_id)
        self.publish(f"cmnd/{device_id}/Status", "0")

    def _get_probe(self, device_id: str) -> Optional[_Probe]:
        with self._probe_lock:
            return self._probes.get(device_id)

    def _cancel_probe(self, device_id: str) -> Optional[_Probe]:
        with self._probe_lock:
            probe = self._probes.pop(device_id, None)
        if probe and probe.timer:
            probe.timer.cancel()
        return probe

    def _probe_timeout(self, device_id: str):
        """No STATUS11 came back in time.

        A device can still be interesting without relays: if the probe
        collected an ENERGY block it is registered as a meter only.
        """
        probe = self._cancel_probe(device_id)
        if probe is None:
            return

        if probe.sensor and isinstance(probe.sensor.get("ENERGY"), dict):
            log.info("%s: no relays but reports energy - registering as a meter",
                     device_id)
            self._register(device_id, probe, channels=0)
            return

        log.info("%s: no usable Status 0 response after %ds - ignoring",
                 device_id, PROBE_TIMEOUT)

    def _finish_probe(self, device_id: str):
        probe = self._cancel_probe(device_id)
        if probe is None:
            return

        channels = self._count_channels(probe.power)
        has_energy = bool(probe.sensor and isinstance(probe.sensor.get("ENERGY"), dict))

        if channels == 0 and not has_energy:
            log.info("%s: no POWER keys and no energy data - sensor only, skipping",
                     device_id)
            return

        log.info("%s: confirmed (%d relay(s), keys=%s)",
                 device_id, channels, sorted(probe.power))
        self._register(device_id, probe, channels)

    @staticmethod
    def _count_channels(power_keys: Dict[str, Any]) -> int:
        """Highest relay index present, e.g. {POWER1, POWER2} -> 2."""
        highest = 0
        for key in power_keys:
            if key == "POWER":
                highest = max(highest, 1)
                continue
            try:
                highest = max(highest, int(key[5:]))
            except ValueError:
                continue
        return highest

    def _register(self, device_id: str, probe: _Probe, channels: int):
        name = probe.name or device_id
        ip   = probe.ip or "unknown"

        with self._lock:
            if device_id in self._devices:
                return
            device = TasmotaDevice(device_id, name, ip, channels, self.publish)
            self._devices[device_id] = device

        if channels > 0 and not device.start_switch():
            log.error("%s: switch service registration failed - dropping device",
                      device_id)
            with self._lock:
                self._devices.pop(device_id, None)
            device.destroy()
            return

        CFG.register_device(device_id, name, ip, channels)

        device.set_online(True)
        if probe.firmware:
            device.set_firmware(probe.firmware)
        if probe.power:
            device.apply_power_keys(probe.power)
        if probe.sensor:
            device.apply_sensor(probe.sensor)

        device.apply_config()

    # -----------------------------------------------------------------
    # Registry
    # -----------------------------------------------------------------
    def _get(self, device_id: str) -> Optional[TasmotaDevice]:
        with self._lock:
            return self._devices.get(device_id)

    # -----------------------------------------------------------------
    # Polling
    # -----------------------------------------------------------------
    def _poll_all(self):
        with self._lock:
            ids = list(self._devices)
        for device_id in ids:
            # Status 11 returns every POWERn key in a single message.
            self.publish(f"cmnd/{device_id}/Status", "11")
        log.debug("Polled %d device(s)", len(ids))

    def _start_poll_timer(self):
        def _loop():
            while not self._stopping:
                time.sleep(POLL_INTERVAL)
                try:
                    self._poll_all()
                except Exception:
                    log.exception("Poll failed")

        threading.Thread(target=_loop, daemon=True, name="poll-timer").start()

    # -----------------------------------------------------------------
    # mDNS
    # -----------------------------------------------------------------
    def _start_mdns(self):
        if not ZEROCONF_AVAILABLE:
            return

        def _on_service(zeroconf, service_type, name, state_change):
            if state_change is not ServiceStateChange.Added:
                return
            try:
                info = zeroconf.get_service_info(service_type, name)
            except Exception:
                return
            if not info or not info.server:
                return

            server = info.server.rstrip(".")
            if "tasmota" not in server.lower():
                return

            ip = socket.inet_ntoa(info.addresses[0]) if info.addresses else "unknown"
            # The mDNS hostname is only a guess at the MQTT topic; devices
            # with a custom topic are found through their retained LWT
            # instead, and the probe here simply times out.
            device_id = server.replace(".local", "").replace(".", "_")

            log.info("mDNS: %s @ %s - probing", device_id, ip)
            self._probe(device_id)

        try:
            zc = Zeroconf()
            ServiceBrowser(zc, "_http._tcp.local.", handlers=[_on_service])
            log.info("mDNS browser started")
        except Exception:
            log.exception("Could not start mDNS browser")

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def shutdown(self):
        if self._stopping:
            return
        self._stopping = True
        log.info("Shutting down")

        if self._mqttc:
            try:
                self._mqttc.loop_stop()
                self._mqttc.disconnect()
            except Exception:
                pass

        with self._probe_lock:
            probes = list(self._probes.values())
            self._probes.clear()
        for probe in probes:
            if probe.timer:
                probe.timer.cancel()

        with self._lock:
            devices = list(self._devices.values())
            self._devices.clear()
        for device in devices:
            try:
                device.destroy()
            except Exception:
                log.exception("Error tearing down a device service")

    def run(self):
        if REAL_DBUS and DBusGMainLoop:
            DBusGMainLoop(set_as_default=True)

        CFG.start_watcher()
        self.start_mqtt()
        self._start_mdns()
        self._start_poll_timer()

        if GLib is not None:
            log.info("Running GLib main loop")
            loop = GLib.MainLoop()

            def _quit(signame):
                log.info("%s received", signame)
                self.shutdown()
                loop.quit()
                return False

            for signame, signum in (("SIGTERM", signal.SIGTERM),
                                    ("SIGINT",  signal.SIGINT)):
                try:
                    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit, signame)
                except Exception:
                    log.debug("Could not install %s handler", signame)

            try:
                loop.run()
            except KeyboardInterrupt:
                pass
            finally:
                self.shutdown()

        else:
            log.info("Dry-run mode (no GLib) - Ctrl+C to stop")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                self.shutdown()


# =====================================================================
# Single-instance guard
# =====================================================================
_LOCK_CANDIDATES = [
    os.environ.get("TASMOTA_LOCK_FILE", "/run/tasmota-discovery.lock"),
    "/tmp/tasmota-discovery.lock",
]


class SingleInstance:
    """Advisory whole-process lock so at most one tasmota.py ever runs.

    Two live processes would both try to own the same
    com.victronenergy.switch.tasmota_* names.  flock settles it:

      * genuine crash   -> the kernel drops the lock, the next start wins
      * wedged old proc -> it still holds the lock, the new start refuses,
                           which is correct: never run two registrants
    """

    def __init__(self, candidates=None):
        self._candidates = candidates or _LOCK_CANDIDATES
        self._fd  = None
        self.path = None

    def acquire(self) -> Optional[str]:
        """None on success, otherwise a description of the current holder."""
        last_err = None
        for path in self._candidates:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as exc:
                last_err = exc
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    holder = os.read(fd, 32).decode("ascii", "replace").strip()
                except OSError:
                    holder = ""
                os.close(fd)
                return holder or "?"

            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                os.fsync(fd)
            except OSError:
                pass

            self._fd  = fd
            self.path = path
            return None

        log.warning(
            "Could not open any lock file (%s) - running without single-instance guard",
            last_err,
        )
        return None

    def release(self):
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    guard  = SingleInstance()
    holder = guard.acquire()
    if holder is not None:
        log.error(
            "Another tasmota.py is already running (pid=%s) - exiting to avoid "
            "duplicate D-Bus services. Kill the stale process if it is wedged.",
            holder,
        )
        # Throttle the supervisor's restart cadence.
        time.sleep(5)
        return 1

    log.info("tasmota.py %s - lock %s (pid=%d)", VERSION, guard.path, os.getpid())

    try:
        TasmotaDiscovery().run()
        return 0
    except KeyboardInterrupt:
        log.info("Stopped by user")
        return 0
    except Exception:
        log.exception("Fatal error")
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
