import copy
import os
import attr

from ..exceptions import InvalidConfigError
from ..factory import target_factory
from .common import NetworkResource, ManagedResource, ResourceManager


def _normalize_ignore(entries, kind):
    """Normalize an ``ignore_resources``/``ignore_drivers`` list into selectors.

    Each entry may be either a class name string or a mapping with a required
    ``cls`` key and an optional ``name`` key. Returns a list of
    ``(cls, name_or_None)`` tuples.

    Args:
        entries: the raw ``ignore_*`` value from the local ``RemotePlace``
        kind (str): "resource" or "driver", used in error messages

    Raises:
        InvalidConfigError: if an entry has an unexpected type or unknown keys
    """
    selectors = []
    for entry in entries:
        if isinstance(entry, str):
            selectors.append((entry, None))
        elif isinstance(entry, dict):
            unknown = set(entry) - {"cls", "name"}
            if unknown:
                raise InvalidConfigError(
                    f"invalid ignore_{kind}s selector {dict(entry)}: unknown keys {sorted(unknown)}"
                )
            if "cls" not in entry:
                raise InvalidConfigError(
                    f"invalid ignore_{kind}s selector {dict(entry)}: missing 'cls' key"
                )
            selectors.append((entry["cls"], entry.get("name")))
        else:
            raise InvalidConfigError(
                f"invalid ignore_{kind}s selector {entry!r}: must be a class name or a mapping"
            )
    return selectors


def _is_ignored(cls, name, selectors):
    """Return True if the (cls, name) pair matches any selector.

    A selector without a name matches every instance of the class; a selector
    with a name matches only that specific named instance.
    """
    for sel_cls, sel_name in selectors:
        if sel_cls != cls:
            continue
        if sel_name is None or sel_name == name:
            return True
    return False


@attr.s(eq=False)
class RemotePlaceManager(ResourceManager):
    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.url = None
        self.loop = None
        self.session = None
        self.ready = None
        self.unmanaged_resources = []

    def _get_credentials(self):
        from ..remote.common import get_client_credentials

        tls = os.environ.get("LG_COORDINATOR_TLS") is not None
        cacert = None

        if self.env:
            config = self.env.config
            tls = config.get_option("coordinator_tls", tls)
            if isinstance(tls, str):
                tls = tls.strip().lower() == "true"
            else:
                tls = tls is True

            cacert = config.get_option("coordinator_cacert", "")
            if cacert:
                cacert = config.resolve_path(str(cacert))
            else:
                cacert = None

        return get_client_credentials(tls, cacert)

    def _start(self):
        if self.session:
            return

        from ..remote.client import start_session
        try:
            self.session = start_session(
                self.url,
                extra={'env': self.env},
                credentials=self._get_credentials(),
            )
        except ConnectionRefusedError as e:
            raise ConnectionRefusedError(f"Could not connect to coordinator {self.url}") \
                from e

        self.loop = self.session.loop

    def on_resource_added(self, resource):
        if not isinstance(resource, RemotePlace):
            # we only need to handle new remote places here
            return
        remote_place = resource
        # Use the config file from the first resources we see (they should all
        # be the same).
        if not self.session:
            self.env = remote_place.target.env
            self.url = os.environ.get("LG_COORDINATOR", "127.0.0.1:20408")
            if self.env:
                config = self.env.config
                self.url = config.get_option("coordinator_address", self.url)
            self._start()
        place = self.session.get_place(remote_place.name)  # pylint: disable=no-member
        resource_entries = self.session.get_target_resources(place)  # pylint: disable=no-member
        expanded = []
        for (resource_name, _), resource_entry in resource_entries.items():
            new = target_factory.make_resource(
                remote_place.target, resource_entry.cls, resource_name, resource_entry.args)
            new.parent = remote_place
            new.avail = resource_entry.avail
            new.extra = resource_entry.extra
            new._remote_entry = resource_entry
            if not isinstance(new, ManagedResource):
                self.unmanaged_resources.append(new)
            expanded.append(new)
        self.logger.debug("expanded remote resources for place %s: %s", remote_place.name, expanded)

        # augment the target with the place config served by the coordinator
        self._apply_place_config(remote_place, place)

        remote_place.avail = True
        remote_place.tags = copy.deepcopy(place.tags)

    def _apply_place_config(self, remote_place, place):
        """Instantiate resources and drivers from the coordinator-side place config.

        The config is centrally managed and effectively untrusted network input,
        so it is validated before use. Nested ``RemotePlace`` resources are always
        dropped to avoid recursion. Local ``ignore_resources``/``ignore_drivers``
        selectors on the ``RemotePlace`` suppress unwanted config entries so that
        explicit local configuration can win deterministically.
        """
        config = place.get_config()
        if not config:
            return

        # fail early and clearly on unknown classes / bad structure
        target_factory.validate_config(config)

        ignore_resources = _normalize_ignore(remote_place.ignore_resources, "resource")
        ignore_drivers = _normalize_ignore(remote_place.ignore_drivers, "driver")

        resources = []
        for item in target_factory._convert_to_named_list(config.get("resources", {})):
            cls, name = item["cls"], item["name"]
            if cls == "RemotePlace":
                # never recurse into another RemotePlace from a place config
                self.logger.debug("ignoring nested RemotePlace in config of place %s", place.name)
                continue
            if _is_ignored(cls, name, ignore_resources):
                self.logger.debug("ignoring config resource %s (name=%s) for place %s", cls, name, place.name)
                continue
            resources.append(item)

        drivers = []
        for item in target_factory._convert_to_named_list(config.get("drivers", {})):
            cls, name = item["cls"], item["name"]
            if _is_ignored(cls, name, ignore_drivers):
                self.logger.debug("ignoring config driver %s (name=%s) for place %s", cls, name, place.name)
                continue
            drivers.append(item)

        target_factory.make_resources_from_config(remote_place.target, resources)
        target_factory.make_drivers_from_config(remote_place.target, drivers)

        # expose target-level options for Target.get_option(); features are
        # intentionally not supported here (tags are available via RemotePlace)
        options = config.get("options", {})
        if not isinstance(options, dict):
            raise InvalidConfigError(
                f"'options' in config of place {place.name} must be a mapping, "
                f"got {type(options).__name__}"
            )
        remote_place.config_options = dict(options)

    def poll(self):
        import asyncio
        if not self.loop.is_running():
            self.loop.run_until_complete(asyncio.sleep(0.1))
        for resource in self.resources + self.unmanaged_resources:
            if isinstance(resource, RemotePlace):
                continue
            attrs = resource._remote_entry.args.copy()
            attrs['avail'] = resource._remote_entry.avail
            # TODO allow the resource to do the update itself?
            changes = []
            fields = attr.fields(resource.__class__)
            for k, v_new in attrs.items():
                # check for attr converters
                attrib = getattr(fields, k)
                if attrib.converter:
                    v_new = attrib.converter(v_new)
                v_old = getattr(resource, k)
                setattr(resource, k, v_new)
                if v_old != v_new:
                    changes.append((k, v_old, v_new))
            if changes:
                self.logger.debug("changed attributes for %s:", resource)
                for k, v_old, v_new in changes:
                    self.logger.debug("  %s: %s -> %s", k, v_old, v_new)


