#!/usr/bin/env python3
"""
Bidirectional SQLite DB sync between host and Docker sandbox via docker cp.

Improvements over the original:
  - WAL checkpoint (TRUNCATE) before/after copies
  - Auto-discover container name (retry until found)
  - Error resilience with consecutive-failure re-discovery
  - Copies .db-wal and .db-shm alongside the main DB
  - fcntl file locking to prevent duplicate instances
"""

import os
import sys
import time
import fcntl
import sqlite3
import subprocess

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HOST_DB = "/Users/jpcliumbp/custom-parts-bureau/cpb.db"
SANDBOX_DB = "/sandbox/.hermes/workspace/cpb.db"
POLL_INTERVAL = 2  # seconds

LOCKFILE = "/tmp/cpb_db_sync.lock"
DISCOVERY_INTERVAL = 5  # seconds between container discovery retries
MAX_CONSECUTIVE_ERRORS = 10  # trigger re-discovery after this many failures

# ---------------------------------------------------------------------------
# File locking – prevent multiple sync instances
# ---------------------------------------------------------------------------

def acquire_lock():
    """Acquire an exclusive lock via fcntl. Exits if another instance holds it."""
    lock_fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ERROR: Another db_sync instance is already running. Exiting.")
        sys.exit(1)
    # Keep fd open for lifetime of process (lock released on close / exit)
    return lock_fd

# ---------------------------------------------------------------------------
# Container discovery
# ---------------------------------------------------------------------------

def discover_container():
    """Find the first running container whose name contains 'openshell'."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=openshell", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    names = result.stdout.strip().split("\n")
    names = [n for n in names if n]  # filter empties
    if not names:
        return None
    return names[0]  # use first matching container


def wait_for_container():
    """Block until a matching container is discovered, retrying every few seconds."""
    print("Searching for openshell container...")
    while True:
        name = discover_container()
        if name:
            print(f"Found container: {name}")
            return name
        print(f"  No container found. Retrying in {DISCOVERY_INTERVAL}s...")
        time.sleep(DISCOVERY_INTERVAL)

# ---------------------------------------------------------------------------
# WAL checkpoint helper
# ---------------------------------------------------------------------------

def wal_checkpoint(db_path):
    """Flush WAL to the main database file via PRAGMA wal_checkpoint(TRUNCATE)."""
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        print(f"  WAL checkpoint completed: {db_path}")
    except Exception as exc:
        print(f"  WAL checkpoint warning ({db_path}): {exc}")

# ---------------------------------------------------------------------------
# Docker helpers (all wrapped for resilience)
# ---------------------------------------------------------------------------

def docker_cp(src, dst):
    """Run 'docker cp' and return True on success."""
    try:
        subprocess.run(["docker", "cp", src, dst], check=True,
                       capture_output=True, text=True)
        return True
    except Exception as exc:
        print(f"  docker cp FAILED ({src} -> {dst}): {exc}")
        return False


def docker_exec(container, *cmd):
    """Run 'docker exec' and return (True, stdout) on success."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, *cmd],
            check=True, capture_output=True, text=True,
        )
        return True, result.stdout
    except Exception as exc:
        print(f"  docker exec FAILED ({' '.join(cmd)}): {exc}")
        return False, ""

# ---------------------------------------------------------------------------
# Copy helpers – main DB + WAL/SHM (best-effort)
# ---------------------------------------------------------------------------

def copy_host_to_sandbox(container):
    """Copy host DB (+ WAL/SHM) into the sandbox container."""
    wal_checkpoint(HOST_DB)

    ok = docker_cp(HOST_DB, f"{container}:{SANDBOX_DB}")
    if not ok:
        return False

    # Best-effort: copy WAL and SHM files if they exist
    for suffix in ("-wal", "-shm"):
        host_file = HOST_DB + suffix
        if os.path.exists(host_file):
            docker_cp(host_file, f"{container}:{SANDBOX_DB}{suffix}")

    # Fix ownership inside the container
    docker_exec(container, "chown", "sandbox:sandbox", SANDBOX_DB)
    for suffix in ("-wal", "-shm"):
        docker_exec(container, "chown", "sandbox:sandbox", SANDBOX_DB + suffix)

    return True


