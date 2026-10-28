#!/usr/bin/env python3
"""
goth — a minimal full-screen TUI for managing and switching between gcloud configurations.

Problem it solves:
  Juggling several GCP projects across different Google accounts can lead to
  running commands against the wrong project. goth uses gcloud's built-in
  named configurations to let you easily switch between context, credentials,
  and projects safely with a single keypress.

  Google now commonly requires a password again every day before gcloud works.
  goth stores that password locally and re-runs login on a schedule (and when
  the TUI opens) so gcloud stays signed in.

Requirements:
  Python 3.9+, textual, playwright, Chrome/Chromium, and gcloud CLI installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import Screen
    from textual.widgets import DataTable, Footer, Input, Label, Static
except ImportError:
    print("\033[91mError: The 'textual' library is not installed.\033[0m")
    print("Please set up the virtual environment and install it:")
    print("  python3 -m venv .venv")
    print("  source .venv/bin/activate")
    print("  pip install -e .")
    print("\nOr run via the virtual environment's python interpreter directly:")
    print("  ./.venv/bin/python goth")
    sys.exit(1)


# --------------------------------------------------------------------------
# paths and constants
# --------------------------------------------------------------------------

GCLOUD_TIMEOUT_SEC = 30

GOTH_BANNER = (
    " █████   ████   █████  ██  ██\n"
    "██      ██  ██    ██   ██  ██\n"
    "██ ███  ██  ██    ██   █████\n"
    "██  ██  ██  ██    ██   ██  ██\n"
    " ████    ████     ██   ██  ██"
)

CONFIG_DIR = Path.home() / ".config" / "goth"
STATE_PATH = CONFIG_DIR / "state.json"
CHROME_PROFILE_DIR = CONFIG_DIR / "chrome-profile"
STATE_DIR = Path.home() / ".local" / "state" / "goth"
KEEPALIVE_LOG = STATE_DIR / "keepalive.log"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TIMER_NAME = "goth-keepalive"
KEEPALIVE_HOURS = ("07:00", "19:00")

OAUTH_URL_RE = re.compile(r"https://accounts\.google\.com/[^\s]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

EDITABLE_FIELDS = (
    ("account", "account"),
    ("project", "project"),
    ("region", "compute/region"),
    ("zone", "compute/zone"),
)


# --------------------------------------------------------------------------
# gcloud helpers — subprocess execution and data extraction
# --------------------------------------------------------------------------


def check_gcloud_installed() -> bool:
    """Check if gcloud is available on PATH."""
    return shutil.which("gcloud") is not None


def run(cmd: list[str], capture=True, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess command safely with a timeout."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout or GCLOUD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr=f"Timed out after {timeout or GCLOUD_TIMEOUT_SEC}s: {' '.join(cmd)}"
        )


def short_error(msg: str, fallback: str = "command failed") -> str:
    """Pick the useful line from noisy gcloud stderr."""
    text = (msg or "").strip()
    if not text:
        return fallback
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ERROR:"):
            return re.sub(r"^ERROR:\s*(\([^)]+\)\s*)?", "", ln).strip() or fallback
    for ln in reversed(lines):
        if not ln.startswith("WARNING:"):
            return ln
    return lines[-1]


@dataclass
class GConfig:
    name: str
    is_active: bool
    account: str
    project: str
    region: str = "(none)"
    zone: str = "(none)"


def list_configurations() -> tuple[list[GConfig], str | None]:
    """Read all named gcloud configurations. Returns (configs, error)."""
    if not check_gcloud_installed():
        return [], "'gcloud' CLI not found on PATH"
    proc = run(["gcloud", "config", "configurations", "list", "--format=json"])
    if proc.returncode != 0:
        return [], short_error(proc.stderr or proc.stdout, "failed to list configurations")
    try:
        raw = json.loads(proc.stdout or "[]")
    except Exception:
        return [], "failed to parse configurations JSON"

    out = []
    for c in raw:
        props = c.get("properties", {}) or {}
        core = props.get("core", {}) or {}
        compute = props.get("compute", {}) or {}
        out.append(
            GConfig(
                name=c.get("name", "?"),
                is_active=bool(c.get("is_active")),
                account=core.get("account", "") or "(no account)",
                project=core.get("project", "") or "(no project)",
                region=compute.get("region", "") or "(none)",
                zone=compute.get("zone", "") or "(none)",
            )
        )
    return out, None


def authed_accounts() -> set[str]:
    """Accounts gcloud currently holds credentials for."""
    if not check_gcloud_installed():
        return set()
    proc = run(["gcloud", "auth", "list", "--format=json"])
    if proc.returncode != 0:
        proc_all = run(["gcloud", "auth", "list", "--format=value(account)"])
        if proc_all.returncode != 0:
            return set()
        return {line.strip() for line in proc_all.stdout.splitlines() if line.strip()}
    try:
        raw = json.loads(proc.stdout or "[]")
        return {item.get("account") for item in raw if item.get("account")}
    except Exception:
        return set()


def activate_configuration(name: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "configurations", "activate", name])
    return proc.returncode == 0, short_error(proc.stderr or proc.stdout, f"failed to activate {name}")


def set_project(project: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "set", "project", project])
    return proc.returncode == 0, short_error(proc.stderr or proc.stdout, "failed to set project")


def set_config_property(configuration: str, key: str, value: str | None) -> tuple[bool, str]:
    """Set or unset a gcloud property on a named configuration."""
    if value is None:
        proc = run(["gcloud", "config", "unset", key, f"--configuration={configuration}"])
        action = f"unset {key}"
    else:
        proc = run(["gcloud", "config", "set", key, value, f"--configuration={configuration}"])
        action = f"set {key}"
    if proc.returncode == 0:
        return True, ""
    return False, short_error(proc.stderr or proc.stdout, f"failed to {action}")


def create_configuration(name: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "configurations", "create", name])
    return proc.returncode == 0, short_error(proc.stderr or proc.stdout, f"failed to create {name}")


def delete_configuration(name: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "configurations", "delete", name, "--quiet"])
    return proc.returncode == 0, short_error(proc.stderr or proc.stdout, f"failed to delete {name}")


def auth_login_blocking(account: str | None = None) -> int:
    """Run `gcloud auth login` with an inherited terminal."""
    cmd = ["gcloud", "auth", "login"]
    if account and account != "(no account)":
        cmd.append(account)
    return subprocess.run(cmd).returncode


async def run_blocking(fn, *args, **kwargs):
    """Run a blocking callable off the UI event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def auth_state(cfg: GConfig, valid_accounts: set[str]) -> tuple[str, str]:
    """Return (dot, label). ● = credentialed, ○ = not."""
    if cfg.account == "(no account)":
        return "○", "no account"
    if cfg.account in valid_accounts:
        return "●", "ok"
    return "○", "needs login"


