import pytest
import socket
import subprocess
import tempfile

from labgrid import Environment
from labgrid.binding import StateError
from labgrid.driver import SSHDriver, ExecutionError
from labgrid.exceptions import NoResourceFoundError
from labgrid.resource import NetworkService
from labgrid.util.helper import get_free_port

@pytest.fixture(scope='function')
def ssh_driver_mocked(target, mocker):
    NetworkService(target, "service", "1.2.3.4", "root")
    call = mocker.patch('subprocess.call')
    call.return_value = 0
    popen = mocker.patch('subprocess.Popen', autospec=True)
    path = mocker.patch('os.path.exists')
    path.return_value = True
    instance_mock = mocker.MagicMock()
    popen.return_value = instance_mock
    instance_mock.wait = mocker.MagicMock(return_value=0)
    instance_mock.communicate = mocker.MagicMock(return_value=(b"", b""))
    return SSHDriver(target, "ssh")

def test_create_fail_missing_resource(target):
    with pytest.raises(NoResourceFoundError):
        SSHDriver(target, "ssh")

def test_create(ssh_driver_mocked):
    assert isinstance(ssh_driver_mocked, SSHDriver)

def test_extra_options_str(target, ssh_driver_mocked):
    s = ssh_driver_mocked
    s.extra_options = "-o HostKeyAlgorithms=+ssh-rsa"
    target.activate(s)
    assert "HostKeyAlgorithms=+ssh-rsa" in s.ssh_prefix

def test_extra_options_list(target, ssh_driver_mocked):
    ssh_driver_mocked.extra_options = [
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "HostKeyAlgorithms=+ssh-dsa",
    ]
    target.activate(ssh_driver_mocked)
    assert "HostKeyAlgorithms=+ssh-rsa" in ssh_driver_mocked.ssh_prefix
    assert "HostKeyAlgorithms=+ssh-dsa" in ssh_driver_mocked.ssh_prefix

def test_extra_options_empty(target, ssh_driver_mocked):
    target.activate(ssh_driver_mocked)
    assert "" not in ssh_driver_mocked.ssh_prefix

def test_run_check(target, ssh_driver_mocked, mocker):
    s = ssh_driver_mocked
    target.activate(s)
    s._run = mocker.MagicMock(return_value=(['success'], [], 0))
    res = s.run_check("test")
    assert res == ['success']
    res = s.run("test")
    assert res == (['success'], [], 0)
    target.deactivate(s)

def test_run_check_raise(target, ssh_driver_mocked, mocker):
    s = ssh_driver_mocked
    target.activate(s)
    s._run = mocker.MagicMock(return_value=(['error'], [], 1))
    with pytest.raises(ExecutionError):
        res = s.run_check("test")
    res = s.run("test")
    assert res == (['error'], [], 1)
    target.deactivate(s)

def test_run_timeout(target, ssh_driver_mocked, mocker):
    s = ssh_driver_mocked
    target.activate(s)
    sub = subprocess.Popen.return_value
    sub.reset_mock()  # forget the calls made while starting the master
    sub.poll.return_value = None  # keepalive is running
    sub.communicate.side_effect = subprocess.TimeoutExpired("ssh", 1)
    with pytest.raises(ExecutionError):
        s.run("sleep 60", timeout=1)
    # the ssh child must be killed and reaped and its pipes closed
    sub.kill.assert_called_once()
    sub.wait.assert_called_once()
    sub.stdout.close.assert_called_once()
    sub.stderr.close.assert_called_once()
    sub.communicate.side_effect = None
    target.deactivate(s)

def test_run_stderr_without_trailing_newline(target, ssh_driver_mocked):
    s = ssh_driver_mocked
    target.activate(s)
    sub = subprocess.Popen.return_value
    sub.poll.return_value = None  # keepalive is running
    sub.communicate.return_value = (b"out\n", b"fatal")
    sub.returncode = 1
    assert s.run("test") == (["out"], ["fatal"], 1)
    sub.communicate.return_value = (b"out\n", b"fatal\n")
    assert s.run("test") == (["out"], ["fatal"], 1)
    sub.communicate.return_value = (b"", b"")
    sub.returncode = 0
    assert s.run("test") == ([], [], 0)
    target.deactivate(s)

