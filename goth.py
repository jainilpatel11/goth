#!/usr/bin/env python3
"""
goth — a minimal full-screen TUI for managing and switching between gcloud configurations.

Problem it solves:
  Juggling several GCP projects across different Google accounts can lead to
  running commands against the wrong project. goth uses gcloud's built-in
  named configurations to let you easily switch between context, credentials,
  and projects safely with a single keypress.

Requirements:
  Python 3.9+, textual, and gcloud CLI installed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import DataTable, Footer, Input, Label, Static
except ImportError:
    print("\033[91mError: The 'textual' library is not installed.\033[0m")
    print("Please set up the virtual environment and install it:")
    print("  python3 -m venv .venv")
    print("  source .venv/bin/activate")
    print("  pip install textual")
    print("\nOr run via the virtual environment's python interpreter directly:")
    print("  ./.venv/bin/python goth")
    sys.exit(1)


# --------------------------------------------------------------------------
# gcloud helpers — subprocess execution and data extraction
# --------------------------------------------------------------------------

GCLOUD_TIMEOUT_SEC = 30

# Solid block wordmark — crisp letterforms, no dither static.
GOTH_BANNER = (
    " █████   ████   █████  ██  ██\n"
    "██      ██  ██    ██   ██  ██\n"
    "██ ███  ██  ██    ██   █████\n"
    "██  ██  ██  ██    ██   ██  ██\n"
    " ████    ████     ██   ██  ██"
)


def check_gcloud_installed() -> bool:
    """Check if gcloud is available on PATH."""
    return shutil.which("gcloud") is not None


def run(cmd: list[str], capture=True) -> subprocess.CompletedProcess:
    """Run a subprocess command safely with a timeout."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=GCLOUD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr=f"Timed out after {GCLOUD_TIMEOUT_SEC}s: {' '.join(cmd)}"
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


async def run_blocking(fn, *args):
    """Run a blocking callable off the UI event loop."""
    return await asyncio.to_thread(fn, *args)


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
# Main App
# --------------------------------------------------------------------------

