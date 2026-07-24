# goth

goth is a minimal full-screen terminal UI for managing and switching between gcloud configurations. It leverages gcloud's built-in named configurations to isolate credentials, projects, regions, and zones, removing the need to repeatedly run login and project setup commands.

The app runs full screen with a black-and-white layout. Keybindings live in the footer only. Prompts for names and delete confirmation overlay near the bottom — type a value or `y`/`n`, then Enter. There is no command palette.

## Requirements

- Python 3.9 or higher
- Google Cloud SDK (gcloud CLI) installed and available in PATH
- textual (Python package)

## Setup

Clone or download the repository, then create a virtual environment and install:

```bash
cd /path/to/goth
python3 -m venv .venv
./.venv/bin/pip install -e .
```

This installs `goth` as a command inside the virtual environment.

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

Display help and keybinding reference:

```bash
goth --help
```

## Keybindings

- **Enter**: Activate the selected configuration. If the associated account is not authenticated, the TUI suspends, runs `gcloud auth login`, and resumes.
- **n**: Create a new configuration (bottom prompt for name → auth login → bottom prompt for project ID).
- **d**: Delete the selected configuration (bottom prompt: type `y`/`yes` or `n`/`no`). If it's active, goth switches away first.
- **r**: Refresh the list of configurations.
- **q**: Quit the application.
- **Esc**: Cancel an open bottom prompt, or quit if no prompt is open.
