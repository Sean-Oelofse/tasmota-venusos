# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import logging
import threading
import socket
import re
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
MQTT_HOST = os.environ.get("TASMOTA_MQTT_HOST", "192.168.10.80")
MQTT_PORT = int(os.environ.get("TASMOTA_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("TASMOTA_MQTT_USER", "")
MQTT_PASS = os.environ.get("TASMOTA_MQTT_PASS", "")
POLL_INTERVAL = int(os.environ.get("TASMOTA_POLL_INTERVAL", "30"))

# ---------------------------------------------------------------------
# Dry-run shim
# ---------------------------------------------------------------------
class _Shim:
    def __init__(self, name, **kw):
        self._name = name
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


# =====================================================================
# TasmotaDevice
# =====================================================================
class TasmotaDevice:

    STATUS_OFF = 0x00
    STATUS_ON = 0x09

    MODULE_CONNECTED = 0x100

    def __init__(self, device_id: str, friendly_name: str, ip: str, channels: int = 1):
        self.device_id = device_id
        self.friendly_name = friendly_name
        self.ip = ip
        self.channels = channels

        self._svc = None
        self._lock = threading.Lock()
        self._mqtt_publish = None

        self._init_service()

    def _init_service(self):

        svc_name = f"com.victronenergy.switch.tasmota_{self.device_id}"

        kwargs = {}

        if VENUS_OS and dbus:
            # Each service needs its own private D-Bus connection.
            # dbus.SystemBus() is a shared singleton - all services would
            # try to register '/' on the same connection and collide.
            # dbus.bus.BusConnection gives a fresh private connection each time.
            private_bus = dbus.bus.BusConnection(
                dbus.bus.BusConnection.TYPE_SYSTEM
            )
            kwargs["bus"] = private_bus
            kwargs["register"] = False  # new velib API: add paths first, then register

        svc = VeDbusService(svc_name, **kwargs)

        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", "1.1.0")
        svc.add_path("/Mgmt/Connection", f"MQTT {MQTT_HOST}")

        svc.add_path("/ProductName", f"Tasmota ({self.friendly_name})")
        svc.add_path("/ProductId", 0xB040)

        svc.add_path("/DeviceInstance", self._stable_instance())

        svc.add_path("/Connected", 0)

        svc.add_path("/FirmwareVersion", "")
        svc.add_path("/HardwareVersion", "")

        svc.add_path("/Serial", self.device_id)
        svc.add_path("/CustomName", self.friendly_name)

        svc.add_path("/State", self.MODULE_CONNECTED)

        for ch in range(self.channels):

            label = ch

            svc.add_path(f"/Channel/{label}/Direction", 0)

            svc.add_path(
                f"/SwitchableOutput/{label}/State",
                0,
                writeable=True,
                onchangecallback=lambda path, val, c=ch: self._on_state_write(c, val),
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Status",
                self.STATUS_OFF
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Name",
                f"CH{ch + 1}" if self.channels > 1 else "Relay"
            )

            ch_name = (
                f"{self.friendly_name} CH{ch + 1}"
                if self.channels > 1
                else self.friendly_name
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/CustomName",
                ch_name,
                writeable=True
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/Type",
                1
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/ValidTypes",
                0b0000000010
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/Function",
                2
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/ValidFunctions",
                0b0000100
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/Group",
                "Tasmota",
                writeable=True
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/ShowUIControl",
                1,
                writeable=True
            )

            svc.add_path(
                f"/SwitchableOutput/{label}/Settings/Adjustable",
                1
            )

        # Call register() after all paths are added (new velib API)
        if VENUS_OS and dbus and hasattr(svc, "register"):
            svc.register()

        self._svc = svc

        log.info(
            "Registered %s (%d channel(s))",
            svc_name,
            self.channels
        )

    def _stable_instance(self) -> int:
        hex_part = re.sub(r"[^0-9A-Fa-f]", "", self.device_id[-6:]) or "1"
        return int(hex_part, 16) % 2000

    def set_online(self, online: bool):

        with self._lock:
            self._svc["/Connected"] = 1 if online else 0

            if online:
                self._svc["/State"] = self.MODULE_CONNECTED

        log.info(
            "%s -> %s",
            self.device_id,
            "ONLINE" if online else "OFFLINE"
        )

    def set_power(self, channel: int, state_str: str):

        ch_index = channel - 1

        on = state_str.upper() == "ON"

        with self._lock:
            self._svc[f"/SwitchableOutput/{ch_index}/State"] = 1 if on else 0

            self._svc[f"/SwitchableOutput/{ch_index}/Status"] = (
                self.STATUS_ON if on else self.STATUS_OFF
            )

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

        cmd = "ON" if new_value else "OFF"

        channel = ch_index + 1

        power_key = (
            "POWER"
            if self.channels == 1
            else f"POWER{channel}"
        )

        topic = f"cmnd/{self.device_id}/{power_key}"

        if self._mqtt_publish:
            self._mqtt_publish(topic, cmd)

            log.info(
                "GUI->MQTT %s %s",
                topic,
                cmd
            )

    def inject_mqtt_publish(self, fn):
        self._mqtt_publish = fn


# =====================================================================
# TasmotaDiscovery
# =====================================================================
class TasmotaDiscovery:

    def __init__(self):

        self._devices = {}
        self._lock = threading.Lock()
        self._mqttc = None

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

        self._mqttc.on_connect = self._on_connect
        self._mqttc.on_disconnect = self._on_disconnect
        self._mqttc.on_message = self._on_message

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
            "stat/+/STATUS",
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
        subtopic = parts[-1]

        # -------------------------------------------------------------
        # LWT
        # -------------------------------------------------------------
        if subtopic == "LWT":

            online = payload.lower() == "online"

            if online:
                self._ensure_device(
                    device_id,
                    device_id,
                    "unknown"
                )

                self._mqttc.publish(
                    f"cmnd/{device_id}/Status",
                    "0"
                )

            dev = self._get(device_id)

            if dev:
                dev.set_online(online)

            return

        # -------------------------------------------------------------
        # INFO1
        # -------------------------------------------------------------
        if subtopic == "INFO1":

            try:
                info = json.loads(payload)

            except json.JSONDecodeError:
                return

            fname = info.get("FriendlyName1", device_id)
            version = info.get("Version", "")
            ip = info.get("IPAddress", "unknown")

            dev = self._ensure_device(
                device_id,
                fname,
                ip
            )

            dev.set_firmware(version)
            dev.set_online(True)

            return

        # -------------------------------------------------------------
        # STATUS
        # -------------------------------------------------------------
        if subtopic == "STATUS":

            try:
                data = json.loads(payload)

                status = data.get("Status", {})

                names = status.get(
                    "FriendlyName",
                    [device_id]
                )

                fname = names[0] if names else device_id

                channels = max(1, len(names))

            except (json.JSONDecodeError, TypeError):
                return

            self._ensure_device(
                device_id,
                fname,
                "unknown",
                channels
            )

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

            else:
                dev = self._devices[device_id]

                if (
                    friendly_name != device_id
                    and dev.friendly_name == device_id
                ):
                    dev.update_name(friendly_name)

        return self._devices[device_id]

    def _get(self, device_id: str) -> Optional[TasmotaDevice]:

        with self._lock:
            return self._devices.get(device_id)

    # -----------------------------------------------------------------
    # Polling
    # -----------------------------------------------------------------
    def _poll_all(self):

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

        def _on_service(zeroconf, service_type, name, state_change, **kwargs):
            zc = zeroconf
            svc_type = service_type

            if state_change is not ServiceStateChange.Added:
                return

            info = zc.get_service_info(
                svc_type,
                name
            )

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
                "mDNS found: %s @ %s",
                device_id,
                ip
            )

            self._ensure_device(
                device_id,
                device_id,
                ip
            )

            self._mqttc.publish(
                f"cmnd/{device_id}/Status",
                "0"
            )

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
