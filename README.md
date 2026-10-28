# goth

goth is a minimal full-screen terminal UI for managing and switching between gcloud configurations. It leverages gcloud's built-in named configurations to isolate credentials, projects, regions, and zones, removing the need to repeatedly run login and project setup commands.

Google often requires your password again every day before gcloud works. goth can store that password locally and renew `gcloud auth login` in the morning and at night so you can keep using gcloud without signing in by hand.

The app runs full screen with a black-and-white layout. Keybindings live in the footer only. Prompts for names and delete confirmation overlay near the bottom — type a value or `y`/`n`, then Enter. There is no command palette.

## Requirements

- Python 3.9 or higher
- Google Cloud SDK (gcloud CLI) installed and available in PATH
- textual and playwright (Python packages)
- Google Chrome or Chromium (used to complete Google's login page)

## Setup

Clone or download the repository, then create a virtual environment and install:

```bash
cd /path/to/goth
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/playwright install chromium
```

This installs `goth` as a command inside the virtual environment. If Google Chrome is already installed, goth prefers it and bundled Chromium is only a fallback.

To run `goth` from any directory without activating the virtual environment each time,
add a permanent alias to your shell configuration (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
alias goth='/path/to/goth/.venv/bin/goth'
```

Reload the shell or source the config file:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

Alternatively, activate the virtual environment once per session:

```bash
source /path/to/goth/.venv/bin/activate
```

## Usage

Once the alias or virtual environment is active, run from any directory:

```bash
goth
```

The start screen has two options:

- **configurations** — switch named gcloud configs; edit account, project, region, and zone
- **configure password** — save your Google password and enable twice-daily re-login

Display help and keybinding reference:

```bash
goth --help
```

Renew login immediately (same job the morning/night timer runs):

```bash
goth keepalive
```

## Keep-alive login

After you save a password, goth:

1. Writes it to `~/.config/goth/state.json` (directory `0700`, file `0600`)
2. Installs a user timer for **07:00** and **19:00** (systemd `--user`, or cron if systemd is missing)
3. Renews login when goth starts, and when you press **k** on the password option

gcloud itself has no password flag. Keep-alive opens Google's OAuth page, fills the stored password, and finishes `gcloud auth login`. This will not complete unattended 2-step verification or SSO/SAML. If Google asks for a second factor, sign in once by hand from a desktop session.

Do not commit or share `~/.config/goth/`.

## Keybindings

Start menu:

- **Enter**: Open the selected option
- **d**: Delete a stored Google password (password option)
- **k**: Login now with the stored password
- **q / Esc**: Quit

Switcher:

- **Enter**: Activate the selected configuration. If the associated account is not authenticated, goth uses the stored password when present, otherwise it suspends and runs `gcloud auth login`.
- **n**: Create a new configuration (name → account → login → project → region → zone).
- **e**: Edit account, project, region, and zone for the selected configuration. Enter keeps the current value; `-` unsets.
- **d**: Delete the selected configuration (bottom prompt: type `y`/`yes` or `n`/`no`). If it's active, goth switches away first.
- **r**: Refresh the list of configurations.
- **q**: Quit the application.
- **Esc**: Cancel an open bottom prompt, or return to the start menu.