def test_master_retry_cleans_up(target, ssh_driver_mocked, monkeypatch, tmp_path):
    # let mkdtemp() create the control directories below tmp_path
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    s = ssh_driver_mocked
    s.connection_timeout = 0.3
    sub = subprocess.Popen.return_value
    sub.wait.return_value = 255  # connection refused
    with pytest.raises(Exception, match="Timeout while waiting for ssh connection"):
        target.activate(s)
    # every failed attempt must remove its temporary directory again
    assert list(tmp_path.glob("lg-ssh-*")) == []
    # ConnectTimeout must never be rounded down to 0 (unbounded)
    assert subprocess.Popen.call_count >= 2
    for call in subprocess.Popen.call_args_list:
        args = call.args[0]
        assert "ConnectTimeout=0" not in args
        assert "ConnectTimeout=1" in args

def test_master_timeout_kills_process(target, ssh_driver_mocked, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    s = ssh_driver_mocked
    s.connection_timeout = 0.3
    sub = subprocess.Popen.return_value

    def wait(timeout=None):
        # ssh does not return in time, but is gone after kill()
        if timeout is not None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return -9

    sub.wait.side_effect = wait
    with pytest.raises(Exception, match="Timeout while waiting for ssh connection"):
        target.activate(s)
    # every attempt that timed out must kill its ssh process
    assert subprocess.Popen.call_count >= 1
    assert sub.kill.call_count == subprocess.Popen.call_count
    assert list(tmp_path.glob("lg-ssh-*")) == []

def test_master_interrupt_kills_process(target, ssh_driver_mocked, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    s = ssh_driver_mocked
    sub = subprocess.Popen.return_value

    def wait(timeout=None):
        # the user interrupts while waiting for ssh
        if timeout is not None:
            raise KeyboardInterrupt()
        return -9

    sub.wait.side_effect = wait
    with pytest.raises(KeyboardInterrupt):
        target.activate(s)
    # the ssh process must not be left behind and the interrupt must not be
    # turned into another exception
    assert subprocess.Popen.call_count == 1
    sub.kill.assert_called_once()
    assert list(tmp_path.glob("lg-ssh-*")) == []

def test_master_failure_removes_files(target, ssh_driver_mocked, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    s = ssh_driver_mocked
    s.password = "secret"
    subprocess.Popen.side_effect = FileNotFoundError("ssh")
    with pytest.raises(FileNotFoundError):
        target.activate(s)
    # neither the control directory nor the password file may be left behind
    assert list(tmp_path.iterdir()) == []

def test_password_keeps_environment(target, ssh_driver_mocked, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    s = ssh_driver_mocked
    s.password = "secret"
    target.activate(s)
    env = subprocess.Popen.call_args_list[0].kwargs["env"]
    # SSH_ASKPASS must be set up, but the inherited environment must be kept
    assert env["SSH_ASKPASS"]
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"
    target.deactivate(s)

def test_interact_requires_active(ssh_driver_mocked):
    with pytest.raises(StateError):
        ssh_driver_mocked.interact()

def test_ssh_version(ssh_driver_mocked, mocker):
    run = mocker.patch("subprocess.run")
    run.return_value.stderr = "OpenSSH_9.6p1 Ubuntu-3ubuntu13, OpenSSL 3.0.13 30 Jan 2024\n"
    assert ssh_driver_mocked._ssh_version == (9, 6)

def test_ssh_version_unparseable(ssh_driver_mocked, mocker):
    run = mocker.patch("subprocess.run")
    run.return_value.stderr = "ssh: command not found\n"
    with pytest.raises(ExecutionError):
        ssh_driver_mocked._ssh_version

def test_default_tools(target):
    NetworkService(target, "service", "1.2.3.4", "root")
    s = SSHDriver(target, "ssh")
    assert [s._ssh, s._scp, s._sshfs, s._rsync] == ["ssh", "scp", "sshfs", "rsync"]

def test_custom_tools(target, tmpdir):
    p = tmpdir.join("config.yaml")
    p.write(
        """
        tools:
          ssh: "/path/to/ssh"
          scp: "/path/to/scp"
          sshfs: "/path/to/sshfs"
          rsync: "/path/to/rsync"
        """
    )
    target.env = Environment(str(p))
    NetworkService(target, "service", "1.2.3.4", "root")
    s = SSHDriver(target, "ssh")
    assert [s._ssh, s._scp, s._sshfs, s._rsync] == [f"/path/to/{t}" for t in ("ssh", "scp", "sshfs", "rsync")]

@pytest.fixture(scope='function')
def ssh_localhost(target, pytestconfig):
    name = pytestconfig.getoption("--ssh-username")
    NetworkService(target, "service", "localhost", name)
    SSHDriver(target, "ssh")
    s = target.get_driver("SSHDriver")
    return s

@pytest.mark.sshusername
def test_local_put(ssh_localhost, tmpdir):
    p = tmpdir.join("config.yaml")
    p.write(
        """PUT Teststring"""
    )

    ssh_localhost.put(str(p), "/tmp/test_put_yaml")
    assert open('/tmp/test_put_yaml', 'r').readlines() == [ "PUT Teststring" ]

@pytest.mark.sshusername
def test_local_no_final_line_remove(ssh_localhost, tmpdir):
    p = tmpdir.join("test_no_line_remove")
    p.write("teststring")

    ssh_localhost.put(str(p), "/tmp/test_line_remove")
    stdout, stderr, _ = ssh_localhost.run("cat /tmp/test_line_remove")
    assert stdout == [ "teststring" ]

@pytest.mark.sshusername
def test_local_final_line_remove_empty(ssh_localhost, tmpdir):
    p = tmpdir.join("test_no_line_remove")
    p.write("teststring\n")

    ssh_localhost.put(str(p), "/tmp/test_line_remove")
    stdout, stderr, _ = ssh_localhost.run("cat /tmp/test_line_remove")
    assert stdout == [ "teststring" ]

@pytest.mark.sshusername
def test_local_put_dir(ssh_localhost, tmpdir):
    d = tmpdir.mkdir("test_put_dir");
    p = d.join("config.yaml")
    p.write(
        """PUT Teststring"""
    )

    ssh_localhost.put(str(d), "/tmp/test_put_dir")
    assert open('/tmp/test_put_dir/config.yaml', 'r').readlines() == [ "PUT Teststring" ]

@pytest.mark.sshusername
def test_local_get(ssh_localhost, tmpdir):
    p = tmpdir.join("config.yaml")
    p.write(
        """GET Teststring"""
    )

    ssh_localhost.get(str(p), "/tmp/test_get_yaml")
    assert open('/tmp/test_get_yaml', 'r').readlines() == [ "GET Teststring" ]

@pytest.mark.sshusername
def test_local_get_dir(ssh_localhost, tmpdir):
    d = tmpdir.mkdir("test_get_dir");
    p = d.join("config.yaml")
    p.write(
        """GET Teststring"""
    )

    ssh_localhost.get(str(d), "/tmp/test_get_dir")
    assert open('/tmp/test_get_dir/config.yaml', 'r').readlines() == [ "GET Teststring" ]

@pytest.mark.sshusername
def test_local_run(ssh_localhost, tmpdir):

    res = ssh_localhost.run("echo Hello")
    assert res == (["Hello"], [], 0)

@pytest.mark.sshusername
def test_local_run_check(ssh_localhost, tmpdir):

    res = ssh_localhost.run_check("echo Hello")
    assert res == (["Hello"])

@pytest.mark.sshusername
def test_local_port_forward(ssh_localhost, tmpdir):
    remoteport = get_free_port()
    test_string = "Hello World"

    with ssh_localhost.forward_local_port(remoteport) as localport:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as send_socket:
                server_socket.bind(("127.0.0.1", remoteport))
                server_socket.listen(1)

                send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                send_socket.connect(("127.0.0.1", localport))

                client_socket, address = server_socket.accept()
                send_socket.send(test_string.encode('utf-8'))

                assert client_socket.recv(16).decode("utf-8") == test_string

@pytest.mark.sshusername
def test_local_remote_forward(ssh_localhost, tmpdir):
    remoteport = get_free_port()
    localport = get_free_port()
    test_string = "Hello World"

    with ssh_localhost.forward_remote_port(remoteport, localport):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as send_socket:
                server_socket.bind(("127.0.0.1", localport))
                server_socket.listen(1)

                send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                send_socket.connect(("127.0.0.1", remoteport))

                client_socket, address = server_socket.accept()
                send_socket.send(test_string.encode('utf-8'))

                assert client_socket.recv(16).decode("utf-8") == test_string


@pytest.mark.sshusername
def test_unix_socket_forward(ssh_localhost, tmpdir):
    p = tmpdir.join("console.sock")
    test_string = "Hello World"

    with ssh_localhost.forward_unix_socket(str(p)) as localport:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server_socket:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as send_socket:
                server_socket.bind(str(p))
                server_socket.listen(1)

                send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                send_socket.connect(("127.0.0.1", localport))

                client_socket, address = server_socket.accept()
                send_socket.send(test_string.encode("utf-8"))

                assert client_socket.recv(16).decode("utf-8") == test_string
