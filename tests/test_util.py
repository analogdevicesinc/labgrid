import os.path
import re
import subprocess
import socket
import atexit
import warnings

from shutil import which

import attr
import pytest
import logging

from labgrid.util import diff_dict, flat_dict, filter_dict
from labgrid.util.helper import get_free_port
from labgrid.util.ssh import ForwardError, SSHConnection, SSHConnectionManager, sshmanager
from labgrid.util.proxy import proxymanager
from labgrid.util.managedfile import ManagedFile, ManagedFileError
from labgrid.driver.exception import ExecutionError
from labgrid.resource.serialport import NetworkSerialPort
from labgrid.resource.common import Resource, NetworkResource
from labgrid.util import diff_dict, flat_dict, filter_dict, find_dict

@pytest.fixture
def connection_localhost():
    con = SSHConnection("localhost")
    con.connect()
    yield con
    con.disconnect()

@pytest.fixture
def connection_localhost_no_cleanup():
    con = SSHConnection("localhost")
    con.connect()
    return con

@pytest.fixture
def sshmanager_fix():
    yield sshmanager
    sshmanager.close_all()

def test_diff_dict():
    dict_a = {"a": 1,
              "b": 2}
    dict_b = {"a": 1,
              "b": 3}
    gen = diff_dict(dict_a, dict_b)
    for res in gen:
        assert res[0] == 'b'
        assert res[1] == 2
        assert res[2] == 3

def test_flat_dict():
    dict_a = {"a":
              {"b": 3},
              "b": 2}
    res = flat_dict(dict_a)
    assert res == {"a.b": 3, "b": 2}

def test_filter_dict():
    @attr.s
    class A:
        foo = attr.ib()

    d_orig = {'foo': 1, 'bar': 2, 'baz': 3}

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        d_filtered = filter_dict(d_orig, A)
    assert d_filtered is not d_orig
    assert d_filtered == {'foo': 1}

    with pytest.warns(UserWarning) as record:
        d_filtered = filter_dict(d_orig, A, warn=True)
    assert len(record) == 2
    assert str(record[0].message) == "unsupported attribute 'bar' with value '2' for class 'A'"
    assert str(record[1].message) == "unsupported attribute 'baz' with value '3' for class 'A'"
    assert d_filtered is not d_orig
    assert d_filtered == {'foo': 1}

def test_sshmanager_get():
    assert sshmanager != None

def test_sshconnection_get():
    from labgrid.util.ssh import SSHConnection
    SSHConnection("localhost")

@pytest.mark.skipif(not which("ssh"), reason="ssh not available")
def test_sshconnection_inactive_raise():
    from labgrid.util.ssh import SSHConnection
    con = SSHConnection("localhost")
    with pytest.raises(ExecutionError):
        con.run_check("echo Hallo")

@pytest.mark.localsshmanager
def test_sshconnection_connect(connection_localhost):
    assert connection_localhost.isconnected()
    assert os.path.exists(connection_localhost._socket)

@pytest.mark.localsshmanager
def test_sshconnection_run(connection_localhost):
    stdout, stderr, exitcode = connection_localhost.run("echo stderr >&2; echo stdout1; echo stdout2")
    assert exitcode == 0
    assert stderr == ["stderr"]
    assert stdout == ["stdout1", "stdout2"]

@pytest.mark.localsshmanager
def test_sshconnection_run_log(connection_localhost, caplog):
    caplog.set_level(logging.INFO)
    stdout, stderr, exitcode = connection_localhost.run("echo stderr >&2; echo stdout1; echo stdout2",
            stdout_loglevel=logging.INFO, stderr_loglevel=logging.WARNING)
    assert exitcode == 0
    assert stderr == ["stderr"]
    assert stdout == ["stdout1", "stdout2"]
    assert sorted([(rec[1], rec[2]) for rec in caplog.record_tuples]) == [
        (logging.INFO, 'stdout1'),
        (logging.INFO, 'stdout2'),
        (logging.WARNING, 'stderr'),
   ]

