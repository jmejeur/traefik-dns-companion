from __future__ import annotations

import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from backends import _http

log = logging.getLogger("dns-companion")
DESCRIPTION = "traefik-dns-companion"
CF_API = "https://api.cloudflare.com/client/v4"
_DDNS_SOURCES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",          # Cloudflare-operated
    "https://whatismyip.akamai.com",  # Akamai-operated
]
# Strict majority of all configured sources (scales automatically with list length).
_DDNS_QUORUM = len(_DDNS_SOURCES) // 2 + 1


def _fetch_ip_from(url: str) -> str:
    """Fetch public IP from a single URL. Returns '' on any failure."""
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        ip = r.text.strip()
        ipaddress.IPv4Address(ip)
        return ip
    except Exception:
        return ""


def _fetch_ip_quorum() -> str:
    """Ask all sources; return an IP agreed on by a strict majority or '' if none."""
    with ThreadPoolExecutor(max_workers=len(_DDNS_SOURCES)) as pool:
        results = list(pool.map(_fetch_ip_from, _DDNS_SOURCES))
    for ip in set(results):
        if ip and results.count(ip) >= _DDNS_QUORUM:
            return ip
    return ""


def _parse_zones(zones_raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in filter(None, zones_raw.split(",")):
        domain, _, zone_id = entry.partition("=")
        result[domain.strip()] = zone_id.strip()
    return result


def _zone_for(fqdn: str, zones: dict[str, str]) -> str | None:
    # Longest matching suffix wins: app.sub.example.com → example.com
    for suffix in sorted(zones, key=len, reverse=True):
        if fqdn == suffix or fqdn.endswith("." + suffix):
            return zones[suffix]
    return None


class CloudflareBackend:
    """
    Manages Cloudflare DNS records for one of three provider types:
      tunnel  — CNAME → CF_TUNNEL_TARGET, always proxied
      direct  — A record, never proxied, IP from A_SOURCE (traefik/static/ddns)
      proxied — A record, always proxied, IP from A_SOURCE
    """

    def __init__(
        self,
        token: str,
        target: str,
        zone_id: str | None,
        zones_raw: str | None,
        provider: str,          # "tunnel" | "direct" | "proxied"
        ddns: bool = False,
        opt_in: bool = False,
    ) -> None:
        self._token = token
        self._target = target
        self._provider = provider
        self._single_zone: str | None = zone_id
        self._zones: dict[str, str] = {} if zone_id else _parse_zones(zones_raw or "")
        self._ddns = ddns
        self._ddns_ip: str = ""
        self._source_index: int = 0
        self.name = f"cloudflare-{provider}"
        self.opt_in = opt_in
        self._session = requests.Session()

    def refresh_ip(self) -> bool:
        if not self._ddns:
            return False

        # Startup: no cached IP — query all sources and require 2-of-3 agreement.
        if not self._ddns_ip:
            ip = _fetch_ip_quorum()
            if not ip:
                log.warning("cloudflare DDNS: all IP sources failed and no cached IP — skipping sync")
                return False
            log.info("cloudflare DDNS: public IP (initial) %s", ip)
            self._ddns_ip = ip
            return True

        # Normal poll: round-robin through sources one at a time.
        url = _DDNS_SOURCES[self._source_index]
        self._source_index = (self._source_index + 1) % len(_DDNS_SOURCES)
        polled_ip = _fetch_ip_from(url)

        if not polled_ip:
            log.warning("cloudflare DDNS: %s failed — retaining cached %s", url, self._ddns_ip)
            return False

        if polled_ip == self._ddns_ip:
            return False

        # Potential change — confirm with all sources before updating records.
        log.info("cloudflare DDNS: potential IP change (%s → %s) — confirming with quorum",
                 self._ddns_ip, polled_ip)
        confirmed = _fetch_ip_quorum()
        if not confirmed:
            log.warning("cloudflare DDNS: quorum inconclusive — retaining cached %s", self._ddns_ip)
            return False
        if confirmed == self._ddns_ip:
            log.info("cloudflare DDNS: quorum confirms no change (%s)", self._ddns_ip)
            return False

        log.info("cloudflare DDNS: IP confirmed %s → %s", self._ddns_ip, confirmed)
        self._ddns_ip = confirmed
        return True

    def _cf(self, method: str, path: str, **kw: Any) -> Any:
        return _http(method, f"{CF_API}{path}", token=self._token, session=self._session, **kw)

    def _zone_for(self, fqdn: str) -> str | None:
        if self._single_zone:
            return self._single_zone
        return _zone_for(fqdn, self._zones)

    def _record_body(self, fqdn: str) -> dict[str, Any]:
        if self._provider == "tunnel":
            return {
                "type": "CNAME",
                "name": fqdn,
                "content": self._target,
                "proxied": True,
                "comment": DESCRIPTION,
            }
        content = self._ddns_ip if self._ddns else self._target
        return {
            "type": "A",
            "name": fqdn,
            "content": content,
            "proxied": self._provider == "proxied",
            "comment": DESCRIPTION,
        }

    def list_managed(self) -> dict[str, list[str]]:
        zone_ids = {self._single_zone} if self._single_zone else set(self._zones.values())
        managed: dict[str, list[str]] = {}
        for zone_id in zone_ids:
            page = 1
            while True:
                resp = self._cf(
                    "GET", f"/zones/{zone_id}/dns_records",
                    params={"comment": DESCRIPTION, "per_page": 1000, "page": page},
                )
                for r in resp.get("result", []):
                    managed.setdefault(r["name"], []).append(r["id"])
                if page >= resp.get("result_info", {}).get("total_pages", 1):
                    break
                page += 1
        return managed

    def resolve_ip(self, default_ip: str) -> str:
        """Return the IP this backend will write to DNS records.

        For DDNS backends the public IP is discovered independently; for all
        other provider types the caller's default_ip (Traefik's IP) is used.
        """
        return self._ddns_ip if self._ddns else default_ip

    def add_record(self, fqdn: str, ip: str) -> None:
        zone_id = self._zone_for(fqdn)
        if not zone_id:
            log.warning("cloudflare: no zone for %s — skipping", fqdn)
            return
        if self._ddns and not self._ddns_ip:
            log.warning("cloudflare DDNS: no IP available for %s — skipping", fqdn)
            return

        resp = self._cf(
            "GET", f"/zones/{zone_id}/dns_records",
            params={"name": fqdn}
        )
        existing = resp.get("result", []) if isinstance(resp, dict) else []
        for r in existing:
            if isinstance(r, dict) and r.get("comment") != DESCRIPTION:
                log.warning("cloudflare: skipping %s — conflicting non-companion record exists", fqdn)
                return

        body = self._record_body(fqdn)
        self._cf("POST", f"/zones/{zone_id}/dns_records", json=body)
        log.info("cloudflare: added  %s %s → %s", body["type"], fqdn, body["content"])

    def update_record(self, record_id: str, fqdn: str, _ip: str) -> None:
        """Atomically replace a record's content via PUT (no NXDOMAIN window)."""
        zone_id = self._zone_for(fqdn)
        if not zone_id:
            log.warning("cloudflare: no zone for %s — cannot update", fqdn)
            return
        if self._ddns and not self._ddns_ip:
            log.warning("cloudflare DDNS: no IP available for %s — skipping update", fqdn)
            return
        body = self._record_body(fqdn)
        self._cf("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=body)
        log.info("cloudflare: updated %s %s → %s", body["type"], fqdn, body["content"])

    def del_record(self, record_id: str, fqdn: str) -> None:
        zone_id = self._zone_for(fqdn)
        if not zone_id:
            log.warning("cloudflare: no zone for %s — cannot delete", fqdn)
            return
        self._cf("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        log.info("cloudflare: removed %s", fqdn)

    def apply_changes(self) -> None:
        pass  # Cloudflare applies records immediately
