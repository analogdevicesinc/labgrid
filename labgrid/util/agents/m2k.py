# SPDX-License-Identifier: GPL-2.0-or-later
"""Agent for controlling an ADALM2000 (M2K) via the libm2k python bindings.

The device is identified by its USB bus/device number (from udev). libiio's USB
backend claims the USB interface exclusively, so only one process could use the
device at a time. The ADALM2000 also runs iiod on its USB ethernet gadget, which
accepts several clients. When that interface is up, the agent connects over it
(the gadget's first address, 192.168.2.1 by default) and falls back to the USB
backend otherwise. The device serial is checked against sysfs in both cases.

DIO pins and the power supply channels are accessed at attribute level only,
so they never disturb each other. The analog inputs need calibration and a
capture buffer. The calibration state lives on the device, so it is only
performed if the device reports itself as uncalibrated: from values stored on
the device when the firmware provides them, otherwise by a one-time ADC
calibration when the first analog input is set up.
"""

import contextlib
import fcntl
import glob
import ipaddress
import os
import re
import socket
import struct
import sys
import time

try:
    import libm2k
except ImportError:  # pragma: no cover - depends on the host installation
    libm2k = None

DIO_CHANNELS = 16
SUPPLY_CHANNELS = 2
ANALOG_CHANNELS = 2
# the ADC buffer is exclusive, retry for a short while if someone else holds it
BUSY_RETRIES = 5
BUSY_DELAY = 0.2

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"


class M2KArgumentError(ValueError):
    """Raised for invalid arguments; unlike device errors it does not invalidate the context."""


def _read_sysfs(path):
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def _sysfs_device(busnum, devnum):
    """Return the sysfs directory of the USB device with the given bus/device number."""
    for path in glob.glob(os.path.join(SYSFS_USB_DEVICES, "*")):
        try:
            if int(_read_sysfs(os.path.join(path, "busnum"))) != busnum:
                continue
            if int(_read_sysfs(os.path.join(path, "devnum"))) != devnum:
                continue
        except (TypeError, ValueError):
            continue
        return path
    return None


def _gadget_interfaces(sysdev):
    """Return the network interface names belonging to the USB device."""
    return sorted(os.path.basename(path) for path in glob.glob(os.path.join(sysdev, "*", "net", "*")))


def _interface_network(ifname):
    """Return the IPv4 network configured on the interface, or None."""
    ifreq = struct.pack("256s", ifname.encode("ascii")[:15])
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            addr = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), SIOCGIFADDR, ifreq)[20:24])
            mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, ifreq)[20:24])
    except OSError:
        return None
    return ipaddress.ip_interface(f"{addr}/{mask}").network


def _usb_uri(busnum, devnum):
    """Return the libiio USB URI ("usb:<bus>.<address>.<interface>") of the device, or None."""
    pattern = re.compile(rf"^usb:{int(busnum)}\.{int(devnum)}(\.\d+)?$")
    for uri in libm2k.getAllContexts():
        if pattern.match(uri):
            return uri
    return None


def _network_uris(busnum, devnum):
    """Return the shareable network URIs of the device's USB ethernet gadget interfaces."""
    uris = []
    sysdev = _sysfs_device(busnum, devnum)
    if sysdev is not None:
        for ifname in _gadget_interfaces(sysdev):
            network = _interface_network(ifname)
            if network is not None and network.num_addresses > 2:
                # the ADALM2000 runs iiod on the first address of its gadget network
                uris.append(f"ip:{network.network_address + 1}")
    return uris


