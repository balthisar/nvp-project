#!/usr/bin/env python3
"""
nvp.py - Neovim Panes
A spatial Neovim manager for Tmux: Open files in dedicated panes 
without leaving your current shell.

Module level constants:
    CONFIG_DIR          XDG configuration path to our defaults directory.
    CONFIG_FILE         Path to the configuration file itself.
    USER_DEFAULT_PANE   The pane number where Neovim will be opened by default.
    SPLIT_DIRECTION     The default split direction when the pane is already busy.
    IN_TMUX             A simple flag indicating whether or not we're inside Tmux.
"""

import os
import sys
import time
import subprocess
import shlex
import configparser
import shutil
import argparse
from pathlib import Path
from importlib.metadata import version, PackageNotFoundError
from pynvim import attach


def get_version():
    """Get version information.
    Version information comes from the toml file the describes the project.
    """
    try:
        return version("nvp")
    except PackageNotFoundError:
        return "1.0-dev"


def load_config():
    """Load the user's configuration.
    If the user configuration file isn't present, then create it from the
    default template. If the template isn't present, then manually create
    the user configuration with same basic values.
    """
    config = configparser.ConfigParser(allow_no_value=True)
    package_dir = Path(__file__).parent.absolute()
    default_template = package_dir / "nvp.defaults.conf"
    
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if default_template.exists():
            shutil.copy(default_template, CONFIG_FILE)
        else:
            with open(CONFIG_FILE, 'w') as f:
                f.write("[Settings]\nuser_default_pane = None\nsplit_direction = v\n")
    
    config.read(CONFIG_FILE)
    return config['Settings']


def run_tmux(args):
    """Run a Tmux command.
    If we're in tmux, run a tmux command, returning its status.
    """
    if not IN_TMUX:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
    try:
        return subprocess.run(["tmux"] + args, capture_output=True, text=True, check=False)
    except Exception:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")


def is_pane_busy(pane_addr):
    """Determines if a pane is occupied by a non-shell process.
    We'll use this check to create a new split if the target plane is already
    busy with something else.
    """
    if run_tmux(["has-session", "-t", pane_addr]).returncode != 0:
        return False
    proc = run_tmux(["display-message", "-t", pane_addr, "-p", "#{pane_current_command}"])
    cmd = proc.stdout.strip().lower()
    # Shell-agnostic check
    return cmd not in ["zsh", "bash", "sh", "fish", "dash", "tcsh"]


def monitor_window(nvim_obj, buf_id):
    """Performs blocking while `buf_id` exists.
    Certain invocations depend on waiting until the editor is done before
    proceeding, such as `git commit` or using C-x C-e to edit the command
    line in the editor.
    """
    try:
        while nvim_obj.call("win_findbuf", buf_id):
            time.sleep(0.05)
    except (EOFError, OSError, Exception):
        pass


