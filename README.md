# Handshake Career Pathway Labeler

An agent that opens Handshake in a real Chromium window, walks through recent
job postings, and applies **one** of the four NYUAD Career Pathway labels to
each posting that does not already have one:

- `NYUAD Health, Science, and Engineering`
- `NYUAD Media, Arts, and Communications`
- `NYUAD Social Impact and Global Affairs`
- `NYUAD Technology, Business, and Innovation`

It follows the workflow in the **Career Pathway Collections – Step-By-Step
Guide** PDF:

- Goes to Jobs, sorted by **Date Posted** (newest first).
- Opens each posting and reads the title, description, and any required /
  preferred majors.
- Classifies the posting with a major + keyword rule engine that mirrors the
  "Career Pathway Labels & What They Include" table in the guide.
- Adds **exactly one** pathway label via the Labels control.
- Waits for the label to register (the guide notes labels take a moment and
  the screen refreshes) and verifies it appears before moving on.
- **Skips** jobs that already have any of the four pathway labels – so you can
  re-run the agent without worrying about double-labeling.
- Never deletes existing labels and never approves/rejects the posting.
- Labeled jobs stay in the list (as per the guide); the agent uses a
  visited-set so it doesn't loop forever on them.

## 1. One-time setup

```bash
cd HandshakeAutomation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
```

## 2. Log in (first run only)

The agent uses a persistent Chromium profile so you only log in once. There
are two ways to hand it the login:

**Option A – Automatic (non-SSO accounts):** copy `.env.example` to `.env`
and fill in:

```bash
cp .env.example .env
# then edit .env and set HANDSHAKE_EMAIL / HANDSHAKE_PASSWORD
```

`.env` is gitignored – the file never leaves your machine. On launch the
agent will auto-fill those fields on Handshake's login page. If your
account uses institutional SSO, the agent will stop at the SSO screen and
wait for you to finish manually; that's fine.

**Option B – Fully manual:** just run the agent and log in yourself in the
Chromium window that opens.

Either way, kick off a dry run – a Chromium window will open, navigate to
Handshake, and wait for you to sign in (NYU SSO, 2FA, etc.) if needed.

```bash
python3 -m handshake_agent --dry-run --max-jobs 5
```

After you finish signing in, the jobs list should load and the agent will
print the labels it *would* apply (without changing anything).

Your session is stored in `./.handshake-profile/` – keep that folder private.

## 3. Everyday usage

Recommended first real run – the agent proposes each label and you confirm:

```bash
python3 -m handshake_agent --confirm
```

Prompts you'll see per posting:

- `y` – apply the proposed label
- `s` – skip this posting
- `1` / `2` / `3` / `4` – override with Health / Media / Social / Tech
- `q` – quit

Once you trust it, let it run on its own (it will still prompt you on
low-confidence postings):

```bash
python3 -m handshake_agent
```

Useful flags:

| Flag | What it does |
| --- | --- |
| `--dry-run` | Don't change anything, just print proposals |
| `--confirm` | Ask before every label |
| `--max-jobs N` | Stop after labeling N postings |
| `--min-confidence N` | Score threshold below which the agent asks you (default 3) |
| `--headless` | Run without showing the browser (only after you're confident) |
| `--slow-mo 100` | Slow Playwright down if Handshake isn't keeping up |

## 4. How classification works

Scoring per pathway:

- Match on required/preferred majors: +5 per hit
- Match on a keyword in the **title**: +3 per hit
- Match on a keyword in the **description**: +1 per hit

Highest-scoring pathway wins. On ties, priority order is
Tech/Business → Health → Social → Media. If the winner isn't ahead of the
runner-up by at least 2 points (or score < `--min-confidence`), the agent
treats it as **low-confidence** and asks you.

If nothing matches at all, it falls back to Technology, Business &
Innovation and flags it for confirmation – this is usually the right call
for generic corporate postings.

Run the (offline) classifier sanity tests:

```bash
python3 -m tests.test_classifier
```

## 5. Troubleshooting

**"Still not on the jobs page. Waiting up to N seconds..."** – the agent
didn't recognize your current URL as a jobs page. Make sure you are on one
of these pages (Career Services staff view):

- `app.joinhandshake.com/edu/postings/pending`
- `app.joinhandshake.com/edu/postings/in_progress`
- `app.joinhandshake.com/postings`

If you're on an institutional homepage (e.g. `/stu/home`) the agent can't
reach the jobs list automatically – navigate to **Jobs → Job postings** in
the left sidebar yourself, then the agent will pick up.

**Cloudflare "Just a moment..." screen** – this is the automated-browser
check. Solve it once in the window; Playwright's persistent profile will
keep the cookie. If it reappears constantly, try `--slow-mo 150`.

## 6. Notes / known quirks

- The four label strings in `handshake_agent/labels.py` must exactly match
  the labels in Handshake. If NYUAD renames a label, update it there.
- Handshake's DOM is not officially documented; the selectors in
  `handshake_agent/agent.py` are defensive (multiple fallbacks). If
  Handshake redesigns the Labels control, update the selectors in
  `_open_labels_editor`, `_apply_label`, and `_read_existing_labels`.
- The agent never clicks Approve/Reject, never removes labels, and will not
  add a second pathway label to a job that already has one.
- Keep the Chromium window focused – some Handshake dropdowns dismiss
  themselves if the window loses focus.