def copy_sandbox_to_host(container):
    """Copy sandbox DB (+ WAL/SHM) to the host."""
    ok = docker_cp(f"{container}:{SANDBOX_DB}", HOST_DB)
    if not ok:
        return False

    # Best-effort: copy WAL and SHM files from sandbox
    for suffix in ("-wal", "-shm"):
        docker_cp(f"{container}:{SANDBOX_DB}{suffix}", HOST_DB + suffix)

    wal_checkpoint(HOST_DB)
    return True

# ---------------------------------------------------------------------------
# Mtime helpers
# ---------------------------------------------------------------------------

def get_host_mtime():
    try:
        return int(os.path.getmtime(HOST_DB))
    except Exception:
        return 0


def get_sandbox_mtime(container):
    ok, out = docker_exec(container, "stat", "-c", "%Y", SANDBOX_DB)
    if ok:
        try:
            return int(out.strip())
        except ValueError:
            return 0
    return 0

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Prevent duplicate instances
    _lock_fd = acquire_lock()  # noqa: F841 – must stay open

    # Discover container (blocks until found)
    container = wait_for_container()

    # Startup banner
    print("=" * 60)
    print("  CPB Database Sync Daemon")
    print("=" * 60)
    print(f"  Host DB   : {HOST_DB}")
    print(f"  Sandbox DB: {SANDBOX_DB}")
    print(f"  Container : {container}")
    print(f"  Poll      : every {POLL_INTERVAL}s")
    print("=" * 60)

    consecutive_errors = 0

    last_host_mtime = get_host_mtime()
    last_sandbox_mtime = get_sandbox_mtime(container)

    # -- Initial sync --
    if last_sandbox_mtime == 0 and last_host_mtime > 0:
        print("Initial sync: Host -> Sandbox")
        if copy_host_to_sandbox(container):
            last_sandbox_mtime = get_sandbox_mtime(container)
        else:
            consecutive_errors += 1
    elif last_host_mtime == 0 and last_sandbox_mtime > 0:
        print("Initial sync: Sandbox -> Host")
        if copy_sandbox_to_host(container):
            last_host_mtime = get_host_mtime()
        else:
            consecutive_errors += 1

    # -- Main loop --
    while True:
        time.sleep(POLL_INTERVAL)

        try:
            current_host_mtime = get_host_mtime()
            current_sandbox_mtime = get_sandbox_mtime(container)

            if current_host_mtime > last_host_mtime:
                print("Host DB changed. Syncing to Sandbox...")
                if copy_host_to_sandbox(container):
                    consecutive_errors = 0
                    last_host_mtime = get_host_mtime()
                    last_sandbox_mtime = get_sandbox_mtime(container)
                else:
                    consecutive_errors += 1
                    print(f"  Consecutive errors: {consecutive_errors}")

            elif current_sandbox_mtime > last_sandbox_mtime:
                print("Sandbox DB changed. Syncing to Host...")
                if copy_sandbox_to_host(container):
                    consecutive_errors = 0
                    last_host_mtime = get_host_mtime()
                    last_sandbox_mtime = get_sandbox_mtime(container)
                else:
                    consecutive_errors += 1
                    print(f"  Consecutive errors: {consecutive_errors}")

        except Exception as exc:
            consecutive_errors += 1
            print(f"Unexpected error in sync loop: {exc}")
            print(f"  Consecutive errors: {consecutive_errors}")

        # Re-discover container after too many consecutive failures
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"Hit {MAX_CONSECUTIVE_ERRORS} consecutive errors. Re-discovering container...")
            container = wait_for_container()
            consecutive_errors = 0
            # Re-read mtimes with the new container
            last_host_mtime = get_host_mtime()
            last_sandbox_mtime = get_sandbox_mtime(container)
            print("Resumed sync with new container.")


if __name__ == "__main__":
    main()