class GothApp(App):
    """Minimal black-and-white full-screen TUI for gcloud configurations."""

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #000000;
        color: #ffffff;
        layers: base overlay;
    }

    #header {
        height: auto;
        padding: 1 2 0 2;
        background: #000000;
    }

    #title-art {
        color: #ffffff;
        text-style: bold;
        width: auto;
        height: auto;
    }

    #status {
        height: 1;
        margin: 0 2;
        padding: 0 1;
        color: #c0c0c0;
        background: #000000;
        border-bottom: solid #404040;
    }

    #body {
        height: 1fr;
        padding: 1 2;
        background: #000000;
    }

    #table-pane {
        width: 2fr;
        height: 1fr;
        padding-right: 1;
    }

    #details-pane {
        width: 1fr;
        height: 1fr;
        min-width: 28;
        border-left: solid #404040;
        padding: 0 0 0 2;
        background: #000000;
    }

    #details-title {
        width: 100%;
        height: 1;
        color: #000000;
        background: #ffffff;
        text-style: bold;
        padding: 0 1;
    }

    #details-body {
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

    /* Centered compact prompt above the footer */
    #prompt-dock {
        display: none;
        dock: bottom;
        layer: overlay;
        width: 100%;
        height: auto;
        align: center middle;
        background: transparent;
        padding-bottom: 2;
    }

    #prompt-dock.visible {
        display: block;
    }

    #prompt-bar {
        width: 56;
        max-width: 90%;
        height: auto;
        background: #000000;
        color: #ffffff;
        padding: 0 1;
        border: solid #ffffff;
    }

    #prompt-label {
        width: 1fr;
        height: 1;
        color: #ffffff;
        text-style: bold;
        background: #000000;
        padding: 0 1;
    }

    #prompt {
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

    #prompt:focus {
        border: none;
        background: #ffffff;
        color: #000000;
        background-tint: 0%;
        text-style: bold;
    }

    #prompt > .input--cursor {
        background: #000000;
        color: #ffffff;
        text-style: bold;
    }

    #prompt > .input--placeholder {
        color: #666666;
        text-style: none;
    }

    #prompt > .input--selection {
        background: #000000;
        color: #ffffff;
    }
    """

    BINDINGS = [
        Binding("enter", "activate", "activate"),
        Binding("n", "new_config", "new"),
        Binding("d", "delete_config", "delete"),
        Binding("r", "refresh", "refresh"),
        Binding("q", "quit", "quit"),
        Binding("escape", "escape", "cancel", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.configs: list[GConfig] = []
        self.valid_accounts: set[str] = set()
        self._prompt_future: asyncio.Future[str | None] | None = None
        self._prompting = False
        self._prompt_message = ""
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="header"):
            yield Label(GOTH_BANNER, id="title-art")
        yield Static("loading…", id="status", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="table-pane"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static("no configurations yet — press n to create one", id="empty")
            with Vertical(id="details-pane"):
                yield Label("selected configuration", id="details-title")
                yield Static("select a configuration", id="details-body", markup=False)
        with Vertical(id="prompt-dock"):
            with Vertical(id="prompt-bar"):
                yield Label("", id="prompt-label")
                yield Input(id="prompt", placeholder="type here…", compact=True)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.title = "goth"

        if not check_gcloud_installed():
            self.query_one("#status", Static).update(
                "error: 'gcloud' cli not found on path — install google cloud sdk first"
            )
            return

        table = self.query_one(DataTable)
        table.add_columns("", "configuration", "account", "project", "auth")
        self.run_worker(self._refresh_async(announce=False), exclusive=True)
        table.focus()

    # -- data refresh ----------------------------------------------------

    def action_refresh(self) -> None:
        if self._prompting or self._busy:
            return
        self.run_worker(self._refresh_async(announce=True), exclusive=True)

    async def _refresh_async(self, announce: bool = False) -> None:
        owned_busy = not self._busy
        if owned_busy:
            self._busy = True
        status = self.query_one("#status", Static)
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
        status = self.query_one("#status", Static)
        if active:
            status.update(
                f"active  {active.name}  ·  {active.account}  ·  {active.project}"
            )
        else:
            status.update("no active configuration")

    def _render_table(self, prefer_name: str | None = None) -> None:
        table = self.query_one(DataTable)
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
        title = self.query_one("#details-title", Label)
        body = self.query_one("#details-body", Static)
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
        body.update(details)

    def _selected_config_name(self) -> str | None:
        cfg = self._selected_config()
        return cfg.name if cfg else None

    def _selected_config(self) -> GConfig | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return next((c for c in self.configs if c.name == row_key.value), None)
        except Exception:
            return None

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "table":
            return
        self._render_details(self._selected_config())

    # -- overlay prompt --------------------------------------------------

    async def _ask(self, message: str) -> str | None:
        """Show centered bottom prompt; Enter submits, Esc cancels."""
        if self._prompting:
            return None

        dock = self.query_one("#prompt-dock", Vertical)
        label = self.query_one("#prompt-label", Label)
        prompt = self.query_one("#prompt", Input)

        self._prompting = True
        self._prompt_message = message
        label.update(message)
        prompt.value = ""
        prompt.styles.background = "#ffffff"
        prompt.styles.color = "#000000"
        dock.add_class("visible")
        prompt.focus()

        loop = asyncio.get_running_loop()
        self._prompt_future = loop.create_future()
        try:
            return await self._prompt_future
        finally:
            self._prompt_future = None
            self._prompting = False
            dock.remove_class("visible")
            prompt.value = ""
            label.update("")
            self.query_one(DataTable).focus()

    def _resolve_prompt(self, value: str | None) -> None:
        if self._prompt_future is not None and not self._prompt_future.done():
            self._prompt_future.set_result(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Echo typed text into the label so input is always readable."""
        if event.input.id != "prompt" or not self._prompting:
            return
        typed = event.value
        base = self._prompt_message
        if typed:
            self.query_one("#prompt-label", Label).update(f"{base}  {typed}")
        else:
            self.query_one("#prompt-label", Label).update(base)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt" or not self._prompting:
            return
        event.stop()
        self._resolve_prompt(event.value.strip() or None)

    def action_escape(self) -> None:
        if self._prompting:
            self._resolve_prompt(None)
        else:
            self.exit()

    # -- actions -----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._prompting or self._busy:
            return
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        if self._prompting or self._busy:
            return
        cfg = self._selected_config()
        if cfg is None:
            self._set_status("nothing to activate — press n to create one")
            return
        if cfg.is_active:
            self._set_status(f"already active: {cfg.name}")
            return
        self.run_worker(self._activate_flow(cfg), exclusive=True)

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
                self._set_status(f"{cfg.account} needs re-auth — launching gcloud auth login…")
                with self.suspend():
                    auth_login_blocking(cfg.account)
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

            self._set_status(f"created '{name}' — launching gcloud auth login…")
            with self.suspend():
                rc = auth_login_blocking()
            if rc != 0:
                self._set_status(f"auth login failed/cancelled for '{name}'")
                await self._refresh_async(announce=False)
                return

            project = await self._ask(f"gcp project id for '{name}' (enter to skip):")
            if project:
                ok, msg = await run_blocking(set_project, project)
                if not ok:
                    self._set_status(f"could not set project: {msg}")
                    await self._refresh_async(announce=False)
                    return

            await self._refresh_async(announce=False)
            self._set_status(f"'{name}' is ready and active")
        finally:
            self._busy = False

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


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help"):
        print(GOTH_BANNER)
        print("\ngoth — minimal full-screen tui for gcloud configurations\n")
        print("usage:")
        print("  goth              start the interactive switcher")
        print("  goth -h, --help   show this help message")
        print("\nkeybindings:")
        print("  enter        activate the selected configuration")
        print("  n            create a new configuration (bottom prompt)")
        print("  d            delete the selected configuration (y/n prompt)")
        print("  r            refresh the configuration list")
        print("  q / esc      quit (esc cancels an open prompt)")
        sys.exit(0)

    GothApp().run()


if __name__ == "__main__":
    main()
