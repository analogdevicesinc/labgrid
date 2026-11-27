import abc


class AnalogInputProtocol(abc.ABC):
    """Abstract class providing the AnalogInputProtocol interface"""

    @abc.abstractmethod
    def get_voltage(self):
        """Implementations should return the voltage measured at the analog input in volts."""
        raise NotImplementedError
