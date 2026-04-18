"""Browser automation for NYUAD Handshake Career Pathway labeling.

The agent connects to a user-launched Chromium session (via Playwright's
persistent context), so the operator only needs to log into Handshake (with
NYU SSO / 2FA) once. After that the agent:

1. Opens the Jobs tab sorted by Date Posted.
2. Walks each job row.
3. Opens the job, reads title / description / required majors.
4. Skips the job if it already has ANY of the four NYUAD Career Pathway
   labels (prevents double-labeling).
5. Otherwise classifies the posting and adds exactly one label via the
   Labels dropdown.
6. Waits for the label to register (the page typically refreshes), then
   moves on.

Because the PDF notes that:
  - The screen refreshes after labeling.
  - It takes a moment for the label to appear.
  - Jobs do NOT get removed from the list after labeling.
...we always re-query job rows from scratch per iteration and use a
visited-set of job IDs so we don't loop forever.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)
from rich.console import Console

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from .classifier import Classification, classify
from .labels import ALL_LABELS


console = Console()


HANDSHAKE_JOBS_URL = (
    "https://app.joinhandshake.com/edu/postings/pending?page=1&per_page=25"
)

JOBS_PAGE_URL_RE = re.compile(
    r"joinhandshake\.com/(?:edu/)?postings(?:/[a-z_]+)?(?:\?|$|/)",
    re.IGNORECASE,
)


@dataclass
class AgentConfig:
    user_data_dir: str = "./.handshake-profile"
    headless: bool = False
    confirm: bool = False
    dry_run: bool = False
    max_jobs: int | None = None
    min_confidence_to_auto: int = 3
    slow_mo_ms: int = 50
    email: str | None = None
    password: str | None = None
    login_timeout_s: int = 600


@dataclass
class AgentStats:
    scanned: int = 0
    labeled: int = 0
    skipped_already_labeled: int = 0
    skipped_low_confidence: int = 0
    errors: int = 0
    visited_ids: set[str] = field(default_factory=set)


class HandshakeLabelAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.stats = AgentStats()

    async def run(self) -> AgentStats:
        async with async_playwright() as pw:
            context: BrowserContext = await pw.chromium.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
                viewport={"width": 1440, "height": 900},
                accept_downloads=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()

            await self._ensure_logged_in(page)
            await self._process_jobs(page)

            await context.close()
        return self.stats

    async def _ensure_logged_in(self, page: Page) -> None:
        console.print("[cyan]Opening Handshake jobs list...[/cyan]")
        await page.goto(HANDSHAKE_JOBS_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeoutError:
            pass

        if self._on_jobs_page(page):
            console.print("[green]Logged in to Handshake.[/green]")
            return

        email = self.config.email or os.environ.get("HANDSHAKE_EMAIL")
        password = self.config.password or os.environ.get("HANDSHAKE_PASSWORD")

        if email and password:
            console.print("[cyan]Attempting automatic login with credentials from env...[/cyan]")
            try:
                await self._attempt_autologin(page, email, password)
            except Exception as exc:
                console.print(f"[yellow]Auto-login attempt failed: {exc}[/yellow]")

        if self._on_jobs_page(page):
            console.print("[green]Logged in to Handshake.[/green]")
            return

        console.print(
            "[yellow]Still not on the jobs page. If a 2FA/SSO prompt is showing,"
            " finish it in the Chromium window. Waiting up to"
            f" {self.config.login_timeout_s} seconds...[/yellow]"
        )
        deadline = self.config.login_timeout_s
        waited = 0
        while waited < deadline:
            await page.wait_for_timeout(1_500)
            waited += 2
            if self._on_jobs_page(page):
                break
        else:
            raise RuntimeError(
                "Timed out waiting for Handshake login. Current URL: " + page.url
            )
        console.print("[green]Logged in to Handshake.[/green]")
        if "/edu/postings" not in page.url:
            try:
                await page.goto(HANDSHAKE_JOBS_URL, wait_until="domcontentloaded")
            except Exception:
                pass

    def _on_jobs_page(self, page: Page) -> bool:
        url = page.url
        if "login" in url:
            return False
        return bool(JOBS_PAGE_URL_RE.search(url))

    async def _attempt_autologin(self, page: Page, email: str, password: str) -> None:
        """Best-effort: fill Handshake's employer / career-services login form.

        Handshake's login screen has evolved; we try a few selectors and fall
        back silently if the form doesn't match. If a school SSO redirect
        happens, we leave it to the operator to complete.
        """
        email_selectors = (
            "input[name='user[email]']",
            "input[type='email']",
            "input#email-address-identifier",
            "input[name='email']",
            "input[autocomplete='username']",
        )
        password_selectors = (
            "input[name='user[password]']",
            "input[type='password']",
            "input[autocomplete='current-password']",
        )

        email_input = None
        for sel in email_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=2_000):
                    email_input = loc
                    break
            except Exception:
                continue
        if email_input is None:
            raise RuntimeError("Could not find an email input on the login page.")

        await email_input.fill(email)

        for sel in (
            "button[type='submit']",
            "button:has-text('Next')",
            "button:has-text('Continue')",
        ):
            btn = page.locator(sel).first
            try:
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    break
            except Exception:
                continue

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeoutError:
            pass

        if self._on_jobs_page(page):
            return

        password_input = None
        for sel in password_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=4_000):
                    password_input = loc
                    break
            except Exception:
                continue
        if password_input is None:
            console.print(
                "[yellow]Password field not visible – this account probably uses"
                " institutional SSO. Finish login in the Chromium window.[/yellow]"
            )
            return

        await password_input.fill(password)
        for sel in (
            "button[type='submit']",
            "button:has-text('Sign in')",
            "button:has-text('Log in')",
        ):
            btn = page.locator(sel).first
            try:
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    break
            except Exception:
                continue

        try:
            await page.wait_for_url(
                re.compile(r"app\.joinhandshake\.com/postings"), timeout=20_000
            )
        except PWTimeoutError:
            pass

    async def _process_jobs(self, page: Page) -> None:
        while True:
            if self.config.max_jobs and self.stats.labeled >= self.config.max_jobs:
                console.print("[cyan]Reached max-jobs limit.[/cyan]")
                return

            row = await self._find_next_unvisited_job_row(page)
            if row is None:
                console.print("[cyan]No more unvisited jobs on this page.[/cyan]")
                if not await self._go_to_next_page(page):
                    return
                continue

            job_id = await self._job_row_id(row)
            if not job_id:
                await row.click()
            else:
                self.stats.visited_ids.add(job_id)

            try:
                await row.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeoutError:
                pass

            await self._handle_current_job(page, job_id or "")

    async def _find_next_unvisited_job_row(self, page: Page):
        try:
            await page.wait_for_selector(
                "a[href*='/jobs/'], a[href*='/postings/'], tr[data-hook='postings-table-row']",
                timeout=10_000,
            )
        except PWTimeoutError:
            return None

        rows = await page.locator(
            "a[href*='/jobs/'], a[href*='/postings/']"
        ).element_handles()
        for r in rows:
            href = await r.get_attribute("href") or ""
            m = re.search(r"/(?:jobs|postings)/(\d+)", href)
            if not m:
                continue
            jid = m.group(1)
            if jid in self.stats.visited_ids:
                continue
            return r
        return None

    async def _job_row_id(self, row) -> str | None:
        href = await row.get_attribute("href") or ""
        m = re.search(r"/(?:jobs|postings)/(\d+)", href)
        return m.group(1) if m else None

    async def _go_to_next_page(self, page: Page) -> bool:
        for sel in (
            "a[aria-label='Next page']",
            "button[aria-label='Next page']",
            "a:has-text('Next')",
            "button:has-text('Next')",
        ):
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    await loc.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                    return True
            except Exception:
                continue
        return False

    async def _handle_current_job(self, page: Page, job_id: str) -> None:
        self.stats.scanned += 1

        try:
            title = await self._read_title(page)
            description = await self._read_description(page)
            majors = await self._read_majors(page)
            existing_labels = await self._read_existing_labels(page)
        except Exception as exc:
            console.print(f"[red]Error reading job {job_id}: {exc}[/red]")
            self.stats.errors += 1
            await self._back_to_list(page)
            return

        pathway_labels_on_job = [lbl for lbl in existing_labels if lbl in ALL_LABELS]

        console.rule(f"[bold]Job {job_id}[/bold] – {title[:80]}")
        console.print(f"[dim]Existing labels:[/dim] {existing_labels or '—'}")
        if majors:
            console.print(f"[dim]Required majors:[/dim] {majors}")

        if pathway_labels_on_job:
            console.print(
                f"[yellow]Already has pathway label ({pathway_labels_on_job[0]}). Skipping.[/yellow]"
            )
            self.stats.skipped_already_labeled += 1
            await self._back_to_list(page)
            return

        result = classify(title=title, description=description, majors=majors)
        console.print(
            f"[green]→ Proposed label:[/green] {result.label}  "
            f"[dim](score {result.score}, "
            f"runner-up: {result.runner_up or '—'}={result.runner_up_score})[/dim]"
        )
        for reason in result.reasons:
            console.print(f"   [dim]· {reason}[/dim]")

        if self.config.dry_run:
            console.print("[magenta]DRY RUN – not applying label.[/magenta]")
            await self._back_to_list(page)
            return

        if self.config.confirm or not result.confident or result.score < self.config.min_confidence_to_auto:
            console.print(
                "[yellow]Low-confidence or --confirm mode.[/yellow] "
                "[y]=apply, [s]=skip, [1..4]=override, [q]=quit"
            )
            choice = (await asyncio.to_thread(input, "> ")).strip().lower()
            if choice == "q":
                raise KeyboardInterrupt()
            if choice == "s" or choice == "n":
                self.stats.skipped_low_confidence += 1
                await self._back_to_list(page)
                return
            if choice in {"1", "2", "3", "4"}:
                result = Classification(
                    label=ALL_LABELS[int(choice) - 1],
                    score=result.score,
                    reasons=["manual override"],
                    runner_up=result.runner_up,
                    runner_up_score=result.runner_up_score,
                )

        try:
            await self._apply_label(page, result.label)
            console.print(f"[bold green]✓ Applied:[/bold green] {result.label}")
            self.stats.labeled += 1
        except Exception as exc:
            console.print(f"[red]Failed to apply label: {exc}[/red]")
            self.stats.errors += 1

        await self._back_to_list(page)

    async def _back_to_list(self, page: Page) -> None:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
        except PWTimeoutError:
            await page.goto(HANDSHAKE_JOBS_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeoutError:
            pass

    async def _read_title(self, page: Page) -> str:
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        m = re.search(r"#\d+\s+([^\n]+?)\s+at\s+[^\n]+", body)
        if m:
            return m.group(1).strip()
        for sel in (
            "h1:not(:has-text('Jobs'))",
            "[data-hook='job-title']",
            "header h1, header h2",
            "h1",
        ):
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    txt = (await loc.inner_text()).strip()
                    if txt and txt.lower() not in {"jobs", "job"}:
                        return txt
            except Exception:
                continue
        return ""

    async def _read_description(self, page: Page) -> str:
        for sel in (
            "[data-hook='job-description']",
            "section:has(h2:has-text('Description'))",
            "section:has(h3:has-text('Description'))",
            "div[class*='description']",
            "main",
        ):
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    txt = (await loc.inner_text()).strip()
                    if len(txt) > 80:
                        return txt
            except Exception:
                continue
        try:
            return (await page.inner_text("body")).strip()
        except Exception:
            return ""

    async def _read_majors(self, page: Page) -> str:
        body = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            return ""
        m = re.search(
            r"(required majors?|preferred majors?|majors?)[:\s]+(.{0,400}?)(\n\n|\r\n\r\n|$)",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(2)).strip()
        return ""

    def _label_panel_selector(self) -> str:
        # The Label panel contains a "NORMAL LABELS" header + a Select2
        # dropdown whose placeholder is "Select a label...". We anchor on
        # either of those so we can scope other lookups to just that panel.
        return (
            "div:has(> *:has-text('NORMAL LABELS')), "
            "section:has(*:has-text('NORMAL LABELS')), "
            "div:has(.select2-choice:has-text('Select a label'))"
        )

    async def _read_existing_labels(self, page: Page) -> list[str]:
        """Read the blue 'pill' chips from the NORMAL LABELS section."""
        labels: list[str] = []

        # The pills live inside a Select2 multi-select, so the visible chip
        # text is typically `<li class="select2-search-choice">LABEL<a>×</a></li>`.
        pill_selectors = (
            "ul.select2-choices li.select2-search-choice",
            "li.select2-search-choice",
            "div:has(> *:has-text('NORMAL LABELS')) li.select2-search-choice",
        )
        for sel in pill_selectors:
            try:
                items = await page.locator(sel).all_inner_texts()
            except Exception:
                items = []
            for t in items:
                # Pill text often ends with an '×' remove control.
                t = re.sub(r"[\u00d7xX×]\s*$", "", t or "").strip()
                if t and t not in labels:
                    labels.append(t)
            if labels:
                break

        # Handshake may also show labels as simple badge spans elsewhere.
        if not labels:
            fallback_selectors = (
                "div:has(> *:has-text('NORMAL LABELS')) span[style*='background']",
                "div:has(> *:has-text('NORMAL LABELS')) span.badge",
            )
            for sel in fallback_selectors:
                try:
                    items = await page.locator(sel).all_inner_texts()
                except Exception:
                    items = []
                for t in items:
                    t = (t or "").strip()
                    if t and t not in labels:
                        labels.append(t)
                if labels:
                    break

        return [lbl.lower() for lbl in labels if lbl]

    async def _apply_label(self, page: Page, label: str) -> None:
        """Apply one label via the 'Select a label...' dropdown.

        Handshake uses a Select2 combobox. Clicking it reveals a search
        input (often attached to document.body), into which we type the
        label's lowercase name. The matching option in the list gets
        clicked. Handshake auto-saves – no button needs pressing.
        """
        await page.wait_for_timeout(400)

        combobox = page.locator(
            "a.select2-choice:has-text('Select a label'), "
            ".select2-container:has-text('Select a label')"
        ).first
        if not await combobox.is_visible():
            combobox = page.get_by_text("Select a label...", exact=False).first
            if not await combobox.is_visible():
                raise RuntimeError("Could not find the 'Select a label...' dropdown.")
        await combobox.click()
        await page.wait_for_timeout(300)

        search = page.locator(
            ".select2-drop-active input.select2-input, "
            "input.select2-input:visible, "
            ".select2-search input:visible"
        ).first
        await search.wait_for(state="visible", timeout=5_000)
        await search.fill("")
        await search.type(label, delay=10)
        await page.wait_for_timeout(700)

        option_selectors = (
            f".select2-drop-active .select2-results li:has-text(\"{label}\")",
            f".select2-results li:has-text(\"{label}\")",
            f"li.select2-result-selectable:has-text(\"{label}\")",
        )
        option = None
        for sel in option_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=2_000):
                    option = loc
                    break
            except Exception:
                continue

        if option is None:
            # Fallback: some skins render options with matched substrings
            # wrapped in <span class="select2-match">…</span>, so the
            # simple :has-text check above can fail for hyphenated labels.
            # Try pressing Enter to pick the first result.
            await page.keyboard.press("Enter")
        else:
            await option.click()

        await page.wait_for_timeout(1_200)

        # Close the dropdown if it is still open; Handshake auto-saves.
        try:
            if await page.locator(".select2-drop-active").is_visible():
                await page.keyboard.press("Escape")
        except Exception:
            pass

        await self._wait_for_label_to_appear(page, label)

    async def _wait_for_label_to_appear(self, page: Page, label: str) -> None:
        """The PDF warns that labels take a moment to show up and the page
        refreshes. Poll for up to ~15s until we see the label."""
        target = label.lower().strip()
        for _ in range(30):
            current = [c.lower().strip() for c in await self._read_existing_labels(page)]
            if target in current:
                return
            try:
                await page.wait_for_load_state("networkidle", timeout=1_000)
            except PWTimeoutError:
                pass
            await page.wait_for_timeout(500)

    # ------------------------------------------------------------------

    def summary(self) -> str:
        s = self.stats
        return (
            f"Scanned: {s.scanned}\n"
            f"Labeled: {s.labeled}\n"
            f"Skipped (already labeled): {s.skipped_already_labeled}\n"
            f"Skipped (manual): {s.skipped_low_confidence}\n"
            f"Errors: {s.errors}"
        )