@pytest.mark.localsshmanager
def test_sshconnection_run_merged_stderr(connection_localhost):
    stdout, stderr, exitcode = connection_localhost.run(
            "echo stderr >&2; echo stdout", stderr_merge=True)
    assert exitcode == 0
    assert sorted(stdout) == ["stderr", "stdout"]
    assert stderr == []

@pytest.mark.localsshmanager
def test_sshconnection_run_fail(connection_localhost):
    stdout, stderr, exitcode = connection_localhost.run("false")
    assert exitcode != 0


@pytest.mark.localsshmanager
def test_sshconnection_port_forward_add_remove(connection_localhost):
    port = get_free_port()
    test_string = "Hello World"

    local_port = connection_localhost.add_port_forward('localhost', port)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", port))
    server_socket.listen(1)
    send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    send_socket.connect(("127.0.0.1", local_port))
    client_socket, address = server_socket.accept()
    send_socket.send(test_string.encode('utf-8'))

    assert client_socket.recv(16).decode("utf-8") == test_string
    connection_localhost.remove_port_forward('localhost', port)

@pytest.mark.localsshmanager
def test_sshconnection_port_forward_remove_raise(connection_localhost):
    port = get_free_port()

    with pytest.raises(ForwardError):
        connection_localhost.remove_port_forward('localhost', port)

@pytest.mark.localsshmanager
def test_sshconnection_port_forward_add_duplicate(connection_localhost):
    port = get_free_port()

    first_port = connection_localhost.add_port_forward('localhost', port)
    second_port = connection_localhost.add_port_forward('localhost', port)
    assert first_port == second_port


@pytest.mark.localsshmanager
def test_sshconnection_port_remote_forward_add_remove(connection_localhost):
    rport = get_free_port()
    lport = get_free_port()
    test_string = "Hello World"

    connection_localhost.add_remote_port_forward(rport, lport)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", lport))
    server_socket.listen(1)
    send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    send_socket.connect(("127.0.0.1", rport))
    client_socket, address = server_socket.accept()
    send_socket.send(test_string.encode('utf-8'))

    assert client_socket.recv(16).decode("utf-8") == test_string
    connection_localhost.remove_remote_port_forward(rport, lport)
    assert connection_localhost._r_forwards == set()


@pytest.mark.localsshmanager
def test_sshconnection_put_file(connection_localhost, tmpdir):
    p = tmpdir.join("config.yaml")
    p.write(
        """Teststring"""
    )
    connection_localhost.put_file(p, '/tmp/test')

    assert os.path.isfile('/tmp/test')
    assert open('/tmp/test', 'r').readlines() == [ "Teststring" ]

@pytest.mark.localsshmanager
def test_sshconnection_get_file(connection_localhost, tmpdir):

    p = tmpdir.join("test")
    connection_localhost.get_file('/tmp/test', p)

@pytest.mark.localsshmanager
def test_sshconnection_cleanup(connection_localhost_no_cleanup):

    connection_localhost_no_cleanup.cleanup()

    # do not call cleanup() again on termination
    atexit.unregister(connection_localhost_no_cleanup.cleanup)

@pytest.mark.localsshmanager
def test_sshmanager_open(sshmanager_fix):
    con = sshmanager_fix.open("localhost")

    assert isinstance(con, SSHConnection)

@pytest.mark.localsshmanager
def test_sshmanager_add_forward(sshmanager_fix):
    port = sshmanager_fix.request_forward("localhost", 'localhost', 3000)

    assert port < 65536

@pytest.mark.localsshmanager
def test_sshmanager_remove_forward(sshmanager_fix):
    port = sshmanager_fix.request_forward("localhost", 'localhost', 3000)
    sshmanager_fix.remove_forward('localhost', 'localhost', 3000)

    assert 3000 not in sshmanager_fix.get('localhost')._l_forwards

@pytest.mark.localsshmanager
def test_sshmanager_close(sshmanager_fix):
    con = sshmanager_fix.open("localhost")

    assert isinstance(con, SSHConnection)
    sshmanager_fix.close("localhost")