def main():
    """Run Neovim as appropriate given our varying circumstances.
    """

    #
    # Handle command line interface arguments, using the standard library
    # configparser module.
    #
    parser = argparse.ArgumentParser(
        prog="nvp",
        description="A spatial Neovim manager for Tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("-p", "--pane", help="Override target tmux pane index")
    parser.add_argument("-s", "--split", choices=['v', 'h'], help="Override split direction")
    parser.add_argument("files", nargs="*", help="Files to open")
    args = parser.parse_args()

    #
    # Settings resolution
    #
    current_pane_id = os.environ.get("TMUX_PANE") 
    base_idx_proc = run_tmux(["show-options", "-gv", "pane-base-index"])
    stdout_val = getattr(base_idx_proc, "stdout", "") or ""
    base_idx = stdout_val.strip() or "1"

    target_idx = args.pane or os.environ.get("NV_TARGET_PANE") or USER_DEFAULT_PANE or base_idx
    target_pane_addr = f".{target_idx}"
    active_split_dir = args.split or SPLIT_DIRECTION
    
    #
    # We need to ensure that the target isn't busy before trying to send-keys
    # to it, and if it's busy, see if it's busy because we're running.
    #
    resolved_target_id = None
    if IN_TMUX:
        proc = run_tmux(["display-message", "-t", target_pane_addr, "-p", "#{pane_id}"])
        resolved_target_id = proc.stdout.strip()

    is_self_target = (current_pane_id == resolved_target_id) and IN_TMUX

    #
    # We're going to manage sockets names automatically ourselves, based on both
    # the session and the window. This will allow us to run up to one instance
    # of Neovim per tmux window in each session.
    #
    if IN_TMUX:
        info_proc = run_tmux(["display-message", "-p", "#S:#I"])
        session, window = info_proc.stdout.strip().split(':')
    else:
        session, window = "standalone", "0"
    
    #
    # Make a socket path using what we've gleaned above (unless the standard
    # NVIM_LISTEN_ADDRESS is already set), and get a list of files that we'll
    # open via RPC or the command line.
    #
    socket_path = os.environ.get("NVIM_LISTEN_ADDRESS") or f"/tmp/nvimsocket-{session}-{window}"
    files = [os.path.realpath(os.path.abspath(f)) for f in args.files]

    #
    # Determine whether this is a blocking edit or not. Here we're tying to
    # check for common git filenames or temp file filenames used by various
    # shells for temp files (such as visudo or C-x C-e). 
    #
    system_filenames = ["COMMIT_EDITMSG", "GIT_REBASE_TODO", "MERGE_MSG"]
    is_shell_tmp = any(
        (os.path.basename(f).startswith(("zsh", "bash", "fish")) or "fish.cmd" in f)
        and ("/tmp" in f or "/private/tmp" in f) for f in files
    )
    is_git_tmp = any(os.path.basename(f) in system_filenames for f in files)
    should_block = is_shell_tmp or is_git_tmp

    try:
        #
        # --- RPC ATTEMPT ---
        #
        if os.path.exists(socket_path):
            nvim = attach('socket', path=socket_path)
            if files:
                first_file = nvim.call("fnameescape", files[0])
                nvim.command(f"edit {first_file}")
                # Fix for shell-specific temp file formatting
                if is_shell_tmp:
                    nvim.command("setlocal nofixeol noendofline")
                for f in files[1:]:
                    nvim.command(f"badd {nvim.call('fnameescape', f)}")
                
                buf_id = nvim.current.buffer.number
                if IN_TMUX:
                    run_tmux(["select-pane", "-t", target_pane_addr])
                if should_block:
                    monitor_window(nvim, buf_id)
                    if IN_TMUX:
                        run_tmux(["last-pane"])
                sys.exit(0) 
            else:
                if IN_TMUX:
                    run_tmux(["select-pane", "-t", target_pane_addr])
                sys.exit(0)
        else:
            raise Exception("No socket found.")

    except Exception:
        #
        # --- FALLBACK: LAUNCH NEW ---
        #
        if not IN_TMUX or is_self_target:
            os.execvp("nvim", ["nvim", "--listen", socket_path] + files)

        if run_tmux(["has-session", "-t", target_pane_addr]).returncode != 0 or is_pane_busy(target_pane_addr):
            dir_flag = "-v" if active_split_dir == "h" else "-h"
            proc = run_tmux(["split-window", dir_flag, "-b", "-p", "50", "-t", target_pane_addr, "-P", "-F", "#{pane_id}"])
            target_pane_addr = proc.stdout.strip()

        launch_args = ["nvim", "--listen", socket_path] + files
        launch_cmd = " clear && " + " ".join(shlex.quote(arg) for arg in launch_args)

        #
        # We do *not* want to allow Neovim to add anything to the end of the
        # line if we're editing shell temporary files, as this interferes with
        # quotes and confuses the shell upon return.
        #
        if is_shell_tmp:
            launch_cmd += " -c 'set nofixeol noendofline'"

        #
        # Finally, use send-keys to launch in the correct pane, and focus
        # that pane for use.
        #
        run_tmux(["send-keys", "-t", target_pane_addr, launch_cmd, "C-m"])
        run_tmux(["select-pane", "-t", target_pane_addr])

        #
        # In order to block successfully, we have to first wait for the
        # socket to be available, and then monitor the existence of the
        # buffer until it stops existing.
        #
        if should_block and files:
            nvim_fallback = None
            for _ in range(50): 
                try:
                    time.sleep(0.05)
                    nvim_fallback = attach('socket', path=socket_path)
                    break
                except: continue
            
            if nvim_fallback:
                monitor_window(nvim_fallback, nvim_fallback.current.buffer.number)
            run_tmux(["last-pane"])
        sys.exit(0)


CONFIG_DIR = Path.home() / ".config" / "nvp"
CONFIG_FILE = CONFIG_DIR / "nvp.conf"

_cfg = load_config()

USER_DEFAULT_PANE = None if _cfg.get('user_default_pane') == 'None' else _cfg.get('user_default_pane')
SPLIT_DIRECTION = _cfg.get('split_direction', 'v')
IN_TMUX = "TMUX" in os.environ


if __name__ == "__main__":
    main()