@target_factory.reg_resource
@attr.s(eq=False)
class RemotePlace(ManagedResource):
    manager_cls = RemotePlaceManager

    #: Suppress resources supplied by the coordinator-side place config. Each
    #: entry is either a class name (matching all instances of that class) or a
    #: mapping ``{cls: <class>, name: <name>}`` (matching a single named
    #: instance).
    ignore_resources = attr.ib(
        default=attr.Factory(list),
        validator=attr.validators.instance_of(list),
    )
    #: Suppress drivers supplied by the coordinator-side place config, using the
    #: same selector syntax as :attr:`ignore_resources`.
    ignore_drivers = attr.ib(
        default=attr.Factory(list),
        validator=attr.validators.instance_of(list),
    )

    def __attrs_post_init__(self):
        self.timeout = 10.0
        self.tags = {}
        # target-level options supplied by the coordinator-side place config,
        # populated by RemotePlaceManager once the place is expanded
        self.config_options = {}
        super().__attrs_post_init__()

@attr.s(eq=False)
class RemoteUSBResource(NetworkResource, ManagedResource):
    manager_cls = RemotePlaceManager

    busnum = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(int)))
    devnum = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(int)))
    path = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(str)))
    vendor_id = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(int)))
    model_id = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(int)))


@target_factory.reg_resource
@attr.s(eq=False)
class RemoteAndroidUSBFastboot(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkAndroidFastboot(RemoteAndroidUSBFastboot):
    def __attrs_post_init__(self):
        import warnings
        warnings.warn("NetworkAndroidFastboot is deprecated, use RemoteAndroidUSBFastboot instead",
                      DeprecationWarning)
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class RemoteAndroidNetFastboot(NetworkResource):
    address = attr.ib(validator=attr.validators.instance_of(str))
    port = attr.ib(default=5554, validator=attr.validators.instance_of(int))
    protocol = attr.ib(default="udp", validator=attr.validators.instance_of(str))


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkDFUDevice(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkIMXUSBLoader(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkMXSUSBLoader(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkRKUSBLoader(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkAlteraUSBBlaster(RemoteUSBResource):
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkSigrokUSBDevice(RemoteUSBResource):
    """The NetworkSigrokUSBDevice describes a remotely accessible sigrok USB device"""
    driver = attr.ib(
        default=None,
        validator=attr.validators.instance_of(str)
    )
    channels = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    channel_group = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkSigrokUSBSerialDevice(RemoteUSBResource):
    """The NetworkSigrokUSBSerialDevice describes a remotely accessible sigrok USB device"""
    driver = attr.ib(
        default=None,
        validator=attr.validators.instance_of(str)
    )
    channels = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    channel_group = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBMassStorage(RemoteUSBResource):
    """The NetworkUSBMassStorage describes a remotely accessible USB storage device"""
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBSDMuxDevice(RemoteUSBResource):
    """The NetworkUSBSDMuxDevice describes a remotely accessible USBSDMux device"""
    control_path = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBSDWireDevice(RemoteUSBResource):
    """The NetworkUSBSDWireDevice describes a remotely accessible USBSDWire device"""
    control_serial = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()

@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBSDWire3Device(RemoteUSBResource):
    """The NetworkUSBSDWire3Device describes a remotely accessible USBSDWire3 device"""
    control_serial = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()

@target_factory.reg_resource
@attr.s(eq=False)
class NetworkSiSPMPowerPort(RemoteUSBResource):
    """The NetworkSiSPMPowerPort describes a remotely accessible SiS-PM power port"""
    index = attr.ib(default=None, validator=attr.validators.instance_of(int))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBPowerPort(RemoteUSBResource):
    """The NetworkUSBPowerPort describes a remotely accessible USB hub port with power switching"""
    index = attr.ib(default=None, validator=attr.validators.instance_of(int))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBVideo(RemoteUSBResource):
    """The NetworkUSBVideo describes a remotely accessible USB video device"""
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBAudioInput(RemoteUSBResource):
    """The NetworkUSBAudioInput describes a remotely accessible USB audio input device"""
    index = attr.ib(default=0, validator=attr.validators.instance_of(int))
    alsa_name = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str))
    )
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBTMC(RemoteUSBResource):
    """The NetworkUSBTMC describes a remotely accessible USB TMC device"""
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBDebugger(RemoteUSBResource):
    """The NetworkUSBDebugger describes a remotely accessible USB JTAG/Debugger device"""
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkDeditecRelais8(RemoteUSBResource):
    """The NetworkDeditecRelais8 describes a remotely accessible USB relais port"""
    index = attr.ib(default=None, validator=attr.validators.instance_of(int))
    invert = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkHIDRelay(RemoteUSBResource):
    """The NetworkHIDRelay describes a remotely accessible USB relay port"""
    index = attr.ib(default=1, validator=attr.validators.instance_of(int))
    invert = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkFTDIGPIO(RemoteUSBResource):
    """The NetworkFTDIGPIO describes a remotely accessible FTDI GPIO line"""
    index = attr.ib(
        default=0,
        converter=int,
        validator=attr.validators.and_(
            attr.validators.instance_of(int),
            attr.validators.ge(0),
            attr.validators.le(7),
        ),
    )
    interface = attr.ib(
        default=1,
        converter=int,
        validator=attr.validators.and_(
            attr.validators.instance_of(int),
            attr.validators.ge(1),
        ),
    )
    invert = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()


@target_factory.reg_resource
@attr.s(eq=False)
class NetworkSysfsGPIO(NetworkResource, ManagedResource):
    manager_cls = RemotePlaceManager

    """The NetworkSysfsGPIO describes a remotely accessible gpio line"""
    index = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(int)))
    invert = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()