@pytest.mark.localsshmanager
def test_sshmanager_remove_raise(sshmanager_fix):
    con = sshmanager_fix.open("localhost")
    con.connect()
    with pytest.raises(ExecutionError):
        sshmanager_fix.remove_connection(con)

@pytest.mark.skipif(not which("ssh"), reason="ssh not available")
def test_sshmanager_add_duplicate(sshmanager_fix):
    host = 'localhost'
    con = SSHConnection(host)
    sshmanager_fix.add_connection(con)
    con_there = sshmanager_fix._connections[host]
    sshmanager_fix.add_connection(con)
    con_now = sshmanager_fix._connections[host]

    assert con_now == con_there

@pytest.mark.skipif(not which("ssh"), reason="ssh not available")
def test_sshmanager_add_new(sshmanager_fix):
    host = 'other_host'
    con = SSHConnection(host)
    sshmanager_fix.add_connection(con)
    con_now = sshmanager_fix._connections[host]

    assert con_now == con

@pytest.mark.skipif(not which("ssh"), reason="ssh not available")
def test_sshmanager_invalid_host_raise():
    con = SSHConnection("nosuchhost.notavailable")
    with pytest.raises(ExecutionError):
        con.connect()

class FakeSSHProcess:
    def __init__(self, args, returncode=0, stdout=b"", alive=False, hang=False, error=None):
        self.args = args
        self.returncode = None if alive else returncode
        self._stdout = stdout
        self._hang = hang
        self._error = error

    def communicate(self, input=None, timeout=None):
        if self._error is not None:
            # e.g. the user interrupts while waiting for the process
            error, self._error = self._error, None
            raise error
        if self._hang and timeout is not None:
            # the process stays in the foreground until it is killed
            raise subprocess.TimeoutExpired(self.args, timeout)
        return (self._stdout, b"")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeSSHSubprocess:
    """Stand-in for the subprocess module used by labgrid.util.ssh.

    Emulates the ssh processes started by SSHConnection: the check for an
    external master (fails), the own control master, the keepalive and
    control socket commands."""
    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT
    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError
    SubprocessError = subprocess.SubprocessError

    def __init__(self):
        self.calls = []
        self.master_returncode = 0
        self.master_error = None
        self.keepalive_error = None
        self.popen_error = None
        self.keepalives = []
        # the master was killed, control commands fail and its socket is stale
        self.master_dead = False

    @staticmethod
    def _control_path(args):
        for arg in args:
            if arg.startswith("ControlPath="):
                return arg.split("=", 1)[1]
        return None

    def Popen(self, args, **kwargs):
        self.calls.append(args)
        if self.popen_error is not None:
            raise self.popen_error
        if "-O" in args and "check" in args:
            # no external master available
            return FakeSSHProcess(args, returncode=1, stdout=b"No such file or directory")
        if "-MN" in args:
            if self.master_error is not None:
                error, self.master_error = self.master_error, None
                return FakeSSHProcess(args, alive=True, error=error)
            if self.master_returncode == 0:
                control = self._control_path(args)
                if os.path.exists(control):
                    # like OpenSSH: "ControlSocket ... already exists,
                    # disabling multiplexing", the process does not return
                    return FakeSSHProcess(args, hang=True)
                open(control, "w").close()
            return FakeSSHProcess(args, returncode=self.master_returncode)
        if args[-1] == "cat":
            if self.keepalive_error is not None:
                raise self.keepalive_error
            keepalive = FakeSSHProcess(args, alive=True)
            self.keepalives.append(keepalive)
            return keepalive
        return FakeSSHProcess(args)

    def check_call(self, args, **kwargs):
        self.calls.append(args)
        if self.master_dead:
            raise subprocess.CalledProcessError(255, args)
        if "-O" in args and "exit" in args:
            # the master removes its socket when it exits
            control = self._control_path(args)
            if control and os.path.exists(control):
                os.unlink(control)
        return 0