def auth_badge(cfg: GConfig, valid_accounts: set[str]) -> Text:
    """Monochrome scannable auth status (filled = ok, hollow = not)."""
    dot, label = auth_state(cfg, valid_accounts)
    return Text(f"{dot} {label}")


# --------------------------------------------------------------------------
# password store and keep-alive
# --------------------------------------------------------------------------


def _empty_state() -> dict:
    return {
        "passwords": {},
        "keepalive": {
            "enabled": False,
            "hours": list(KEEPALIVE_HOURS),
            "last_success": None,
            "last_error": None,
        },
    }


def load_state() -> dict:
    state = _empty_state()
    if not STATE_PATH.is_file():
        return state
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return state
    if not isinstance(raw, dict):
        return state
    passwords = raw.get("passwords") or {}
    if isinstance(passwords, dict):
        state["passwords"] = {
            str(k).strip(): str(v) for k, v in passwords.items() if str(k).strip() and str(v)
        }
    keepalive = raw.get("keepalive") or {}
    if isinstance(keepalive, dict):
        state["keepalive"].update({k: v for k, v in keepalive.items() if k in state["keepalive"]})
    return state


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONFIG_DIR.stat().st_mode & 0o077:
        os.chmod(CONFIG_DIR, 0o700)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_PATH)
    os.chmod(STATE_PATH, 0o600)


def stored_accounts() -> list[str]:
    return sorted(load_state()["passwords"].keys())


def set_stored_password(account: str, password: str) -> None:
    state = load_state()
    state["passwords"][account.strip()] = password
    state["keepalive"]["enabled"] = True
    save_state(state)


def delete_stored_password(account: str) -> bool:
    state = load_state()
    removed = state["passwords"].pop(account.strip(), None) is not None
    if not state["passwords"]:
        state["keepalive"]["enabled"] = False
    if removed:
        save_state(state)
    return removed


def record_keepalive_result(ok: bool, message: str) -> None:
    state = load_state()
    now = datetime.now().isoformat(timespec="seconds")
    if ok:
        state["keepalive"]["last_success"] = now
        state["keepalive"]["last_error"] = None
    else:
        state["keepalive"]["last_error"] = f"{now} {message}"
    save_state(state)


def keepalive_status_text() -> str:
    state = load_state()
    accounts = list(state["passwords"])
    ka = state["keepalive"]
    if not accounts:
        return "no google password stored"
    hours = ", ".join(ka.get("hours") or KEEPALIVE_HOURS)
    last = ka.get("last_success") or "never"
    err = ka.get("last_error")
    bits = [f"{len(accounts)} account{'s' if len(accounts) != 1 else ''}", f"timer {hours}", f"last ok {last}"]
    if err:
        bits.append(f"last error {err}")
    return "  ·  ".join(bits)


def _append_keepalive_log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with KEEPALIVE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {line}\n")


def goth_command() -> list[str]:
    """Absolute command used by the systemd/cron timer."""
    script = Path(__file__).resolve()
    which = shutil.which("goth")
    if which:
        return [str(Path(which).resolve()), "keepalive"]
    return [sys.executable, str(script), "keepalive"]


def _ensure_display() -> None:
    if os.environ.get("DISPLAY"):
        return
    if Path("/tmp/.X11-unix/X0").exists():
        os.environ["DISPLAY"] = ":0"
    xauth = Path.home() / ".Xauthority"
    if xauth.is_file() and not os.environ.get("XAUTHORITY"):
        os.environ["XAUTHORITY"] = str(xauth)


