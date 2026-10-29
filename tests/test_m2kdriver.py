import types
from unittest.mock import MagicMock

import pytest

from labgrid.driver import m2kdriver
from labgrid.driver.m2kdriver import M2KAnalogInDriver, M2KDigitalIODriver, M2KPowerSupplyDriver
from labgrid.protocol import AnalogInputProtocol
from labgrid.remote.client import ClientSession
from labgrid.remote.exporter import USBM2KExport
from labgrid.resource.common import ResourceManager
from labgrid.resource.remote import NetworkM2KAnalogIn, NetworkM2KDigitalIO, NetworkM2KPowerSupply
from labgrid.resource.udev import M2KAnalogIn, M2KDigitalIO, M2KPowerSupply
from labgrid.util.agents import m2k

USB = {
    "host": "exporter",
    "busnum": 1,
    "devnum": 7,
    "path": "1-3.2",
    "vendor_id": 0x0456,
    "model_id": 0xB672,
}


@pytest.fixture(autouse=True)
def clear_m2k_state():
    m2kdriver._shared_agents.clear()
    m2k._devices.clear()
    yield
    m2kdriver._shared_agents.clear()
    m2k._devices.clear()


@pytest.fixture
def remote_managers(monkeypatch):
    monkeypatch.setattr(NetworkM2KDigitalIO, "manager_cls", ResourceManager)
    monkeypatch.setattr(NetworkM2KPowerSupply, "manager_cls", ResourceManager)
    monkeypatch.setattr(NetworkM2KAnalogIn, "manager_cls", ResourceManager)


