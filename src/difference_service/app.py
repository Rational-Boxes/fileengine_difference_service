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

"""FastAPI application factory for difference_service (SPECIFICATION.md §8, §9).

``build_app`` wires the shared services onto ``app.state`` and includes the router:

  state.config            Config
  state.token_store       TokenStore (bearer tokens issued by /auth/token)
  state.bridge_verifier   BridgeTokenVerifier (accept http_bridge tokens)
  state.registry          PluginRegistry (MIME -> DiffPlugin dispatch)

``build_app`` stays pure (no .env side effects) so tests are hermetic; ``create_app``
loads ``./.env`` first for real launches.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from . import __version__
from .bridge_auth import BridgeTokenVerifier
from .config import Config
from .plugins.registry import PluginRegistry, default_registry
from .token_store import TokenStore

log = logging.getLogger("difference_service.app")


def build_app(config: Config | None = None, *,
              token_store: TokenStore | None = None,
              bridge_verifier: BridgeTokenVerifier | None = None,
              registry: PluginRegistry | None = None) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="difference_service", version=__version__)

    # Capture the caller's IP into a request-scoped contextvar so per-user core
    # calls forward it (core audit source_addr). Trusted-proxy aware: honors
    # FILEENGINE_TRUSTED_PROXIES exactly like the C++ bridges.
    from .core_client import request_source_addr
    from .netutil import client_ip_from_request

    @app.middleware("http")
    async def _capture_client_ip(request, call_next):
        token = request_source_addr.set(client_ip_from_request(request))
        try:
            return await call_next(request)
        finally:
            request_source_addr.reset(token)

    # Route-scoped IP allowlist for the unauthenticated monitoring endpoints.
    # They already bind loopback; when FILEENGINE_MONITORING_ALLOW_IPS is set
    # (comma-separated client IPs), a monitoring request from a non-listed address
    # is refused with 403.
    import os as _os
    from fastapi.responses import JSONResponse as _JSONResponse
    _monitor_allow = {ip.strip() for ip in
                      _os.environ.get("FILEENGINE_MONITORING_ALLOW_IPS", "").split(",") if ip.strip()}

    @app.middleware("http")
    async def _guard_monitoring(request, call_next):
        if _monitor_allow and request.url.path in {"/healthz", "/readyz", "/poolz"}:
            client = request.client.host if request.client else ""
            if client not in _monitor_allow:
                return _JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    # Browser CORS for a SPA on another origin (off unless DIFF_CORS_ORIGINS set).
    # Explicit origins (never "*") so credentialed bearer + X-Tenant requests work.
    if config.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state.config = config
    app.state.token_store = token_store or TokenStore(ttl_seconds=config.token_ttl)
    app.state.bridge_verifier = bridge_verifier or BridgeTokenVerifier(
        config.bridge_url, config.bridge_introspect_ttl, jwt_secret=config.jwt_secret)
    app.state.registry = registry or default_registry(config)

    from .api import router as api_router
    app.include_router(api_router)
    return app


def create_app() -> FastAPI:
    """ASGI factory that loads ``./.env`` then builds the app — for launching via
    ``uvicorn difference_service.app:create_app --factory`` or the
    ``difference-service`` script."""
    from .config import load_dotenv
    load_dotenv()
    return build_app(Config())


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    cfg = app.state.config
    log.info("difference_service %s — http=%s:%s core=%s plugins=%d", __version__,
             cfg.http_host, cfg.http_port, cfg.grpc_address,
             len(app.state.registry.plugins))
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":
    main()