def install_keepalive_timer() -> tuple[bool, str]:
    """Install a twice-daily user timer (systemd, else cron)."""
    cmd = goth_command()
    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    display = os.environ.get("DISPLAY", ":0")
    xauth = os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority"))

    if shutil.which("systemctl"):
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(_systemd_quote(part) for part in cmd)
        service = f"""[Unit]
Description=goth gcloud keep-alive login

[Service]
Type=oneshot
ExecStart={exec_start}
Environment=PATH={path_env}
Environment=DISPLAY={display}
Environment=XAUTHORITY={xauth}
Environment=HOME={Path.home()}
"""
        hours = KEEPALIVE_HOURS
        on_calendar = "\n".join(f"OnCalendar=*-*-* {h}:00" for h in hours)
        timer = f"""[Unit]
Description=Run goth keep-alive twice daily

[Timer]
{on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""
        (SYSTEMD_USER_DIR / f"{TIMER_NAME}.service").write_text(service, encoding="utf-8")
        (SYSTEMD_USER_DIR / f"{TIMER_NAME}.timer").write_text(timer, encoding="utf-8")
        reload = run(["systemctl", "--user", "daemon-reload"], timeout=8)
        enable = run(["systemctl", "--user", "enable", "--now", f"{TIMER_NAME}.timer"], timeout=8)
        if reload.returncode == 0 and enable.returncode == 0:
            return True, f"keep-alive timer installed ({' and '.join(hours)})"
        cron_ok, cron_msg = _install_cron(cmd)
        if cron_ok:
            return True, cron_msg
        return False, short_error(enable.stderr or reload.stderr, "failed to enable systemd timer")

    return _install_cron(cmd)


def uninstall_keepalive_timer() -> tuple[bool, str]:
    messages = []
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "disable", "--now", f"{TIMER_NAME}.timer"])
        for suffix in (".timer", ".service"):
            path = SYSTEMD_USER_DIR / f"{TIMER_NAME}{suffix}"
            if path.exists():
                path.unlink()
        run(["systemctl", "--user", "daemon-reload"])
        messages.append("removed systemd timer")
    _uninstall_cron()
    if not messages:
        messages.append("removed cron keep-alive if it existed")
    return True, "; ".join(messages)


def _systemd_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _cron_line(cmd: list[str]) -> str:
    joined = " ".join(_systemd_quote(part) for part in cmd)
    hours = ",".join(str(int(h.split(":")[0])) for h in KEEPALIVE_HOURS)
    return f"0 {hours} * * * {joined} >> {KEEPALIVE_LOG} 2>&1"


def _is_keepalive_cron(line: str) -> bool:
    return "goth" in line and "keepalive" in line


def _install_cron(cmd: list[str]) -> tuple[bool, str]:
    if not shutil.which("crontab"):
        return False, "neither systemd --user nor crontab is available"
    proc = run(["crontab", "-l"])
    existing = proc.stdout if proc.returncode == 0 else ""
    lines = [ln for ln in existing.splitlines() if not _is_keepalive_cron(ln)]
    lines.append(_cron_line(cmd))
    text = "\n".join(lines).rstrip() + "\n"
    try:
        subprocess.run(["crontab", "-"], input=text, text=True, check=True, timeout=10)
    except Exception as exc:
        return False, f"failed to install crontab: {exc}"
    return True, f"keep-alive cron installed ({' and '.join(KEEPALIVE_HOURS)})"


def _uninstall_cron() -> None:
    if not shutil.which("crontab"):
        return
    proc = run(["crontab", "-l"])
    if proc.returncode != 0:
        return
    lines = [ln for ln in proc.stdout.splitlines() if not _is_keepalive_cron(ln)]
    text = ("\n".join(lines).rstrip() + "\n") if lines else ""
    try:
        subprocess.run(["crontab", "-"], input=text, text=True, check=True, timeout=10)
    except Exception:
        pass


def _extract_oauth_code(page) -> str | None:
    try:
        text = page.inner_text("body", timeout=2000)
    except Exception:
        text = ""
    match = re.search(r"\b(4/[A-Za-z0-9_+\-/]+)\b", text)
    if match:
        return match.group(1)
    try:
        for loc in page.locator("input, textarea").all():
            try:
                val = (loc.input_value(timeout=500) or "").strip()
            except Exception:
                continue
            if val.startswith("4/"):
                return val
    except Exception:
        pass
    return None


def _playwright_complete_login(url: str, account: str, password: str) -> tuple[str | None, str | None]:
    """Drive Google's OAuth page. Returns (error, authorization_code)."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright is not installed — pip install playwright && playwright install chromium", None

    _ensure_display()
    headed = bool(os.environ.get("DISPLAY"))
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def launch(p):
        errors = []
        args = ["--disable-blink-features=AutomationControlled"]
        for channel in ("chrome", "google-chrome", "msedge", None):
            try:
                kwargs = {
                    "headless": not headed,
                    "args": args,
                    "ignore_default_args": ["--enable-automation"],
                }
                if channel:
                    kwargs["channel"] = channel
                return p.chromium.launch_persistent_context(str(CHROME_PROFILE_DIR), **kwargs)
            except Exception as exc:
                errors.append(f"{channel or 'chromium'}: {exc}")
        raise RuntimeError("; ".join(errors))

    try:
        with sync_playwright() as p:
            context = launch(p)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 120
            filled_password = False
            while time.time() < deadline:
                current = page.url
                if re.search(r"https?://(localhost|127\.0\.0\.1)", current):
                    page.wait_for_timeout(1500)
                    context.close()
                    return None, None

                body = ""
                try:
                    body = page.inner_text("body", timeout=2000).lower()
                except Exception:
                    pass

                if (
                    filled_password
                    or "approval" in current
                    or "authorization code" in body
                    or "copy this code" in body
                ):
                    code = _extract_oauth_code(page)
                    if code:
                        context.close()
                        return None, code

                if any(token in body for token in ("2-step", "2-step verification", "verify it’s you", "verify it's you", "authenticator")):
                    context.close()
                    return "google asked for 2-step verification — keep-alive cannot finish unattended", None

                if any(token in current.lower() for token in ("okta", "saml", "onelogin", "auth0")):
                    context.close()
                    return "SSO/SAML login is not supported for keep-alive", None

                if "browser or app may not be secure" in body:
                    context.close()
                    return "google blocked the automated browser — try again from a desktop session", None

                chooser = page.locator(f'div[data-identifier="{account}"]')
                if chooser.count() > 0:
                    chooser.first.click()
                    page.wait_for_timeout(800)
                    continue

                email = page.locator('input[type="email"]').first
                if email.count() > 0 and email.is_visible():
                    email.fill(account)
                    next_btn = page.locator("#identifierNext, button:has-text('Next')").first
                    if next_btn.count() > 0:
                        next_btn.click()
                    else:
                        email.press("Enter")
                    page.wait_for_timeout(1200)
                    continue

                pw = page.locator('input[name="Passwd"], input[type="password"]').first
                if pw.count() > 0 and pw.is_visible():
                    pw.fill(password)
                    filled_password = True
                    next_btn = page.locator("#passwordNext, button:has-text('Next')").first
                    if next_btn.count() > 0:
                        next_btn.click()
                    else:
                        pw.press("Enter")
                    page.wait_for_timeout(1500)
                    continue

                for label in ("Allow", "Continue", "I agree", "Yes"):
                    btn = page.locator(f'button:has-text("{label}")').first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(800)
                        break
                else:
                    page.wait_for_timeout(500)

            context.close()
            if filled_password:
                return "timed out waiting for google oauth to finish after password", None
            return "timed out on the google login page", None
    except PlaywrightTimeout:
        return "timed out talking to the google login page", None
    except Exception as exc:
        return f"browser login failed: {exc}", None


