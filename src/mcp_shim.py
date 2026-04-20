#!/usr/bin/env python3
"""Host-side stdio MCP shim that proxies `captcha-solver:solve` tool calls to
the container's FastAPI at http://127.0.0.1:8899.

Runs as a subprocess under Hermes's MCP client. Stays tiny so any failure is
easy to diagnose in `hermes mcp test captcha-solver`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


DEFAULT_URL = "http://127.0.0.1:8899"
SOLVER_URL = os.environ.get("CAPTCHA_SOLVER_URL", DEFAULT_URL).rstrip("/")

server = Server("captcha-solver")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="solve",
            description=(
                "Solve a CAPTCHA challenge. Returns either a response token "
                "(reCAPTCHA/hCaptcha/Turnstile) or a cookies+user_agent bundle "
                "(Cloudflare Managed Challenge). The agent is responsible for "
                "detecting the CAPTCHA type from the page, extracting the "
                "site_key, calling this tool, and injecting the token or "
                "cookies back into its own browser session."
            ),
            inputSchema={
                "type": "object",
                "required": ["type", "site_url"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "recaptcha_v2",
                            "recaptcha_v3",
                            "hcaptcha",
                            "turnstile",
                            "cf_managed",
                        ],
                        "description": "CAPTCHA variety",
                    },
                    "site_url": {
                        "type": "string",
                        "description": "Full URL of the page hosting the CAPTCHA",
                    },
                    "site_key": {
                        "type": "string",
                        "description": (
                            "Public data-sitekey for the CAPTCHA widget. Required "
                            "for all types EXCEPT cf_managed (which is detected "
                            "from the page)."
                        ),
                    },
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 600,
                        "default": 180,
                        "description": "How long the solver waits before giving up.",
                    },
                },
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "solve":
        return [TextContent(type="text", text=f"unknown tool: {name}")]
    try:
        async with httpx.AsyncClient(timeout=arguments.get("timeout_s", 180) + 30) as client:
            resp = await client.post(f"{SOLVER_URL}/solve", json=arguments)
        if resp.status_code >= 400:
            return [TextContent(
                type="text",
                text=f"solver error HTTP {resp.status_code}: {resp.text[:500]}",
            )]
        return [TextContent(type="text", text=resp.text)]
    except httpx.RequestError as exc:
        return [TextContent(
            type="text",
            text=(
                f"failed to reach captcha-solver at {SOLVER_URL}: {exc}. "
                f"Is the docker container running? "
                f"Check with: docker ps | grep captcha-solver"
            ),
        )]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