class M2K:
    def __init__(self, busnum, devnum):
        if libm2k is None:
            raise RuntimeError("libm2k python bindings are not installed on this host")
        self._serial = self._sysfs_serial(busnum, devnum)
        self._uri, self._ctx = self._open(busnum, devnum)
        self._digital = self._ctx.getDigital()
        self._supply = self._ctx.getPowerSupply()
        self._analog = None
        self._samplerates = {}

    @staticmethod
    def _sysfs_serial(busnum, devnum):
        sysdev = _sysfs_device(busnum, devnum)
        if sysdev is None:
            return None
        return _read_sysfs(os.path.join(sysdev, "serial"))

    def _open(self, busnum, devnum):
        errors = []
        # shareable network URIs first, the exclusive USB backend last
        candidates = _network_uris(busnum, devnum)
        usb_uri = _usb_uri(busnum, devnum)
        if usb_uri is not None:
            candidates.append(usb_uri)
        else:
            errors.append(f"ADALM2000 on USB bus {busnum} device {devnum} not found by libiio")
        for uri in candidates:
            try:
                ctx = libm2k.m2kOpen(uri)
            except Exception as e:  # pylint: disable=broad-except
                errors.append(f"{uri}: {e}")
                continue
            if ctx is None:
                errors.append(f"{uri}: failed to open")
                continue
            serial = ctx.getSerialNumber()
            if self._serial and serial != self._serial:
                # deinit=False: do not power down the other device's outputs
                libm2k.contextClose(ctx, False)
                errors.append(f"{uri}: serial {serial} does not match USB device serial {self._serial}")
                continue
            if errors:
                print(f"ADALM2000: using {uri} after: {'; '.join(errors)}", file=sys.stderr)
            return uri, ctx
        raise ValueError(f"failed to open ADALM2000 on USB bus {busnum} device {devnum}: " + "; ".join(errors))

    def close(self):
        """Drop the context without deinitializing the device (other clients keep working)."""
        with contextlib.suppress(Exception):
            libm2k.contextClose(self._ctx, False)

    @staticmethod
    def _validate_dio(channel):
        if not 0 <= channel < DIO_CHANNELS:
            raise M2KArgumentError(f"ADALM2000 DIO channel must be 0-{DIO_CHANNELS - 1}")

    @staticmethod
    def _validate_supply(channel):
        if not 0 <= channel < SUPPLY_CHANNELS:
            raise M2KArgumentError(f"ADALM2000 power supply channel must be 0-{SUPPLY_CHANNELS - 1}")

    @staticmethod
    def _validate_analog(channel):
        if not 0 <= channel < ANALOG_CHANNELS:
            raise M2KArgumentError(f"ADALM2000 analog input channel must be 0-{ANALOG_CHANNELS - 1}")

    def info(self):
        return {
            "uri": self._uri,
            "serial": self._ctx.getSerialNumber(),
            "firmware": self._ctx.getFirmwareVersion(),
        }

    def dio_setup(self, channel, output):
        self._validate_dio(channel)
        direction = libm2k.DIO_OUTPUT if output else libm2k.DIO_INPUT
        self._digital.setDirection(channel, direction)

    def dio_get(self, channel):
        self._validate_dio(channel)
        return self._digital.getValueRaw(channel) == libm2k.HIGH

    def dio_set(self, channel, status):
        self._validate_dio(channel)
        if self._digital.getDirection(channel) != libm2k.DIO_OUTPUT:
            raise M2KArgumentError(f"ADALM2000 DIO{channel} is configured as input, cannot set")
        self._digital.setValueRaw(channel, libm2k.HIGH if status else libm2k.LOW)

    def supply_set(self, channel, voltage, enable):
        self._validate_supply(channel)
        self._supply.pushChannel(channel, float(voltage))
        self._supply.enableChannel(channel, bool(enable))

    def supply_enable(self, channel, enable):
        self._validate_supply(channel)
        self._supply.enableChannel(channel, bool(enable))

    def supply_push(self, channel, voltage):
        """Change the setpoint without touching the enable state."""
        self._validate_supply(channel)
        self._supply.pushChannel(channel, float(voltage))

    def supply_read(self, channel):
        self._validate_supply(channel)
        return float(self._supply.readChannel(channel))

    def _calibrate(self):
        # The calibration state lives on the device and is visible to every
        # context, so a device calibrated earlier (by any client, since power-up)
        # is not touched again.
        if self._ctx.isCalibrated():
            return
        if self._ctx.hasContextCalibration():
            try:
                self._ctx.calibrateFromContext()
                return
            except Exception as e:  # pylint: disable=broad-except
                print(f"ADALM2000: calibration from context failed ({e}), calibrating ADC", file=sys.stderr)
        if not self._ctx.calibrateADC():
            raise RuntimeError("ADALM2000 ADC calibration failed")

    def _get_analog(self):
        if self._analog is None:
            self._calibrate()
            self._analog = self._ctx.getAnalogIn()
            # one kernel buffer, so every read returns fresh samples
            self._analog.setKernelBuffersCount(1)
        return self._analog

    def analog_setup(self, channel, range_name, samplerate):
        self._validate_analog(channel)
        analog = self._get_analog()
        # getSamples() returns one list per channel, keep both enabled so the
        # channel index always matches
        for chn in range(ANALOG_CHANNELS):
            analog.enableChannel(chn, True)
        if range_name == "low":
            analog.setRange(channel, libm2k.PLUS_MINUS_2_5V)
        elif range_name == "high":
            analog.setRange(channel, libm2k.PLUS_MINUS_25V)
        else:
            raise M2KArgumentError(f"unknown ADALM2000 analog input range {range_name!r}")
        # the sample rate is shared by both channels of the device
        others = {chn: rate for chn, rate in self._samplerates.items() if chn != channel and rate != samplerate}
        if others:
            raise M2KArgumentError(
                f"ADALM2000 analog input sample rate {samplerate} conflicts with {others} already configured"
            )
        analog.setSampleRate(float(samplerate))
        self._samplerates[channel] = samplerate

    def _get_samples(self, samples):
        analog = self._get_analog()
        for attempt in range(BUSY_RETRIES):
            try:
                return analog.getSamples(int(samples))
            except Exception as e:  # pylint: disable=broad-except
                if "busy" not in str(e).lower() or attempt == BUSY_RETRIES - 1:
                    raise
                time.sleep(BUSY_DELAY)
        raise RuntimeError("unreachable")  # pragma: no cover

    def analog_samples(self, channel, samples):
        self._validate_analog(channel)
        if samples < 1:
            raise M2KArgumentError("ADALM2000 analog input needs at least one sample")
        return [float(v) for v in self._get_samples(samples)[channel]]

    def analog_read(self, channel, samples):
        values = self.analog_samples(channel, samples)
        return sum(values) / len(values)


