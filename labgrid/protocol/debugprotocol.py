import abc


class DebugProtocol(abc.ABC):
    """Abstract class for drivers which run a debug server for the target"""

    @abc.abstractmethod
    def start(self, services=None):
        """
        Start the debug server.

        Args:
            services (dict): optional, service names with the local TCP port
                to listen on or None for a free port, {"gdb": None} by default

        Returns:
            dict of the service names with their local TCP ports, as a list
            (a GDB server has a port for each core)
        """
        raise NotImplementedError

    @abc.abstractmethod
    def stop(self):
        """Stop the debug server. Does nothing if it is not running."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_status(self):
        """Returns True while the debug server is running."""
        raise NotImplementedError
