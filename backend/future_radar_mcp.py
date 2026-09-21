"""Authenticated Streamable HTTP MCP surface for Future Radar Sync."""

from __future__ import annotations

import asyncio
import secrets
import urllib.parse
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .chatgpt_monitor_ingestion import ChatGPTMonitorIngestionService
from .future_radar.schemas import FrostFireSyncV1


class StaticSyncTokenVerifier(TokenVerifier):
    """Verify the dedicated deployment secret without exposing database credentials."""

    def __init__(self, token: str, resource_url: str):
        self.token = token
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self.token or not secrets.compare_digest(token, self.token):
            return None
        return AccessToken(
            token=token,
            client_id="future-radar-sync",
            scopes=["monitor:sync"],
            resource=self.resource_url,
            subject="authorized-monitor",
        )


def build_future_radar_mcp(
    service: ChatGPTMonitorIngestionService, *, token: str, public_base_url: str,
) -> tuple[MCPServer, Any]:
    resource_url = f"{public_base_url.rstrip('/')}/mcp"
    host = urllib.parse.urlsplit(public_base_url).hostname or "frostfire-ai.onrender.com"
    server = MCPServer(
        name="Future Radar Sync",
        description=(
            "Synchronizes structured recruitment-monitor results into Future Radar, "
            "where they are validated, deduplicated, verified and evaluated using "
            "Future Radar's own priority rules."
        ),
        instructions=(
            "Use sync_monitor_result only for FROSTFIRE_SYNC_V1 recruitment-monitor "
            "payloads. Source-provided tiers are provisional and never override the "
            "Future Radar scoring engine."
        ),
        version="1.0.0",
        token_verifier=StaticSyncTokenVerifier(token, resource_url),
        auth=AuthSettings(
            issuer_url=public_base_url,
            resource_server_url=resource_url,
            required_scopes=["monitor:sync"],
            validate_token_resource=True,
        ),
    )

    @server.tool(
        name="sync_monitor_result",
        title="Sync recruitment monitor result",
        description=(
            "Idempotently validates and imports a FROSTFIRE_SYNC_V1 monitor batch "
            "through Future Radar's controlled ingestion pipeline."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
            idempotentHint=True,
        ),
        structured_output=True,
    )
    async def sync_monitor_result(payload: FrostFireSyncV1) -> dict[str, Any]:
        return await asyncio.to_thread(
            service.ingest,
            payload.model_dump(mode="json"),
            idempotency_key=payload.monitor_run_id or payload.batch_id,
        )

    mcp_app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        max_request_body_size=2 * 1024 * 1024,
        max_sessions=64,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, "localhost", "127.0.0.1", "testserver"],
            allowed_origins=[public_base_url, "http://localhost", "http://127.0.0.1"],
        ),
    )
    return server, mcp_app

