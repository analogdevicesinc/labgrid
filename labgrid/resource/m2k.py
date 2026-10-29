# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared definitions for ADALM2000 (M2K) resources.

The ADALM2000 enumerates as USB ID 0456:b672. It provides 16 digital I/O pins
(DIO0..DIO15), two programmable power supply channels (channel 0 is V+, 0 V to
5 V, channel 1 is V-, -5 V to 0 V) and two differential analog inputs
(channel 0 is 1+/1-, channel 1 is 2+/2-).
"""

import attr

M2K_USB_VENDOR_ID = "0456"
M2K_USB_MODEL_ID = "b672"
M2K_DIO_CHANNELS = 16
M2K_SUPPLY_CHANNELS = 2
M2K_SUPPLY_RANGES = {
    0: (0.0, 5.0),
    1: (-5.0, 0.0),
}
M2K_ANALOG_CHANNELS = 2
# "high" is the +-25 V range, "low" the +-2.5 V range with higher resolution
M2K_ANALOG_RANGES = ("high", "low")
M2K_ANALOG_SAMPLERATES = (1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000)


def m2k_dio_channel():
    return attr.ib(
        default=0,
        converter=int,
        validator=attr.validators.and_(
            attr.validators.instance_of(int),
            attr.validators.ge(0),
            attr.validators.le(M2K_DIO_CHANNELS - 1),
        ),
    )


def m2k_supply_channel():
    return attr.ib(
        default=0,
        converter=int,
        validator=attr.validators.in_(tuple(M2K_SUPPLY_RANGES)),
    )


def m2k_supply_voltage():
    return attr.ib(
        default=None,
        converter=attr.converters.optional(float),
        validator=attr.validators.optional(attr.validators.instance_of(float)),
    )


def m2k_analog_channel():
    return attr.ib(
        default=0,
        converter=int,
        validator=attr.validators.in_(tuple(range(M2K_ANALOG_CHANNELS))),
    )


def m2k_analog_range():
    return attr.ib(default="high", validator=attr.validators.in_(M2K_ANALOG_RANGES))


def m2k_analog_samplerate():
    return attr.ib(
        default=100_000,
        converter=int,
        validator=attr.validators.in_(M2K_ANALOG_SAMPLERATES),
    )


def m2k_analog_samples():
    return attr.ib(
        default=1000,
        converter=int,
        validator=attr.validators.and_(attr.validators.instance_of(int), attr.validators.ge(1)),
    )


def validate_m2k_supply_voltage(channel, voltage):
    """Raise ValueError if voltage is missing or outside the channel's range."""
    if voltage is None:
        raise ValueError("M2KPowerSupply requires a voltage")
    low, high = M2K_SUPPLY_RANGES[channel]
    if not low <= voltage <= high:
        name = "V+" if channel == 0 else "V-"
        raise ValueError(f"M2KPowerSupply channel {channel} ({name}) voltage must be within {low:g}..{high:g} V")