class FakeProxy:
    """Stands in for the agent module proxy and records every call."""

    def __init__(self):
        self.calls = []
        self.dio = {}
        self.supply = {}

    def dio_setup(self, busnum, devnum, channel, output):
        self.calls.append(("dio_setup", busnum, devnum, channel, output))

    def dio_get(self, busnum, devnum, channel):
        self.calls.append(("dio_get", busnum, devnum, channel))
        return self.dio.get(channel, False)

    def dio_set(self, busnum, devnum, channel, status):
        self.calls.append(("dio_set", busnum, devnum, channel, status))
        self.dio[channel] = status

    def supply_set(self, busnum, devnum, channel, voltage, enable):
        self.calls.append(("supply_set", busnum, devnum, channel, voltage, enable))
        self.supply[channel] = (voltage, enable)

    def supply_enable(self, busnum, devnum, channel, enable):
        self.calls.append(("supply_enable", busnum, devnum, channel, enable))
        voltage, _ = self.supply.get(channel, (0.0, False))
        self.supply[channel] = (voltage, enable)

    def supply_push(self, busnum, devnum, channel, voltage):
        self.calls.append(("supply_push", busnum, devnum, channel, voltage))
        _, enable = self.supply.get(channel, (0.0, False))
        self.supply[channel] = (voltage, enable)

    def supply_read(self, busnum, devnum, channel):
        self.calls.append(("supply_read", busnum, devnum, channel))
        voltage, enable = self.supply.get(channel, (0.0, False))
        return voltage if enable else 0.0

    def analog_setup(self, busnum, devnum, channel, range_name, samplerate):
        self.calls.append(("analog_setup", busnum, devnum, channel, range_name, samplerate))

    def analog_read(self, busnum, devnum, channel, samples):
        self.calls.append(("analog_read", busnum, devnum, channel, samples))
        return 1.25

    def analog_samples(self, busnum, devnum, channel, samples):
        self.calls.append(("analog_samples", busnum, devnum, channel, samples))
        return [1.0, 1.5] * (samples // 2)


def make_fake_wrapper(proxy):
    class FakeWrapper:
        instances = []

        def __init__(self, host):
            self.host = host
            self.closed = False
            FakeWrapper.instances.append(self)

        def load(self, name):
            assert name == "m2k"
            return proxy

        def close(self):
            self.closed = True

    return FakeWrapper


# resources


def test_m2k_resource_create(target, monkeypatch):
    monkeypatch.setattr(M2KDigitalIO, "manager_cls", ResourceManager)
    monkeypatch.setattr(M2KPowerSupply, "manager_cls", ResourceManager)

    gpio = M2KDigitalIO(target, name=None, channel=3)
    assert gpio.channel == 3
    assert gpio.direction == "out"
    assert gpio.invert is False
    assert gpio.match["DEVTYPE"] == "usb_device"

    supply = M2KPowerSupply(target, name=None, channel=1, voltage=-5)
    assert supply.channel == 1
    assert supply.voltage == -5.0


def test_m2k_resource_validates(target, monkeypatch):
    monkeypatch.setattr(M2KDigitalIO, "manager_cls", ResourceManager)
    monkeypatch.setattr(M2KPowerSupply, "manager_cls", ResourceManager)

    with pytest.raises(ValueError):
        M2KDigitalIO(target, name=None, channel=16)
    with pytest.raises(ValueError):
        M2KDigitalIO(target, name=None, channel=-1)
    with pytest.raises(ValueError):
        M2KDigitalIO(target, name=None, direction="sideways")

    with pytest.raises(ValueError, match="requires a voltage"):
        M2KPowerSupply(target, name=None, channel=0)
    with pytest.raises(ValueError, match="0..5 V"):
        M2KPowerSupply(target, name=None, channel=0, voltage=5.5)
    with pytest.raises(ValueError, match="-5..0 V"):
        M2KPowerSupply(target, name=None, channel=1, voltage=3.3)
    with pytest.raises(ValueError):
        M2KPowerSupply(target, name=None, channel=2, voltage=1.0)


def test_m2k_resource_filters_devices(target, monkeypatch):
    monkeypatch.setattr(M2KDigitalIO, "manager_cls", ResourceManager)
    resource = M2KDigitalIO(target, name=None)

    def device(vendor_id, model_id):
        return types.SimpleNamespace(
            device_type="usb_device",
            properties={"ID_VENDOR_ID": vendor_id, "ID_MODEL_ID": model_id},
        )

    assert resource.filter_match(device("0456", "b672")) is True
    assert resource.filter_match(device("0456", "b673")) is False  # ADALM-PLUTO
    assert resource.filter_match(device("0403", "6014")) is False


def test_network_m2k_resource_validates(target, remote_managers):
    with pytest.raises(ValueError):
        NetworkM2KDigitalIO(target, name=None, channel=16, **USB)
    with pytest.raises(ValueError, match="requires a voltage"):
        NetworkM2KPowerSupply(target, name=None, channel=0, **USB)
    with pytest.raises(ValueError, match="-5..0 V"):
        NetworkM2KPowerSupply(target, name=None, channel=1, voltage=1.0, **USB)


def test_m2k_analog_resource(target, monkeypatch):
    monkeypatch.setattr(M2KAnalogIn, "manager_cls", ResourceManager)

    analog = M2KAnalogIn(target, name=None, channel=1, range="low", samplerate=1000000, samples=50)
    assert (analog.channel, analog.range, analog.samplerate, analog.samples) == (1, "low", 1000000, 50)
    defaults = M2KAnalogIn(target, name=None)
    assert (defaults.channel, defaults.range, defaults.samplerate, defaults.samples) == (0, "high", 100000, 1000)

    with pytest.raises(ValueError):
        M2KAnalogIn(target, name=None, channel=2)
    with pytest.raises(ValueError):
        M2KAnalogIn(target, name=None, range="medium")
    with pytest.raises(ValueError):
        M2KAnalogIn(target, name=None, samplerate=12345)
    with pytest.raises(ValueError):
        M2KAnalogIn(target, name=None, samples=0)


def test_network_m2k_analog_resource_validates(target, remote_managers):
    with pytest.raises(ValueError):
        NetworkM2KAnalogIn(target, name=None, channel=2, **USB)
    with pytest.raises(ValueError):
        NetworkM2KAnalogIn(target, name=None, samplerate=5, **USB)


# drivers


def test_m2k_digital_driver_set_get(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    monkeypatch.setattr(m2kdriver, "AgentWrapper", make_fake_wrapper(proxy))

    gpio = NetworkM2KDigitalIO(target, name=None, channel=2, invert=True, **USB)
    gpio.avail = True
    driver = M2KDigitalIODriver(target, name=None)

    target.activate(driver)
    assert proxy.calls == [("dio_setup", 1, 7, 2, True)]

    driver.set(True)
    assert proxy.calls[-1] == ("dio_set", 1, 7, 2, False)
    assert driver.get() is True
    assert proxy.calls[-1] == ("dio_get", 1, 7, 2)

    target.deactivate(driver)
    assert m2kdriver._shared_agents == {}


def test_m2k_digital_driver_input_direction(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    monkeypatch.setattr(m2kdriver, "AgentWrapper", make_fake_wrapper(proxy))

    gpio = NetworkM2KDigitalIO(target, name=None, channel=9, direction="in", **USB)
    gpio.avail = True
    driver = M2KDigitalIODriver(target, name=None)

    target.activate(driver)
    assert proxy.calls == [("dio_setup", 1, 7, 9, False)]
    proxy.dio[9] = True
    assert driver.get() is True
    target.deactivate(driver)


def test_m2k_digital_driver_setup_failure_releases_agent(target, remote_managers, monkeypatch):
    proxy = FakeProxy()

    def failing_setup(*args):
        raise RuntimeError("setup failed")

    proxy.dio_setup = failing_setup
    wrapper_cls = make_fake_wrapper(proxy)
    monkeypatch.setattr(m2kdriver, "AgentWrapper", wrapper_cls)

    gpio = NetworkM2KDigitalIO(target, name=None, channel=2, **USB)
    gpio.avail = True
    driver = M2KDigitalIODriver(target, name=None)

    with pytest.raises(RuntimeError, match="setup failed"):
        target.activate(driver)

    assert driver._proxy is None
    assert driver._key is None
    assert wrapper_cls.instances[0].closed is True
    assert m2kdriver._shared_agents == {}


def test_m2k_driver_releases_agent_after_reenumeration(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    wrapper_cls = make_fake_wrapper(proxy)
    monkeypatch.setattr(m2kdriver, "AgentWrapper", wrapper_cls)

    gpio = NetworkM2KDigitalIO(target, name=None, channel=0, **USB)
    gpio.avail = True
    driver = M2KDigitalIODriver(target, name=None)
    target.activate(driver)
    assert ("exporter", 1, 7) in m2kdriver._shared_agents

    # the device re-enumerates while the driver is active
    gpio.devnum = 8
    target.deactivate(driver)
    assert wrapper_cls.instances[0].closed is True
    assert m2kdriver._shared_agents == {}


def test_m2k_drivers_share_agent_per_device(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    wrapper_cls = make_fake_wrapper(proxy)
    monkeypatch.setattr(m2kdriver, "AgentWrapper", wrapper_cls)

    gpio = NetworkM2KDigitalIO(target, name="reset", channel=0, **USB)
    gpio.avail = True
    supply = NetworkM2KPowerSupply(target, name="vcc", channel=0, voltage=3.3, **USB)
    supply.avail = True
    gpio_driver = M2KDigitalIODriver(target, name="reset")
    supply_driver = M2KPowerSupplyDriver(target, name="vcc")

    target.activate(gpio_driver)
    target.activate(supply_driver)
    assert len(wrapper_cls.instances) == 1
    assert m2kdriver._shared_agents[("exporter", 1, 7)]["refs"] == 2

    target.deactivate(gpio_driver)
    assert wrapper_cls.instances[0].closed is False
    target.deactivate(supply_driver)
    assert wrapper_cls.instances[0].closed is True
    assert m2kdriver._shared_agents == {}


def test_m2k_driver_release_ignores_unknown_agent():
    m2kdriver._release_agent(("exporter", 1, 7))
    m2kdriver._release_agent(None)


def test_m2k_power_driver(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    monkeypatch.setattr(m2kdriver, "AgentWrapper", make_fake_wrapper(proxy))
    monkeypatch.setattr(m2kdriver.time, "sleep", lambda _: None)

    supply = NetworkM2KPowerSupply(target, name=None, channel=0, voltage=3.3, **USB)
    supply.avail = True
    driver = M2KPowerSupplyDriver(target, name=None, delay=0.5)
    target.activate(driver)

    assert driver.get() is False

    driver.on()
    assert proxy.calls[-1] == ("supply_set", 1, 7, 0, 3.3, True)
    assert driver.get() is True
    assert driver.get_voltage() == 3.3

    # changing the setpoint only pushes the value, the enable state is left alone
    driver.set_voltage(5)
    assert proxy.calls[-1] == ("supply_push", 1, 7, 0, 5.0)
    assert driver.get_voltage() == 5.0

    with pytest.raises(ValueError, match="0..5 V"):
        driver.set_voltage(5.5)
    assert driver.get_voltage() == 5.0

    driver.off()
    assert proxy.calls[-1] == ("supply_enable", 1, 7, 0, False)
    assert driver.get() is False

    # ... so a setpoint change while off does not switch the output on
    driver.set_voltage(1.8)
    assert proxy.calls[-1] == ("supply_push", 1, 7, 0, 1.8)
    assert driver.get() is False
    driver.on()
    assert proxy.calls[-1] == ("supply_set", 1, 7, 0, 1.8, True)

    driver.cycle()
    assert [c[0] for c in proxy.calls[-2:]] == ["supply_enable", "supply_set"]
    driver.reset()
    assert [c[0] for c in proxy.calls[-2:]] == ["supply_enable", "supply_set"]

    target.deactivate(driver)
    assert m2kdriver._shared_agents == {}


def test_m2k_analog_driver(target, remote_managers, monkeypatch):
    proxy = FakeProxy()
    monkeypatch.setattr(m2kdriver, "AgentWrapper", make_fake_wrapper(proxy))

    analog = NetworkM2KAnalogIn(target, name=None, channel=1, range="low", samplerate=10000, samples=4, **USB)
    analog.avail = True
    driver = M2KAnalogInDriver(target, name=None)
    assert isinstance(driver, AnalogInputProtocol)

    target.activate(driver)
    assert proxy.calls == [("analog_setup", 1, 7, 1, "low", 10000)]

    assert driver.get_voltage() == 1.25
    assert proxy.calls[-1] == ("analog_read", 1, 7, 1, 4)

    assert driver.get_samples() == [1.0, 1.5, 1.0, 1.5]
    assert proxy.calls[-1] == ("analog_samples", 1, 7, 1, 4)
    assert driver.get_samples(2) == [1.0, 1.5]
    assert proxy.calls[-1] == ("analog_samples", 1, 7, 1, 2)

    target.deactivate(driver)
    assert m2kdriver._shared_agents == {}


def test_m2k_analog_driver_setup_failure_releases_agent(target, remote_managers, monkeypatch):
    proxy = FakeProxy()

    def failing_setup(*args):
        raise RuntimeError("no calibration")

    proxy.analog_setup = failing_setup
    wrapper_cls = make_fake_wrapper(proxy)
    monkeypatch.setattr(m2kdriver, "AgentWrapper", wrapper_cls)

    analog = NetworkM2KAnalogIn(target, name=None, **USB)
    analog.avail = True
    driver = M2KAnalogInDriver(target, name=None)

    with pytest.raises(RuntimeError, match="no calibration"):
        target.activate(driver)
    assert driver._proxy is None
    assert wrapper_cls.instances[0].closed is True
    assert m2kdriver._shared_agents == {}


# agent


class FakeDigital:
    def __init__(self):
        self.direction = {}
        self.values = {}

    def setDirection(self, channel, direction):
        self.direction[channel] = direction

    def getDirection(self, channel):
        return self.direction.get(channel, 0)

    def setValueRaw(self, channel, level):
        self.values[channel] = level

    def getValueRaw(self, channel):
        return self.values.get(channel, 0)


class FakeSupply:
    def __init__(self):
        self.voltage = {}
        self.enabled = {}

    def pushChannel(self, channel, voltage):
        self.voltage[channel] = voltage

    def enableChannel(self, channel, enable):
        self.enabled[channel] = enable

    def readChannel(self, channel):
        return self.voltage.get(channel, 0.0) if self.enabled.get(channel) else 0.0


class FakeAnalogIn:
    def __init__(self):
        self.enabled = {}
        self.range = {}
        self.samplerate = None
        self.kernel_buffers = None
        self.data = [[0.0], [0.0]]
        self.busy_failures = 0
        self.get_calls = 0

    def enableChannel(self, channel, enable):
        self.enabled[channel] = enable

    def setRange(self, channel, range_):
        self.range[channel] = range_

    def setSampleRate(self, samplerate):
        self.samplerate = samplerate

    def setKernelBuffersCount(self, count):
        self.kernel_buffers = count

    def getSamples(self, samples):
        self.get_calls += 1
        if self.busy_failures:
            self.busy_failures -= 1
            raise RuntimeError("Unable to refill buffer: Device or resource busy")
        return [chn[:samples] for chn in self.data]


class FakeContext:
    def __init__(self, context_calibration=True, calibrated=False, serial="104473aa00bb00cc", adc_result=True):
        self.digital = FakeDigital()
        self.supply = FakeSupply()
        self.analog = FakeAnalogIn()
        self.context_calibration = context_calibration
        self.calibrated = calibrated
        self.calibrations = []
        self.serial = serial
        self.adc_result = adc_result
        self.closed = None  # deinit flag passed to contextClose

    def isCalibrated(self):
        return self.calibrated

    def getDigital(self):
        return self.digital

    def getPowerSupply(self):
        return self.supply

    def getAnalogIn(self):
        return self.analog

    def hasContextCalibration(self):
        return self.context_calibration

    def calibrateFromContext(self):
        self.calibrations.append("context")
        if self.context_calibration == "broken":
            raise ValueError("Calibration from context is unavailable")
        self.calibrated = True
        return 0.0

    def calibrateADC(self):
        self.calibrations.append("adc")
        self.calibrated = self.adc_result
        return self.adc_result

    def getSerialNumber(self):
        return self.serial

    def getFirmwareVersion(self):
        return "v0.33"


class FakeLibm2k:
    DIO_INPUT = 0
    DIO_OUTPUT = 1
    LOW = 0
    HIGH = 1
    PLUS_MINUS_25V = 0
    PLUS_MINUS_2_5V = 1

    def __init__(self, uris, context, contexts=None):
        self.uris = uris
        self.context = context
        # optional per-URI contexts (None means the open fails)
        self.contexts = contexts
        self.opened = []

    def getAllContexts(self):
        return list(self.uris)

    def m2kOpen(self, uri):
        self.opened.append(uri)
        if self.contexts is not None:
            return self.contexts.get(uri)
        return self.context

    def contextClose(self, ctx, deinit=True):
        ctx.closed = deinit


@pytest.fixture(autouse=True)
def no_sysfs_device(monkeypatch):
    """By default no sysfs entry is found, so the agent uses the USB backend."""
    monkeypatch.setattr(m2k, "_sysfs_device", lambda busnum, devnum: None)


def test_m2k_agent(monkeypatch):
    context = FakeContext()
    fake = FakeLibm2k(["ip:192.168.2.1", "usb:1.17.5", "usb:1.7.5"], context)
    monkeypatch.setattr(m2k, "libm2k", fake)

    assert m2k.handle_info(1, 7) == {"uri": "usb:1.7.5", "serial": "104473aa00bb00cc", "firmware": "v0.33"}
    assert fake.opened == ["usb:1.7.5"]

    m2k.handle_dio_setup(1, 7, 2, True)
    assert context.digital.direction[2] == FakeLibm2k.DIO_OUTPUT
    m2k.handle_dio_set(1, 7, 2, True)
    assert context.digital.values[2] == FakeLibm2k.HIGH
    assert m2k.handle_dio_get(1, 7, 2) is True
    m2k.handle_dio_set(1, 7, 2, False)
    assert m2k.handle_dio_get(1, 7, 2) is False

    m2k.handle_dio_setup(1, 7, 3, False)
    assert context.digital.direction[3] == FakeLibm2k.DIO_INPUT
    with pytest.raises(ValueError, match="configured as input"):
        m2k.handle_dio_set(1, 7, 3, True)
    context.digital.values[3] = FakeLibm2k.HIGH
    assert m2k.handle_dio_get(1, 7, 3) is True

    m2k.handle_supply_set(1, 7, 0, 3.3, True)
    assert context.supply.voltage[0] == 3.3
    assert context.supply.enabled[0] is True
    assert m2k.handle_supply_read(1, 7, 0) == 3.3
    m2k.handle_supply_enable(1, 7, 0, False)
    assert m2k.handle_supply_read(1, 7, 0) == 0.0

    # the context is opened once per device and reused
    assert fake.opened == ["usb:1.7.5"]
    assert list(m2k._devices) == [(1, 7)]

    with pytest.raises(ValueError, match="DIO channel"):
        m2k.handle_dio_get(1, 7, 16)
    with pytest.raises(ValueError, match="power supply channel"):
        m2k.handle_supply_read(1, 7, 2)


def test_m2k_agent_analog(monkeypatch):
    context = FakeContext()
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    context.analog.data = [[0.5, 0.5, 0.5, 0.5], [1.0, 2.0, 3.0, 4.0]]

    # digital and supply access must not trigger any calibration
    m2k.handle_dio_get(1, 7, 0)
    m2k.handle_supply_read(1, 7, 0)
    assert context.calibrations == []

    m2k.handle_analog_setup(1, 7, 1, "low", 100000)
    assert context.calibrations == ["context"]
    assert context.analog.kernel_buffers == 1
    assert context.analog.enabled == {0: True, 1: True}
    assert context.analog.range == {1: FakeLibm2k.PLUS_MINUS_2_5V}
    assert context.analog.samplerate == 100000.0

    m2k.handle_analog_setup(1, 7, 0, "high", 100000)
    assert context.calibrations == ["context"]  # calibrated once per device
    assert context.analog.range[0] == FakeLibm2k.PLUS_MINUS_25V

    assert m2k.handle_analog_read(1, 7, 1, 4) == 2.5
    assert m2k.handle_analog_samples(1, 7, 1, 2) == [1.0, 2.0]
    assert m2k.handle_analog_read(1, 7, 0, 3) == 0.5

    with pytest.raises(ValueError, match="unknown ADALM2000 analog input range"):
        m2k.handle_analog_setup(1, 7, 0, "medium", 100000)
    with pytest.raises(ValueError, match="analog input channel"):
        m2k.handle_analog_read(1, 7, 2, 1)
    with pytest.raises(ValueError, match="at least one sample"):
        m2k.handle_analog_read(1, 7, 0, 0)


def test_m2k_agent_analog_falls_back_to_adc_calibration(monkeypatch):
    context = FakeContext(context_calibration=False)
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    m2k.handle_analog_setup(1, 7, 0, "high", 100000)
    assert context.calibrations == ["adc"]


def test_m2k_agent_analog_recovers_from_broken_context_calibration(monkeypatch, capsys):
    context = FakeContext(context_calibration="broken")
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    m2k.handle_analog_setup(1, 7, 0, "high", 100000)
    assert context.calibrations == ["context", "adc"]
    assert "calibrating ADC" in capsys.readouterr().err


def test_m2k_agent_analog_rejects_failed_adc_calibration(monkeypatch):
    context = FakeContext(context_calibration=False, adc_result=False)
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    with pytest.raises(RuntimeError, match="calibration failed"):
        m2k.handle_analog_setup(1, 7, 0, "high", 100000)


def test_m2k_agent_analog_rejects_conflicting_sample_rate(monkeypatch):
    context = FakeContext(calibrated=True)
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    m2k.handle_analog_setup(1, 7, 0, "high", 1000)
    with pytest.raises(ValueError, match="conflicts"):
        m2k.handle_analog_setup(1, 7, 1, "high", 1000000)
    assert context.analog.samplerate == 1000.0
    # the same channel may be reconfigured, and the other channel may use the same rate
    m2k.handle_analog_setup(1, 7, 0, "high", 10000)
    m2k.handle_analog_setup(1, 7, 1, "low", 10000)
    assert context.analog.samplerate == 10000.0
    # an argument error must not drop the device
    assert (1, 7) in m2k._devices


def test_m2k_agent_drops_device_on_errors(monkeypatch):
    contexts = [FakeContext(), FakeContext()]
    fake = FakeLibm2k(["usb:1.7.5"], None)
    fake.m2kOpen = lambda uri: (fake.opened.append(uri), contexts[len(fake.opened) - 1])[1]
    monkeypatch.setattr(m2k, "libm2k", fake)

    m2k.handle_dio_setup(1, 7, 0, True)
    assert (1, 7) in m2k._devices

    def broken(channel, level):
        raise ValueError("Module libm2k error: ERR: Runtime - Connection lost")

    contexts[0].digital.setValueRaw = broken
    with pytest.raises(ValueError, match="Connection lost"):
        m2k.handle_dio_set(1, 7, 0, True)
    # the stale device was closed without deinit and removed from the cache ...
    assert (1, 7) not in m2k._devices
    assert contexts[0].closed is False
    # ... so the next call reconnects transparently
    m2k.handle_dio_setup(1, 7, 0, True)
    m2k.handle_dio_set(1, 7, 0, True)
    assert fake.opened == ["usb:1.7.5", "usb:1.7.5"]
    assert contexts[1].digital.values[0] == FakeLibm2k.HIGH


def test_m2k_agent_analog_skips_calibration_on_calibrated_device(monkeypatch):
    # e.g. another client calibrated the device earlier since power-up
    context = FakeContext(calibrated=True)
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    m2k.handle_analog_setup(1, 7, 0, "high", 100000)
    assert context.calibrations == []
    assert context.analog.kernel_buffers == 1


def test_m2k_agent_analog_retries_busy_buffer(monkeypatch):
    context = FakeContext()
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], context))
    monkeypatch.setattr(m2k.time, "sleep", lambda _: None)
    context.analog.data = [[0.5], [1.0]]

    context.analog.busy_failures = 2
    assert m2k.handle_analog_read(1, 7, 0, 1) == 0.5
    assert context.analog.get_calls == 3

    context.analog.get_calls = 0
    context.analog.busy_failures = m2k.BUSY_RETRIES
    with pytest.raises(RuntimeError, match="busy"):
        m2k.handle_analog_read(1, 7, 0, 1)
    assert context.analog.get_calls == m2k.BUSY_RETRIES


def test_m2k_agent_prefers_gadget_network(monkeypatch):
    usb_ctx = FakeContext()
    net_ctx = FakeContext()
    fake = FakeLibm2k(["usb:1.7.5"], None, contexts={"usb:1.7.5": usb_ctx, "ip:192.168.2.1": net_ctx})
    monkeypatch.setattr(m2k, "libm2k", fake)
    monkeypatch.setattr(m2k, "_sysfs_device", lambda busnum, devnum: "/sys/bus/usb/devices/1-3.2")
    monkeypatch.setattr(m2k, "_read_sysfs", lambda path: "104473aa00bb00cc" if path.endswith("/serial") else None)
    monkeypatch.setattr(m2k, "_gadget_interfaces", lambda sysdev: ["enx00e022e08aa1"])
    import ipaddress

    monkeypatch.setattr(m2k, "_interface_network", lambda ifname: ipaddress.ip_network("192.168.2.0/24"))

    assert m2k.handle_info(1, 7)["uri"] == "ip:192.168.2.1"
    assert fake.opened == ["ip:192.168.2.1"]
    assert usb_ctx.closed is None and net_ctx.closed is None


def test_m2k_agent_falls_back_to_usb(monkeypatch, capsys):
    usb_ctx = FakeContext()
    other = FakeContext(serial="deadbeef")
    fake = FakeLibm2k(["usb:1.7.5"], None, contexts={"usb:1.7.5": usb_ctx, "ip:192.168.2.1": other})
    monkeypatch.setattr(m2k, "libm2k", fake)
    monkeypatch.setattr(m2k, "_sysfs_device", lambda busnum, devnum: "/sys/bus/usb/devices/1-3.2")
    monkeypatch.setattr(m2k, "_read_sysfs", lambda path: "104473aa00bb00cc" if path.endswith("/serial") else None)
    monkeypatch.setattr(m2k, "_gadget_interfaces", lambda sysdev: ["enxaaaa", "enxbbbb"])
    import ipaddress

    networks = {"enxaaaa": None, "enxbbbb": ipaddress.ip_network("192.168.2.0/24")}
    monkeypatch.setattr(m2k, "_interface_network", networks.get)

    # the network context belongs to another ADALM2000 (serial mismatch) and is
    # closed again without deinitializing (powering down) that other device
    assert m2k.handle_info(1, 7)["uri"] == "usb:1.7.5"
    assert fake.opened == ["ip:192.168.2.1", "usb:1.7.5"]
    assert other.closed is False
    assert "does not match" in capsys.readouterr().err


def test_m2k_agent_rejects_serial_mismatch_on_usb(monkeypatch):
    fake = FakeLibm2k(["usb:1.7.5"], FakeContext(serial="deadbeef"))
    monkeypatch.setattr(m2k, "libm2k", fake)
    monkeypatch.setattr(m2k, "_sysfs_device", lambda busnum, devnum: "/sys/bus/usb/devices/1-3.2")
    monkeypatch.setattr(m2k, "_read_sysfs", lambda path: "104473aa00bb00cc" if path.endswith("/serial") else None)
    monkeypatch.setattr(m2k, "_gadget_interfaces", lambda sysdev: [])

    with pytest.raises(ValueError, match="does not match"):
        m2k.handle_info(1, 7)


def test_m2k_agent_sysfs_helpers(tmp_path, monkeypatch):
    dev = tmp_path / "1-3.2"
    (dev / "1-3.2:1.0" / "net" / "enx00e022e08aa1").mkdir(parents=True)
    (dev / "busnum").write_text("1\n")
    (dev / "devnum").write_text("7\n")
    (dev / "serial").write_text("104473aa00bb00cc\n")
    (tmp_path / "usb1").mkdir()
    (tmp_path / "usb1" / "busnum").write_text("1\n")
    (tmp_path / "usb1" / "devnum").write_text("1\n")
    (tmp_path / "1-4").mkdir()  # hub port without busnum/devnum files
    monkeypatch.setattr(m2k, "SYSFS_USB_DEVICES", str(tmp_path))
    monkeypatch.undo()  # drop the autouse _sysfs_device stub for this test
    monkeypatch.setattr(m2k, "SYSFS_USB_DEVICES", str(tmp_path))

    assert m2k._sysfs_device(1, 7) == str(dev)
    assert m2k._sysfs_device(1, 9) is None
    assert m2k._gadget_interfaces(str(dev)) == ["enx00e022e08aa1"]
    assert m2k.M2K._sysfs_serial(1, 7) == "104473aa00bb00cc"
    assert m2k.M2K._sysfs_serial(1, 9) is None
    # an interface that does not exist has no network
    assert m2k._interface_network("labgrid-nonexistent0") is None


def test_m2k_agent_rejects_missing_device(monkeypatch):
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.17.5"], FakeContext()))
    with pytest.raises(ValueError, match="not found"):
        m2k.handle_dio_get(1, 7, 0)


def test_m2k_agent_reports_all_open_errors(monkeypatch):
    class RaisingLibm2k(FakeLibm2k):
        def m2kOpen(self, uri):
            self.opened.append(uri)
            raise RuntimeError("connection refused")

    fake = RaisingLibm2k(["usb:1.17.5"], None)
    monkeypatch.setattr(m2k, "libm2k", fake)
    monkeypatch.setattr(m2k, "_sysfs_device", lambda busnum, devnum: "/sys/bus/usb/devices/1-3.2")
    monkeypatch.setattr(m2k, "_read_sysfs", lambda path: None)
    monkeypatch.setattr(m2k, "_gadget_interfaces", lambda sysdev: ["enxaaaa"])
    import ipaddress

    monkeypatch.setattr(m2k, "_interface_network", lambda ifname: ipaddress.ip_network("192.168.2.0/24"))

    with pytest.raises(ValueError) as excinfo:
        m2k.handle_dio_get(1, 7, 0)
    # the network error and the USB scan failure are both reported, once each
    message = str(excinfo.value)
    assert message.count("ip:192.168.2.1: connection refused") == 1
    assert message.count("not found") == 1
    assert fake.opened == ["ip:192.168.2.1"]


def test_m2k_agent_rejects_failed_open(monkeypatch):
    monkeypatch.setattr(m2k, "libm2k", FakeLibm2k(["usb:1.7.5"], None))
    with pytest.raises(ValueError, match="usb:1.7.5: failed to open"):
        m2k.handle_dio_get(1, 7, 0)


def test_m2k_agent_requires_libm2k(monkeypatch):
    monkeypatch.setattr(m2k, "libm2k", None)
    with pytest.raises(RuntimeError, match="libm2k"):
        m2k.handle_dio_get(1, 7, 0)


# exporter


def test_m2k_export_params():
    # the export selects the extra parameters by the local resource's class name
    class M2KDigitalIO(types.SimpleNamespace):  # pylint: disable=redefined-outer-name
        pass

    class M2KPowerSupply(types.SimpleNamespace):  # pylint: disable=redefined-outer-name
        pass

    class M2KAnalogIn(types.SimpleNamespace):  # pylint: disable=redefined-outer-name
        pass

    usb = {"busnum": 1, "devnum": 7, "path": "1-3.2", "vendor_id": 0x0456, "model_id": 0xB672}

    export = object.__new__(USBM2KExport)
    export.host = "exporter"
    export.local = M2KDigitalIO(channel=2, direction="in", invert=True, **usb)
    assert export._get_params() == {"host": "exporter", "channel": 2, "direction": "in", "invert": True, **usb}

    export.local = M2KPowerSupply(channel=1, voltage=-5.0, **usb)
    assert export._get_params() == {"host": "exporter", "channel": 1, "voltage": -5.0, **usb}

    export.local = M2KAnalogIn(channel=0, range="low", samplerate=1000, samples=10, **usb)
    assert export._get_params() == {
        "host": "exporter",
        "channel": 0,
        "range": "low",
        "samplerate": 1000,
        "samples": 10,
        **usb,
    }


# client


def make_session(target, **args):
    session = object.__new__(ClientSession)
    session.args = types.SimpleNamespace(**args)
    session.get_acquired_place = lambda: types.SimpleNamespace(name="test")
    session._get_target = lambda place: target
    return session


def test_m2k_client_digital_io(target, remote_managers):
    driver = types.SimpleNamespace(set=MagicMock())
    session = make_session(target, action="high", name=None)
    session._get_driver_or_new = MagicMock(return_value=driver)

    NetworkM2KDigitalIO(target, name=None, channel=2, **USB)
    session.digital_io()

    session._get_driver_or_new.assert_called_once_with(target, "M2KDigitalIODriver", name=None)
    driver.set.assert_called_once_with(True)


def test_m2k_client_power(target, remote_managers, capsys):
    driver = types.SimpleNamespace(on=MagicMock(), get=MagicMock(return_value=True))
    session = make_session(target, action="on", delay=None, name="vcc")
    session._get_driver_or_new = MagicMock(return_value=driver)

    NetworkM2KPowerSupply(target, name="vcc", channel=0, voltage=3.3, **USB)
    session.power()
    session._get_driver_or_new.assert_called_once_with(target, "M2KPowerSupplyDriver", name="vcc")
    driver.on.assert_called_once_with()

    session.args.action = "get"
    session.power()
    assert "power vcc for place test is on" in capsys.readouterr().out
