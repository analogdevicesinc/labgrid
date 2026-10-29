# SPDX-License-Identifier: GPL-2.0-or-later
"""ADALM2000 (M2K) drivers using a labgrid agent running libm2k."""

import threading
import time

import attr

from ..factory import target_factory
from ..protocol import AnalogInputProtocol, DigitalOutputProtocol, PowerProtocol
from ..resource.m2k import validate_m2k_supply_voltage
from ..resource.remote import NetworkM2KAnalogIn, NetworkM2KDigitalIO, NetworkM2KPowerSupply
from ..step import step
from ..util.agentwrapper import AgentWrapper
from .common import Driver
from .powerdriver import PowerResetMixin

# All drivers for one physical ADALM2000 share one agent (and thereby one
# libm2k context). The lock protects only this client-side lifecycle
# bookkeeping; agent requests are handled synchronously by the agent process.
_shared_agents = {}
_shared_lock = threading.Lock()


def _agent_key(resource, remote_cls):
    host = resource.host if isinstance(resource, remote_cls) else None
    return (host, resource.busnum, resource.devnum)


def _acquire_agent(key):
    host = key[0]
    with _shared_lock:
        entry = _shared_agents.get(key)
        if entry is None:
            wrapper = AgentWrapper(host)
            proxy = wrapper.load("m2k")
            entry = {"wrapper": wrapper, "proxy": proxy, "refs": 0}
            _shared_agents[key] = entry
        entry["refs"] += 1
        return entry["proxy"]


def _release_agent(key):
    with _shared_lock:
        entry = _shared_agents.get(key)
        if entry is None:
            return
        entry["refs"] -= 1
        if entry["refs"] <= 0:
            del _shared_agents[key]
            entry["wrapper"].close()


@target_factory.reg_driver
@attr.s(eq=False)
class M2KDigitalIODriver(Driver, DigitalOutputProtocol):
    """Control one ADALM2000 DIO pin through a labgrid agent."""

    bindings = {
        "gpio": {"M2KDigitalIO", NetworkM2KDigitalIO},
    }

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._proxy = None
        self._key = None

    def on_activate(self):
        # remember the key: the resource's bus/device numbers may change on re-enumeration
        self._key = _agent_key(self.gpio, NetworkM2KDigitalIO)
        proxy = _acquire_agent(self._key)
        # Program the pin direction from the resource config before any
        # read/write. Only this pin is touched, other pins keep their state.
        try:
            proxy.dio_setup(
                self.gpio.busnum,
                self.gpio.devnum,
                self.gpio.channel,
                self.gpio.direction == "out",
            )
        except Exception:
            _release_agent(self._key)
            self._key = None
            raise
        self._proxy = proxy

    def on_deactivate(self):
        self._proxy = None
        _release_agent(self._key)
        self._key = None

    @Driver.check_active
    @step(result=True)
    def get(self):
        status = self._proxy.dio_get(self.gpio.busnum, self.gpio.devnum, self.gpio.channel)
        if self.gpio.invert:
            status = not status
        return status

    @Driver.check_active
    @step(args=["status"])
    def set(self, status):
        if self.gpio.invert:
            status = not status
        self._proxy.dio_set(self.gpio.busnum, self.gpio.devnum, self.gpio.channel, status)


@target_factory.reg_driver
@attr.s(eq=False)
class M2KPowerSupplyDriver(Driver, PowerResetMixin, PowerProtocol):
    """Control one ADALM2000 power supply channel (V+ or V-) through a labgrid agent."""

    bindings = {
        "supply": {"M2KPowerSupply", NetworkM2KPowerSupply},
    }
    delay = attr.ib(default=2.0, validator=attr.validators.instance_of(float))

    # Outputs below this absolute voltage are reported as off by get()
    ON_THRESHOLD = 0.1

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._proxy = None
        self._key = None
        self._voltage = self.supply.voltage

    def on_activate(self):
        self._key = _agent_key(self.supply, NetworkM2KPowerSupply)
        self._proxy = _acquire_agent(self._key)

    def on_deactivate(self):
        self._proxy = None
        _release_agent(self._key)
        self._key = None

    def _call(self, method, *args):
        return getattr(self._proxy, method)(self.supply.busnum, self.supply.devnum, self.supply.channel, *args)

    @Driver.check_active
    @step()
    def on(self):
        self._call("supply_set", self._voltage, True)

    @Driver.check_active
    @step()
    def off(self):
        self._call("supply_enable", False)

    @Driver.check_active
    @step()
    def cycle(self):
        self.off()
        time.sleep(self.delay)
        self.on()

    @Driver.check_active
    @step(result=True)
    def get(self):
        return abs(self._call("supply_read")) >= self.ON_THRESHOLD

    @Driver.check_active
    @step(result=True)
    def get_voltage(self):
        """Return the voltage currently measured at the output."""
        return self._call("supply_read")

    @Driver.check_active
    @step(args=["voltage"])
    def set_voltage(self, voltage):
        """Change the setpoint without changing the on/off state (takes effect immediately if on)."""
        voltage = float(voltage)
        validate_m2k_supply_voltage(self.supply.channel, voltage)
        self._voltage = voltage
        self._call("supply_push", voltage)


@target_factory.reg_driver
@attr.s(eq=False)
class M2KAnalogInDriver(Driver, AnalogInputProtocol):
    """Read one ADALM2000 differential analog input through a labgrid agent."""

    bindings = {
        "analog": {"M2KAnalogIn", NetworkM2KAnalogIn},
    }

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._proxy = None
        self._key = None

    def on_activate(self):
        self._key = _agent_key(self.analog, NetworkM2KAnalogIn)
        proxy = _acquire_agent(self._key)
        try:
            proxy.analog_setup(
                self.analog.busnum,
                self.analog.devnum,
                self.analog.channel,
                self.analog.range,
                self.analog.samplerate,
            )
        except Exception:
            _release_agent(self._key)
            self._key = None
            raise
        self._proxy = proxy

    def on_deactivate(self):
        self._proxy = None
        _release_agent(self._key)
        self._key = None

    @Driver.check_active
    @step(result=True)
    def get_voltage(self):
        """Return the voltage averaged over the configured number of samples."""
        return self._proxy.analog_read(
            self.analog.busnum, self.analog.devnum, self.analog.channel, self.analog.samples
        )

    @Driver.check_active
    @step(args=["samples"], result=True)
    def get_samples(self, samples=None):
        """Return a list of voltage samples taken at the configured sample rate."""
        if samples is None:
            samples = self.analog.samples
        return self._proxy.analog_samples(self.analog.busnum, self.analog.devnum, self.analog.channel, samples)
