# Roadmap

## Traefik API discovery for non-Docker routes

Docker-provider routes are already discovered via Docker events and labels (preserving `dns.backends` per-service routing). This feature adds a periodic poll of the Traefik HTTP API to pick up routes from **non-Docker providers** (file, Kubernetes ingress, etc.) that are otherwise invisible to the companion:

- On each poll, query `GET /api/http/routers` against `TRAEFIK_API_URL`
- Filter to routers whose provider is not `docker` or `swarm` (router names follow `<name>@<provider>`)
- Parse `rule` fields using the same `Host()` regex and merge into the wanted set for all backends
- Poll runs on the fallback interval by default; configurable via `TRAEFIK_POLL_INTERVAL`
- Requires `TRAEFIK_API_URL` env var and Traefik v3+ (the API shape changed between v2 and v3)

Non-Docker routes have no `dns.backends` label available (Traefik does not forward custom Docker labels). A `TRAEFIK_BACKENDS` env var provides the equivalent at the container level — a comma-separated list of backends that receive all Traefik-API-discovered routes (default: all configured backends). This mirrors the per-service `dns.backends` label but applied globally to non-Docker providers:

| Route source | Backend selection |
|---|---|
| Docker service, no `dns.backends` label | all backends |
| Docker service, `dns.backends: "unbound"` | per-service label |
| Traefik API (non-Docker provider) | `TRAEFIK_BACKENDS` env var (default: all) |

This approach largely eliminates the need for `STATIC_HOSTS` entries for non-Docker services.

## Pi-hole backend

Add a `PiholeBackend` that manages local DNS records in Pi-hole via its API:

- Pi-hole v5/v6 exposes a `POST /admin/api.php?customdns` endpoint (with token auth) for adding and removing custom DNS entries
- Records are identified by hostname + IP; ownership tagging would require storing managed FQDNs separately (e.g., a local state file) since Pi-hole has no record metadata field
- Useful for setups where Pi-hole is the local resolver instead of Unbound (OPNsense/pfSense)

## AdGuard Home backend

Add an `AdGuardBackend` that manages rewrite rules in AdGuard Home via its REST API:

- AdGuard Home exposes `POST /control/rewrite/add` and `POST /control/rewrite/delete` endpoints (Basic auth)
- `GET /control/rewrite/list` returns all rewrite rules; filter by matching the companion's managed FQDNs (ownership requires a local state file or a naming convention since AdGuard has no metadata field)
- Supports both A records and CNAME rewrites; companion would use A records pointing to `TRAEFIK_IP`
- Useful for setups where AdGuard Home is the local resolver, either standalone or as an upstream of another resolver

## UniFi DNS backend

Add a `UnifiBackend` for UniFi Dream Machine / UDM Pro local DNS:

- UDM exposes an undocumented REST API for managing `dnsmasq` host overrides
- Requires cookie-based auth (username/password login then session cookie)
- Useful for all-UniFi network setups where the UDM is also the local DNS resolver

## Excluded hosts / regex filters

Add `EXCLUDED_HOSTS` env var support to skip specific hostnames from sync:

- Comma-separated list of FQDNs and/or regex patterns
- Hostnames matching any pattern are excluded from `wanted` before diffing against managed records
- Useful for services that have Traefik rules but should not get DNS entries (e.g., internal admin UIs, development services)

## Healthcheck improvements

The current healthcheck only verifies that `/tmp/heartbeat` was touched recently (i.e. the process is alive and not stuck). Improvements:

- **Backend reachability probe** — on each fallback sync, attempt a lightweight API call to each configured backend (e.g. `GET /api/unbound/settings/searchHostOverride` with a short timeout) and record success/failure per backend
- **Separate health states** — distinguish between "process healthy" and "backend reachable"; expose both via the heartbeat file or a small HTTP health endpoint
- **Docker HEALTHCHECK granularity** — current check is binary (alive/dead); a `/healthz` HTTP endpoint (served on a configurable port, default off) would allow orchestrators to surface degraded state without killing the container

## Docker Secrets support

Currently, `CF_TOKEN` and `UNBOUND_API_SECRET` are passed as raw environment variables. These are visible in plaintext to anyone who runs `docker inspect` or accesses the Swarm dashboard. Supporting Docker Secrets requires:

- Update `build_backends()` to support `_FILE` suffixes (e.g., `CF_TOKEN_FILE=/run/secrets/cf_token`)
- Read the secret value from the mounted file instead of the environment variable
- Keep the secrets entirely in-memory and out of the environment variables
- Aligns with standard Docker Swarm secret management practices

## Network isolation hardening

The companion only needs to make outbound HTTP requests. It doesn't serve any traffic (other than the optional Prometheus metrics).

- Document best practices for keeping the container on an internal backend overlay network, isolated from Traefik's public ingress network
- Ensure the metrics port is not exposed publicly, keeping it internal to the cluster for Prometheus scraping only
