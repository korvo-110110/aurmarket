#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import curses
except ImportError:
    curses = None

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing. Install it with: sudo pacman -S python-yaml")

try:
    import readline
except ImportError:
    pass

AUR_RPC = "https://aur.archlinux.org/rpc/v5"
AUR_GIT = "https://aur.archlinux.org/{}.git"
AUR_SNAPSHOT = "https://aur.archlinux.org/cgit/aur.git/snapshot/{}.tar.gz"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "aurmarket"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "aurmarket"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "download_dir": "~/aur-packages",  
    "cache_ttl": 600,                 
    "default_limit": 20,              
    "default_sort": "popularity",
    "search_by": "name-desc",
    "default_repo": "all",
    "color": True,
    "review_pkgbuild": True,
    "pager": "",
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user = yaml.safe_load(CONFIG_FILE.read_text()) or {}
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except yaml.YAMLError as e:
            print(f"warning: could not parse {CONFIG_FILE}: {e}", file=sys.stderr)
    cfg["download_dir"] = Path(str(cfg["download_dir"])).expanduser()
    return cfg


def write_default_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))
        return True
    return False


def cache_get(url, ttl):
    if ttl <= 0:
        return None
    f = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".yaml")
    try:
        entry = yaml.safe_load(f.read_text())
        if time.time() - entry["ts"] < ttl:
            return entry["data"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        pass
    return None


def cache_put(url, data):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".yaml")
        f.write_text(yaml.safe_dump({"ts": time.time(), "url": url, "data": data}))
    except OSError:
        pass

class Style:
    enabled = True
    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36"}

    @classmethod
    def setup(cls, want_color):
        cls.enabled = bool(want_color) and sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text, *styles):
    if not Style.enabled:
        return str(text)
    codes = ";".join(Style.CODES[s] for s in styles)
    return f"\033[{codes}m{text}\033[0m"


def term_width():
    return max(60, shutil.get_terminal_size((100, 24)).columns)


def fmt_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "n/a"


def confirm(question, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return default if not ans else ans in ("y", "yes")


def have(cmd):
    return shutil.which(cmd) is not None


def run_pacman(args):
    if not have("pacman"):
        return ""
    env = dict(os.environ, LC_ALL="C")
    r = subprocess.run(["pacman", *args], capture_output=True, text=True, env=env)
    return r.stdout if r.returncode == 0 else ""


def installed_sets():
    allp = set(run_pacman(["-Qq"]).split())
    foreign = set(run_pacman(["-Qmq"]).split())
    return allp, foreign


HEADER_RE = re.compile(r"^(?P<repo>[^/\s]+)/(?P<name>\S+)\s+(?P<ver>\S+)(?P<rest>.*)$")


def parse_pacman_search(text):
    pkgs, cur = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] not in " \t":
            m = HEADER_RE.match(line)
            if m:
                cur = {
                    "source": "repo",
                    "repo": m["repo"],
                    "name": m["name"],
                    "version": m["ver"],
                    "desc": "",
                    "installed": "installed" in m["rest"],
                    "votes": None,
                    "popularity": None,
                    "ood": False,
                    "maintainer": None,
                }
                pkgs.append(cur)
        elif cur is not None:
            cur["desc"] = (cur["desc"] + " " + line.strip()).strip()
    return pkgs


def pacman_search(terms):
    if not have("pacman"):
        return []
    return parse_pacman_search(run_pacman(["-Ss", *terms]))


def pacman_info(name):
    return run_pacman(["-Si", name]) or run_pacman(["-Qi", name])