def auth_login_with_password(account: str, password: str, *, update_adc: bool = False) -> tuple[bool, str]:
    """Complete `gcloud auth login` by filling the Google password in a browser."""
    if not check_gcloud_installed():
        return False, "'gcloud' CLI not found on PATH"
    cmd = [
        "gcloud",
        "auth",
        "login",
        account,
        "--force",
        "--no-activate",
        "--no-launch-browser",
    ]
    if update_adc:
        cmd.append("--update-adc")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    url = None
    leftover: list[str] = []

    def drain() -> None:
        try:
            rest = proc.stdout.read()
            if rest:
                leftover.append(rest)
        except Exception:
            pass

    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        clean = ANSI_RE.sub("", line).strip()
        match = OAUTH_URL_RE.search(clean)
        if match:
            url = match.group(0).rstrip(").,]\"'")
            break
    threading.Thread(target=drain, daemon=True).start()

    if not url:
        try:
            proc.kill()
        except Exception:
            pass
        extra = "".join(leftover)
        return False, short_error(extra, "could not get google login url from gcloud")

    err, code = _playwright_complete_login(url, account, password)
    if err:
        try:
            proc.kill()
        except Exception:
            pass
        return False, err

    if code and proc.stdin is not None:
        try:
            proc.stdin.write(code + "\n")
            proc.stdin.close()
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            return False, f"could not send authorization code to gcloud: {exc}"

    try:
        rc = proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "gcloud auth login timed out after browser login"
    if rc != 0:
        extra = "".join(leftover)
        return False, short_error(extra, "gcloud auth login failed")
    return True, "ok"


def run_keepalive(accounts: list[str] | None = None, quiet: bool = False) -> int:
    """Re-login every stored Google account. Used by the timer and the TUI."""
    def emit(message: str) -> None:
        _append_keepalive_log(message)
        if not quiet:
            print(message)

    _ensure_display()
    state = load_state()
    passwords = state["passwords"]
    if accounts:
        targets = [(acct, passwords.get(acct)) for acct in accounts]
    else:
        targets = list(passwords.items())
    if not targets:
        msg = "no google password stored — open goth and choose configure password"
        emit(msg)
        return 1

    configs, _ = list_configurations()
    active = next((c for c in configs if c.is_active), None)
    active_account = active.account if active else None

    overall_ok = True
    last_error = "keep-alive failed"
    for account, password in targets:
        if not password:
            overall_ok = False
            last_error = f"no password stored for {account}"
            emit(last_error)
            continue
        emit(f"renewing gcloud login for {account}")
        update_adc = account == active_account
        ok, msg = auth_login_with_password(account, password, update_adc=update_adc)
        if ok:
            emit(f"ok {account}")
        else:
            overall_ok = False
            last_error = f"{account}: {msg}"
            emit(f"failed {last_error}")

    record_keepalive_result(overall_ok, last_error)
    return 0 if overall_ok else 1


# --------------------------------------------------------------------------
# shared TUI helpers
# --------------------------------------------------------------------------

APP_CSS = """
    Screen {
        background: #000000;
        color: #ffffff;
        layers: base overlay;
    }

    .header {
        height: auto;
        padding: 1 2 0 2;
        background: #000000;
    }

    .title-art {
        color: #ffffff;
        text-style: bold;
        width: auto;
        height: auto;
    }

    .status {
        height: 1;
        margin: 0 2;
        padding: 0 1;
        color: #c0c0c0;
        background: #000000;
        border-bottom: solid #404040;
    }

    .body-row {
        height: 1fr;
        padding: 1 2;
        background: #000000;
    }

    .table-pane {
        width: 2fr;
        height: 1fr;
        padding-right: 1;
    }

    .details-pane {
        width: 1fr;
        height: 1fr;
        min-width: 28;
        border-left: solid #404040;
        padding: 0 0 0 2;
        background: #000000;
    }

    .details-title {
        width: 100%;
        height: 1;
        color: #000000;
        background: #ffffff;
        text-style: bold;
        padding: 0 1;
    }

    .details-body {
        color: #ffffff;
        height: 1fr;
        padding-top: 1;
    }

    DataTable {
        height: 1fr;
        background: #000000;
        color: #ffffff;
        border: none;
    }

    DataTable > .datatable--header {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }

    DataTable > .datatable--hover {
        background: #1a1a1a;
        color: #ffffff;
    }

    DataTable > .datatable--odd-row {
        background: #000000;
    }

    DataTable > .datatable--even-row {
        background: #0d0d0d;
    }

    #empty {
        display: none;
        height: auto;
        color: #808080;
        text-align: left;
        padding: 1 0;
    }

    #empty.visible {
        display: block;
    }

    Footer {
        background: #000000;
        color: #808080;
        dock: bottom;
        layer: base;
    }

    Footer > .footer--key {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }

    Footer > .footer--description {
        background: #000000;
        color: #ffffff;
        text-style: none;
    }

    Footer > .footer--highlight {
        background: #ffffff;
        color: #000000;
    }

    .prompt-dock {
        display: none;
        dock: bottom;
        layer: overlay;
        width: 100%;
        height: auto;
        align: center middle;
        background: transparent;
        padding-bottom: 2;
    }

    .prompt-dock.visible {
        display: block;
    }

    .prompt-bar {
        width: 56;
        max-width: 90%;
        height: auto;
        background: #000000;
        color: #ffffff;
        padding: 0 1;
        border: solid #ffffff;
    }

    .prompt-label {
        width: 1fr;
        height: 1;
        color: #ffffff;
        text-style: bold;
        background: #000000;
        padding: 0 1;
    }

    .prompt-input {
        width: 1fr;
        height: 1;
        min-height: 1;
        background: #ffffff;
        color: #000000;
        border: none;
        padding: 0 1;
        background-tint: 0%;
        text-style: bold;
    }

    .prompt-input:focus {
        border: none;
        background: #ffffff;
        color: #000000;
        background-tint: 0%;
        text-style: bold;
    }

    .prompt-input > .input--cursor {
        background: #000000;
        color: #ffffff;
        text-style: bold;
    }

    .prompt-input > .input--placeholder {
        color: #666666;
        text-style: none;
    }

    .prompt-input > .input--selection {
        background: #000000;
        color: #ffffff;
    }
"""


