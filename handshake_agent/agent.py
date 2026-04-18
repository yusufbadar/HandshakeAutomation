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


HANDSHAKE_JOBS_BASE = "https://app.joinhandshake.com/edu/postings/pending"
HANDSHAKE_JOB_URL = "https://app.joinhandshake.com/jobs/{job_id}"


def jobs_list_url(page_num: int, per_page: int = 25) -> str:
    return f"{HANDSHAKE_JOBS_BASE}?page={page_num}&per_page={per_page}"


HANDSHAKE_JOBS_URL = jobs_list_url(1)

JOBS_PAGE_URL_RE = re.compile(
    r"joinhandshake\.com/(?:edu/)?postings(?:/[a-z_]+)?(?:\?|$|/)",
    re.IGNORECASE,
)


@dataclass
class AgentConfig:
    user_data_dir: str = "./.handshake-profile"
    headless: bool = False
    confirm: bool = False
    yes: bool = False
    dry_run: bool = False
    max_jobs: int | None = None
    min_confidence_to_auto: int = 3
    slow_mo_ms: int = 0
    email: str | None = None
    password: str | None = None
    login_timeout_s: int = 600
    per_page: int = 25
    start_page: int = 1
    max_pages: int = 200


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
        page_num = self.config.start_page
        empty_streak = 0
        while page_num < self.config.start_page + self.config.max_pages:
            if self.config.max_jobs and self.stats.labeled >= self.config.max_jobs:
                console.print("[cyan]Reached max-jobs limit.[/cyan]")
                return

            url = jobs_list_url(page_num, self.config.per_page)
            console.rule(f"[bold]Page {page_num}[/bold] ({url})")
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(
                    "a[href*='/jobs/'], a[href*='/postings/']",
                    state="attached",
                    timeout=8_000,
                )
            except PWTimeoutError:
                pass

            job_ids = await self._collect_job_ids(page)
            fresh_ids = [j for j in job_ids if j not in self.stats.visited_ids]
            console.print(
                f"[dim]Page {page_num}: {len(job_ids)} jobs on page, "
                f"{len(fresh_ids)} not yet visited.[/dim]"
            )

            if not job_ids:
                empty_streak += 1
                if empty_streak >= 2:
                    console.print("[cyan]Two consecutive empty pages – stopping.[/cyan]")
                    return
                page_num += 1
                continue
            empty_streak = 0

            for jid in fresh_ids:
                if self.config.max_jobs and self.stats.labeled >= self.config.max_jobs:
                    console.print("[cyan]Reached max-jobs limit.[/cyan]")
                    return
                self.stats.visited_ids.add(jid)
                await self._open_job(page, jid)
                await self._handle_current_job(page, jid)

            page_num += 1

    async def _collect_job_ids(self, page: Page) -> list[str]:
        """Return the list of unique job IDs from the current postings page,
        preserving row order."""
        try:
            await page.wait_for_selector(
                "a[href*='/jobs/'], a[href*='/postings/']",
                timeout=10_000,
            )
        except PWTimeoutError:
            return []

        hrefs: list[str] = []
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href*='/jobs/'], a[href*='/postings/']",
                "els => els.map(e => e.getAttribute('href') || '')",
            )
        except Exception:
            hrefs = []

        ids: list[str] = []
        seen: set[str] = set()
        for h in hrefs:
            m = re.search(r"/(?:jobs|postings)/(\d+)", h or "")
            if not m:
                continue
            jid = m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            ids.append(jid)
        return ids

    async def _open_job(self, page: Page, job_id: str) -> None:
        url = HANDSHAKE_JOB_URL.format(job_id=job_id)
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                "text=NORMAL LABELS, text=Select a label",
                state="visible",
                timeout=8_000,
            )
        except PWTimeoutError:
            pass

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
            return

        auto_mode = self.config.yes and not self.config.confirm
        needs_prompt = (
            self.config.confirm
            or (not auto_mode and (not result.confident or result.score < self.config.min_confidence_to_auto))
        )

        if needs_prompt:
            console.print(
                "[yellow][y]=apply, [s]=skip, [1..4]=override, [q]=quit[/yellow]"
            )
            choice = (await asyncio.to_thread(input, "> ")).strip().lower()
            if choice == "q":
                raise KeyboardInterrupt()
            if choice == "s" or choice == "n":
                self.stats.skipped_low_confidence += 1
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
            # Detect Handshake's "This label has already been applied"
            # toast, which means the job silently had the label before we
            # got here – treat as a skip, not a success.
            if await self._saw_already_applied_toast(page):
                console.print(
                    "[yellow]Handshake reports this label was already applied – treating as skip.[/yellow]"
                )
                self.stats.skipped_already_labeled += 1
            else:
                console.print(f"[bold green]✓ Applied:[/bold green] {result.label}")
                self.stats.labeled += 1
        except Exception as exc:
            console.print(f"[red]Failed to apply label: {exc}[/red]")
            self.stats.errors += 1

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

    async def _saw_already_applied_toast(self, page: Page) -> bool:
        """Handshake flashes a red 'This label has already been applied'
        toast when you try to re-add an existing label. Quick check."""
        try:
            return (
                await page.get_by_text("already been applied", exact=False).count() > 0
            )
        except Exception:
            return False

    async def _scroll_label_panel_into_view(self, page: Page) -> None:
        """Scroll the 'NORMAL LABELS' panel into view so clicks work."""
        try:
            header = page.get_by_text("NORMAL LABELS", exact=False).first
            if await header.count() > 0:
                await header.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass

    async def _read_existing_labels(self, page: Page) -> list[str]:
        """Read the blue pill chips from the 'NORMAL LABELS' section.

        The pill is a small element with the label text followed by an
        'x' close button. We scan candidate elements in the label panel
        and match anything that looks like a short labelled chip.
        """
        await self._scroll_label_panel_into_view(page)

        labels: list[str] = []
        known_lower = {lbl.lower() for lbl in ALL_LABELS}

        # Try: find every visible element whose inner text matches a known
        # pathway label verbatim. This is the most reliable signal.
        for lbl in ALL_LABELS:
            try:
                loc = page.get_by_text(lbl, exact=True)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=500):
                    labels.append(lbl)
            except Exception:
                continue

        if labels:
            return [l.lower() for l in labels]

        # Fallback: scrape any short text blob inside elements near the
        # NORMAL LABELS header.
        try:
            candidates = await page.locator(
                "xpath=//*[contains(translate(text(),'NORMAL LABELS','normal labels'),"
                "'normal labels')]/following::*[position()<30]"
            ).all_inner_texts()
        except Exception:
            candidates = []

        for t in candidates:
            t = (t or "").strip()
            t = re.sub(r"[\u00d7xX×]\s*$", "", t).strip()
            if not t or len(t) > 120:
                continue
            if t.lower() in known_lower and t not in labels:
                labels.append(t)

        return [l.lower() for l in labels]

    def _label_panel(self, page: Page):
        """Locate the Label panel on the job detail page.

        The panel is uniquely identified by containing the 'Create New
        Label' button, so we anchor everything else inside it.
        """
        return page.locator(
            "xpath=//button[normalize-space()='Create New Label']"
            "/ancestor::*[self::div or self::section][1]"
        ).first

    async def _apply_label(self, page: Page, label: str) -> None:
        """Open the 'Select a label…' chooser, type the label, click the option.

        Handshake's chooser is a custom React combobox in the Label panel
        (bottom-left of the job page, above the 'Create New Label' button):
          - closed state shows 'Select a label…',
          - on click, a visible <input placeholder='Type to search…'>
            appears,
          - option rows show the label name on line 1 and 'Normal Label'
            on line 2.

        Handshake auto-saves; no Save button is needed.
        """
        panel = self._label_panel(page)
        try:
            await panel.wait_for(state="visible", timeout=5_000)
        except PWTimeoutError as exc:
            raise RuntimeError(
                "Could not locate the Label panel on the job page."
            ) from exc
        try:
            await panel.scroll_into_view_if_needed(timeout=1_500)
        except Exception:
            pass

        if not await self._click_select_a_label(page, panel):
            raise RuntimeError("Could not find the 'Select a label...' dropdown.")

        # Scope the search input STRICTLY to the label panel so we never
        # accidentally grab the global 'Search all of Handshake…' bar.
        search = panel.locator("input:visible").first
        try:
            await search.wait_for(state="visible", timeout=4_000)
        except PWTimeoutError as exc:
            raise RuntimeError(
                "Label chooser opened but no search input appeared in the Label panel."
            ) from exc

        try:
            await search.fill("")
            await search.fill(label)
        except Exception as exc:
            raise RuntimeError(f"Could not type into label search: {exc}") from exc

        option_handle = await self._wait_for_label_option(page, label, timeout_ms=4_000)

        if option_handle is None:
            try:
                await search.press("Enter")
            except Exception:
                pass
            await page.wait_for_timeout(300)
            option_handle = await self._wait_for_label_option(page, label, timeout_ms=1_500)

        if option_handle is None:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            raise RuntimeError(f"Dropdown did not offer an option matching {label!r}.")

        try:
            await option_handle.click()
        except Exception:
            try:
                await search.press("Enter")
            except Exception:
                pass

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

        await self._wait_for_label_to_appear(page, label)

    async def _click_select_a_label(self, page: Page, panel) -> bool:
        """Click the 'Select a label...' chooser inside the Label panel."""
        candidates = (
            panel.locator("xpath=.//*[normalize-space()='Select a label...']").first,
            panel.locator("xpath=.//*[contains(normalize-space(),'Select a label')]").first,
            panel.get_by_text("Select a label", exact=False).first,
        )
        for cand in candidates:
            try:
                if not await cand.count():
                    continue
                await cand.scroll_into_view_if_needed(timeout=1_000)
            except Exception:
                pass
            try:
                await cand.click(timeout=1_500)
            except Exception:
                try:
                    handle = await cand.element_handle()
                    if handle is not None:
                        await handle.evaluate("el => el.click()")
                    else:
                        continue
                except Exception:
                    continue
            # Wait briefly for a visible input to appear in the panel.
            try:
                await panel.locator("input:visible").first.wait_for(
                    state="visible", timeout=1_500
                )
                return True
            except PWTimeoutError:
                continue
        return False

    async def _wait_for_label_option(self, page: Page, label: str, timeout_ms: int):
        """Poll up to `timeout_ms` for a dropdown option matching `label`.

        Returns an ElementHandle (stable, non-stale) or None.
        """
        esc = label.replace('"', '\\"')
        selectors = [
            f"li:has-text(\"{esc}\"):has-text(\"Normal Label\")",
            f"[role='option']:has-text(\"{esc}\"):has-text(\"Normal Label\")",
            f"div:has-text(\"{esc}\"):has-text(\"Normal Label\")",
            f"li[role='option']:has-text(\"{esc}\")",
            f"[role='option']:has-text(\"{esc}\")",
            f"li:has-text(\"{esc}\")",
        ]
        deadline_iters = max(1, timeout_ms // 200)
        for _ in range(deadline_iters):
            for sel in selectors:
                try:
                    handle = await page.query_selector(sel)
                except Exception:
                    handle = None
                if handle is None:
                    continue
                try:
                    if await handle.is_visible():
                        return handle
                except Exception:
                    continue
            await page.wait_for_timeout(200)
        return None

    async def _wait_for_label_to_appear(self, page: Page, label: str) -> None:
        """The PDF warns that labels take a moment to show up. Poll briefly
        but don't block the whole run on confirmation."""
        target = label.lower().strip()
        for _ in range(6):
            try:
                if await page.get_by_text(label, exact=True).count() > 0:
                    return
            except Exception:
                pass
            await page.wait_for_timeout(300)

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
