"""Tests for coordinator-side place config augmenting a RemotePlace target.

These tests exercise:
- Place.get_config() YAML parsing and validation (common.py)
- RemotePlaceManager augmenting a target from the place config
- local vs remote precedence, ignore selectors, nested RemotePlace handling
- Target.get_option() merging local and remote options
"""
from unittest.mock import Mock, patch

import pytest

from labgrid import Target
from labgrid.exceptions import InvalidConfigError
from labgrid.factory import target_factory
from labgrid.remote.common import Place
from labgrid.resource.remote import (
    RemotePlace,
    RemotePlaceManager,
    _normalize_ignore,
    _is_ignored,
)


# ---------------------------------------------------------------------------
# Place.get_config()
# ---------------------------------------------------------------------------

def test_get_config_empty():
    assert Place(name="p", config=None).get_config() == {}
    assert Place(name="p", config="").get_config() == {}
    # YAML that parses to None (e.g. only comments)
    assert Place(name="p", config="# just a comment\n").get_config() == {}


def test_get_config_valid():
    place = Place(name="p", config="resources:\n- NetworkService: {address: host}\n")
    config = place.get_config()
    assert "resources" in config


def test_get_config_malformed_yaml():
    place = Place(name="p", config="resources: [unclosed\n")
    with pytest.raises(InvalidConfigError) as err:
        place.get_config()
    assert "p" in str(err.value)
    assert "YAML" in str(err.value)


def test_get_config_wrong_root_type():
    place = Place(name="p", config="- just\n- a\n- list\n")
    with pytest.raises(InvalidConfigError) as err:
        place.get_config()
    assert "mapping" in str(err.value)
    assert "p" in str(err.value)


# ---------------------------------------------------------------------------
# ignore selector helpers
# ---------------------------------------------------------------------------

def test_normalize_ignore_string():
    assert _normalize_ignore(["NetworkService"], "resource") == [("NetworkService", None)]


def test_normalize_ignore_mapping():
    assert _normalize_ignore(
        [{"cls": "NetworkService", "name": "dut"}], "resource"
    ) == [("NetworkService", "dut")]


def test_normalize_ignore_mapping_without_name():
    assert _normalize_ignore([{"cls": "SSHDriver"}], "driver") == [("SSHDriver", None)]


def test_normalize_ignore_invalid_type():
    with pytest.raises(InvalidConfigError, match="must be a class name or a mapping"):
        _normalize_ignore([123], "resource")


def test_normalize_ignore_missing_cls():
    with pytest.raises(InvalidConfigError, match="missing 'cls'"):
        _normalize_ignore([{"name": "dut"}], "resource")


def test_normalize_ignore_unknown_keys():
    with pytest.raises(InvalidConfigError, match="unknown keys"):
        _normalize_ignore([{"cls": "NetworkService", "typo": "x"}], "resource")


def test_is_ignored_class_matches_all_names():
    selectors = [("NetworkService", None)]
    assert _is_ignored("NetworkService", "dut", selectors)
    assert _is_ignored("NetworkService", "other", selectors)
    assert not _is_ignored("NetworkSerialPort", "dut", selectors)


def test_is_ignored_named_selector():
    selectors = [("NetworkService", "dut")]
    assert _is_ignored("NetworkService", "dut", selectors)
    assert not _is_ignored("NetworkService", "other", selectors)


# ---------------------------------------------------------------------------
# RemotePlaceManager augmentation
# ---------------------------------------------------------------------------

@pytest.fixture
def target():
    return Target("test-target")


@pytest.fixture(autouse=True)
def reset_manager():
    if RemotePlaceManager in RemotePlaceManager.instances:
        del RemotePlaceManager.instances[RemotePlaceManager]
    yield
    if RemotePlaceManager in RemotePlaceManager.instances:
        del RemotePlaceManager.instances[RemotePlaceManager]


def make_place(config=None, tags=None):
    """Create a real Place with the given config YAML."""
    return Place(name="test-place", tags=tags or {}, config=config)


def setup_manager(place, resource_entries=None):
    manager = RemotePlaceManager.get()
    manager.session = Mock()
    manager.loop = Mock()
    manager.session.get_place = Mock(return_value=place)
    manager.session.get_target_resources = Mock(return_value=resource_entries or {})
    return manager


def instantiate(target, manager, **remote_place_kwargs):
    with patch.object(manager, "_start"):
        return RemotePlace(target, "test-place", **remote_place_kwargs)


def test_remote_config_only(target):
    place = make_place(
        config="resources:\n- NetworkService: {address: remote-host}\ndrivers:\n- SSHDriver: {}\n"
    )
    manager = setup_manager(place)
    instantiate(target, manager)

    resource_classes = [r.__class__.__name__ for r in target.resources]
    driver_classes = [d.__class__.__name__ for d in target.drivers]
    assert resource_classes == ["RemotePlace", "NetworkService"]
    assert driver_classes == ["SSHDriver"]
    assert target.get_resource("NetworkService", wait_avail=False).address == "remote-host"


def test_empty_config(target):
    place = make_place(config=None)
    manager = setup_manager(place)
    instantiate(target, manager)
    assert [r.__class__.__name__ for r in target.resources] == ["RemotePlace"]
    assert target.drivers == []


