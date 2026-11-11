import pytest
from shutil import which
import socket
import subprocess

from labgrid.driver.exception import ExecutionError
from labgrid.resource.remote import NetworkUSBDebugger
from labgrid.resource.udev import USBDebugger
from labgrid.driver.openocddriver import OpenOCDDriver
from labgrid.util.helper import get_free_port


pytestmark = pytest.mark.skipif(not which("openocd"), reason="openocd not available")


def test_openocd_resource(target):
    r = USBDebugger(target, name=None, match={"sys_name": "1-12"})


def test_openocd_driver_activate(target):
    r = USBDebugger(target, name=None, match={"sys_name": "1-12"})
    r.avail = True
    d = OpenOCDDriver(target, name=None)
    target.activate(d)


def test_openocd_driver(target, tmpdir):
    r = USBDebugger(target, name=None, match={"sys_name": "1-12"})
    r.avail = True
    d = OpenOCDDriver(target, name=None, load_commands=["shutdown"])
    target.activate(d)
    d.load(__file__)

    with pytest.raises(subprocess.CalledProcessError):
        d.execute(["invalid_command_labgrid"])

    d.execute(["shutdown"])


@pytest.fixture
def debug_driver(target, tmp_path):
    """An OpenOCDDriver for an adapter that needs no hardware."""
    config = tmp_path / "dummy.cfg"
    config.write_text("adapter driver dummy\n")
    r = USBDebugger(target, name=None, match={"sys_name": "1-12"})
    r.avail = True
    d = OpenOCDDriver(target, name=None, config=str(config))
    target.activate(d)
    yield d
    target.deactivate(d)


@pytest.fixture
def agent(mocker):
    """Stands in for the agent which runs OpenOCD."""
    wrapper = mocker.patch("labgrid.driver.openocddriver.AgentWrapper").return_value
    wrapper.agent.poll.return_value = None
    agent = wrapper.load.return_value
    agent.start.return_value = {"ports": {"gdb": [4000, 4001]}, "output": []}
    agent.stop.return_value = []
    agent.get_status.return_value = True
    return agent


def connect(port):
    socket.create_connection(("127.0.0.1", port), timeout=5).close()


def test_openocd_debug_server(debug_driver):
    ports = debug_driver.start({"telnet": None, "tcl": None})

    assert list(ports) == ["telnet", "tcl"]
    connect(ports["telnet"][0])
    connect(ports["tcl"][0])
    assert debug_driver.get_status()

    debug_driver.stop()

    assert not debug_driver.get_status()
    with pytest.raises(OSError):
        connect(ports["telnet"][0])


def test_openocd_debug_server_port(debug_driver):
    port = get_free_port()

    assert debug_driver.start({"telnet": port}) == {"telnet": [port]}
    connect(port)


def test_openocd_debug_server_blocks_bootstrap(debug_driver):
    debug_driver.start({"telnet": None})

    with pytest.raises(ExecutionError):
        debug_driver.load(__file__)
    with pytest.raises(ExecutionError):
        debug_driver.execute(["shutdown"])
    with pytest.raises(ExecutionError):
        debug_driver.start({"telnet": None})

    debug_driver.stop()
    debug_driver.execute(["shutdown"])


def test_openocd_debug_server_stopped_on_deactivate(target, debug_driver):
    port = debug_driver.start({"telnet": None})["telnet"][0]

    target.deactivate(debug_driver)

    with pytest.raises(OSError):
        connect(port)
    target.activate(debug_driver)


def test_openocd_debug_server_fails(debug_driver):
    debug_driver.debug_commands = ["invalid_command_labgrid"]

    with pytest.raises(ExecutionError, match="invalid_command_labgrid"):
        debug_driver.start({"telnet": None})

    assert not debug_driver.get_status()


@pytest.mark.parametrize("services", [{}, {"rtt": None}, {"gdb": "3333"}, ["gdb"]])
def test_openocd_debug_server_invalid_services(debug_driver, services):
    with pytest.raises(ValueError):
        debug_driver.start(services)


def test_openocd_debug_server_gdb(debug_driver, agent):
    assert debug_driver.start() == {"gdb": [4000, 4001]}

    command, services, timeout = agent.start.call_args.args
    assert {"gdb_port 0", "telnet_port disabled", "tcl_port disabled"} <= set(command)
    assert (services, timeout) == (["gdb"], 30)


def test_openocd_debug_server_remote(target, agent, mocker):
    r = NetworkUSBDebugger(target, name=None, host="exporter", busnum=1, devnum=2, path="1-2", vendor_id=1, model_id=2)
    mocker.patch.object(r.manager, "poll")
    r.avail = True
    wrapper = mocker.patch("labgrid.driver.openocddriver.AgentWrapper")
    wrapper.return_value.load.return_value = agent
    ssh = mocker.patch("labgrid.driver.openocddriver.sshmanager.get").return_value
    ssh.add_port_forward.side_effect = lambda host, port, local_port: local_port
    agent.start.return_value = {"ports": {"gdb": [4000]}, "output": []}
    d = OpenOCDDriver(target, name=None)
    target.activate(d)
    port = get_free_port()

    assert d.start({"gdb": port}) == {"gdb": [port]}

    # OpenOCD uses a free port on the exporter, the given port is the local one
    wrapper.assert_called_once_with("exporter")
    assert "gdb_port 0" in agent.start.call_args.args[0]
    ssh.add_port_forward.assert_called_once_with("localhost", 4000, port)

    d.stop()

    ssh.remove_port_forward.assert_called_once_with("localhost", 4000)
    wrapper.return_value.close.assert_called_once()
