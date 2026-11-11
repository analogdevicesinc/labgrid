"""
This module runs OpenOCD as a debug server. OpenOCD is stopped when the agent
exits, so it does not outlive the connection to the host of the debug adapter.
"""

import atexit
import re
import subprocess
import tempfile
import time

READY = "labgrid: debug server ready"
PORT = re.compile(r"Listening on port (\d+) for (\w+) connections")

server = None
log = None
log_position = 0


def read_log():
    """Returns the complete lines OpenOCD has written since the last call."""
    global log_position

    log.seek(log_position)
    data = log.read()
    data = data[: data.rfind(b"\n") + 1]
    log_position += len(data)
    return data.decode(errors="replace").splitlines()


def handle_start(command, services, timeout):
    """Starts OpenOCD and returns its output with the ports of the services or an error."""
    global server, log, log_position

    handle_stop()
    log = tempfile.TemporaryFile("a+b")  # noqa: SIM115
    log_position = 0
    command += ["--command", "init", "--command", f'echo "{READY}"']
    server = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)

    output = []
    deadline = time.monotonic() + timeout
    while True:
        exited = server.poll() is not None
        output += read_log()
        ports = {}
        for port, service in PORT.findall("\n".join(output)):
            ports.setdefault(service, []).append(int(port))
        if READY in output and all(service in ports for service in services):
            return {"ports": ports, "output": output}

        if exited:
            return {"error": "OpenOCD exited before its debug server was ready", "output": output}
        if time.monotonic() > deadline:
            handle_stop()
            return {"error": f"OpenOCD debug server not ready after {timeout}s", "output": output}
        time.sleep(0.05)


def handle_stop():
    """Stops OpenOCD and returns its remaining output."""
    global server

    if server is None:
        return []
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    server = None
    output = read_log()
    log.close()
    return output


def handle_get_status():
    return server is not None and server.poll() is None


atexit.register(handle_stop)

methods = {
    "start": handle_start,
    "stop": handle_stop,
    "get_status": handle_get_status,
}