@attr.s(eq=False)
class NetworkLXAIOBusNode(ManagedResource):
    manager_cls = RemotePlaceManager

    host = attr.ib(validator=attr.validators.instance_of(str))
    node = attr.ib(validator=attr.validators.instance_of(str))

    def __attrs_post_init__(self):
        self.timeout = 30.0
        super().__attrs_post_init__()

@target_factory.reg_resource
@attr.s(eq=False)
class NetworkLXAIOBusPIO(NetworkLXAIOBusNode):
    pin = attr.ib(validator=attr.validators.instance_of(str))
    invert = attr.ib(default=False, validator=attr.validators.instance_of(bool))

@target_factory.reg_resource
@attr.s(eq=False)
class NetworkLXAUSBMux(RemoteUSBResource):
    """The NetworkLXAUSBMux describes a remotely accessible USBMux device"""
    def __attrs_post_init__(self):
        self.timeout = 10.0
        super().__attrs_post_init__()

@target_factory.reg_resource
@attr.s(eq=False)
class NetworkUSBFlashableDevice(RemoteUSBResource):
    devnode = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(str)))

@attr.s(eq=False)
class NetworkMQTTResource(ManagedResource):
    manager_cls = RemotePlaceManager

    host = attr.ib(validator=attr.validators.instance_of(str))
    avail_topic = attr.ib(validator=attr.validators.instance_of(str))

    def __attrs_post_init__(self):
        self.timeout = 30.0
        super().__attrs_post_init__()

@target_factory.reg_resource
@attr.s(eq=False)
class RemoteNetworkInterface(NetworkResource, ManagedResource):
    manager_cls = RemotePlaceManager

    ifname = attr.ib(default=None)

@attr.s(eq=False)
class RemoteBaseProvider(NetworkResource):
    internal = attr.ib(validator=attr.validators.instance_of(str))
    external = attr.ib(validator=attr.validators.instance_of(str))


@target_factory.reg_resource
@attr.s(eq=False)
class RemoteTFTPProvider(RemoteBaseProvider):
    pass


@target_factory.reg_resource
@attr.s(eq=False)
class RemoteNFSProvider(NetworkResource):
    pass


@target_factory.reg_resource
@attr.s(eq=False)
class RemoteHTTPProvider(RemoteBaseProvider):
    pass
