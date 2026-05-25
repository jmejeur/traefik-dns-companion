# traefik-dns-companion

[![GitHub Release](https://img.shields.io/github/v/release/jmejeur/traefik-dns-companion)](https://github.com/jmejeur/traefik-dns-companion/releases)
[![License: MIT](https://img.shields.io/github/license/jmejeur/traefik-dns-companion)](https://github.com/jmejeur/traefik-dns-companion/blob/main/LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/jmejeur/traefik-dns-companion?style=flat)](https://github.com/jmejeur/traefik-dns-companion/stargazers)
[![Container](https://img.shields.io/badge/ghcr.io-traefik--dns--companion-blue?logo=docker)](https://github.com/jmejeur/traefik-dns-companion/pkgs/container/traefik-dns-companion)
[![Python](https://img.shields.io/badge/python-3.14-blue?logo=python)](https://www.python.org/)

Watches Docker Swarm service labels and keeps DNS backends in sync with Traefik `Host()` router rules. Reacts immediately to service create/update/remove events, with a full reconcile every 1 hour as a fallback.

Supported backends:
- **Unbound (OPNsense/pfSense)** — internal/local DNS (A records)
- **Cloudflare** — public DNS via Cloudflare Tunnel, proxied CNAME, or A records

Both backends can run simultaneously. Each Swarm service controls which backends handle its hostnames via a `dns.backends` label.

Cloudflare supports three independent providers that can run simultaneously:
- **cloudflare-tunnel** — CNAME → `CF_TUNNEL_TARGET`, always proxied (Cloudflare Tunnel)
- **cloudflare-direct** — A record, never proxied (e.g. VPN servers)
- **cloudflare-proxied** — A record, always proxied (orange cloud)

Only manages records it created (`description = "traefik-dns-companion"`) — all other DNS records are never touched.

## Docker Mode

The companion supports both **Docker Swarm Mode** and **standalone Docker**, and auto-detects which mode to use at startup (`DOCKER_MODE=auto`, the default).

### Swarm mode (recommended for production)

In Swarm mode the companion uses `client.services.list()` and `type=service` events to discover Traefik router rules across the cluster. It must run on a manager node.

If you are not already running Swarm, initialize it on your Docker host:

```bash
docker swarm init
```

Deploy with `docker stack deploy` — it must be a Swarm service to have access to the full service registry and to respect the `deploy:` placement constraints that pin it to a manager.

### Standalone mode

In standalone mode the companion watches container `start`, `stop`, and `die` events and reads labels from running containers. Deploy with `docker run` or `docker compose`. Set `DOCKER_MODE=standalone` to force this mode, or leave it on `auto` and the companion detects that Swarm is not active.

## How it works

1. On startup, performs a full reconcile against all current services (or containers in standalone mode).
2. Subscribes to the Docker event stream. In Swarm mode, reacts to `type=service` create/update/remove events; in standalone mode, reacts to container `start`/`stop`/`die` events.
3. A background thread re-runs the full reconcile every `FALLBACK_INTERVAL` seconds in case an event is missed.

## Per-service backend routing

Add a `dns.backends` label to any Swarm service to control which backends handle its hostnames:

```yaml
labels:
  traefik.http.routers.myapp.rule: "Host(`app.example.com`)"
  dns.backends: "unbound,cloudflare"         # both (default when label is absent)
  # dns.backends: "unbound"                  # internal only
  # dns.backends: "cloudflare"               # global Cloudflare provider only
  # dns.backends: "cloudflare-tunnel"        # Cloudflare Tunnel specifically
  # dns.backends: "cloudflare-direct"        # direct A record specifically
  # dns.backends: "unbound,cloudflare-direct" # unbound + specific CF provider
  # dns.backends: "none"                     # skip entirely
```

`cloudflare` in `dns.backends` routes to whichever Cloudflare provider is the global (catch-all). Use `cloudflare-tunnel`, `cloudflare-direct`, or `cloudflare-proxied` to target a specific provider directly (useful when multiple CF providers are configured).

## Environment variables

### Common

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRAEFIK_IP` | Yes | — | IP address Traefik listens on; used for Unbound A records and as the fallback IP for `STATIC_HOSTS` entries without an explicit IP |
| `STATIC_HOSTS` | No | `""` | Extra hostnames — see format below |
| `FALLBACK_INTERVAL` | No | `3600` | Full-reconcile interval in seconds (default 1 hour) |
| `LOG_LEVEL` | No | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DRY_RUN` | No | `false` | Log what would change without writing any DNS records |
| `ENABLE_IPV6` | No | `false` | Enable dual-stack AAAA record creation alongside A records |
| `TRAEFIK_IPV6` | No | — | IPv6 address for AAAA records (required when `ENABLE_IPV6=true`) |
| `METRICS_PORT` | No | — | Expose Prometheus metrics on this port (disabled when unset) |
| `DOCKER_MODE` | No | `auto` | `swarm`, `standalone`, or `auto` (auto-detects via `docker info`) |

### Unbound (OPNsense/pfSense) backend

OPNsense and pfSense both expose the same Unbound REST API.

| Variable | Required | Default | Description |
|---|---|---|---|
| `UNBOUND_URL` | Yes (if using) | — | Base URL of your OPNsense or pfSense host, e.g. `https://opnsense.lan` |
| `UNBOUND_API_KEY` | Yes (if using) | — | API key |
| `UNBOUND_API_SECRET` | Yes (if using) | — | API secret |
| `UNBOUND_TLS_VERIFY` | No | `false` | Set to `true` once your firewall has a valid TLS cert (trusted CA) |
| `UNBOUND_CA_CERT` | No | — | Path to a CA bundle PEM file for TLS verification (use instead of `UNBOUND_TLS_VERIFY` when your firewall uses a self-signed cert) |

### Cloudflare backend

Up to three independent Cloudflare providers can run simultaneously. Enable each by setting its activation variable.

#### Shared (required when any CF provider is active)

| Variable | Required | Default | Description |
|---|---|---|---|
| `CF_TOKEN` | Yes | — | Cloudflare API token (scoped to DNS edit) |
| `CF_ZONE_ID` | One of these | — | Single zone ID — all CF hostnames go here |
| `CF_ZONES` | One of these | — | Multi-zone: `domain=zone_id` pairs, comma-separated |
| `CF_DEFAULT` | If multiple providers | — | Which provider is the global catch-all: `tunnel`, `direct`, or `proxied`. Not required when only one provider is configured. |

`CF_ZONE_ID` and `CF_ZONES` are mutually exclusive. If both are set, `CF_ZONES` takes priority.

All active Cloudflare providers (tunnel, direct, proxied) share the same zone routing — a hostname is always resolved to the same zone regardless of which provider handles it. When using `CF_ZONES`, your `CF_TOKEN` must have DNS edit permission on every zone listed.

#### Tunnel provider (`cloudflare-tunnel`)

Creates CNAME records pointing to a Cloudflare Tunnel, always proxied.

| Variable | Required | Description |
|---|---|---|
| `CF_TUNNEL_TARGET` | Yes (enables provider) | CNAME target — your tunnel hostname, e.g. `uuid.cfargotunnel.com` |

#### Direct provider (`cloudflare-direct`)

Creates unproxied A records. Useful for VPN servers or anything that must bypass the Cloudflare proxy.

| Variable | Required | Description |
|---|---|---|
| `CF_DIRECT_A_SOURCE` | Yes (enables provider) | IP source: `traefik`, `static`, or `ddns` |
| `CF_DIRECT_A_IP` | If `CF_DIRECT_A_SOURCE=static` | Static IP for A records |

#### Proxied provider (`cloudflare-proxied`)

Creates proxied A records (orange cloud). Traffic routes through Cloudflare.

| Variable | Required | Description |
|---|---|---|
| `CF_PROXIED_A_SOURCE` | Yes (enables provider) | IP source: `traefik`, `static`, or `ddns` |
| `CF_PROXIED_A_IP` | If `CF_PROXIED_A_SOURCE=static` | Static IP for A records |

#### A record IP sources (`traefik` / `static` / `ddns`)

| Source | Behaviour |
|---|---|
| `traefik` | Uses `TRAEFIK_IP` for all A records |
| `static` | Uses the fixed IP in `CF_DIRECT_A_IP` / `CF_PROXIED_A_IP` |
| `ddns` | Discovers and tracks your public IP automatically (see below) |

### STATIC_HOSTS format

Comma-separated entries. Each entry can specify a custom IP and/or restrict which backends receive the host:

```
STATIC_HOSTS=nas.lan=10.50.1.5@unbound,pub.example.com=1.2.3.4@unbound+cloudflare,pbs.lan
```

- `host.domain` — uses `TRAEFIK_IP`, all non-opt-in backends
- `host.domain=1.2.3.4` — uses specified IP, all non-opt-in backends
- `host.domain=1.2.3.4@unbound` — uses specified IP, Unbound only
- `host.domain=1.2.3.4@unbound+cloudflare` — uses specified IP, Unbound + global Cloudflare provider (`+` separates backend names)
- `host.domain=1.2.3.4@cloudflare-direct` — targets the direct provider specifically

Use specific provider names (`cloudflare-tunnel`, `cloudflare-direct`, `cloudflare-proxied`) in `@backend` when you need a particular Cloudflare provider. Use plain `cloudflare` to route to whichever provider is the global catch-all.

## Prometheus metrics

Set `METRICS_PORT` to expose a `/metrics` endpoint for Prometheus scraping (disabled by default):

```yaml
environment:
  METRICS_PORT: 9090
```

Available metrics:

| Metric | Type | Description |
|---|---|---|
| `dns_sync_total{backend, result}` | Counter | Sync operations by backend (`unbound`, `cloudflare`) and result (`success`, `error`) |
| `dns_records_managed{backend}` | Gauge | Number of DNS records currently managed per backend |
| `dns_sync_duration_seconds{backend}` | Histogram | Time taken for each sync operation |
| `dns_last_sync_timestamp{backend}` | Gauge | Unix timestamp of the last successful sync — useful for alerting on stale syncs |
| `dns_ddns_ip_changes_total{backend}` | Counter | Number of public IP changes confirmed by DDNS quorum — useful for alerting on unexpected IP churn |

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: traefik-dns-companion
    static_configs:
      - targets: ["dns-companion-host:9090"]
```

## Unbound (OPNsense/pfSense) API setup

### OPNsense

1. Go to **System → Access → Users** and create a dedicated user (e.g. `dns-companion`).
2. Generate an API key/secret pair for that user.
3. Grant the user the **Unbound DNS** privilege (`System: Unbound DNS`).

### pfSense

1. Go to **System → User Manager** and create a dedicated user (e.g. `dns-companion`).
2. Generate an API key/secret pair for that user.
3. Grant the user the **WebCfg - Services: DNS Resolver** privilege.

## Cloudflare API setup

1. In the Cloudflare dashboard, go to **My Profile → API Tokens → Create Token**.
2. Use the **Edit zone DNS** template, scoped to the zones this companion will manage.

## Usage

All examples deploy via `docker stack deploy`. The companion must run on a Swarm manager node — workers do not have access to the full service registry.

### Unbound (OPNsense/pfSense) only

```yaml
services:
  traefik-dns-companion:
    image: ghcr.io/jmejeur/traefik-dns-companion:2.0.0
    environment:
      TRAEFIK_IP: 10.40.40.40
      UNBOUND_URL: https://opnsense.lan
      UNBOUND_API_KEY: your-api-key
      UNBOUND_API_SECRET: your-api-secret
      STATIC_HOSTS: nas.lan=10.50.1.5@unbound,pbs.lan
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.role == manager
```

### Cloudflare Tunnel only

```yaml
services:
  traefik-dns-companion:
    image: ghcr.io/jmejeur/traefik-dns-companion:2.0.0
    environment:
      TRAEFIK_IP: 10.40.40.40        # required at startup; used as fallback for STATIC_HOSTS
      CF_TOKEN: your-cf-token
      CF_TUNNEL_TARGET: uuid.cfargotunnel.com
      CF_ZONE_ID: your-zone-id
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.role == manager
```

For proxied A records instead of a Cloudflare Tunnel CNAME:

```yaml
    environment:
      TRAEFIK_IP: 10.40.40.40
      CF_TOKEN: your-cf-token
      CF_ZONE_ID: your-zone-id
      CF_PROXIED_A_SOURCE: traefik    # uses TRAEFIK_IP for all A records, always proxied
```

For a home lab without a static IP (Cloudflare DDNS):

```yaml
    environment:
      TRAEFIK_IP: 10.40.40.40        # still required for Unbound and STATIC_HOSTS fallback
      CF_TOKEN: your-cf-token
      CF_ZONE_ID: your-zone-id
      CF_PROXIED_A_SOURCE: ddns       # discovers and tracks your public IP automatically
```

For a mixed setup — most services use a Cloudflare Tunnel, but a VPN server needs a direct unproxied A record with DDNS:

```yaml
    environment:
      TRAEFIK_IP: 10.40.40.40
      CF_TOKEN: your-cf-token
      CF_ZONE_ID: your-zone-id
      CF_TUNNEL_TARGET: uuid.cfargotunnel.com
      CF_DIRECT_A_SOURCE: ddns
      CF_DEFAULT: tunnel              # tunnel is the catch-all; direct is opt-in only
```

Services that want the direct provider opt in explicitly:
```yaml
labels:
  traefik.http.routers.vpn.rule: "Host(`vpn.example.com`)"
  dns.backends: "cloudflare-direct"
```

With `CF_DIRECT_A_SOURCE=ddns` or `CF_PROXIED_A_SOURCE=ddns`, the companion discovers your public IP via five independent IP-echo services (`api.ipify.org`, `ifconfig.me`, `checkip.amazonaws.com`, `icanhazip.com`, `whatismyip.akamai.com`):

- **Startup**: queries all five sources and requires a strict majority (3-of-5) before accepting the IP.
- **Normal polls** (every `FALLBACK_INTERVAL`): rotates through sources one at a time to spread load.
- **Change detected**: if the polled source returns a new IP, all five are queried again to confirm the change (3-of-5 majority required). If they disagree, the cached IP is retained and a warning is logged.

When the IP changes, existing A records are updated via Cloudflare's PUT endpoint — the record is never absent (no NXDOMAIN window), which matters for VPN servers and other latency-sensitive services. If no IP has ever been resolved (e.g., all sources unreachable on startup), DNS writes are skipped until the next cycle.

Note: `STATIC_HOSTS` entries routed to a DDNS-enabled Cloudflare provider are written with the DDNS IP — the per-entry IP in `STATIC_HOSTS` is used for Unbound only.

### Standalone Docker (docker run / docker compose)

When Swarm is not in use, the companion auto-detects standalone mode. No placement constraints or stack deploy required:

```yaml
services:
  traefik-dns-companion:
    image: ghcr.io/jmejeur/traefik-dns-companion:2.0.0
    environment:
      TRAEFIK_IP: 10.40.40.40
      UNBOUND_URL: https://opnsense.lan
      UNBOUND_API_KEY: your-api-key
      UNBOUND_API_SECRET: your-api-secret
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

To use a Docker Socket Proxy instead of mounting the socket directly, set `DOCKER_HOST` in the environment and omit the volume:

```yaml
environment:
  DOCKER_HOST: tcp://socket-proxy:2375
```

### Both backends

```yaml
services:
  traefik-dns-companion:
    image: ghcr.io/jmejeur/traefik-dns-companion:2.0.0
    environment:
      TRAEFIK_IP: 10.40.40.40
      UNBOUND_URL: https://opnsense.lan
      UNBOUND_API_KEY: your-api-key
      UNBOUND_API_SECRET: your-api-secret
      CF_TOKEN: your-cf-token
      CF_TUNNEL_TARGET: uuid.cfargotunnel.com
      CF_ZONE_ID: your-zone-id
      STATIC_HOSTS: nas.lan=10.50.1.5@unbound,pub.example.com=1.2.3.4@unbound+cloudflare
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.role == manager
```

## Security Considerations

### Trust Boundaries and DNS Authority
The companion reads Traefik `.rule` labels from any discovered container/service and creates DNS records for them. There is **no allowlist**. Anyone who can deploy a container or service in your environment can claim arbitrary hostnames—including subdomains of your Cloudflare zones—and point them to their own containers. In single-tenant homelabs this is fine, but in multi-tenant environments this is a privilege escalation path. You can use the `dns.backends: none` label to opt a service out, but you cannot restrict which hostnames a service is allowed to claim.

### Docker Socket Access
The companion requires access to the Docker socket to read container/service labels. Access to the Docker socket is equivalent to **root access on the host**. If the companion container is compromised, the attacker can take full control of the Docker host. We recommend using a Docker Socket Proxy (as shown in the Standalone Docker example) to restrict API access to read-only events and container inspections.

If you configure `DOCKER_HOST` to point to a remote daemon (e.g., `tcp://socket-proxy:2375`), note that port 2375 is unencrypted by default. All Docker API traffic will be transmitted in plaintext. Use port 2376 with TLS if communicating over an untrusted network.

### TLS Verification for Unbound
By default, `UNBOUND_TLS_VERIFY` is `false`. This is a pragmatic default because most OPNsense/pfSense firewalls ship with self-signed certificates, but it means the API calls — which carry your API key and secret as HTTP Basic Auth — are vulnerable to Man-In-The-Middle (MITM) attacks on your LAN segment. The recommended paths, in order of preference:

**Best: use a trusted certificate.** If your firewall has a certificate signed by a public CA (e.g. via Let's Encrypt with the ACME plugin) or by an internal CA that your systems already trust, set:

```yaml
environment:
  UNBOUND_TLS_VERIFY: "true"
```

**Good: pin the self-signed CA.** Export the CA certificate from OPNsense (**System → Trust → Authorities**, export as PEM) and mount it into the container. Set `UNBOUND_CA_CERT` to the in-container path instead of using `UNBOUND_TLS_VERIFY`:

```yaml
environment:
  UNBOUND_CA_CERT: /run/secrets/opnsense-ca.pem
secrets:
  - opnsense-ca.pem
```

When `UNBOUND_CA_CERT` is set, it takes precedence over `UNBOUND_TLS_VERIFY` — the companion verifies the server certificate against your CA bundle rather than the system trust store. The TLS insecure warning is also suppressed.

**Avoid: disable verification (default).** Leaving `UNBOUND_TLS_VERIFY=false` and omitting `UNBOUND_CA_CERT` silences urllib3 warnings and skips certificate validation entirely. This is the default only because it requires no firewall configuration changes; it should be treated as a temporary starting point, not a permanent setting.

## Development

```bash
uv venv --python 3.14
uv pip install -r requirements.txt -r requirements-dev.txt
uv run pytest
```

## Releases

Images are published to `ghcr.io/jmejeur/traefik-dns-companion` on every version tag (`v*`). Tags follow semver: `2.0.0` and `2.0`.

## Contributors

[![Contributors](https://contrib.rocks/image?repo=jmejeur/traefik-dns-companion)](https://github.com/jmejeur/traefik-dns-companion/graphs/contributors)

## License

MIT
