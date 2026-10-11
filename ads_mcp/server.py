"""Entry point for the Google Ads MCP server.

Transports:
  - stdio (default): for local clients like Claude Code / Claude Desktop.
  - streamable-http: enabled when OAuth is configured or
    GOOGLE_ADS_MCP_TRANSPORT=http, for remote clients such as claude.ai
    custom connectors.

Set GOOGLE_ADS_MCP_READ_ONLY=true to expose only monitoring/reporting
tools and disable everything that mutates the account.
"""

import os

from ads_mcp.coordinator import mcp

# Importing a tool module registers its tools on the shared `mcp` instance.
from ads_mcp.tools import (  # noqa: F401
    accounts,
    audit,
    metadata,
    reporting,
    search,
)


def _is_read_only() -> bool:
    return os.environ.get("GOOGLE_ADS_MCP_READ_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )


if not _is_read_only():
    from ads_mcp.tools import (  # noqa: F401
        ad_groups,
        ads,
        assets,
        budgets,
        campaigns,
        conversions,
        keywords,
        targeting,
    )


def run_server() -> None:
    oauth_configured = os.environ.get(
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID"
    ) and os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
    force_http = (
        os.environ.get("GOOGLE_ADS_MCP_TRANSPORT", "").lower() == "http"
    )
    port = int(os.environ.get("PORT", "8080"))

    if oauth_configured:
        mcp.run(transport="streamable-http", port=port, host="0.0.0.0")
    elif force_http:
        # Without OAuth the endpoint has no login, so it is only served
        # under a long secret path (the URL itself is the password).
        # Suited to free hosts whose disk is wiped on restart, where the
        # OAuth mode would lose its sessions every time the server sleeps.
        secret = os.environ.get("GOOGLE_ADS_MCP_SECRET_PATH", "").strip("/ ")
        if len(secret) < 24:
            raise SystemExit(
                "GOOGLE_ADS_MCP_SECRET_PATH must be set to a random string of "
                "at least 24 characters when running over HTTP without OAuth."
            )
        mcp.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            path=f"/{secret}/mcp",
        )
    else:
        mcp.run()


if __name__ == "__main__":
    run_server()