@pytest.fixture
def fake_ssh_subprocess(monkeypatch):
    import labgrid.util.ssh as ssh_module

    fake = FakeSSHSubprocess()
    monkeypatch.setattr(ssh_module, "subprocess", fake)
    return fake


@pytest.fixture
def fake_connection(fake_ssh_subprocess):
    con = SSHConnection("fakehost")
    # don't try to clean up with the real subprocess module on exit
    atexit.unregister(con.cleanup)
    yield con
    # disconnect and remove the temporary directory
    con.cleanup()

def test_sshconnection_cleanup_idempotent(fake_connection):
    assert os.path.isdir(fake_connection._tmpdir)
    fake_connection.cleanup()
    assert not os.path.isdir(fake_connection._tmpdir)
    # a second call (e.g. via atexit) must not raise
    fake_connection.cleanup()

def test_sshconnection_connect_disconnect_mocked(fake_connection):
    fake_connection.connect()
    assert fake_connection.isconnected()
    assert fake_connection._socket is not None
    fake_connection.disconnect()
    assert not fake_connection.isconnected()
    assert fake_connection._master is None
    assert fake_connection._keepalive is None

def test_sshconnection_master_failure_allows_retry(fake_connection, fake_ssh_subprocess):
    fake_ssh_subprocess.master_returncode = 255
    with pytest.raises(ExecutionError):
        fake_connection.connect()
    assert fake_connection._master is None
    assert not fake_connection._connected

    # a retry must fail the same way, not with an AssertionError
    with pytest.raises(ExecutionError):
        fake_connection.connect()

    fake_ssh_subprocess.master_returncode = 0
    fake_connection.connect()
    assert fake_connection.isconnected()

def test_sshconnection_master_interrupt_allows_retry(fake_connection, fake_ssh_subprocess):
    # the user interrupts while waiting for the master to connect
    fake_ssh_subprocess.master_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        fake_connection.connect()
    assert fake_connection._master is None
    assert not fake_connection._connected

    fake_connection.connect()
    assert fake_connection.isconnected()

def test_sshconnection_keepalive_failure_allows_retry(fake_connection, fake_ssh_subprocess):
    fake_ssh_subprocess.keepalive_error = OSError("keepalive failed")
    with pytest.raises(OSError):
        fake_connection.connect()
    assert fake_connection._master is None
    assert fake_connection._socket is None
    assert not fake_connection._connected

    fake_ssh_subprocess.keepalive_error = None
    fake_connection.connect()
    assert fake_connection.isconnected()

def test_sshconnection_run_popen_error(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()

    fake_ssh_subprocess.popen_error = FileNotFoundError("ssh")
    with pytest.raises(ExecutionError) as excinfo:
        fake_connection.run("true")
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)

    # only OSError must be turned into an ExecutionError
    fake_ssh_subprocess.popen_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        fake_connection.run("true")

    fake_ssh_subprocess.popen_error = None

def test_proxymanager_no_proxy(target):
    host = 'localhost'
    port = 5000
    nr = NetworkSerialPort(target, host=host, port=port, name=None)
    nr.proxy = ''
    nr.proxy_required = False

    assert (host, port) == proxymanager.get_host_and_port(nr)

@pytest.mark.localsshmanager
def test_proxymanager_remote_forced_proxy(target):
    sshmanager.close_all()
    host = 'localhost'
    port = 5000
    nr = NetworkSerialPort(target, host=host, port=port, name=None)
    extras = { 'proxy': 'localhost', 'proxy_required': True }
    nr.extra = extras
    nhost, nport = proxymanager.get_host_and_port(nr)

    assert (host, port) != (nhost, nport)
    sshmanager.close_all()

@pytest.mark.localsshmanager
def test_proxymanager_local_forced_proxy(target):
    host = 'localhost'
    port = 5000
    nr = NetworkSerialPort(target, host=host, port=port, name=None)
    extras = { 'proxy': 'localhost', 'proxy_required': True }
    nr.extra = extras
    nhost, nport = proxymanager.get_host_and_port(nr)

    assert (host, port) != (nhost, nport)