def http_json(cfg, url):
    cached = cache_get(url, cfg["cache_ttl"])
    if cached is not None:
        return cached
    req = urllib.request.Request(url, headers={"User-Agent": "aurmarket/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error talking to the AUR: {e}")
    cache_put(url, data)
    return data


def to_pkg(r, allp):
    return {
        "source": "aur",
        "repo": "aur",
        "name": r["Name"],
        "base": r.get("PackageBase", r["Name"]),
        "version": r.get("Version", "?"),
        "desc": r.get("Description") or "",
        "installed": r["Name"] in allp,
        "votes": r.get("NumVotes", 0),
        "popularity": r.get("Popularity", 0.0),
        "ood": bool(r.get("OutOfDate")),
        "maintainer": r.get("Maintainer"),
        "updated": r.get("LastModified", 0),
    }


def aur_search(cfg, terms, by, allp):
    keyword = max(terms, key=len)
    if len(keyword) < 2:
        return []
    url = f"{AUR_RPC}/search/{urllib.parse.quote(keyword)}?by={by}"
    data = http_json(cfg, url)
    if data.get("type") == "error":
        print(paint(f"AUR: {data.get('error')}", "yellow"), file=sys.stderr)
        return []
    results = [to_pkg(r, allp) for r in data.get("results", [])]
    if by in ("name", "name-desc"):
        low = [t.lower() for t in terms]
        results = [p for p in results
                   if all(t in (p["name"] + " " + p["desc"]).lower() for t in low)]
    return results


def aur_info(cfg, names):
    q = "&".join("arg[]=" + urllib.parse.quote(n) for n in names)
    data = http_json(cfg, f"{AUR_RPC}/info?{q}")
    return data.get("results", [])


SORTERS = {
    "popularity": lambda p: -(p["popularity"] or 0),
    "votes": lambda p: -(p["votes"] or 0),
    "name": lambda p: p["name"].lower(),
    "updated": lambda p: -(p.get("updated") or 0),
}


def search(cfg, terms, repo, by, sort, limit):
    allp, _ = installed_sets()
    results = []
    if repo in ("all", "pacman"):
        results += pacman_search(terms)[:limit]
    if repo in ("all", "aur"):
        aur = aur_search(cfg, terms, by, allp)
        joined = " ".join(terms).lower()
        aur.sort(key=lambda p: (p["name"].lower() != joined, SORTERS[sort](p)))
        results += aur[:limit]
    return results


def print_results(results):
    if not results:
        print(paint("No packages found.", "yellow"))
        return
    width = term_width()
    for i, p in enumerate(results, 1):
        tag = paint(f"[{p['repo']}]", "magenta" if p["source"] == "aur" else "blue")
        head = f"{paint(f'{i:>3}', 'dim')} {tag} {paint(p['name'], 'bold', 'cyan')} " \
               f"{paint(p['version'], 'green')}"
        if p["installed"]:
            head += " " + paint("[installed]", "green", "bold")
        if p["ood"]:
            head += " " + paint("[out-of-date]", "red")
        if p["source"] == "aur":
            head += paint(f"  ★{p['votes']}  ♨{p['popularity']:.2f}", "yellow")
        print(head)
        if p["desc"]:
            print(textwrap.fill(p["desc"], width=width, initial_indent="      ",
                                subsequent_indent="      "))


def aur_info_rows(r):
    return [
        ("Name", r["Name"]),
        ("Base", r.get("PackageBase")),
        ("Version", r.get("Version")),
        ("Description", r.get("Description")),
        ("URL", r.get("URL")),
        ("License", ", ".join(r.get("License", []))),
        ("Maintainer", r.get("Maintainer") or "orphan"),
        ("Votes", r.get("NumVotes")),
        ("Popularity", f"{r.get('Popularity', 0):.2f}"),
        ("First submitted", fmt_date(r.get("FirstSubmitted"))),
        ("Last modified", fmt_date(r.get("LastModified"))),
        ("Out of date", fmt_date(r["OutOfDate"]) if r.get("OutOfDate") else "no"),
        ("Depends", " ".join(r.get("Depends", [])) or "none"),
        ("Make deps", " ".join(r.get("MakeDepends", [])) or "none"),
        ("Opt deps", " ".join(r.get("OptDepends", [])) or "none"),
        ("Conflicts", " ".join(r.get("Conflicts", [])) or "none"),
        ("AUR page", f"https://aur.archlinux.org/packages/{r['Name']}"),
    ]


def print_aur_info(r):
    for k, v in aur_info_rows(r):
        if v is None:
            continue
        print(f"{paint(f'{k:<16}', 'bold')}: {v}")


def show_info(cfg, name):
    text = pacman_info(name)
    if text:
        print(text.strip())
        return True
    res = aur_info(cfg, [name])
    if res:
        print_aur_info(res[0])
        return True
    print(paint(f"Package '{name}' not found.", "red"))
    return False


def download_aur(cfg, name):
    """Fetch build files for an AUR package. Returns the directory or None."""
    res = aur_info(cfg, [name])
    if not res:
        print(paint(f"'{name}' is not in the AUR.", "red"))
        return None
    base = res[0].get("PackageBase", name)
    dest_root = cfg["download_dir"]
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / base

    if have("git"):
        if (dest / ".git").is_dir():
            print(f"Updating {dest} ...")
            r = subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"])
        else:
            print(f"Cloning {AUR_GIT.format(base)} ...")
            r = subprocess.run(["git", "clone", AUR_GIT.format(base), str(dest)])
        if r.returncode == 0:
            print(paint(f"Saved to {dest}", "green"))
            return dest
        print(paint("git failed, trying snapshot tarball ...", "yellow"))

    url = AUR_SNAPSHOT.format(base)
    tmp = dest_root / f"{base}.tar.gz"
    try:
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp) as tf:
            tf.extractall(dest_root, filter="data") if sys.version_info >= (3, 12) \
                else tf.extractall(dest_root)
        tmp.unlink(missing_ok=True)
        print(paint(f"Saved to {dest}", "green"))
        return dest
    except Exception as e:  # noqa: BLE001
        print(paint(f"Download failed: {e}", "red"))
        return None