def test_local_and_remote_coexist(target):
    # local resource added before RemotePlace expansion
    target_factory.make_resources_from_config(
        target, [{"NetworkSerialPort": {"host": "local-host", "port": 4000}}]
    )
    place = make_place(config="resources:\n- NetworkService: {address: remote-host}\n")
    manager = setup_manager(place)
    instantiate(target, manager)

    resource_classes = sorted(r.__class__.__name__ for r in target.resources)
    assert resource_classes == ["NetworkSerialPort", "NetworkService", "RemotePlace"]


def test_ignore_remote_resource_by_class(target):
    place = make_place(
        config="resources:\n- NetworkService: {address: remote-host}\n"
        "- NetworkSerialPort: {host: remote-host, port: 4000}\n"
    )
    manager = setup_manager(place)
    instantiate(target, manager, ignore_resources=["NetworkService"])

    resource_classes = [r.__class__.__name__ for r in target.resources]
    assert "NetworkService" not in resource_classes
    assert "NetworkSerialPort" in resource_classes


def test_ignore_remote_driver_by_class(target):
    place = make_place(config="drivers:\n- SSHDriver: {}\n")
    manager = setup_manager(place)
    instantiate(target, manager, ignore_drivers=["SSHDriver"])
    assert target.drivers == []


def test_ignore_named_resource_keeps_other_instances(target):
    place = make_place(
        config="resources:\n"
        "- NetworkService: {name: dut, address: dut-host}\n"
        "- NetworkService: {name: aux, address: aux-host}\n"
    )
    manager = setup_manager(place)
    instantiate(target, manager, ignore_resources=[{"cls": "NetworkService", "name": "dut"}])

    names = sorted(
        r.name for r in target.resources if r.__class__.__name__ == "NetworkService"
    )
    assert names == ["aux"]


def test_invalid_ignore_selector_raises(target):
    place = make_place(config="resources:\n- NetworkService: {address: h}\n")
    manager = setup_manager(place)
    with pytest.raises(InvalidConfigError):
        instantiate(target, manager, ignore_resources=[{"name": "dut"}])


def test_nested_remote_place_is_dropped(target):
    place = make_place(
        config="resources:\n"
        "- RemotePlace: {name: nested}\n"
        "- NetworkService: {address: remote-host}\n"
    )
    manager = setup_manager(place)
    instantiate(target, manager)

    # only the original RemotePlace, no nested one
    remote_places = [r for r in target.resources if r.__class__.__name__ == "RemotePlace"]
    assert len(remote_places) == 1
    assert "NetworkService" in [r.__class__.__name__ for r in target.resources]


def test_unknown_resource_class_raises(target):
    place = make_place(config="resources:\n- NoSuchResource: {}\n")
    manager = setup_manager(place)
    with pytest.raises(InvalidConfigError, match="unknown resource class"):
        instantiate(target, manager)


def test_unknown_driver_class_raises(target):
    place = make_place(config="drivers:\n- NoSuchDriver: {}\n")
    manager = setup_manager(place)
    with pytest.raises(InvalidConfigError, match="unknown driver class"):
        instantiate(target, manager)


def test_config_options_populated(target):
    place = make_place(config="options:\n  board: example-board\n  baudrate: 115200\n")
    manager = setup_manager(place)
    remote_place = instantiate(target, manager)
    assert remote_place.config_options == {"board": "example-board", "baudrate": 115200}


def test_config_options_wrong_type_raises(target):
    place = make_place(config="options:\n- not-a-mapping\n")
    manager = setup_manager(place)
    with pytest.raises(InvalidConfigError, match="options"):
        instantiate(target, manager)


# ---------------------------------------------------------------------------
# Target.get_option()
# ---------------------------------------------------------------------------

def test_get_option_remote(target):
    place = make_place(config="options:\n  board: remote-board\n")
    manager = setup_manager(place)
    instantiate(target, manager)
    assert target.get_option("board") == "remote-board"


def test_get_option_default(target):
    place = make_place(config=None)
    manager = setup_manager(place)
    instantiate(target, manager)
    assert target.get_option("missing", "fallback") == "fallback"
    with pytest.raises(KeyError):
        target.get_option("missing")


class _FakeConfig:
    def __init__(self, targets):
        self._targets = targets

    def get_targets(self):
        return self._targets

    def _get_target_option(self, target, name, default=None):
        options = (self._targets.get(target) or {}).get("options", {})
        if name in options:
            return options[name]
        if default is None:
            raise KeyError(name)
        return default


class _FakeEnv:
    def __init__(self, targets):
        self.config = _FakeConfig(targets)


def test_get_option_local_wins_over_remote():
    targets = {"test-target": {"options": {"board": "local-board"}}}
    target = Target("test-target", env=_FakeEnv(targets))
    place = make_place(config="options:\n  board: remote-board\n")
    manager = setup_manager(place)
    instantiate(target, manager)
    assert target.get_option("board") == "local-board"


def test_get_option_raises_when_remote_place_declared_but_not_instantiated():
    targets = {
        "test-target": {
            "resources": {"RemotePlace": {"name": "test-place"}},
        }
    }
    target = Target("test-target", env=_FakeEnv(targets))
    # RemotePlace declared in config but never instantiated on the target
    with pytest.raises(KeyError, match="not been instantiated"):
        target.get_option("board")
