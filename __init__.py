"""captcha-solver-mcp — Hermes plugin wrapper for a containerized CAPTCHA solver.

The plugin does not register Python tools of its own. Its job is:

1. Install/uninstall scripts stand up / tear down the docker compose stack
   (`captcha-solver` container running FastAPI on 127.0.0.1:8899) and merge an
   MCP entry into `~/.hermes/config.yaml` pointing at the host-side stdio shim
   that proxies to the container's HTTP surface.
2. Register the SKILL.md so the agent can consult `captcha-solver-mcp:captcha-solver`
   explicitly, and (via a symlink created at install time) so Hermes's default
   skill discovery picks it up automatically from
   `~/.hermes/skills/mcp/captcha-solver/SKILL.md`.

The actual MCP tool (`solve`) is implemented in `src/mcp_shim.py`.
"""

from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    """Entry point called by Hermes at plugin load."""
    skill_path = Path(__file__).parent / "skills" / "captcha-solver" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            name="captcha-solver",
            path=skill_path,
            description=(
                "How to use the captcha-solver MCP: detect CAPTCHA type, extract "
                "site key, call the solver, inject token or cf_clearance cookie. "
                "Covers reCAPTCHA v2/v3, hCaptcha (incl. Enterprise), Turnstile, "
                "and Cloudflare Managed Challenge. Also covers cost awareness and "
                "failure modes."
            ),
        )
