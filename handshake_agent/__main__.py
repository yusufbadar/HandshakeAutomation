"""CLI entry point for the Handshake Career Pathway labeling agent."""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console

from .agent import AgentConfig, HandshakeLabelAgent


console = Console()


def _parse_args() -> AgentConfig:
    p = argparse.ArgumentParser(
        prog="handshake-agent",
        description=(
            "Apply NYUAD Career Pathway labels to Handshake job postings. "
            "Runs in a real Chromium window you can watch / interrupt."
        ),
    )
    p.add_argument(
        "--user-data-dir",
        default="./.handshake-profile",
        help="Chromium profile dir. Log in once; the session persists here.",
    )
    p.add_argument("--headless", action="store_true", help="Run browser headless.")
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Ask for confirmation before every label is applied.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse jobs and print proposed labels but do NOT change anything.",
    )
    p.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Stop after labeling this many jobs (not counting skips).",
    )
    p.add_argument(
        "--min-confidence",
        type=int,
        default=3,
        help=(
            "Minimum classifier score required to auto-label without asking. "
            "Below this the agent will prompt you."
        ),
    )
    p.add_argument(
        "--slow-mo",
        type=int,
        default=50,
        help="Slow down Playwright actions by this many ms (helps Handshake keep up).",
    )
    p.add_argument(
        "--login-timeout",
        type=int,
        default=600,
        help="How long (seconds) to wait for manual login/2FA before giving up.",
    )
    args = p.parse_args()

    return AgentConfig(
        user_data_dir=args.user_data_dir,
        headless=args.headless,
        confirm=args.confirm,
        dry_run=args.dry_run,
        max_jobs=args.max_jobs,
        min_confidence_to_auto=args.min_confidence,
        slow_mo_ms=args.slow_mo,
        login_timeout_s=args.login_timeout,
    )


def main() -> int:
    cfg = _parse_args()
    agent = HandshakeLabelAgent(cfg)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user.[/yellow]")
    finally:
        console.rule("[bold]Summary[/bold]")
        console.print(agent.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
