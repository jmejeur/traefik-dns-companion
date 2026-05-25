from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

import requests


class Backend(Protocol):
    """Duck-typed interface every DNS backend must satisfy."""

    name: str
    opt_in: bool

    def list_managed(self) -> dict[str, list[str]]: ...
    def add_record(self, fqdn: str, ip: str) -> None: ...
    def del_record(self, record_id: str, fqdn: str) -> None: ...
    def apply_changes(self) -> None: ...


def _http(
    method: str,
    url: str,
    token: str | None = None,
    auth: tuple[str, str] | None = None,
    verify: bool | str = True,
    session: requests.Session | None = None,
    **kw: Any,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for attempt in range(3):
        if session:
            r = session.request(method, url, headers=headers, auth=auth,
                                verify=verify, timeout=15, **kw)
        else:
            r = requests.request(method, url, headers=headers, auth=auth,
                                 verify=verify, timeout=15, **kw)
        if r.status_code < 500 or attempt == 2:
            r.raise_for_status()
            return r.json()
        time.sleep(2 ** attempt)


def _resolve_a_source(source: str, traefik_ip: str, a_ip: str, ip_var: str) -> tuple[str, bool]:
    if source == "traefik":
        return traefik_ip, False
    if source == "static":
        if not a_ip:
            raise SystemExit(f"CF_A_SOURCE=static requires {ip_var}")
        return a_ip, False
    if source == "ddns":
        return "", True
    raise SystemExit(f"A source must be traefik, static, or ddns (got {source!r})")


def build_backends(env: Mapping[str, str]) -> list[Backend]:
    import logging
    from backends.unbound import UnboundBackend
    from backends.cloudflare import CloudflareBackend

    log = logging.getLogger("dns-companion")
    active: list[Backend] = []

    if env.get("UNBOUND_URL"):
        for var in ("UNBOUND_API_KEY", "UNBOUND_API_SECRET"):
            if not env.get(var):
                raise SystemExit(f"Unbound backend requires {var}")
        enable_ipv6 = env.get("ENABLE_IPV6", "false").lower() == "true"
        ca_cert = env.get("UNBOUND_CA_CERT", "")
        verify: bool | str = ca_cert if ca_cert else env.get("UNBOUND_TLS_VERIFY", "false").lower() == "true"
        active.append(UnboundBackend(
            url=env["UNBOUND_URL"],
            key=env["UNBOUND_API_KEY"],
            secret=env["UNBOUND_API_SECRET"],
            verify=verify,
            ipv6=env.get("TRAEFIK_IPV6") if enable_ipv6 else None,
        ))

    if env.get("CF_TOKEN"):
        token    = env["CF_TOKEN"]
        zone_id  = env.get("CF_ZONE_ID")
        zones_raw = env.get("CF_ZONES")
        if zone_id and zones_raw:
            log.warning("CF_ZONE_ID and CF_ZONES both set — CF_ZONES takes priority")
            zone_id = None
        if not zone_id and not zones_raw:
            raise SystemExit("Cloudflare backend requires CF_ZONE_ID or CF_ZONES")

        cf_providers: list[CloudflareBackend] = []

        if env.get("CF_TUNNEL_TARGET"):
            cf_providers.append(CloudflareBackend(
                token=token,
                target=env["CF_TUNNEL_TARGET"],
                zone_id=zone_id,
                zones_raw=zones_raw,
                provider="tunnel",
            ))

        if env.get("CF_DIRECT_A_SOURCE"):
            target, ddns = _resolve_a_source(
                env["CF_DIRECT_A_SOURCE"].lower(),
                env.get("TRAEFIK_IP", ""),
                env.get("CF_DIRECT_A_IP", ""),
                "CF_DIRECT_A_IP",
            )
            cf_providers.append(CloudflareBackend(
                token=token,
                target=target,
                zone_id=zone_id,
                zones_raw=zones_raw,
                provider="direct",
                ddns=ddns,
            ))

        if env.get("CF_PROXIED_A_SOURCE"):
            target, ddns = _resolve_a_source(
                env["CF_PROXIED_A_SOURCE"].lower(),
                env.get("TRAEFIK_IP", ""),
                env.get("CF_PROXIED_A_IP", ""),
                "CF_PROXIED_A_IP",
            )
            cf_providers.append(CloudflareBackend(
                token=token,
                target=target,
                zone_id=zone_id,
                zones_raw=zones_raw,
                provider="proxied",
                ddns=ddns,
            ))

        if not cf_providers:
            raise SystemExit(
                "CF_TOKEN set but no provider enabled — set CF_TUNNEL_TARGET, "
                "CF_DIRECT_A_SOURCE, or CF_PROXIED_A_SOURCE"
            )

        if len(cf_providers) > 1:
            cf_default = env.get("CF_DEFAULT", "").lower()
            if not cf_default:
                raise SystemExit(
                    "Multiple Cloudflare providers require CF_DEFAULT "
                    "(tunnel, direct, or proxied)"
                )
            if cf_default not in ("tunnel", "direct", "proxied"):
                raise SystemExit(
                    f"CF_DEFAULT must be tunnel, direct, or proxied (got {cf_default!r})"
                )
            active_names = {p.name for p in cf_providers}
            if f"cloudflare-{cf_default}" not in active_names:
                raise SystemExit(
                    f"CF_DEFAULT={cf_default!r} does not match any active provider "
                    f"(active: {sorted(n.split('-', 1)[1] for n in active_names)})"
                )
            for p in cf_providers:
                if p.name != f"cloudflare-{cf_default}":
                    p.opt_in = True

        active.extend(cf_providers)

    if not active:
        raise SystemExit("No backends configured — set UNBOUND_URL and/or CF_TOKEN")

    return active