class PromptMixin:
    """Bottom overlay prompt shared by home and switcher screens."""

    _prompt_future: asyncio.Future[str | None] | None
    _prompting: bool
    _prompt_message: str
    _prompt_secret: bool
    _busy: bool

    def _init_prompt(self) -> None:
        self._prompt_future = None
        self._prompting = False
        self._prompt_message = ""
        self._prompt_secret = False
        self._busy = False

    def _prompt_value_from_input(self) -> str | None:
        prompt = self.query_one(".prompt-input", Input)
        raw = prompt.value
        if self._prompt_secret:
            return raw if raw else None
        return raw.strip()

    def _submit_open_prompt(self) -> bool:
        """Complete the overlay prompt. Esc still cancels via None."""
        if not self._prompting:
            return False
        self._resolve_prompt(self._prompt_value_from_input())
        return True

    async def _ask(self, message: str, *, secret: bool = False) -> str | None:
        if self._prompting:
            return None

        dock = self.query_one(".prompt-dock", Vertical)
        label = self.query_one(".prompt-label", Label)
        prompt = self.query_one(".prompt-input", Input)
        table = self.query_one(DataTable)

        self._prompting = True
        self._prompt_secret = secret
        self._prompt_message = message
        label.update(message)
        prompt.password = secret
        prompt.value = ""
        prompt.styles.background = "#ffffff"
        prompt.styles.color = "#000000"
        table.disabled = True
        dock.add_class("visible")
        self.call_after_refresh(prompt.focus)

        loop = asyncio.get_running_loop()
        self._prompt_future = loop.create_future()
        try:
            return await self._prompt_future
        finally:
            self._prompt_future = None
            self._prompting = False
            self._prompt_secret = False
            prompt.password = False
            dock.remove_class("visible")
            prompt.value = ""
            label.update("")
            table.disabled = False
            table.focus()

    def _resolve_prompt(self, value: str | None) -> None:
        if self._prompt_future is not None and not self._prompt_future.done():
            self._prompt_future.set_result(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not event.input.has_class("prompt-input") or not self._prompting:
            return
        typed = event.value
        base = self._prompt_message
        label = self.query_one(".prompt-label", Label)
        if not typed:
            label.update(base)
            return
        if self._prompt_secret:
            label.update(f"{base}  {'•' * len(typed)}")
        else:
            label.update(f"{base}  {typed}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not event.input.has_class("prompt-input") or not self._prompting:
            return
        event.stop()
        self._submit_open_prompt()


def _prompt_dock() -> Vertical:
    return Vertical(
        Vertical(
            Label("", classes="prompt-label"),
            Input(classes="prompt-input", placeholder="type here…", compact=True),
            classes="prompt-bar",
        ),
        classes="prompt-dock",
    )


def parse_edit_value(raw: str | None, current: str, *, optional: bool) -> tuple[bool, str | None]:
    """Return (changed, new_value_or_None_to_unset). Empty keeps current."""
    if raw is None:
        return False, current
    value = raw.strip()
    if value == "":
        return False, current
    if value in ("-", "none", "(none)"):
        if optional:
            unset = current in ("(none)", "(no account)", "(no project)", "")
            return (not unset, None)
        return False, current
    if value == current:
        return False, current
    return True, value


# --------------------------------------------------------------------------
# screens
# --------------------------------------------------------------------------


class HomeScreen(PromptMixin, Screen):
    """Start menu: configurations or configure password."""

    BINDINGS = [
        Binding("enter", "select", "select"),
        Binding("d", "delete_password", "delete password"),
        Binding("k", "run_keepalive", "login now"),
        Binding("q", "app.quit", "quit"),
        Binding("escape", "escape", "quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._init_prompt()
        self._columns_ready = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="header"):
            yield Label(GOTH_BANNER, classes="title-art")
        yield Static("loading…", classes="status", markup=False)
        with Horizontal(classes="body-row"):
            with Vertical(classes="table-pane"):
                yield DataTable(id="home-table", cursor_type="row", zebra_stripes=True)
            with Vertical(classes="details-pane"):
                yield Label("goth", classes="details-title")
                yield Static("select an option", classes="details-body", markup=False)
        yield _prompt_dock()
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        table = self.query_one("#home-table", DataTable)
        table.add_columns("option", "what it does")
        self._columns_ready = True
        self._render_menu()
        table.focus()
        if stored_accounts():
            self.run_worker(self._keepalive_on_start(), exclusive=True)

    def _set_status(self, message: str) -> None:
        self.query_one(".status", Static).update(message)

    def _selected_option(self) -> str | None:
        table = self.query_one("#home-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def _render_menu(self, prefer: str | None = None) -> None:
        table = self.query_one("#home-table", DataTable)
        table.clear()
        accounts = stored_accounts()
        pw_note = (
            f"{len(accounts)} google account{'s' if len(accounts) != 1 else ''} saved"
            if accounts
            else "store google password for daily re-login"
        )
        table.add_row(Text("configurations", style="bold"), "switch projects, edit region / zone / account", key="configurations")
        table.add_row(Text("configure password"), pw_note, key="password")
        restore = 0 if prefer != "password" else 1
        if table.row_count:
            table.move_cursor(row=restore)
        self._render_details()
        self._set_status(keepalive_status_text())

    def _render_details(self) -> None:
        title = self.query_one(".details-title", Label)
        body = self.query_one(".details-body", Static)
        option = self._selected_option()
        details = Text()
        if option == "password":
            title.update("configure password")
            accounts = stored_accounts()
            details.append("store your google password so goth can\n")
            details.append("renew gcloud login every morning and night.\n\n")
            if accounts:
                details.append("saved accounts\n", style="bold")
                for acct in accounts:
                    details.append(f"  {acct}\n")
            else:
                details.append("no password stored yet\n", style="dim")
            details.append("\nenter  save / update password\n")
            details.append("d      delete a stored password\n")
            details.append("k      login now with stored password\n")
        else:
            title.update("configurations")
            details.append("named gcloud configurations:\n")
            details.append("account, project, region, zone.\n\n")
            details.append("enter opens the switcher.\n")
            details.append("in the switcher, press e to edit.\n")
        body.update(details)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "home-table":
            return
        self._render_details()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._prompting or self._busy:
            return
        if event.data_table.id != "home-table":
            return
        event.stop()
        self.action_select()

    def action_escape(self) -> None:
        if self._prompting:
            self._resolve_prompt(None)
        else:
            self.app.exit()

    def action_select(self) -> None:
        if self._submit_open_prompt():
            return
        if self._busy:
            return
        option = self._selected_option()
        if option == "configurations":
            self.app.push_screen(SwitcherScreen())
        elif option == "password":
            self._busy = True
            self.run_worker(self._password_flow(), exclusive=True)

    def action_delete_password(self) -> None:
        if self._prompting or self._busy:
            return
        if self._selected_option() != "password":
            return
        self.run_worker(self._delete_password_flow(), exclusive=True)

    def action_run_keepalive(self) -> None:
        if self._prompting or self._busy:
            return
        if not stored_accounts():
            self._set_status("no google password stored — select configure password")
            return
        self.run_worker(self._keepalive_now(), exclusive=True)

    async def _password_flow(self) -> None:
        self._busy = True
        try:
            default = next(iter(stored_accounts()), "")
            if not default:
                configs, _ = await run_blocking(list_configurations)
                for cfg in configs:
                    if cfg.account not in ("(no account)", ""):
                        default = cfg.account
                        break
            hint = f" (enter for {default})" if default else ""
            email = await self._ask(f"google account email{hint}:")
            if email is None:
                self._set_status("password cancelled")
                return
            email = email or default
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                self._set_status("invalid email — type the address or press enter to use the default")
                return

            password = await self._ask(f"google password for {email}:", secret=True)
            if password is None:
                self._set_status("password cancelled")
                return
            if not password:
                self._set_status("password was empty — not saved")
                return

            self._set_status(f"saving password for {email}…")
            try:
                await run_blocking(set_stored_password, email, password)
            except Exception as exc:
                self._set_status(f"could not save password: {exc}")
                return

            self._render_menu(prefer="password")
            self._set_status(f"saved password for {email} — installing keep-alive timer…")
            try:
                ok, msg = await run_blocking(install_keepalive_timer)
            except Exception as exc:
                ok, msg = False, str(exc)
            self._render_menu(prefer="password")
            if ok:
                self._set_status(f"saved {email}  ·  {msg}  ·  press k to login now")
            else:
                self._set_status(f"saved {email}  ·  timer skipped: {msg}  ·  press k to login now")
        finally:
            self._busy = False

    async def _delete_password_flow(self) -> None:
        accounts = stored_accounts()
        if not accounts:
            self._set_status("no stored password to delete")
            return
        hint = accounts[0] if len(accounts) == 1 else "email"
        email = await self._ask(f"delete stored password for [{hint}]:")
        if email is None:
            self._set_status("delete cancelled")
            return
        if email == "" and len(accounts) == 1:
            email = accounts[0]
        if email not in accounts:
            self._set_status("that email has no stored password")
            return
        removed = await run_blocking(delete_stored_password, email)
        if removed and not stored_accounts():
            await run_blocking(uninstall_keepalive_timer)
        self._render_menu(prefer="password")
        self._set_status(f"deleted stored password for {email}" if removed else "nothing deleted")

    async def _keepalive_on_start(self) -> None:
        self._set_status("renewing gcp login with stored password…")
        await self._keepalive_now()

    async def _keepalive_now(self, accounts: list[str] | None = None) -> None:
        owned = not self._busy
        if owned:
            self._busy = True
        self._set_status("logging in to google for gcloud…")
        try:
            rc = await run_blocking(run_keepalive, accounts, quiet=True)
            self._render_menu(prefer=self._selected_option())
            if rc == 0:
                self._set_status("gcp login renewed — gcloud should stay signed in")
            else:
                self._set_status(f"keep-alive failed  ·  {keepalive_status_text()}")
        finally:
            if owned:
                self._busy = False


class SwitcherScreen(PromptMixin, Screen):
    """Named gcloud configuration switcher."""

    BINDINGS = [
        Binding("enter", "activate", "activate"),
        Binding("n", "new_config", "new"),
        Binding("e", "edit_config", "edit"),
        Binding("d", "delete_config", "delete"),
        Binding("r", "refresh", "refresh"),
        Binding("q", "app.quit", "quit"),
        Binding("escape", "escape", "home"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._init_prompt()
        self.configs: list[GConfig] = []
        self.valid_accounts: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="header"):
            yield Label(GOTH_BANNER, classes="title-art")
        yield Static("loading…", classes="status", markup=False)
        with Horizontal(classes="body-row"):
            with Vertical(classes="table-pane"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static("no configurations yet — press n to create one", id="empty")
            with Vertical(classes="details-pane"):
                yield Label("selected configuration", classes="details-title")
                yield Static("select a configuration", classes="details-body", markup=False)
        yield _prompt_dock()
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        if not check_gcloud_installed():
            self.query_one(".status", Static).update(
                "error: 'gcloud' cli not found on path — install google cloud sdk first"
            )
            return

        table = self.query_one("#table", DataTable)
        table.add_columns("", "configuration", "account", "project", "auth")
        self.run_worker(self._refresh_async(announce=False), exclusive=True)
        table.focus()

    def action_refresh(self) -> None:
        if self._prompting or self._busy:
            return
        self.run_worker(self._refresh_async(announce=True), exclusive=True)

    async def _refresh_async(self, announce: bool = False) -> None:
        owned_busy = not self._busy
        if owned_busy:
            self._busy = True
        status = self.query_one(".status", Static)
        if announce:
            status.update("refreshing…")

        try:
            selected = self._selected_config_name()
            configs, err = await run_blocking(list_configurations)
            accounts = await run_blocking(authed_accounts)

            if err:
                status.update(f"refresh failed: {err}")
                return

            self.configs = configs
            self.valid_accounts = accounts
            self._render_table(prefer_name=selected)
            self._render_details(self._selected_config())
            if announce:
                n = len(self.configs)
                status.update(f"refreshed — {n} configuration{'s' if n != 1 else ''}")
            else:
                self._render_status()
        finally:
            if owned_busy:
                self._busy = False

    def _render_status(self) -> None:
        active = next((c for c in self.configs if c.is_active), None)
        status = self.query_one(".status", Static)
        if active:
            status.update(
                f"active  {active.name}  ·  {active.account}  ·  {active.project}"
            )
        else:
            status.update("no active configuration")

    def _render_table(self, prefer_name: str | None = None) -> None:
        table = self.query_one("#table", DataTable)
        empty = self.query_one("#empty", Static)
        table.clear()

        if not self.configs:
            empty.add_class("visible")
            self._render_details(None)
            return

        empty.remove_class("visible")
        restore_index = 0
        for i, c in enumerate(self.configs):
            if c.is_active:
                marker = Text("▸", style="bold")
                name = Text(c.name, style="bold")
                account = Text(c.account, style="bold")
                project = Text(c.project, style="bold")
                auth = auth_badge(c, self.valid_accounts)
                auth.stylize("bold")
            else:
                marker = Text(" ")
                name = Text(c.name)
                account = Text(c.account)
                project = Text(c.project)
                auth = auth_badge(c, self.valid_accounts)

            table.add_row(marker, name, account, project, auth, key=c.name)
            if prefer_name and c.name == prefer_name:
                restore_index = i
            elif c.is_active and not prefer_name:
                restore_index = i

        if table.row_count:
            table.move_cursor(row=restore_index)

    def _render_details(self, cfg: GConfig | None) -> None:
        title = self.query_one(".details-title", Label)
        body = self.query_one(".details-body", Static)
        if cfg is None:
            title.update("selected configuration")
            body.update(Text("select a configuration", style="dim"))
            return

        dot, auth_label = auth_state(cfg, self.valid_accounts)
        state = "active" if cfg.is_active else "inactive"
        title.update(Text(cfg.name, style="bold"))

        details = Text()

        def add_field(label: str, value: str, *, dim_if_none: bool = False) -> None:
            details.append(f"{label:<10}", style="dim")
            style = "dim" if dim_if_none and value == "(none)" else ""
            details.append(value, style=style)
            details.append("\n")

        add_field("account", cfg.account)
        add_field("project", cfg.project)
        add_field("region", cfg.region, dim_if_none=True)
        add_field("zone", cfg.zone, dim_if_none=True)
        add_field("auth", f"{dot} {auth_label}")
        add_field("state", state)
        details.append("\npress e to edit account, project, region, zone", style="dim")
        body.update(details)

    def _selected_config_name(self) -> str | None:
        cfg = self._selected_config()
        return cfg.name if cfg else None

    def _selected_config(self) -> GConfig | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return next((c for c in self.configs if c.name == row_key.value), None)
        except Exception:
            return None

    def _set_status(self, message: str) -> None:
        self.query_one(".status", Static).update(message)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "table":
            return
        self._render_details(self._selected_config())

    def action_escape(self) -> None:
        if self._prompting:
            self._resolve_prompt(None)
        else:
            self.app.pop_screen()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._prompting or self._busy:
            return
        if event.data_table.id != "table":
            return
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        if self._submit_open_prompt():
            return
        if self._busy:
            return
        cfg = self._selected_config()
        if cfg is None:
            self._set_status("nothing to activate — press n to create one")
            return
        if cfg.is_active:
            self._set_status(f"already active: {cfg.name}")
            return
        self.run_worker(self._activate_flow(cfg), exclusive=True)

    async def _login_account(self, account: str | None) -> int:
        if account and account != "(no account)":
            password = load_state()["passwords"].get(account)
            if password:
                self._set_status(f"logging in {account} with stored password…")
                ok, msg = await run_blocking(auth_login_with_password, account, password, update_adc=True)
                if ok:
                    return 0
                self._set_status(f"stored-password login failed: {msg} — opening browser…")
        with self.app.suspend():
            return auth_login_blocking(account if account != "(no account)" else None)

    async def _activate_flow(self, cfg: GConfig) -> None:
        self._busy = True
        self._set_status(f"activating {cfg.name}…")
        try:
            ok, msg = await run_blocking(activate_configuration, cfg.name)
            if not ok:
                self._set_status(f"failed to activate {cfg.name}: {msg}")
                return

            self.valid_accounts = await run_blocking(authed_accounts)
            if cfg.account not in self.valid_accounts and cfg.account != "(no account)":
                self._set_status(f"{cfg.account} needs re-auth…")
                await self._login_account(cfg.account)
                self.valid_accounts = await run_blocking(authed_accounts)

            await self._refresh_async(announce=False)
            active = next((c for c in self.configs if c.name == cfg.name), cfg)
            self._set_status(
                f"switched to {active.name}  ·  {active.account}  ·  {active.project}"
            )
        finally:
            self._busy = False

    def action_new_config(self) -> None:
        if self._prompting or self._busy:
            return
        self.run_worker(self._new_config_flow(), exclusive=True)

    async def _apply_optional_property(self, name: str, key: str, prompt: str) -> bool:
        value = await self._ask(prompt)
        if value is None:
            return False
        if not value or value in ("-", "none"):
            return True
        ok, msg = await run_blocking(set_config_property, name, key, value)
        if not ok:
            self._set_status(f"could not set {key}: {msg}")
            return False
        return True

    async def _new_config_flow(self) -> None:
        name = await self._ask("new configuration name:")
        if not name:
            self._set_status("create cancelled")
            return

        if not re.fullmatch(r"[A-Za-z0-9][-A-Za-z0-9_]*", name):
            self._set_status(
                "invalid name — letters, numbers, hyphens, underscores; start alphanumeric"
            )
            return

        self._busy = True
        self._set_status(f"creating '{name}'…")
        try:
            ok, msg = await run_blocking(create_configuration, name)
            if not ok:
                self._set_status(f"could not create '{name}': {msg}")
                return

            account = await self._ask(f"google account for '{name}' (enter to pick in browser):")
            if account is None:
                self._set_status("create cancelled")
                await self._refresh_async(announce=False)
                return
            if account:
                ok, msg = await run_blocking(set_config_property, name, "account", account)
                if not ok:
                    self._set_status(f"could not set account: {msg}")
                    await self._refresh_async(announce=False)
                    return

            self._set_status(f"created '{name}' — signing in…")
            rc = await self._login_account(account or None)
            if rc != 0:
                self._set_status(f"auth login failed/cancelled for '{name}'")
                await self._refresh_async(announce=False)
                return

            project = await self._ask(f"gcp project id for '{name}' (enter to skip):")
            if project is None:
                await self._refresh_async(announce=False)
                self._set_status(f"'{name}' created — remaining fields cancelled")
                return
            if project:
                ok, msg = await run_blocking(set_config_property, name, "project", project)
                if not ok:
                    self._set_status(f"could not set project: {msg}")
                    await self._refresh_async(announce=False)
                    return

            if not await self._apply_optional_property(
                name, "compute/region", f"compute region for '{name}' (enter to skip):"
            ):
                await self._refresh_async(announce=False)
                return
            if not await self._apply_optional_property(
                name, "compute/zone", f"compute zone for '{name}' (enter to skip):"
            ):
                await self._refresh_async(announce=False)
                return

            await self._refresh_async(announce=False)
            self._set_status(f"'{name}' is ready and active")
        finally:
            self._busy = False

    def action_edit_config(self) -> None:
        if self._prompting or self._busy:
            return
        cfg = self._selected_config()
        if cfg is None:
            self._set_status("nothing to edit")
            return
        self.run_worker(self._edit_flow(cfg), exclusive=True)

    async def _edit_flow(self, cfg: GConfig) -> None:
        current = {
            "account": cfg.account,
            "project": cfg.project,
            "region": cfg.region,
            "zone": cfg.zone,
        }
        changed = []
        for label, key in EDITABLE_FIELDS:
            shown = current[label]
            optional = label in ("region", "zone", "account", "project")
            raw = await self._ask(f"{label} [{shown}] (enter keep, - unset):")
            if raw is None:
                self._set_status("edit cancelled")
                return
            did, value = parse_edit_value(raw, shown, optional=optional)
            if not did:
                continue
            ok, msg = await run_blocking(set_config_property, cfg.name, key, value)
            if not ok:
                self._set_status(f"could not update {label}: {msg}")
                await self._refresh_async(announce=False)
                return
            changed.append(label)

        await self._refresh_async(announce=False)
        if changed:
            self._set_status(f"updated {cfg.name}: {', '.join(changed)}")
        else:
            self._set_status("no changes")

    def action_delete_config(self) -> None:
        if self._prompting or self._busy:
            return
        cfg = self._selected_config()
        if cfg is None:
            self._set_status("nothing to delete")
            return
        self.run_worker(self._delete_flow(cfg), exclusive=True)

    async def _delete_flow(self, cfg: GConfig) -> None:
        if len(self.configs) == 1:
            self._set_status("can't delete the only configuration — create another first")
            return

        answer = await self._ask(f"delete '{cfg.name}'? [y/n]:")
        if answer is None:
            self._set_status("delete cancelled")
            return
        if answer.lower() not in ("y", "yes"):
            self._set_status("delete cancelled")
            return

        self._busy = True
        self._set_status(f"deleting '{cfg.name}'…")
        try:
            if cfg.is_active:
                other = next((c for c in self.configs if c.name != cfg.name), None)
                if other is None:
                    self._set_status("can't delete the only configuration")
                    return
                ok, msg = await run_blocking(activate_configuration, other.name)
                if not ok:
                    self._set_status(f"could not switch away before delete: {msg}")
                    return

            ok, msg = await run_blocking(delete_configuration, cfg.name)
            await self._refresh_async(announce=False)
            if ok:
                self._set_status(f"deleted '{cfg.name}'")
            else:
                self._set_status(f"delete failed: {msg}")
        finally:
            self._busy = False


class GothApp(App):
    """Minimal black-and-white full-screen TUI for gcloud configurations."""

    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS

    def on_mount(self) -> None:
        self.title = "goth"
        self.push_screen(HomeScreen())


def _print_help() -> None:
    print(GOTH_BANNER)
    print("\ngoth — minimal full-screen tui for gcloud configurations\n")
    print("usage:")
    print("  goth                 start the interactive switcher")
    print("  goth keepalive       renew gcloud login with the stored password")
    print("  goth -h, --help      show this help message")
    print("\nstart menu:")
    print("  configurations       switch and edit named gcloud configs")
    print("  configure password   save google password; keep gcloud signed in")
    print("\nkeybindings (switcher):")
    print("  enter        activate the selected configuration")
    print("  n            create a new configuration (bottom prompt)")
    print("  e            edit account, project, region, zone")
    print("  d            delete the selected configuration (y/n prompt)")
    print("  r            refresh the configuration list")
    print("  q            quit")
    print("  esc          back to start menu (cancels an open prompt)")


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("-h", "--help", "help"):
            _print_help()
            sys.exit(0)
        if arg in ("keepalive", "--keepalive", "reauth"):
            sys.exit(run_keepalive())
        if arg in ("install-timer",):
            ok, msg = install_keepalive_timer()
            print(msg)
            sys.exit(0 if ok else 1)
        print(f"unknown command: {arg}", file=sys.stderr)
        _print_help()
        sys.exit(2)

    GothApp().run()


if __name__ == "__main__":
    main()
