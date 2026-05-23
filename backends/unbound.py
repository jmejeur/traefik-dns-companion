from __future__ import annotations

import ipaddress
import logging
from typing import Any
from backends import _http

log = logging.getLogger("dns-companion")
DESCRIPTION = "traefik-dns-companion"


class UnboundBackend:
    name = "unbound"
    opt_in: bool = False

    def __init__(
        self,
        url: str,
        key: str,
        secret: str,
        verify: bool,
        ipv6: str | None = None,
    ) -> None:
        self._url = url
        self._auth = (key, secret)
        self._verify = verify
        self._ipv6 = ipv6

    def _opn(self, method: str, path: str, **kw: Any) -> Any:
        return _http(method, f"{self._url}{path}",
                     auth=self._auth, verify=self._verify, **kw)

    def list_managed(self) -> dict[str, list[str]]:
        rows = self._opn("GET", "/api/unbound/settings/searchHostOverride").get("rows", [])
        managed: dict[str, list[str]] = {}
        for r in rows:
            if r.get("description") != DESCRIPTION:
                continue
            fqdn = f"{r['hostname']}.{r['domain']}"
            managed.setdefault(fqdn, []).append(r["uuid"])
        return managed

    def add_record(self, fqdn: str, ip: str) -> None:
        host, _, domain = fqdn.partition(".")
        if not domain:
            log.warning("unbound: skipping %r — not a valid FQDN", fqdn)
            return
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            log.warning("unbound: skipping %s — not a valid IP address: %r", fqdn, ip)
            return
        rr = "AAAA" if isinstance(addr, ipaddress.IPv6Address) else "A"
        self._opn("POST", "/api/unbound/settings/addHostOverride", json={"host": {
            "enabled": "1",
            "hostname": host,
            "domain": domain,
            "rr": rr,
            "server": ip,
            "description": DESCRIPTION,
        }})
        log.info("unbound: added  %s %s → %s", rr, fqdn, ip)
        if rr == "A" and self._ipv6:
            self._opn("POST", "/api/unbound/settings/addHostOverride", json={"host": {
                "enabled": "1",
                "hostname": host,
                "domain": domain,
                "rr": "AAAA",
                "server": self._ipv6,
                "description": DESCRIPTION,
            }})
            log.info("unbound: added  AAAA %s → %s", fqdn, self._ipv6)

    def del_record(self, uuid: str, fqdn: str) -> None:
        self._opn("POST", f"/api/unbound/settings/delHostOverride/{uuid}")
        log.info("unbound: removed %s", fqdn)

    def apply_changes(self) -> None:
        self._opn("POST", "/api/unbound/service/reconfigure")