@pytest.mark.localsshmanager
def test_remote_managedfile(target, tmpdir):
    import hashlib
    import getpass

    res = NetworkResource(target, "test", "localhost")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    hash = hashlib.sha256(t.read().encode("utf-8")).hexdigest()
    mf = ManagedFile(t, res, detect_nfs=False)
    mf.sync_to_resource()

    assert os.path.isfile(f"/var/cache/labgrid/{getpass.getuser()}/{hash}/test")
    assert hash == mf.get_hash()
    assert f"/var/cache/labgrid/{getpass.getuser()}/{hash}/test" == mf.get_remote_path()

@pytest.mark.localsshmanager
def test_remote_managedfile_on_nfs(target, tmpdir):
    res = NetworkResource(target, "test", "localhost")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    mf = ManagedFile(t, res, detect_nfs=True)
    mf.sync_to_resource()

    assert str(t) == mf.get_remote_path()

def test_local_managedfile(target, tmpdir):
    import hashlib

    res = Resource(target, "test")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    hash = hashlib.sha256(t.read().encode("utf-8")).hexdigest()
    mf = ManagedFile(t, res, detect_nfs=False)
    mf.sync_to_resource()

    assert hash == mf.get_hash()
    assert str(t) == mf.get_remote_path()

def test_network_managedfile_no_sync(target, tmpdir):
    res = NetworkResource(target, "test", "localhost")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    mf = ManagedFile(t, res, detect_nfs=False)

    expected = re.escape("sync_to_resource() needs to be called before the remote-path can be retrieved")
    with pytest.raises(ManagedFileError, match=expected) as e:
        mf.get_remote_path()

def test_local_managedfile_symlink(target, tmpdir):
    res = Resource(target, "test")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    mf = ManagedFile(t, res, detect_nfs=False)
    mf.sync_to_resource(symlink=tmpdir.join("link"))

    assert str(t) == mf.get_remote_path()
    assert os.path.islink(tmpdir.join("link"))

@pytest.mark.localsshmanager
def test_remote_managedfile_symlink(target, tmpdir):
    res = NetworkResource(target, "test", "localhost")
    t = tmpdir.join("test")
    t.write(
"""
Test
"""
    )
    mf = ManagedFile(t, res, detect_nfs=False)
    mf.sync_to_resource(symlink=tmpdir.join("link"))

    assert os.path.islink(tmpdir.join("link"))

def test_find_dict():
    dict_a = {"a": {"a.a": {"a.a.a": "a.a.a_val"}}, "b": "b_val"}
    assert find_dict(dict_a, "b") == "b_val"
    assert find_dict(dict_a, "a") == {"a.a": {"a.a.a": "a.a.a_val"}}
    assert find_dict(dict_a, "a.a") == {"a.a.a": "a.a.a_val"}
    assert find_dict(dict_a, "a.a.a") == "a.a.a_val"
    assert find_dict(dict_a, "x") == None