def review_pkgbuild(cfg, directory):
    pkgbuild = directory / "PKGBUILD"
    if not pkgbuild.exists():
        print(paint("No PKGBUILD found!", "red"))
        return False
    pager = cfg["pager"] or os.environ.get("PAGER") or "less"
    print(paint("\nAUR packages are user-submitted. Read the PKGBUILD before building.\n",
                "yellow", "bold"))
    if sys.stdout.isatty() and have(pager.split()[0]):
        subprocess.run([*pager.split(), str(pkgbuild)])
    else:
        print(pkgbuild.read_text())
    return confirm("Build and install this package?", default=False)


def install(cfg, name):
    if run_pacman(["-Si", name]):
        print(f"'{name}' is in the official repos.")
        if confirm(f"Run: sudo pacman -S --needed {name}?", default=True):
            subprocess.run(["sudo", "pacman", "-S", "--needed", name])
        return
    directory = download_aur(cfg, name)
    if not directory:
        return
    if cfg["review_pkgbuild"] and not review_pkgbuild(cfg, directory):
        print("Aborted. Build files are still in", directory)
        return
    if not have("makepkg"):
        print(paint("makepkg not found (install base-devel).", "red"))
        return
    print(paint("Note: AUR dependencies are not resolved automatically; "
                "install them first if makepkg complains.", "dim"))
    subprocess.run(["makepkg", "-si"], cwd=directory)


HELP = """\
Commands:
  <text>        search for packages (AUR + pacman)
  <N>           show details of result N
  d <N>         download build files of result N (AUR)
  i <N>         install result N (pacman -S, or review + makepkg for AUR)
  repo <x>      restrict source: all | aur | pacman
  sort <x>      popularity | votes | name | updated
  help          this text
  q             quit"""


