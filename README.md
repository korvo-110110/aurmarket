# aurmarket

A terminal "marketplace" and search engine for the **AUR** and the official **pacman** repositories. Search like you would in a search engine, browse results with descriptions, inspect packages, and download or install them, all from your terminal.

Built with Python and YAML, for Arch Linux.

## Features

- Full-screen TUI with a search bar that shows multiple results at once
- Searches the AUR and official repos together, or just one of them
- Sort results (for example by votes)
- Detailed package info
- Download (git clone) of AUR build files
- One-step install: download, review the PKGBUILD, then build and install
- Simple line-based interactive mode for plain terminals
- YAML configuration

## Requirements

- `python`
- `python-yaml`
- `git` (needed for downloads)

On Arch:

```bash
sudo pacman -S python python-yaml git
```

## Installation

### AUR (coming soon)

aurmarket will be available on the AUR, so you'll be able to install it with yay helper:

```bash
yay -S aurmarket
```

### Install script

```bash
git clone https://github.com/korvo-110110/aurmarket.git
cd aurmarket
./install.sh                # install for your user (~/.local/bin/aurmarket)
sudo ./install.sh --system  # or install for all users (/usr/local/bin/aurmarket)
```

To uninstall, run `./install.sh --uninstall` (add `--system` if you installed system-wide).

If `~/.local/bin` is not in your `PATH`, add this to your shell config:

```bash
export PATH="$HOME/.local/bin:$PATH"
```
## Usage

```text
aurmarket                             full-screen TUI with a search bar (default)
aurmarket tui firefox                 TUI, starting with a query
aurmarket search firefox              plain search AUR + official repos
aurmarket search -r aur -s votes vlc  AUR only, sorted by votes
aurmarket info yay                    detailed package info
aurmarket get yay                     download (git clone) build files
aurmarket install yay                 download, review PKGBUILD, build + install
aurmarket browse                      simple line-based interactive mode
aurmarket config                      show / create the YAML config
```

### Commands

| Command | Description |
|---------|-------------|
| `tui [query]` | Launch the full-screen TUI, optionally starting with a query. This is the default when no command is given. |
| `search <query>` | Print search results for the AUR and official repos. |
| `info <package>` | Show detailed information about a package. |
| `get <package>` | Download (git clone) the package's build files. |
| `install <package>` | Download, let you review the PKGBUILD, then build and install. |
| `browse` | Simple line-based interactive mode. |
| `config` | Show the YAML config, creating it if it doesn't exist. |

### Search options

| Option | Description |
|--------|-------------|
| `-r, --repo <source>` | Restrict the search to one source, e.g. `-r aur`. |
| `-s, --sort <field>` | Sort results by a field, e.g. `-s votes`. |

## Examples

Search everywhere for Firefox:

```bash
aurmarket search firefox
```

Search only the AUR and show the most-voted VLC packages first:

```bash
aurmarket search -r aur -s votes vlc
```

Install `yay`, reviewing the PKGBUILD before it builds:

```bash
aurmarket install yay
```

## Configuration

Run `aurmarket config` to display the current settings or create the YAML config file with defaults.

## Security note

AUR packages are user-submitted and not vetted by the Arch team. Always read the PKGBUILD before building. `aurmarket install` pauses so you can review it first.

## License

aurmarket is released under the [MIT License](LICENSE).
