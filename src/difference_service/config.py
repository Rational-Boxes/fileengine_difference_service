# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Environment loading + Config for difference_service (SPECIFICATION.md §9).

Shared cross-service knobs keep the ``FILEENGINE_*`` prefix (gRPC, LDAP, Redis, the
event stream, the JWT secret) so one login and one event bus span every service.
Service-private knobs use ``DIFF_*``. The background worker acts as a dedicated
service principal — ``FILEENGINE_DIFF_USER`` / ``FILEENGINE_DIFF_PASSWORD`` /
``FILEENGINE_DIFF_TENANT`` — falling back to the shared LDAP account; on-demand
requests act as the *end user* instead (§2)."""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), _strip_value(val))


def _strip_value(val: str) -> str:
    val = val.strip()
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    if val.startswith("#"):
        return ""
    hi = val.find(" #")
    if hi != -1:
        val = val[:hi]
    return val.strip()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _first(*keys_and_default: str) -> str:
    *keys, default = keys_and_default
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        # --- gRPC core (SHARED) ---
        self.grpc_host = _env("FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _env("FILEENGINE_GRPC_PORT", "50051")
        self.grpc_address = f"{self.grpc_host}:{self.grpc_port}"

        # --- Tenant + this service's own worker principal (§2) ---
        # Used ONLY by the background worker (event-driven precompute). On-demand
        # requests run as the calling user so core ACLs gate them directly.
        self.tenant = _env("FILEENGINE_DIFF_TENANT", "default")
        self.agent_user = _first("FILEENGINE_DIFF_USER", "FILEENGINE_LDAP_USER", "")
        self.agent_password = _first("FILEENGINE_DIFF_PASSWORD", "FILEENGINE_LDAP_PASSWORD", "")

        # --- LDAP (SHARED) ---
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_uri_replica = _env("FILEENGINE_LDAP_ENDPOINT_REPLICA", "")
        if not self.ldap_uri_replica and _bool("FILEENGINE_LDAP_REPLICA_ENABLED", False):
            self.ldap_uri_replica = "ldap://localhost:1389"
        self.ldap_replica_enabled = bool(self.ldap_uri_replica)
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")
        self.failover_cooldown_s = _int("DIFF_FAILOVER_COOLDOWN_S", 30)

        # --- HTTP surface (PRIVATE) — loopback by default (monitoring convention) ---
        self.http_host = _env("DIFF_HTTP_HOST", "127.0.0.1")
        self.http_port = _int("DIFF_HTTP_PORT", 8100)
        self.cors_origins = [o.strip() for o in _env("DIFF_CORS_ORIGINS", "").split(",") if o.strip()]

        # --- Auth coordination (accept http_bridge bearer tokens) ---
        self.bridge_url = _env("DIFF_BRIDGE_URL", "")
        self.bridge_introspect_ttl = _int("DIFF_BRIDGE_INTROSPECT_TTL", 60)
        self.jwt_secret = _env("FILEENGINE_JWT_SECRET", "")  # SHARED
        self.token_ttl = _int("DIFF_TOKEN_TTL", 3600)
        self.permission_cache_ttl = _int("DIFF_PERMISSION_CACHE_TTL", 300)

        # --- Events: consume the shared core stream, own private group (§2.1) ---
        self.redis_host = _env("FILEENGINE_REDIS_HOST", "localhost")
        self.redis_port = _int("FILEENGINE_REDIS_PORT", 6379)
        self.redis_password = _env("FILEENGINE_REDIS_PASSWORD", "")
        self.redis_db = _int("FILEENGINE_REDIS_DB", 0)
        self.events_stream = _env("FILEENGINE_EVENTS_STREAM", "fileengine:events")  # SHARED
        self.events_group = _env("DIFF_EVENTS_GROUP", "difference_service")         # PRIVATE
        self.consumer_name = _env("DIFF_CONSUMER_NAME", "worker-1")
        # Reconcile sweep: 0 = one-shot (run once and exit), else loop at this
        # interval. The sweep backfills missing diffs and, after a plugin version
        # bump, regenerates every stale one (§6).
        self.reconcile_interval_s = _int("DIFF_RECONCILE_INTERVAL_S", 0)
        self.reconcile_max_files = _int("DIFF_RECONCILE_MAX_FILES", 0)

        # --- Worker / plugin tuning (PRIVATE) ---
        self.worker_concurrency = _int("DIFF_WORKER_CONCURRENCY", 2)
        # Restrict which registered plugins are active (comma-separated); empty = all.
        self.enabled_plugins = {p.strip() for p in _env("DIFF_ENABLED_PLUGINS", "").split(",") if p.strip()}
        # Hard ceiling on a single version's content, both sides of a pair. A pair
        # larger than this degrades rather than exhausting the worker (§4).
        self.max_source_bytes = _int("DIFF_MAX_SOURCE_BYTES", 268435456)  # 256 MiB
        # Wall-clock budget for one diff run before it degrades a tier / fails.
        self.diff_timeout_s = _int("DIFF_TIMEOUT_S", 600)

        # --- External tool paths (PRIVATE, §9). Resolved lazily by the plugins
        #     that need them; absent tools degrade a tier, never fail the file. ---
        self.poppler_path = _env("DIFF_POPPLER_PATH", "")
        self.convert2xkt_path = _env("DIFF_CONVERT2XKT_PATH", "convert2xkt")

    # NOTE: deliberately no Postgres. DEVELOPMENT_PLAN §2 lists it as needed only
    # "if needed beyond rendition metadata" — and it is not: the cache key and its
    # invalidation live on the renditions themselves (spec §6), and the manifest is
    # the commit marker (§7.1.1). Adding a database would introduce a second source
    # of truth that could disagree with the stored renditions. Revisit only if job
    # state genuinely cannot be derived from what is stored under the file.