_devices = {}


def _call(busnum, devnum, method, *args):
    """Run a method on the (cached) device; drop the device on errors so the next call reconnects."""
    key = (busnum, devnum)
    try:
        if key not in _devices:
            _devices[key] = M2K(busnum, devnum)
        return getattr(_devices[key], method)(*args)
    except M2KArgumentError:
        raise
    except Exception:
        device = _devices.pop(key, None)
        if device is not None:
            device.close()
        raise


def handle_info(busnum, devnum):
    return _call(busnum, devnum, "info")


def handle_dio_setup(busnum, devnum, channel, output):
    _call(busnum, devnum, "dio_setup", int(channel), bool(output))


def handle_dio_get(busnum, devnum, channel):
    return _call(busnum, devnum, "dio_get", int(channel))


def handle_dio_set(busnum, devnum, channel, status):
    _call(busnum, devnum, "dio_set", int(channel), bool(status))


def handle_supply_set(busnum, devnum, channel, voltage, enable):
    _call(busnum, devnum, "supply_set", int(channel), float(voltage), bool(enable))


def handle_supply_enable(busnum, devnum, channel, enable):
    _call(busnum, devnum, "supply_enable", int(channel), bool(enable))


def handle_supply_push(busnum, devnum, channel, voltage):
    _call(busnum, devnum, "supply_push", int(channel), float(voltage))


def handle_supply_read(busnum, devnum, channel):
    return _call(busnum, devnum, "supply_read", int(channel))


def handle_analog_setup(busnum, devnum, channel, range_name, samplerate):
    _call(busnum, devnum, "analog_setup", int(channel), str(range_name), int(samplerate))


def handle_analog_read(busnum, devnum, channel, samples):
    return _call(busnum, devnum, "analog_read", int(channel), int(samples))


def handle_analog_samples(busnum, devnum, channel, samples):
    return _call(busnum, devnum, "analog_samples", int(channel), int(samples))


methods = {
    "info": handle_info,
    "dio_setup": handle_dio_setup,
    "dio_get": handle_dio_get,
    "dio_set": handle_dio_set,
    "supply_set": handle_supply_set,
    "supply_enable": handle_supply_enable,
    "supply_push": handle_supply_push,
    "supply_read": handle_supply_read,
    "analog_setup": handle_analog_setup,
    "analog_read": handle_analog_read,
    "analog_samples": handle_analog_samples,
}
