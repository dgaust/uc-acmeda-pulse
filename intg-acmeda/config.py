"""
Persisted driver configuration.

Stores the hub's host/IP plus a cached list of its rollers (id + name +
whether it has a battery). The roller cache exists purely so that, on a plain
restart, entities can be registered *immediately* - before the (async) live
hub connection completes. The Remote re-subscribes to its remembered entities
the instant it reconnects to the driver, so if the entities aren't already
registered at that moment the subscribe fails with "entity is not available"
and everything shows up disconnected/unknown. Live data from the hub then
refreshes the state of these pre-registered entities.

:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
import os

_LOG = logging.getLogger(__name__)

_FILENAME = "config.json"

_config_path: str | None = None
_host: str | None = None
_rollers: list[dict] = []


def init(config_dir: str) -> None:
    """Set the configuration directory and load any existing configuration."""
    global _config_path, _host, _rollers
    _config_path = os.path.join(config_dir, _FILENAME)
    _LOG.info(
        "Config dir: %r (UC_CONFIG_HOME=%r, HOME=%r) -> config file: %s",
        config_dir,
        os.getenv("UC_CONFIG_HOME"),
        os.getenv("HOME"),
        _config_path,
    )
    _host, _rollers = _load()
    if _host:
        _LOG.info(
            "Loaded saved config: host=%s, %d cached roller(s)", _host, len(_rollers)
        )
    else:
        _LOG.info("No saved hub host found - setup is required")


def _load() -> tuple[str | None, list[dict]]:
    if not _config_path or not os.path.exists(_config_path):
        return None, []
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("host"), data.get("rollers", [])
    except (OSError, ValueError) as ex:
        _LOG.error("Cannot read config file %s: %s", _config_path, ex)
        return None, []


def get_host() -> str | None:
    """Return the configured hub host, or None if not yet configured."""
    return _host


def get_rollers() -> list[dict]:
    """Return the cached roller list (each: id, name, has_battery)."""
    return list(_rollers)


def set_host(host: str) -> None:
    """Persist the hub host, keeping any cached rollers."""
    global _host
    _host = host
    _write()


def set_rollers(rollers: list[dict]) -> None:
    """Persist the cached roller list, keeping the host."""
    global _rollers
    _rollers = list(rollers)
    _write()


def _write() -> None:
    if not _config_path:
        return
    try:
        with open(_config_path, "w", encoding="utf-8") as f:
            json.dump({"host": _host, "rollers": _rollers}, f)
            f.flush()
            # The sandboxed custom-driver process can be killed shortly after a
            # setup completes (observed: driver restarts right after the Remote's
            # WS client disconnects) - fsync so the write survives that, instead
            # of relying on the OS to flush it back on its own schedule.
            os.fsync(f.fileno())
        _LOG.info("Saved config: %s", _config_path)
    except OSError as ex:
        _LOG.error("Cannot write config file %s: %s", _config_path, ex)
