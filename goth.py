#!/usr/bin/env python3
"""
goth — a gothic-themed TUI for managing and switching between gcloud configurations.

Problem it solves:
  Juggling several GCP projects across different Google accounts can lead to
  running commands against the wrong project. goth uses gcloud's built-in 
  named configurations to let you easily switch between context, credentials,
  and projects safely with a single keypress.

Requirements:
  Python 3.9+, textual, and gcloud CLI installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

try:
    from textual.app import App, ComposeResult
    from textual.containers import Vertical, Horizontal
    from textual.screen import Screen, ModalScreen
    from textual.widgets import DataTable, Footer, Header, Input, Static, Label
    from textual.binding import Binding
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

def check_gcloud_installed() -> bool:
    """Check if gcloud is available on PATH."""
    return shutil.which("gcloud") is not None


def run(cmd: list[str], capture=True) -> subprocess.CompletedProcess:
    """Run a subprocess command safely."""
    return subprocess.run(cmd, capture_output=capture, text=True)


@dataclass
class GConfig:
    name: str
    is_active: bool
    account: str
    project: str


def list_configurations() -> list[GConfig]:
    """Read all named gcloud configurations."""
    if not check_gcloud_installed():
        return []
    proc = run(["gcloud", "config", "configurations", "list", "--format=json"])
    if proc.returncode != 0:
        return []
    try:
        raw = json.loads(proc.stdout or "[]")
    except Exception:
        return []
        
    out = []
    for c in raw:
        props = c.get("properties", {}) or {}
        core = props.get("core", {}) or {}
        out.append(
            GConfig(
                name=c.get("name", "?"),
                is_active=bool(c.get("is_active")),
                account=core.get("account", "") or "(no account)",
                project=core.get("project", "") or "(no project)",
            )
        )
    return out


def authed_accounts() -> set[str]:
    """Accounts gcloud currently holds credentials for."""
    if not check_gcloud_installed():
        return set()
    proc = run(["gcloud", "auth", "list", "--format=json"])
    if proc.returncode != 0:
        # Fallback to older format parsing if JSON format fails
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
    return proc.returncode == 0, (proc.stderr or proc.stdout)


def set_project(project: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "set", "project", project])
    return proc.returncode == 0, (proc.stderr or proc.stdout)


def create_configuration(name: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "configurations", "create", name])
    return proc.returncode == 0, (proc.stderr or proc.stdout)


def delete_configuration(name: str) -> tuple[bool, str]:
    proc = run(["gcloud", "config", "configurations", "delete", name, "--quiet"])
    return proc.returncode == 0, (proc.stderr or proc.stdout)


def auth_login_blocking(account: str | None = None) -> int:
    """
    Run `gcloud auth login` using an inherited terminal so interactive flow works.
    """
    cmd = ["gcloud", "auth", "login"]
    if account and account != "(no account)":
        cmd.append(account)
    return subprocess.run(cmd).returncode


# --------------------------------------------------------------------------
# UI Components: Gothic Header
# --------------------------------------------------------------------------

class GothHeader(Static):
    """A custom widget displaying the gothic title ASCII art and description."""
    
    def compose(self) -> ComposeResult:
        yield Label(
            "  ________  ________  _________  ___  ___     \n"
            " |\\   ____\\|\\   __  \\|\\___   ___\\\\  |\\  \\    \n"
            " \\ \\  \\___| \\ \\  |\\  \\|___ \\  \\_\\ \\  \\\\\\  \\   \n"
            "  \\ \\  \\  ___\\ \\  \\\\\\  \\   \\ \\  \\ \\ \\   __  \\  \n"
            "   \\ \\  |\\  \\ \\ \\  \\\\\\  \\   \\ \\  \\ \\ \\  \\ \\  \\ \n"
            "    \\ \\_______\\ \\_______\\   \\ \\__\\ \\ \\__\\ \\__\\\n"
            "     \\|_______|\\|_______|    \\|__|  \\|__|\\|__|",
            id="title-art"
        )
        yield Label("gcloud configuration switcher", id="subtitle")


# --------------------------------------------------------------------------
# Modals: Input & Confirmation Prompts
# --------------------------------------------------------------------------

class TextPrompt(ModalScreen[str | None]):
    """Modal dialog for text input."""
    
    def __init__(self, prompt: str, placeholder: str = ""):
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="prompt-label")
            yield Input(placeholder=self.placeholder, id="prompt-input")
            yield Label("Press [Enter] to submit or [Esc] to cancel", classes="dim-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ConfirmPrompt(ModalScreen[bool]):
    """Modal dialog for confirmation."""
    
    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, id="prompt-label")
            yield Label("[y] Confirm    [n / Esc] Cancel", classes="dim-hint")

    def key_y(self) -> None:
        self.dismiss(True)

    def key_n(self) -> None:
        self.dismiss(False)

    def key_escape(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------
# Main App
# --------------------------------------------------------------------------

class GothApp(App):
    """The gothic-themed TUI application."""
    
    CSS = """
    Screen {
        background: #0c0a0f;
        color: #e9d5ff;
    }
    
    #header-container {
        align: center middle;
        height: auto;
        min-height: 10;
        background: #120e16;
        border-bottom: double #4c1d95;
        padding: 1 0 0 0;
    }
    
    #title-art {
        color: #d8b4fe;
        text-align: center;
        width: 100%;
        text-style: bold;
    }
    
    #subtitle {
        color: #a855f7;
        text-align: center;
        width: 100%;
        text-style: italic;
        margin-top: 1;
        margin-bottom: 1;
    }
    
    #status {
        height: 3;
        padding: 0 2;
        content-align: left middle;
        background: #16141a;
        border-bottom: tall #4c1d95;
        color: #e9d5ff;
    }
    
    DataTable {
        height: 1fr;
        background: #0c0a0f;
        color: #e9d5ff;
        border: none;
        margin: 1 2;
    }
    
    DataTable > .datatable--header {
        background: #2e1065;
        color: #f3e8ff;
        text-style: bold;
    }
    
    DataTable > .datatable--cursor {
        background: #581c87;
        color: #ffffff;
        text-style: bold;
    }
    
    DataTable > .datatable--hover {
        background: #3b0764;
    }
    
    /* Dialogs & Screens */
    TextPrompt, ConfirmPrompt {
        align: center middle;
        background: rgba(12, 10, 15, 0.85);
    }
    
    #dialog {
        width: 60;
        height: auto;
        border: double #a855f7;
        padding: 1 2;
        background: #16141a;
        color: #e9d5ff;
    }
    
    #prompt-label {
        margin-bottom: 1;
        color: #d8b4fe;
        text-style: bold;
    }
    
    #prompt-input {
        border: tall #4c1d95;
        background: #0c0a0f;
        color: #ffffff;
    }
    
    #prompt-input:focus {
        border: tall #d8b4fe;
    }
    
    .dim-hint {
        color: #7c3aed;
        margin-top: 1;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("enter", "activate", "Activate"),
        Binding("n", "new_config", "New project"),
        Binding("d", "delete_config", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.configs: list[GConfig] = []
        self.valid_accounts: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="header-container"):
            yield GothHeader()
        yield Static("", id="status")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "goth — gcloud config switcher"
        
        if not check_gcloud_installed():
            self.query_one("#status", Static).update(
                "[red]Error: 'gcloud' CLI not found on PATH. Please install Google Cloud SDK first.[/red]"
            )
            return
            
        table = self.query_one(DataTable)
        table.add_columns("", "Configuration", "Account", "Project", "Auth Status")
        self.action_refresh()

    # -- data refresh ----------------------------------------------------

    def action_refresh(self) -> None:
        if not check_gcloud_installed():
            return
        self.configs = list_configurations()
        self.valid_accounts = authed_accounts()
        self._render_table()
        self._render_status()

    def _render_status(self) -> None:
        active = next((c for c in self.configs if c.is_active), None)
        status = self.query_one("#status", Static)
        if active:
            status.update(
                f"Active: [b]{active.name}[/b] | "
                f"Account: [cyan]{active.account}[/cyan] | "
                f"Project: [green]{active.project}[/green]"
            )
        else:
            status.update("No active configuration found.")

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for c in self.configs:
            marker = "●" if c.is_active else ""
            if c.account == "(no account)":
                auth_state = "[yellow]no account[/yellow]"
            elif c.account in self.valid_accounts:
                auth_state = "[green]ok[/green]"
            else:
                auth_state = "[red]needs login[/red]"
            table.add_row(marker, c.name, c.account, c.project, auth_state, key=c.name)

    def _selected_config(self) -> GConfig | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return next((c for c in self.configs if c.name == row_key.value), None)
        except Exception:
            return None

    # -- actions -----------------------------------------------------------

    def action_activate(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            return
        self.run_worker(self._activate_flow(cfg), exclusive=True)

    async def _activate_flow(self, cfg: GConfig) -> None:
        status = self.query_one("#status", Static)

        ok, msg = activate_configuration(cfg.name)
        if not ok:
            status.update(f"[red]Failed to activate {cfg.name}: {msg.strip()}[/red]")
            return

        # Re-check credentials.
        self.valid_accounts = authed_accounts()
        if cfg.account not in self.valid_accounts and cfg.account != "(no account)":
            status.update(f"[yellow]{cfg.account} needs re-auth — launching gcloud auth login...[/yellow]")
            with self.suspend():
                auth_login_blocking(cfg.account)
            self.valid_accounts = authed_accounts()

        self.action_refresh()
        status.update(f"[green]Switched to {cfg.name}[/green] (account={cfg.account}, project={cfg.project})")

    def action_new_config(self) -> None:
        self.run_worker(self._new_config_flow(), exclusive=True)

    async def _new_config_flow(self) -> None:
        status = self.query_one("#status", Static)

        name = await self.push_screen_wait(
            TextPrompt("New configuration name (e.g. client-acme-prod):")
        )
        if not name:
            return

        ok, msg = create_configuration(name)
        if not ok:
            status.update(f"[red]Could not create '{name}': {msg.strip()}[/red]")
            return

        status.update(f"Created '{name}'. Launching gcloud auth login...")
        with self.suspend():
            rc = auth_login_blocking()
        if rc != 0:
            status.update(f"[red]auth login failed/cancelled for '{name}'[/red]")
            self.action_refresh()
            return

        project = await self.push_screen_wait(
            TextPrompt(f"GCP project ID for '{name}':")
        )
        if project:
            ok, msg = set_project(project)
            if not ok:
                status.update(f"[red]Could not set project: {msg.strip()}[/red]")

        self.action_refresh()
        status.update(f"[green]'{name}' is ready and active.[/green]")

    def action_delete_config(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            return
        if cfg.is_active:
            self.query_one("#status", Static).update(
                "[red]Can't delete the active configuration — switch away first.[/red]"
            )
            return
        self.run_worker(self._delete_flow(cfg), exclusive=True)

    async def _delete_flow(self, cfg: GConfig) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmPrompt(f"Delete configuration '{cfg.name}'? This can't be undone.")
        )
        if not confirmed:
            return
        ok, msg = delete_configuration(cfg.name)
        status = self.query_one("#status", Static)
        if ok:
            status.update(f"Deleted '{cfg.name}'.")
        else:
            status.update(f"[red]Delete failed: {msg.strip()}[/red]")
        self.action_refresh()


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help"):
        print("\033[95m  ________  ________  _________  ___  ___     ")
        print(" |\\   ____\\|\\   __  \\|\\___   ___\\\\  |\\  \\    ")
        print(" \\ \\  \\___| \\ \\  |\\  \\|___ \\  \\_\\ \\  \\\\\\  \\   ")
        print("  \\ \\  \\  ___\\ \\  \\\\\\  \\   \\ \\  \\ \\ \\   __  \\  ")
        print("   \\ \\  |\\  \\ \\ \\  \\\\\\  \\   \\ \\  \\ \\ \\  \\ \\  \\ ")
        print("    \\ \\_______\\ \\_______\\   \\ \\__\\ \\ \\__\\ \\__\\")
        print("     \\|_______|\\|_______|    \\|__|  \\|__|\\|__|\033[0m")
        print("\n\033[1mgoth\033[0m — A gothic-themed TUI for managing and switching between gcloud configurations.\n")
        print("\033[1mUsage:\033[0m")
        print("  goth              Start the interactive TUI switcher")
        print("  goth -h, --help   Show this help message")
        print("\n\033[1mKeybindings (in TUI):\033[0m")
        print("  \033[1mEnter\033[0m        Activate the selected configuration (runs auth login if needed)")
        print("  \033[1mn\033[0m            Create a new configuration end-to-end")
        print("  \033[1md\033[0m            Delete the selected configuration (cannot delete active configuration)")
        print("  \033[1mr\033[0m            Refresh the configuration list")
        print("  \033[1mq / Esc\033[0m      Quit the application")
        sys.exit(0)

    GothApp().run()


if __name__ == "__main__":
    main()
