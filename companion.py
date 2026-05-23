#!/usr/bin/env python3
"""
Watches Docker events and keeps DNS backends (Unbound, Cloudflare) in sync
with Traefik Host() labels. Supports Docker Swarm Mode (default) and
standalone Docker (DOCKER_MODE=standalone). Reacts immediately to service/
container events; falls back to a full reconcile every FALLBACK_INTERVAL
seconds (default 1 hour) in case an event is missed. Only manages records it created
(description="traefik-dns-companion").

Additional hostnames not in Docker labels are injected via STATIC_HOSTS.
Per-service backend routing is controlled via the dns.backends label.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
from typing import Any

import docker
import urllib3

from backends import build_backends

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dns-companion")

if "TRAEFIK_IP" not in os.environ:
    log.fatal("TRAEFIK_IP environment variable is missing")
    raise SystemExit(1)
TARGET_IP = os.environ["TRAEFIK_IP"]

try:
    FALLBACK_SECS = int(os.environ.get("FALLBACK_INTERVAL", str(3600)))
except ValueError:
    log.fatal("FALLBACK_INTERVAL must be an integer")
    raise SystemExit(1)

try:
    METRICS_PORT = int(os.environ.get("METRICS_PORT", "0"))
except ValueError:
    log.fatal("METRICS_PORT must be an integer")
    raise SystemExit(1)

DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"
HEARTBEAT_FILE = "/tmp/heartbeat"
DOCKER_MODE    = os.environ.get("DOCKER_MODE", "auto").lower()
if DOCKER_MODE not in ("swarm", "standalone", "auto"):
    raise SystemExit(f"DOCKER_MODE must be 'swarm', 'standalone', or 'auto' (got {DOCKER_MODE!r})")

if METRICS_PORT:  # pragma: no cover
    from prometheus_client import Counter, Gauge, Histogram
    from prometheus_client import start_http_server as _start_metrics
    _sync_total:      Any = Counter("dns_sync_total", "DNS sync operations by backend and result", ["backend", "result"])
    _records_managed: Any = Gauge("dns_records_managed", "Currently managed DNS record count", ["backend"])
    _sync_duration:   Any = Histogram("dns_sync_duration_seconds", "DNS sync duration", ["backend"])
    _last_sync:       Any = Gauge("dns_last_sync_timestamp", "Timestamp of last successful sync", ["backend"])
    _ip_changes:      Any = Counter("dns_ddns_ip_changes_total", "Number of public IP changes detected by DDNS backends", ["backend"])
else:
    def _start_metrics(port: int) -> None:  # type: ignore[misc]
        pass

    class _NoOp:
        def labels(self, **_: Any) -> _NoOp:
            return self
        def inc(self) -> None:
            pass
        def set(self, _: float) -> None:
            pass
        def observe(self, _: float) -> None:
            pass

    _noop = _NoOp()
    _sync_total = _records_managed = _sync_duration = _last_sync = _ip_changes = _noop

if os.environ.get("UNBOUND_TLS_VERIFY", "false").lower() != "true":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HOST_BLOCK_RE = re.compile(r'\bHost\s*\(([^)]*)\)', re.IGNORECASE)
_BACKTICK_RE   = re.compile(r'`([^`]+)`')
sync_lock      = threading.Lock()


def _hosts_from_rule(rule: str) -> set[str]:
    """Extract all hostnames from a Traefik router rule, e.g. Host(`a`, `b`)."""
    hosts: set[str] = set()
    for block in _HOST_BLOCK_RE.finditer(rule):
        for h in _BACKTICK_RE.finditer(block.group(1)):
            hosts.add(h.group(1))
    return hosts


def _parse_static_hosts(raw: str) -> dict[str, tuple[str, frozenset[str] | None]]:
    result: dict[str, tuple[str, frozenset[str] | None]] = {}
    for entry in filter(None, raw.split(",")):
        backends: frozenset[str] | None = None
        if "@" in entry:
            entry, _, b = entry.rpartition("@")
            backends = frozenset(s.strip() for s in b.split("+"))
        if "=" in entry:
            fqdn, _, ip = entry.partition("=")
            result[fqdn.strip()] = (ip.strip(), backends)
        else:
            result[entry.strip()] = (TARGET_IP, backends)
    return result


STATIC_HOSTS = _parse_static_hosts(os.environ.get("STATIC_HOSTS", ""))
BACKENDS     = build_backends(os.environ)


def _heartbeat() -> None:
    open(HEARTBEAT_FILE, "w").close()


def swarm_hosts(client: Any, backend_name: str, opt_in: bool = False) -> set[str]:
    hosts: set[str] = set()
    for svc in client.services.list():
        labels: dict[str, str] = svc.attrs.get("Spec", {}).get("Labels", {})
        allowed = labels.get("dns.backends")
        if allowed is not None:
            allowed_set = {s.strip() for s in allowed.split(",")}
            cf_alias = (
                "cloudflare" in allowed_set
                and backend_name.startswith("cloudflare-")
                and not opt_in
            )
            if "none" in allowed_set or (backend_name not in allowed_set and not cf_alias):
                continue
        elif opt_in:
            continue
        for key, val in labels.items():
            if ".rule" in key:
                hosts.update(_hosts_from_rule(val))
    return hosts


def container_hosts(client: Any, backend_name: str, opt_in: bool = False) -> set[str]:
    hosts: set[str] = set()
    for container in client.containers.list():
        labels: dict[str, str] = container.labels or {}
        allowed = labels.get("dns.backends")
        if allowed is not None:
            allowed_set = {s.strip() for s in allowed.split(",")}
            cf_alias = (
                "cloudflare" in allowed_set
                and backend_name.startswith("cloudflare-")
                and not opt_in
            )
            if "none" in allowed_set or (backend_name not in allowed_set and not cf_alias):
                continue
        elif opt_in:
            continue
        for key, val in labels.items():
            if ".rule" in key:
                hosts.update(_hosts_from_rule(val))
    return hosts


def _discover_hosts(client: Any, backend_name: str, opt_in: bool = False) -> set[str]:
    if DOCKER_MODE == "swarm":
        return swarm_hosts(client, backend_name, opt_in)
    return container_hosts(client, backend_name, opt_in)


def sync(client: Any, reason: str = "") -> None:
    with sync_lock:
        for backend in BACKENDS:
            t0 = time.monotonic()
            try:
                ip_changed: bool = getattr(backend, "refresh_ip", lambda: False)()
                opt_in: bool = getattr(backend, "opt_in", False)

                wanted: dict[str, str] = {}
                _resolve_ip = getattr(backend, "resolve_ip", None)

                for fqdn in _discover_hosts(client, backend.name, opt_in):
                    wanted[fqdn] = _resolve_ip(TARGET_IP) if _resolve_ip is not None else TARGET_IP

                for fqdn, (ip, host_backends) in STATIC_HOSTS.items():
                    if host_backends is None:
                        if not opt_in:
                            wanted[fqdn] = ip
                    else:
                        cf_alias = (
                            "cloudflare" in host_backends
                            and backend.name.startswith("cloudflare-")
                            and not opt_in
                        )
                        if backend.name in host_backends or cf_alias:
                            wanted[fqdn] = ip

                # list_managed returns {fqdn: [record_ids]} so A + AAAA records
                # for the same fqdn are grouped and deleted together.
                managed = backend.list_managed()
                to_add  = {f: ip for f, ip in wanted.items() if f not in managed}
                to_del  = {f: ids for f, ids in managed.items() if f not in wanted}
                to_update: dict[str, tuple[list[str], str]] = {}

                # DDNS: when the public IP changed, update stable records in place
                # (PUT avoids the NXDOMAIN window that delete+recreate would cause).
                if ip_changed:
                    _ip_changes.labels(backend=backend.name).inc()
                    for f in [f for f in wanted if f in managed]:
                        if hasattr(backend, "update_record"):
                            to_update[f] = (managed[f], wanted[f])
                        else:
                            to_del[f] = managed[f]
                            to_add[f] = wanted[f]

                for fqdn, ip in to_add.items():
                    if DRY_RUN:
                        log.info("dry-run [%s]: would add %s", backend.name, fqdn)
                    else:
                        backend.add_record(fqdn, ip)
                for fqdn, ids in to_del.items():
                    for record_id in ids:
                        if DRY_RUN:
                            log.info("dry-run [%s]: would remove %s", backend.name, fqdn)
                        else:
                            backend.del_record(record_id, fqdn)
                _update_record = getattr(backend, "update_record", None)
                for fqdn, (ids, ip) in to_update.items():
                    for record_id in ids:
                        if DRY_RUN:
                            log.info("dry-run [%s]: would update %s", backend.name, fqdn)
                        elif _update_record is not None:
                            _update_record(record_id, fqdn, ip)

                if to_add or to_del or to_update:
                    if not DRY_RUN:
                        backend.apply_changes()
                    log.info("%s%s: %d added, %d removed, %d updated%s",
                             "[dry-run] " if DRY_RUN else "",
                             backend.name, len(to_add), len(to_del), len(to_update),
                             f" [{reason}]" if reason else "")

                _sync_duration.labels(backend=backend.name).observe(time.monotonic() - t0)
                _sync_total.labels(backend=backend.name, result="success").inc()
                _records_managed.labels(backend=backend.name).set(len(wanted))
                _last_sync.labels(backend=backend.name).set(time.time())
            except Exception as e:
                log.error("%s sync failed%s: %s",
                          backend.name, f" [{reason}]" if reason else "", e)
                _sync_total.labels(backend=backend.name, result="error").inc()
                _sync_duration.labels(backend=backend.name).observe(time.monotonic() - t0)


def fallback_loop(client: Any) -> None:
    while True:
        time.sleep(FALLBACK_SECS)
        _heartbeat()
        log.info("fallback sync (every %dh)", FALLBACK_SECS // 3600)
        sync(client, reason="fallback")


def event_loop(client: Any) -> None:
    if DOCKER_MODE == "swarm":
        event_filter    = {"type": "service"}
        trigger_actions = {"create", "update", "remove"}
        entity          = "service"
    else:
        event_filter    = {"type": "container"}
        trigger_actions = {"start", "stop", "die"}
        entity          = "container"

    backoff = 10
    while True:
        try:
            _heartbeat()
            for event in client.events(filters=event_filter, decode=True):
                _heartbeat()
                action = event.get("Action", "")
                if action in trigger_actions:
                    name = event.get("Actor", {}).get("Attributes", {}).get("name", "?")
                    log.info("%s %s %sd", entity, name, action)
                    sync(client, reason=f"{entity} {action}")
            backoff = 10
        except Exception as e:
            log.error("event stream error: %s — reconnecting in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            _heartbeat()


def _handle_sigterm(*_: Any) -> None:
    log.info("received SIGTERM")
    sys.exit(0)


def main() -> None:
    global DOCKER_MODE
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if METRICS_PORT:
        _start_metrics(METRICS_PORT)
        log.info("metrics server listening on :%d", METRICS_PORT)
    client = docker.from_env()
    if DOCKER_MODE == "auto":
        swarm_state = client.info().get("Swarm", {}).get("LocalNodeState", "inactive")
        DOCKER_MODE = "swarm" if swarm_state == "active" else "standalone"
        log.info("docker mode: auto-detected %s", DOCKER_MODE)
    else:
        log.info("docker mode: %s (DOCKER_MODE override)", DOCKER_MODE)
    backend_names = ", ".join(b.name for b in BACKENDS)
    log.info("started — backends: %s, target: %s, fallback: %dh",
             backend_names, TARGET_IP, FALLBACK_SECS // 3600)

    _heartbeat()
    sync(client, reason="startup")

    threading.Thread(target=fallback_loop, args=(client,), daemon=True).start()
    event_loop(client)


if __name__ == "__main__":  # pragma: no cover
    main()
