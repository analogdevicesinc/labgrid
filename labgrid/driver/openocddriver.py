import contextlib
import socket
import subprocess
from itertools import chain

import attr

from ..factory import target_factory
from ..protocol import BootstrapProtocol, DebugProtocol
from ..resource.common import NetworkResource
from ..step import step
from ..util.agentwrapper import AgentWrapper
from ..util.helper import processwrapper
from ..util.managedfile import ManagedFile
from ..util.ssh import sshmanager
from .common import Driver
from .exception import ExecutionError


@target_factory.reg_driver
@attr.s(eq=False)
class OpenOCDDriver(Driver, BootstrapProtocol, DebugProtocol):
    bindings = {
        "interface": {
            "AlteraUSBBlaster",
            "NetworkAlteraUSBBlaster",
            "USBDebugger",
            "NetworkUSBDebugger",
        },
    }

    config = attr.ib(
        default=attr.Factory(list), validator=attr.validators.optional(attr.validators.instance_of((str, list)))
    )
    search = attr.ib(
        default=attr.Factory(list), validator=attr.validators.optional(attr.validators.instance_of((str, list)))
    )
    image = attr.ib(default=None, validator=attr.validators.optional(attr.validators.instance_of(str)))
    interface_config = attr.ib(default=None, validator=attr.validators.optional(attr.validators.instance_of(str)))
    board_config = attr.ib(default=None, validator=attr.validators.optional(attr.validators.instance_of(str)))
    load_commands = attr.ib(default=None, validator=attr.validators.optional(attr.validators.instance_of(list)))
    debug_commands = attr.ib(default=None, validator=attr.validators.optional(attr.validators.instance_of(list)))
    debug_timeout = attr.ib(default=30, validator=attr.validators.instance_of(int))

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        # FIXME make sure we always have an environment or config
        if self.target.env:
            self.tool = self.target.env.config.get_tool("openocd")
            self.config = self.target.env.config.resolve_path_str_or_list(self.config)
            self.search = self.target.env.config.resolve_path_str_or_list(self.search)
        else:
            self.tool = "openocd"
            if isinstance(self.config, str):
                self.config = [self.config]
            if isinstance(self.search, str):
                self.search = [self.search]

        self._debug_wrapper = None
        self._debug_forwards = []

    def on_deactivate(self):
        self._stop_debug_server()

    def _get_usb_path_cmd(self):
        # OpenOCD supports "adapter usb location" since a1b308ab, released with 0.11.0-rc1
        return ["--command", f'adapter usb location "{self.interface.path}"']

    def _get_command(self, commands: list, *, ports=None):
        ports = ports or {}

        cmd = [self.tool]
        cmd += chain.from_iterable(("--search", path) for path in self.search)
        cmd += self._get_usb_path_cmd()
        for service in ("gdb", "telnet", "tcl"):
            cmd += ["--command", f"{service}_port {ports.get(service, 'disabled')}"]

        managed_configs = []
        for config in self.config:
            mconfig = ManagedFile(config, self.interface)
            mconfig.sync_to_resource()
            managed_configs.append(mconfig)

        if self.interface_config:
            cmd.append("--file")
            cmd.append(f"interface/{self.interface_config}")

        if self.board_config:
            cmd.append("--file")
            cmd.append(f"board/{self.board_config}")

        for mconfig in managed_configs:
            cmd.append("--file")
            cmd.append(mconfig.get_remote_path())

        cmd += chain.from_iterable(("--command", f"{command}") for command in commands)
        return cmd

    def _check_no_debug_server(self):
        if self.get_status():
            raise ExecutionError("OpenOCD cannot be used while its debug server is running")

    def _run_commands(self, commands: list):
        self._check_no_debug_server()
        cmd = self._get_command(commands)
        processwrapper.check_output(self.interface.wrap_command(cmd), print_on_silent_log=True)

    @Driver.check_active
    @step(args=["filename"])
    def load(self, filename=None):
        self._check_no_debug_server()
        if filename is None and self.image is not None:
            filename = self.target.env.config.get_image_path(self.image)
        mf = ManagedFile(filename, self.interface)
        mf.sync_to_resource()

        if self.load_commands is None:
            commands = [
                "init",
                f"bootstrap {mf.get_remote_path()}",
                "shutdown",
            ]
        else:
            commands = [c.format(filename=mf.get_remote_path()) for c in self.load_commands]

        self._run_commands(commands)

    @Driver.check_active
    @step(args=["commands"])
    def execute(self, commands: list):
        self._run_commands(commands)

    def _forward_debug_port(self, port, local_port):
        if not isinstance(self.interface, NetworkResource):
            return port

        if local_port is not None:
            # ssh forwards even if only ::1 is free
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", local_port))
                except OSError as e:
                    raise ExecutionError(f"local port {local_port} for the debug server is not available: {e}") from e

        host = self.interface.host
        extra = getattr(self.interface, "extra", {})
        if extra.get("proxy_required"):
            host = extra.get("proxy")
        connection = sshmanager.get(host)
        try:
            local_port = connection.add_port_forward("localhost", port, local_port)
        except subprocess.SubprocessError as e:
            raise ExecutionError(f"failed to forward debug server port {port} on {host}: {e}") from e
        self._debug_forwards.append((connection, port))
        return local_port

    def _stop_debug_server(self):
        # the SSH connection and the agent may be gone already
        for connection, port in self._debug_forwards:
            with contextlib.suppress(Exception):
                connection.remove_port_forward("localhost", port)
        self._debug_forwards = []

        if self._debug_wrapper is not None:
            with contextlib.suppress(Exception):
                for line in self._debug_wrapper.load("openocd").stop():
                    self.logger.info("%s", line)
                self._debug_wrapper.close()
            self._debug_wrapper = None

    @Driver.check_active
    @step(args=["services"], result=True)
    def start(self, services=None):
        """
        Start OpenOCD with those of its "gdb", "telnet" and "tcl" servers that
        are named in services, see DebugProtocol. OpenOCD keeps the debug
        adapter busy until stop() is called or the driver is deactivated.
        """
        if services is None:
            services = {"gdb": None}
        if not isinstance(services, dict) or not services or not set(services) <= {"gdb", "telnet", "tcl"}:
            raise ValueError("services must be a dict with keys of gdb, telnet, tcl")
        for port in services.values():
            if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
                raise ValueError(f"invalid port {port!r}")
        if self.get_status():
            raise ExecutionError("OpenOCD debug server is already running")
        self._stop_debug_server()

        remote = isinstance(self.interface, NetworkResource)
        cmd = self._get_command(
            self.debug_commands or [],
            ports={service: 0 if remote or port is None else port for service, port in services.items()},
        )
        try:
            self._debug_wrapper = AgentWrapper(self.interface.host if remote else None)
            result = self._debug_wrapper.load("openocd").start(cmd, list(services), self.debug_timeout)
            if "error" in result:
                output = result["output"]
                raise ExecutionError("\n".join([result["error"]] + output[-10:]), stderr=output)
            for line in result["output"]:
                self.logger.info("%s", line)
            return {
                service: [
                    self._forward_debug_port(port, local_port + index if local_port else None)
                    for index, port in enumerate(result["ports"][service])
                ]
                for service, local_port in services.items()
            }
        except BaseException:
            self._stop_debug_server()
            raise

    @Driver.check_active
    @step()
    def stop(self):
        self._stop_debug_server()

    @Driver.check_active
    def get_status(self):
        if self._debug_wrapper is None or self._debug_wrapper.agent.poll() is not None:
            return False
        return self._debug_wrapper.load("openocd").get_status()
