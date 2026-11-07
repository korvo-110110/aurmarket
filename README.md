# aurmarket
aurmarket - a terminal "marketplace" and search engine for the AUR and pacman repos.

  aurmarket                             full-screen TUI with a search bar (default)
  aurmarket tui firefox                 TUI, starting with a query
  aurmarket search firefox              plain search AUR + official repos
  aurmarket search -r aur -s votes vlc  AUR only, sorted by votes
  aurmarket info yay                    detailed package info
  aurmarket get yay                     download (git clone) build files
  aurmarket install yay                 download, review PKGBUILD, build + install
  aurmarket browse                      simple line-based interactive mode
  aurmarket config                      show / create the YAML config

Requires: python, python-yaml (sudo pacman -S python-yaml), git (for downloads).