def browse(cfg, args):
    repo, sort = args.repo, args.sort
    by, limit = args.by, args.limit
    results = []
    print(paint("aurmarket", "bold", "cyan"), "- type a search term, or 'help'.")
    if args.query:
        results = search(cfg, args.query, repo, by, sort, limit)
        print_results(results)

    def pick(token):
        if token.isdigit() and 1 <= int(token) <= len(results):
            return results[int(token) - 1]
        print(paint("No such result number.", "red"))
        return None

    while True:
        try:
            line = input(paint(f"\n[{repo}|{sort}] > ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        if line in ("q", "quit", "exit"):
            return
        if line in ("help", "?"):
            print(HELP)
        elif line.isdigit():
            p = pick(line)
            if p:
                show_info(cfg, p["name"])
        elif cmd in ("d", "i") and rest.isdigit():
            p = pick(rest)
            if p and cmd == "d":
                if p["source"] == "aur":
                    download_aur(cfg, p["name"])
                else:
                    print("Official repo package - use 'i' to install it.")
            elif p:
                install(cfg, p["name"])
        elif cmd == "repo" and rest in ("all", "aur", "pacman"):
            repo = rest
        elif cmd == "sort" and rest in SORTERS:
            sort = rest
        else:
            results = search(cfg, line.split(), repo, by, sort, limit)
            print_results(results)


ENTER_KEYS = ("\n", "\r")
REPO_CYCLE = ["all", "aur", "pacman"]
SORT_CYCLE = list(SORTERS)


class TUI:
    def __init__(self, scr, cfg, args):
        self.scr, self.cfg = scr, cfg
        self.by, self.limit = args.by, args.limit
        self.repo, self.sort = args.repo, args.sort
        self.query = " ".join(getattr(args, "query", None) or [])
        self.cur = len(self.query)
        self.results = []
        self.sel = self.top = 0
        self.vis = 1
        self.focus = "search"      # "search" | "list"
        self.message = ""
        self.searching = False
        self.searched = None
        self.jump = False          # jump to the list once results arrive
        self.gen = 0
        self.edited = time.time()
        self.lock = threading.Lock()
        self.cursor_x = 0
        self.colors = bool(cfg["color"]) and curses.has_colors() and "NO_COLOR" not in os.environ

   
    def col(self, n):
        return curses.color_pair(n) if self.colors else 0

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        try:
            self.scr.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass

    def touch(self):
        self.edited = time.time()
        self.message = ""


    def start_search(self):
        q = self.query.strip()
        self.searched = q
        self.gen += 1
        gen = self.gen
        if len(q) < 2:
            with self.lock:
                self.results, self.sel, self.top = [], 0, 0
            self.searching = self.jump = False
            return
        self.searching = True

        def work():
            err = ""
            try:
                res = search(self.cfg, q.split(), self.repo, self.by, self.sort, self.limit)
            except BaseException as e:  
                res, err = [], str(e) or e.__class__.__name__
            with self.lock:
                if gen != self.gen:
                    return
                self.results, self.sel, self.top = res, 0, 0
                self.message = err
                self.searching = False
                if self.jump and res:
                    self.focus = "list"
                self.jump = False

        threading.Thread(target=work, daemon=True).start()

    def cycle(self, what):
        if what == "repo":
            self.repo = REPO_CYCLE[(REPO_CYCLE.index(self.repo) + 1) % len(REPO_CYCLE)]
        else:
            self.sort = SORT_CYCLE[(SORT_CYCLE.index(self.sort) + 1) % len(SORT_CYCLE)]
        if len(self.query.strip()) >= 2:
            self.start_search()

   
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        bw = max(20, min(w - 4, 90))
        bx = max(0, (w - bw) // 2)

        self.put(0, bx, "aurmarket", curses.A_BOLD | self.col(1))
        self.put(0, bx + 10, "  AUR + pacman package search", curses.A_DIM)

       
        battr = self.col(1) | (curses.A_BOLD if self.focus == "search" else curses.A_DIM)
        self.put(1, bx, "╭" + "─" * (bw - 2) + "╮", battr)
        self.put(2, bx, "│", battr)
        self.put(2, bx + bw - 1, "│", battr)
        self.put(3, bx, "╰" + "─" * (bw - 2) + "╯", battr)
        self.put(2, bx + 2, "›", curses.A_BOLD | self.col(1))
        tx, inner = bx + 4, bw - 6
        if self.query:
            start = max(0, self.cur - inner + 1)
            self.put(2, tx, self.query[start:start + inner])
            self.cursor_x = tx + self.cur - start
        else:
            self.put(2, tx, "Search AUR & pacman packages…", curses.A_DIM)
            self.cursor_x = tx

       
        with self.lock:
            results = list(self.results)
            message, searching = self.message, self.searching
        self.sel = max(0, min(self.sel, len(results) - 1))
        if searching:
            status, sattr = "Searching…", self.col(5)
        elif message:
            status, sattr = message, self.col(6)
        elif self.searched and len(self.searched) >= 2:
            status, sattr = f"{len(results)} results for “{self.searched}”", curses.A_DIM
        else:
            status, sattr = "Start typing - results appear as you type.", curses.A_DIM
        self.put(4, bx + 2, status[:bw - 4], sattr)
        filt = f"source: {self.repo} · sort: {self.sort}"
        if len(status) + len(filt) + 8 < bw:
            self.put(4, bx + bw - 2 - len(filt), filt, curses.A_DIM)

        
        y0, per = 5, 4
        self.vis = max(1, (h - 1 - y0) // per)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + self.vis:
            self.top = self.sel - self.vis + 1
        for n, p in enumerate(results[self.top:self.top + self.vis]):
            idx = self.top + n
            self.draw_card(y0 + n * per, bx, bw, p, idx == self.sel)

        
        if self.focus == "search":
            hint = "Enter search · ↓/Tab results · F2 source · F3 sort · Esc clear/quit"
        else:
            hint = ("↑↓ move · Enter details · d download · i install · r source · "
                    "s sort · / search · q quit")
        if results:
            hint = f"{self.sel + 1}/{len(results)}  " + hint
        self.put(h - 1, bx, hint[:bw], curses.A_DIM)

        try:
            curses.curs_set(1 if self.focus == "search" else 0)
        except curses.error:
            pass
        if self.focus == "search":
            try:
                scr.move(2, self.cursor_x)
            except curses.error:
                pass
        scr.refresh()

    def draw_card(self, y, bx, bw, p, selected):
        x, width = bx + 2, bw - 4
        if selected:
            for dy in range(3):
                self.put(y + dy, bx, "┃", self.col(5) | curses.A_BOLD)
        tag = f"[{p['repo']}] "
        self.put(y, x, tag, self.col(3 if p["source"] == "aur" else 4) | curses.A_BOLD)
        cx = x + len(tag)
        nattr = self.col(1) | curses.A_BOLD | (curses.A_UNDERLINE if selected else 0)
        self.put(y, cx, p["name"], nattr)
        cx += len(p["name"]) + 1
        self.put(y, cx, p["version"], self.col(2))
        cx += len(p["version"]) + 1
        if p["installed"]:
            self.put(y, cx, "[installed]", self.col(2) | curses.A_BOLD)
            cx += 12
        if p["ood"]:
            self.put(y, cx, "[out-of-date]", self.col(6))
        desc = textwrap.shorten(p["desc"] or "(no description)", width=width, placeholder="…")
        self.put(y + 1, x, desc)
        if p["source"] == "aur":
            meta = (f"★ {p['votes']}   ♨ {p['popularity']:.2f}   ·   "
                    f"{p['maintainer'] or 'orphan'}   ·   updated {fmt_date(p.get('updated'))}")
        else:
            meta = f"official repository · {p['repo']}"
        self.put(y + 2, x, meta, curses.A_DIM)

    
    def getkey(self):
        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    def handle_key(self, k):
        """Return False to quit."""
        if k == curses.KEY_RESIZE:
            return True
        if k == curses.KEY_F2:
            self.cycle("repo")
            return True
        if k == curses.KEY_F3:
            self.cycle("sort")
            return True
        return self.key_search(k) if self.focus == "search" else self.key_list(k)

    def key_search(self, k):
        q, c = self.query, self.cur
        if k in ENTER_KEYS or k == curses.KEY_ENTER:
            self.jump = True
            self.start_search()
        elif k in ("\x7f", "\b") or k == curses.KEY_BACKSPACE:
            if c > 0:
                self.query, self.cur = q[:c - 1] + q[c:], c - 1
                self.touch()
        elif k == curses.KEY_DC:
            self.query = q[:c] + q[c + 1:]
            self.touch()
        elif k == curses.KEY_LEFT:
            self.cur = max(0, c - 1)
        elif k == curses.KEY_RIGHT:
            self.cur = min(len(q), c + 1)
        elif k in (curses.KEY_HOME, "\x01"):
            self.cur = 0
        elif k in (curses.KEY_END, "\x05"):
            self.cur = len(q)
        elif k == "\x15":  # Ctrl+U
            self.query, self.cur = "", 0
            self.touch()
        elif k in (curses.KEY_DOWN, "\t"):
            if self.results:
                self.focus = "list"
        elif k == "\x1b":
            if self.query:
                self.query, self.cur = "", 0
                self.touch()
            else:
                return False
        elif isinstance(k, str) and k.isprintable():
            self.query, self.cur = q[:c] + k + q[c:], c + 1
            self.touch()
        return True

    def key_list(self, k):
        with self.lock:
            res = list(self.results)
        n = len(res)
        ch = k if isinstance(k, str) else ""
        if k == curses.KEY_DOWN or ch == "j":
            self.sel = min(n - 1, self.sel + 1)
        elif k == curses.KEY_UP or ch == "k":
            if self.sel == 0:
                self.focus = "search"
            else:
                self.sel -= 1
        elif k == curses.KEY_NPAGE:
            self.sel = min(n - 1, self.sel + self.vis)
        elif k == curses.KEY_PPAGE:
            self.sel = max(0, self.sel - self.vis)
        elif k == curses.KEY_HOME or ch == "g":
            self.sel = 0
        elif k == curses.KEY_END or ch == "G":
            self.sel = max(0, n - 1)
        elif k in (curses.KEY_RIGHT, curses.KEY_ENTER) or ch in ("\n", "\r", "l"):
            if n:
                self.detail(res[self.sel])
        elif ch == "d" and n:
            p = res[self.sel]
            if p["source"] == "aur":
                self.external(download_aur, self.cfg, p["name"])
            else:
                self.message = "Official repo package - press i to install it."
        elif ch == "i" and n:
            self.external(install, self.cfg, res[self.sel]["name"])
        elif ch == "r":
            self.cycle("repo")
        elif ch == "s":
            self.cycle("sort")
        elif ch in ("/", "\t", "\x1b") or k == curses.KEY_LEFT:
            self.focus = "search"
        elif ch == "q":
            return False
        return True

    
    def external(self, fn, *a):
        
        curses.def_prog_mode()
        curses.endwin()
        try:
            fn(*a)
        except KeyboardInterrupt:
            pass
        try:
            input("\nPress Enter to return to aurmarket...")
        except (EOFError, KeyboardInterrupt):
            pass
        curses.reset_prog_mode()
        self.scr.clear()
        self.scr.refresh()

    def detail(self, p):
        if p["source"] == "repo":
            text = pacman_info(p["name"])
            lines = text.splitlines() if text else ["No info available."]
        else:
            res = aur_info(self.cfg, [p["name"]])
            lines = ([f"{k:<16}: {v}" for k, v in aur_info_rows(res[0]) if v is not None]
                     if res else ["Package not found."])
        off = 0
        while True:
            h, w = self.scr.getmaxyx()
            width = max(30, min(w - 6, 100))
            x = max(1, (w - width) // 2)
            wrapped = []
            for ln in lines:
                wrapped += textwrap.wrap(ln, width, subsequent_indent=" " * 18) or [""]
            body = max(1, h - 4)
            off = max(0, min(off, len(wrapped) - body))
            self.scr.erase()
            self.put(0, x, p["name"], self.col(1) | curses.A_BOLD)
            self.put(0, x + len(p["name"]) + 1, p["version"], self.col(2))
            for i, ln in enumerate(wrapped[off:off + body]):
                if ln[16:18] == ": ":
                    self.put(2 + i, x, ln[:16], curses.A_BOLD)
                    self.put(2 + i, x + 16, ln[16:])
                else:
                    self.put(2 + i, x, ln)
            hint = "↑↓/PgUp/PgDn scroll · d download · i install · q/Esc/← back"
            self.put(h - 1, x, hint, curses.A_DIM)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.scr.refresh()
            k = self.getkey()
            ch = k if isinstance(k, str) else ""
            if k is None or k == curses.KEY_RESIZE:
                continue
            if ch in ("q", "\x1b") or k == curses.KEY_LEFT:
                return
            elif k == curses.KEY_DOWN or ch == "j":
                off += 1
            elif k == curses.KEY_UP or ch == "k":
                off -= 1
            elif k == curses.KEY_NPAGE or ch == " ":
                off += body - 1
            elif k == curses.KEY_PPAGE:
                off -= body - 1
            elif ch == "d" and p["source"] == "aur":
                self.external(download_aur, self.cfg, p["name"])
            elif ch == "i":
                self.external(install, self.cfg, p["name"])

    
    def run(self):
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        if self.colors:
            for i, c in enumerate([curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_MAGENTA,
                                   curses.COLOR_BLUE, curses.COLOR_YELLOW, curses.COLOR_RED], 1):
                curses.init_pair(i, c, -1)
        self.scr.keypad(True)
        self.scr.timeout(100)
        if self.query.strip():
            self.jump = True
            self.start_search()
        while True:
            self.draw()
            k = self.getkey()
            if k is not None:
                if not self.handle_key(k):
                    return
            elif (self.focus == "search" and self.query.strip() != self.searched
                  and time.time() - self.edited > 0.4):
                self.start_search()     


def run_tui(cfg, args):
    if curses is None or not sys.stdout.isatty():
        sys.exit("The TUI needs an interactive terminal. Try: aurmarket search <term>")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(lambda scr: TUI(scr, cfg, args).run())
    except KeyboardInterrupt:
        pass


def build_parser(cfg):
    p = argparse.ArgumentParser(prog="aurmarket",
                                description="Marketplace + search engine for the AUR and pacman.")
    sub = p.add_subparsers(dest="cmd")

    def search_opts(sp):
        sp.add_argument("-r", "--repo", choices=["all", "aur", "pacman"], default=cfg["default_repo"])
        sp.add_argument("-s", "--sort", choices=list(SORTERS), default=cfg["default_sort"])
        sp.add_argument("-b", "--by", default=cfg["search_by"],
                        choices=["name", "name-desc", "maintainer", "depends",
                                 "makedepends", "optdepends", "checkdepends"])
        sp.add_argument("-n", "--limit", type=int, default=cfg["default_limit"])

    s = sub.add_parser("search", aliases=["s"], help="search packages")
    s.add_argument("query", nargs="+")
    search_opts(s)

    i = sub.add_parser("info", help="show package details")
    i.add_argument("package")

    g = sub.add_parser("get", aliases=["download"], help="download AUR build files")
    g.add_argument("package")

    ins = sub.add_parser("install", help="download, review and install a package")
    ins.add_argument("package")

    t = sub.add_parser("tui", help="full-screen search UI (default when no command is given)")
    t.add_argument("query", nargs="*")
    search_opts(t)

    b = sub.add_parser("browse", help="simple line-based interactive mode")
    b.add_argument("query", nargs="*")
    search_opts(b)

    sub.add_parser("config", help="show config (creates default file if missing)")
    return p


def main():
    cfg = load_config()
    Style.setup(cfg["color"])
    parser = build_parser(cfg)
    args = parser.parse_args()

    if args.cmd in (None, "tui"):
        if args.cmd is None:
            args = parser.parse_args(["tui"])
        run_tui(cfg, args)
    elif args.cmd == "browse":
        browse(cfg, args)
    elif args.cmd in ("search", "s"):
        print_results(search(cfg, args.query, args.repo, args.by, args.sort, args.limit))
    elif args.cmd == "info":
        sys.exit(0 if show_info(cfg, args.package) else 1)
    elif args.cmd in ("get", "download"):
        sys.exit(0 if download_aur(cfg, args.package) else 1)
    elif args.cmd == "install":
        install(cfg, args.package)
    elif args.cmd == "config":
        if write_default_config():
            print(f"Created {CONFIG_FILE}\n")
        print(f"# {CONFIG_FILE}")
        shown = dict(cfg, download_dir=str(cfg["download_dir"]))
        print(yaml.safe_dump(shown, sort_keys=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
