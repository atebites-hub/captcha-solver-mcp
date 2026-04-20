#!/usr/bin/env python3
"""Host-side stdio MCP shim that proxies `captcha-solver:solve_tiles` to the
container's FastAPI at http://127.0.0.1:8899.

v0.2.0 — classifier-only contract. The agent drives its own Camofox browser
and hands us per-tile PNGs as base64; we classify and return indices.
"""

from __future__ import annotations

import asyncio
import json
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
            name="solve_tiles",
            description=(
                "Classify CAPTCHA tiles against a target (e.g. 'crosswalks', 'buses'). "
                "Returns which tile indices contain the target. You pass the per-tile "
                "PNGs (typically from browser_screenshot_element on each tile ref); "
                "we run them through a strong VLM and return 0-based indices into "
                "the tile_images list you sent. Use these indices to click the "
                "corresponding refs. If you include instruction_image (a screenshot "
                "of the 'Select all X' bar), we re-read it and override `target` — "
                "useful when reCAPTCHA rewords the challenge mid-flow."
            ),
            inputSchema={
                "type": "object",
                "required": ["target", "tile_images"],
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "The object to find in each tile, lowercase singular "
                                       "or plural (e.g. 'crosswalk', 'buses', 'traffic light').",
                    },
                    "tile_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 64,
                        "description": "Base64-encoded PNG bytes per tile. The order you send "
                                       "is the order we return indices against.",
                    },
                    "instruction_image": {
                        "type": "string",
                        "description": "Optional base64 PNG of the instruction bar. When "
                                       "supplied, we re-read 'Select all images with X' "
                                       "to recover the current target.",
                    },
                },
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "solve_tiles":
        return [TextContent(type="text", text=f"unknown tool: {name}")]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{SOLVER_URL}/solve_tiles", json=arguments)
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