def test_sshconnection_reconnect_dead_keepalive(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()
    assert fake_connection.isconnected()
    fake_connection.add_port_forward("localhost", 22)
    assert fake_connection._l_forwards

    # the keepalive process dies, e.g. because of a network outage
    fake_ssh_subprocess.keepalives[0].returncode = 255
    assert not fake_connection.isconnected()

    fake_connection.connect()
    assert fake_connection.isconnected()
    assert len(fake_ssh_subprocess.keepalives) == 2
    assert fake_connection._keepalive is fake_ssh_subprocess.keepalives[1]
    # a new master was started and stale forwards were dropped
    assert len([c for c in fake_ssh_subprocess.calls if "-MN" in c]) == 2
    assert not fake_connection._l_forwards
    assert fake_connection.get_prefix()[0] == "ssh"

def test_sshconnection_disconnect_dead_keepalive(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()
    fake_ssh_subprocess.keepalives[0].returncode = 255
    assert not fake_connection.isconnected()

    # disconnecting must still be possible to tear down the master
    fake_connection.disconnect()
    assert not fake_connection._connected
    assert fake_connection._master is None
    assert fake_connection._keepalive is None

    with pytest.raises(ExecutionError):
        fake_connection.disconnect()

def test_sshconnection_disconnect_dead_master(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()
    fake_connection.add_port_forward("localhost", 22)
    socket_path = fake_connection._socket

    # the master was killed (e.g. by the OOM killer), the keepalive dies with
    # it and the control socket is left behind
    fake_ssh_subprocess.keepalives[0].returncode = 255
    fake_ssh_subprocess.master_dead = True
    assert not fake_connection.isconnected()

    # disconnecting must not raise and must remove the stale socket
    fake_connection.disconnect()
    assert not fake_connection._connected
    assert fake_connection._master is None
    assert fake_connection._keepalive is None
    assert not fake_connection._l_forwards
    assert not os.path.exists(socket_path)

    # a new master can be started right away
    fake_ssh_subprocess.master_dead = False
    fake_connection.connect()
    assert fake_connection.isconnected()
    assert len([c for c in fake_ssh_subprocess.calls if "-MN" in c]) == 2

def test_sshconnection_disconnect_dead_master_live_keepalive(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()
    socket_path = fake_connection._socket

    # the master died, but the keepalive did not notice yet
    fake_ssh_subprocess.master_dead = True
    assert fake_connection.isconnected()

    fake_connection.disconnect()
    assert not fake_connection._connected
    assert fake_connection._master is None
    assert not os.path.exists(socket_path)

    fake_ssh_subprocess.master_dead = False
    fake_connection.connect()
    assert fake_connection.isconnected()

def test_sshconnection_cleanup_dead_master(fake_connection, fake_ssh_subprocess):
    fake_connection.connect()
    fake_connection.add_port_forward("localhost", 22)
    socket_path = fake_connection._socket

    # the master died, the keepalive is still running, so cancelling the
    # forwards fails
    fake_ssh_subprocess.master_dead = True
    assert fake_connection.isconnected()

    # cleanup() runs at exit and must not raise
    fake_connection.cleanup()
    assert not fake_connection._connected
    assert not os.path.exists(socket_path)
    assert not os.path.isdir(fake_connection._tmpdir)

def test_sshmanager_close_all_continues_after_failure(fake_ssh_subprocess, monkeypatch):
    # use a private manager, close_all() on the global one would also close
    # the real connections of other tests with the fake subprocess module
    manager = SSHConnectionManager()
    atexit.unregister(manager.close_all)
    con_a = manager.get("fakehost")
    con_b = manager.get("fakehost2")
    atexit.unregister(con_a.cleanup)
    atexit.unregister(con_b.cleanup)
    try:
        def fail():
            raise RuntimeError("broken")
        monkeypatch.setattr(con_a, "disconnect", fail)

        manager.close_all()
        # the second connection must be closed although the first one failed
        assert not con_b._connected
        assert not manager._connections
    finally:
        monkeypatch.undo()
        con_a.cleanup()
        con_b.cleanup()

def test_sshconnection_stale_socket_blocks_master(fake_connection, fake_ssh_subprocess):
    # sanity check of the fake: a leftover socket must make the next master
    # attempt fail like with OpenSSH, so the tests above are meaningful
    open(os.path.join(fake_connection._tmpdir, "control-fakehost"), "w").close()
    with pytest.raises(ExecutionError, match="timeout"):
        fake_connection.connect()

def test_sshmanager_get_reconnects_dead_keepalive(fake_ssh_subprocess):
    con = sshmanager.get("fakehost")
    atexit.unregister(con.cleanup)
    try:
        assert con.isconnected()
        fake_ssh_subprocess.keepalives[0].returncode = 255
        assert not con.isconnected()

        assert sshmanager.get("fakehost") is con
        assert con.isconnected()
        assert len(fake_ssh_subprocess.keepalives) == 2
    finally:
        con.cleanup()
        sshmanager.remove_by_name("fakehost")

