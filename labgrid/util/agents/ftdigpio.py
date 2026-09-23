# SPDX-License-Identifier: GPL-2.0-or-later
"""Agent for controlling FTDI data-bus GPIOs via bit-bang mode.
"""

from contextlib import contextmanager

import usb.core
import usb.util

SIO_SET_BITMODE = 11
SIO_READ_PINS = 12
BITMODE_ASYNC_BITBANG = 1
OUT_REQTYPE = 0x40
IN_REQTYPE = 0xC0

USB_TIMEOUT = 1000
GPIO_MASK = 0xFF
SUPPORTED_DEVICES = {
    0x6010: 2,  # FT2232C/D/H Dual UART/FIFO IC
    0x6011: 4,  # FT4232H Quad UART/MPSSE IC
    0x6014: 1,  # FT232HL/Q
}


class FTDIGPIO:
    def __init__(self, vendor_id, model_id, busnum, devnum, interface):
        self._validate_device(vendor_id, model_id, interface)
        self._interface = interface - 1
        self._index = interface
        self._direction = 0

        self._dev = self._find_device(vendor_id, model_id, busnum, devnum)
        self._detach_kernel_driver()
        try:
            cfg = self._dev.get_active_configuration()
        except usb.core.USBError:
            self._dev.set_configuration()
            self._detach_kernel_driver()
            cfg = self._dev.get_active_configuration()

        intf = cfg[(self._interface, 0)]
        self._ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT,
        )
        if self._ep_out is None:
            raise ValueError("FTDI output endpoint not found")

    @contextmanager
    def _claimed(self):
        usb.util.claim_interface(self._dev, self._interface)
        try:
            yield
        finally:
            usb.util.release_interface(self._dev, self._interface)

    def _detach_kernel_driver(self):
        if self._dev.is_kernel_driver_active(self._interface):
            self._dev.detach_kernel_driver(self._interface)

    @staticmethod
    def _validate_device(vendor_id, model_id, interface):
        if vendor_id != 0x0403 or model_id not in SUPPORTED_DEVICES:
            raise ValueError("Unsupported FTDI GPIO device")
        if not 1 <= interface <= SUPPORTED_DEVICES[model_id]:
            raise ValueError("FTDI GPIO interface is not supported by this device")

    @staticmethod
    def _find_device(vendor_id, model_id, busnum, devnum):
        for dev in usb.core.find(find_all=True, idVendor=vendor_id, idProduct=model_id):
            if dev.bus == busnum and dev.address == devnum:
                return dev
        raise ValueError("FTDI device not found")

    def _ctrl_out(self, request, value):
        self._dev.ctrl_transfer(OUT_REQTYPE, request, value, self._index, None, USB_TIMEOUT)

    def _ctrl_in(self, request, value, length):
        return bytes(self._dev.ctrl_transfer(IN_REQTYPE, request, value, self._index, length, USB_TIMEOUT))

    def _set_bitmode(self, mask, mode):
        self._ctrl_out(SIO_SET_BITMODE, mask | (mode << 8))

    def _write(self, data):
        self._ep_out.write(bytes(data), USB_TIMEOUT)

    @staticmethod
    def _validate_index(index):
        if not 0 <= index <= 7:
            raise ValueError("FTDI bit-bang GPIO only supports indexes 0-7")

    def _read_gpio_byte(self):
        data = self._ctrl_in(SIO_READ_PINS, 0, 1)
        if not data:
            raise TimeoutError("FTDI GPIO read returned no data")
        return data[0]

    def setup(self, index, output):
        # Program this line's direction (config-driven, fixed for the session).
        # Update the interface's mask and re-enter async bit-bang mode.
        self._validate_index(index)
        mask = 1 << index
        direction = self._direction | mask if output else self._direction & ~mask
        with self._claimed():
            self._set_bitmode(direction, BITMODE_ASYNC_BITBANG)
        self._direction = direction

    def get(self, index):
        self._validate_index(index)
        with self._claimed():
            value = self._read_gpio_byte()
        return bool(value & (1 << index))

    def set(self, index, status):
        self._validate_index(index)
        mask = 1 << index
        if not self._direction & mask:
            raise ValueError(f"FTDI GPIO line {index} is configured as input, cannot set")
        with self._claimed():
            output = self._read_gpio_byte()
            if status:
                output |= mask
            else:
                output &= ~mask
            self._write([output])


_devices = {}


def _get_device(vendor_id, model_id, busnum, devnum, interface):
    key = (busnum, devnum, interface)
    if key not in _devices:
        _devices[key] = FTDIGPIO(vendor_id, model_id, busnum, devnum, interface)
    return _devices[key]


def handle_get(vendor_id, model_id, busnum, devnum, interface, index):
    return _get_device(vendor_id, model_id, busnum, devnum, interface).get(int(index))


def handle_set(vendor_id, model_id, busnum, devnum, interface, index, status):
    _get_device(vendor_id, model_id, busnum, devnum, interface).set(int(index), bool(status))
    return True


def handle_setup(vendor_id, model_id, busnum, devnum, interface, index, output):
    _get_device(vendor_id, model_id, busnum, devnum, interface).setup(int(index), bool(output))
    return True


methods = {
    "get": handle_get,
    "set": handle_set,
    "setup": handle_setup,
}
