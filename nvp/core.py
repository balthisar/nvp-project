#!/usr/bin/env python3
"""
nvp - Neovim Panes

A spatial Neovim manager for Tmux: Open files in dedicated panes
without leaving your current shell.
"""

from __future__ import annotations
import argparse
import configparser
import os
import shlex
import shutil
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from pynvim import attach


def get_version() -> str:
    """Get version from the installed package metadata."""
    try:
        return version("nvp")
    except PackageNotFoundError:
        return "1.1-dev"


class NvpManager:
    """
    Spatial Neovim manager for Tmux.

    Manages tmux pane targeting, neovim socket lifecycle, and
    file operations. All file opens and buffer settings go through
    a single RPC path — the send-keys path only ever launches a
    bare Neovim instance with a socket.
    """

    SHELL_COMMANDS = frozenset({"zsh", "bash", "sh", "fish", "dash", "tcsh"})
    SYSTEM_FILENAMES = frozenset({"COMMIT_EDITMSG", "GIT_REBASE_TODO", "MERGE_MSG"})

    def __init__(self, args: argparse.Namespace) -> None:
        """Constructor responsible for instantiation and basic setup."""
        self.args = args

        # Configuration from file
        self.config_dir = Path.home() / ".config" / "nvp"
        self.config_file = self.config_dir / "nvp.conf"
        self.user_default_pane: str | None = None
        self.split_direction: str = "v"
        self.in_tmux: bool = "TMUX" in os.environ
        self._load_config()

        # Resolve files to absolute real paths immediately
        self.files: list[str] = [
            os.path.realpath(os.path.abspath(f)) for f in args.files
        ]

        # Blocking-edit detection
        self.is_shell_tmp: bool = any(
            (
                os.path.basename(f).startswith(("zsh", "bash", "fish"))
                or "fish.cmd" in f
            )
            and ("/tmp" in f or "/private/tmp" in f)
            for f in self.files
        )
        self.is_git_tmp: bool = any(
            os.path.basename(f) in self.SYSTEM_FILENAMES for f in self.files
        )
        self.should_block: bool = self.is_shell_tmp or self.is_git_tmp

        # Tmux / pane state — resolved in _resolve_settings()
        self.current_pane_id: str | None = os.environ.get("TMUX_PANE")
        self.target_pane_addr: str = ""
        self.active_split_dir: str = ""
        self.is_self_target: bool = False
        self.socket_path: str = ""


    def _load_config(self) -> None:
        """Load user configuration, creating from template if absent."""
        config = configparser.ConfigParser(allow_no_value=True)
        package_dir = Path(__file__).parent.absolute()
        default_template = package_dir / "nvp.defaults.conf"

        if not self.config_file.exists():
            self.config_dir.mkdir(parents=True, exist_ok=True)
            if default_template.exists():
                shutil.copy(default_template, self.config_file)
            else:
                with open(self.config_file, "w") as f:
                    f.write(
                        "[Settings]\n"
                        "user_default_pane = None\n"
                        "split_direction = v\n"
                    )

        config.read(self.config_file)
        settings = config["Settings"]

        raw_pane = settings.get("user_default_pane")
        self.user_default_pane = None if raw_pane == "None" else raw_pane
        self.split_direction = settings.get("split_direction", "v")


    def _run_tmux(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run a tmux command, returning a CompletedProcess.
        Returns a synthetic failure result when not inside tmux.
        """
        if not self.in_tmux:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        try:
            return subprocess.run(
                ["tmux", *args], capture_output=True, text=True, check=False
            )
        except Exception:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")


    def _is_pane_busy(self, pane_addr: str) -> bool:
        """Determine if a pane is occupied by a non-shell process.

        Used to decide whether to create a new split instead of
        sending keys to an already-occupied pane.
        """
        if self._run_tmux(["has-session", "-t", pane_addr]).returncode != 0:
            return False
        proc = self._run_tmux(
            ["display-message", "-t", pane_addr, "-p", "#{pane_current_command}"]
        )
        cmd = proc.stdout.strip().lower()
        return cmd not in self.SHELL_COMMANDS


    def _resolve_settings(self) -> None:
        """Resolve target pane, split direction, socket path, and
        self-target detection.

        Priority chain for target pane:
            CLI  →  $NV_TARGET_PANE  →  config file  →  pane-base-index
        """
        base_idx_proc = self._run_tmux(["show-options", "-gv", "pane-base-index"])
        stdout_val = getattr(base_idx_proc, "stdout", "") or ""
        base_idx = stdout_val.strip() or "1"

        target_idx = (
            self.args.pane
            or os.environ.get("NV_TARGET_PANE")
            or self.user_default_pane
            or base_idx
        )
        self.target_pane_addr = f".{target_idx}"
        self.active_split_dir = self.args.split or self.split_direction

        # Detect whether we'd be sending keys to ourselves
        resolved_target_id: str | None = None
        if self.in_tmux:
            proc = self._run_tmux(
                ["display-message", "-t", self.target_pane_addr, "-p", "#{pane_id}"]
            )
            resolved_target_id = proc.stdout.strip()
        self.is_self_target = (
            self.current_pane_id == resolved_target_id
        ) and self.in_tmux

        # Socket: one per tmux session+window
        if self.in_tmux:
            info_proc = self._run_tmux(["display-message", "-p", "#S:#I"])
            session, window = info_proc.stdout.strip().split(":")
        else:
            session, window = "standalone", "0"

        self.socket_path = (
            os.environ.get("NVIM_LISTEN_ADDRESS")
            or f"/tmp/nvimsocket-{session}-{window}"
        )


    @staticmethod
    def _monitor_window(nvim_obj, buf_id: int) -> None:
        """Block until the buffer is closed.

        Critical for C-x C-e and git commit — the calling shell
        waits for the editor to finish before proceeding.
        """
        try:
            while nvim_obj.call("win_findbuf", buf_id):
                time.sleep(0.05)
        except (EOFError, OSError, Exception):
            # We enter this path when Neovim exits entirely, and we lose
            # the socket before confirming that the buffer was wiped out.
            # This could cause return to the shell before Neovim is done
            # writing our files completely.
            time.sleep(0.1)


    def _wait_for_socket(self) -> object | None:
        """Poll until Neovim's RPC socket is ready.

        Returns a pynvim connection object, or None if the socket
        never became available within the timeout window.
        """
        for _ in range(50):
            try:
                time.sleep(0.05)
                return attach("socket", path=self.socket_path)
            except:
                continue
        return None


    def _open_via_rpc(self, nvim) -> None:
        """Open files and apply settings via RPC.

        This is the ONE path for all file operations. Whether Neovim
        was already running or just launched, files are always opened
        here via the socket.

        Preserves:
        - fnameescape for every filename
        - setlocal nofixeol noendofline for shell temp files
        - select-pane to the target after opening
        - blocking + last-pane return for shell/git tmp files
        """
        if self.files:
            first_file = nvim.call("fnameescape", self.files[0])
            if self.is_shell_tmp:
                nvim.command(f"edit {first_file} | setlocal nofixeol noendofline")
            else:
                nvim.command(f"edit {first_file}")

            for f in self.files[1:]:
                nvim.command(f"badd {nvim.call('fnameescape', f)}")

            buf_id = nvim.current.buffer.number

            if self.in_tmux:
                self._run_tmux(["select-pane", "-t", self.target_pane_addr])

            if self.should_block:
                self._monitor_window(nvim, buf_id)
                if self.in_tmux:
                    self._run_tmux(["last-pane"])
        else:
            if self.in_tmux:
                self._run_tmux(["select-pane", "-t", self.target_pane_addr])


    def _ensure_nvim(self) -> object | None:
        """Ensure a Neovim instance is running and return an RPC connection.

        If a socket already exists, attach to it. Otherwise, launch
        a bare Neovim (no files, no flags) and poll until the socket
        is ready.

        The send-keys path is trivially simple — it never needs to handle
        filenames, quoting, or -c flags.
        """
        # Try existing socket first
        if os.path.exists(self.socket_path):
            try:
                return attach("socket", path=self.socket_path)
            except Exception:
                pass

        # Need to launch Neovim
        if not self.in_tmux or self.is_self_target:
            # Direct exec replaces this process — _open_via_rpc won't
            # be reached, but files are passed on the command line here
            # as there's no RPC socket to come back to
            os.execvp("nvim", ["nvim", "--listen", self.socket_path] + self.files)

        # Split if the target pane doesn't exist or is busy
        if (
            self._run_tmux(["has-session", "-t", self.target_pane_addr]).returncode != 0
            or self._is_pane_busy(self.target_pane_addr)
        ):
            dir_flag = "-v" if self.active_split_dir == "h" else "-h"
            proc = self._run_tmux([
                "split-window", dir_flag, "-b", "-p", "50",
                "-t", self.target_pane_addr,
                "-P", "-F", "#{pane_id}",
            ])
            self.target_pane_addr = proc.stdout.strip()

        # Launch bare Neovim — no files, no -c flags, no quoting concerns
        launch_cmd = f" clear && nvim --listen {shlex.quote(self.socket_path)}"
        self._run_tmux(["send-keys", "-t", self.target_pane_addr, launch_cmd, "C-m"])

        # Wait for the socket to become available
        return self._wait_for_socket()


    def list_sockets(self, show_buffers: bool = False) -> int:
        """List all active nvp sockets, optionally with buffer/tab details."""
        import glob
        import pwd

        sockets = sorted(glob.glob("/tmp/nvimsocket-*"))

        if not sockets:
            print("No active nvp sockets found.")
            return 0

        current_uid = os.getuid()

        for socket_path in sockets:
            name = socket_path.replace("/tmp/nvimsocket-", "")

            # Determine socket ownership
            try:
                sock_stat = os.stat(socket_path)
                owner_uid = sock_stat.st_uid
                if owner_uid != current_uid:
                    try:
                        owner_name = pwd.getpwuid(owner_uid).pw_name
                    except KeyError:
                        owner_name = str(owner_uid)
                    ownership = f"  (owner: {owner_name})"
                else:
                    ownership = ""
            except OSError:
                ownership = "  (unable to stat)"

            print(f"\n  {name}  ({socket_path}){ownership}")

            if not show_buffers:
                continue

            # Don't attempt RPC on sockets owned by other users
            if ownership and "owner:" in ownership:
                print("    (skipped — owned by another user)")
                continue

            try:
                nvim = attach("socket", path=socket_path)

                # Collect dirty state across listed buffers only
                all_bufs = nvim.buffers
                dirty_count = sum(
                    1 for b in all_bufs
                    if nvim.call("buflisted", b.number)
                    and nvim.call("getbufvar", b.number, "&modified")
                )
                if dirty_count:
                    print(f"    ⚠ {dirty_count} unsaved buffer{'s' if dirty_count != 1 else ''}")

                # List tabs and their visible listed buffers
                tabs = nvim.tabpages
                for i, tab in enumerate(tabs, 1):
                    print(f"    Tab {i}:")
                    for win in tab.windows:
                        buf = win.buffer
                        if not nvim.call("buflisted", buf.number):
                            continue
                        modified = nvim.call("getbufvar", buf.number, "&modified")
                        flag = " [+]" if modified else "    "
                        name = buf.name or "[No Name]"
                        print(f"    {flag} {buf.number}: {name}")

                # List hidden listed buffers (loaded but not visible in any window)
                visible = {
                    win.buffer.number
                    for tab in tabs
                    for win in tab.windows
                }
                hidden = [
                    b for b in all_bufs
                    if b.number not in visible
                    and nvim.call("buflisted", b.number)
                ]
                if hidden:
                    print("    Hidden:")
                    for buf in hidden:
                        modified = nvim.call("getbufvar", buf.number, "&modified")
                        flag = " [+]" if modified else "    "
                        name = buf.name or "[No Name]"
                        print(f"    {flag} {buf.number}: {name}")

            except Exception as e:
                print(f"    (unable to connect: {e})")

        print()
        return 0

    def run(self) -> int:
        """Entry point: resolve settings, ensure Neovim, open files via RPC."""

        if self.args.buffers:
            return self.list_sockets(show_buffers=True)

        if self.args.list:
            return self.list_sockets(show_buffers=False)
        self._resolve_settings()

        try:
            nvim = self._ensure_nvim()
        except Exception:
            nvim = None

        if nvim is None:
            print("nvp: failed to connect to Neovim", file=sys.stderr)
            return 1

        try:
            self._open_via_rpc(nvim)
        except Exception:
            pass

        return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. Accepts optional argv for testing."""
    parser = argparse.ArgumentParser(
        prog="nvp",
        description=f"%(prog)s v{get_version()}: A spatial Neovim manager for Tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    parser.add_argument(
        "-p", "--pane",
        help="Override target tmux pane index",
    )
    parser.add_argument(
        "-s", "--split",
        choices=["v", "h"],
        help="Override split direction",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Files to open",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List all active nvp sockets",
    )
    parser.add_argument(
        "-b", "--buffers",
        action="store_true",
        help="List open buffers and tabs for each socket (implies --list)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point for pip console_scripts."""
    args = _parse_args(argv)
    nvp = NvpManager(args)
    sys.exit(nvp.run())


if __name__ == "__main__":
    main()

