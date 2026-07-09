#!/usr/bin/env python3
"""SSH polling helpers for Armada Bridge provider scripts."""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Any


def wait_for_ssh(
    host: str,
    key_file: str,
    username: str = "ubuntu",
    timeout: int = 300,
) -> None:
    """Poll SSH until host accepts a connection or timeout exceeded.

    Args:
        host: IP or hostname to poll.
        key_file: Path to SSH private key file.
        username: SSH username (default: ubuntu).
        timeout: Max seconds to wait before raising TimeoutError.
    """
    interval = 15
    max_attempts = max(1, timeout // interval)
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "BatchMode=yes",
                    "-i",
                    key_file,
                    f"{username}@{host}",
                    "exit 0",
                ],
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0:
                print(f"  SSH ready after attempt {attempt}", file=sys.stderr)
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        print(
            f"  Waiting for SSH... (attempt {attempt}/{max_attempts})",
            file=sys.stderr,
        )
        time.sleep(interval)

    raise TimeoutError(f"SSH not ready on {username}@{host} after {timeout}s")


def parse_jumphost(jumphost: str) -> tuple[str, str, int]:
    """Parse 'user@host[:port]' into (user, host, port)."""
    user, rest = jumphost.split("@", 1)
    if ":" in rest:
        host, port_str = rest.rsplit(":", 1)
        return user, host, int(port_str)
    return user, rest, 22


def ssh_run_password(
    host: str,
    user: str,
    password: str,
    command: str,
    jumphost: str = "",
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a command on a remote host via password-authenticated SSH.

    Uses Paramiko so that no system sshpass binary is required. Supports an
    optional jumphost (key-authenticated) for multi-hop access.

    Returns:
        (exit_code, stdout, stderr) — all decoded as UTF-8.
    """
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jh_client: paramiko.SSHClient | None = None

    try:
        connect_kwargs: dict[str, Any] = {
            "username": user,
            "password": password,
            "timeout": timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if jumphost:
            jh_user, jh_host, jh_port = parse_jumphost(jumphost)
            jh_client = paramiko.SSHClient()
            jh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            jh_client.connect(
                jh_host, port=jh_port, username=jh_user,
                timeout=timeout, look_for_keys=True, allow_agent=True,
            )
            transport = jh_client.get_transport()
            assert transport is not None
            connect_kwargs["sock"] = transport.open_channel(
                "direct-tcpip", (host, 22), ("127.0.0.1", 0)
            )

        client.connect(host, **connect_kwargs)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        # Read buffers before recv_exit_status() to avoid deadlock:
        # recv_exit_status() blocks until the remote command exits, but the
        # remote side can block if the local SSH buffer is full. Draining
        # stdout and stderr first guarantees the channel stays unblocked.
        stdout_data = stdout.read().decode("utf-8", errors="replace")
        stderr_data = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout_data, stderr_data
    finally:
        client.close()
        if jh_client:
            try:
                jh_client.close()
            except Exception:
                pass


def ssh_run_key(
    host: str,
    user: str,
    key_file: str,
    command: str,
    jumphost: str = "",
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a command on a remote host via SSH key authentication.

    Mirrors ``ssh_run_password`` but uses a private key file instead of a
    password. Supports an optional jumphost (also key-authenticated).

    Returns:
        (exit_code, stdout, stderr) — all decoded as UTF-8.
    """
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jh_client: paramiko.SSHClient | None = None

    try:
        connect_kwargs: dict[str, Any] = {
            "username": user,
            "key_filename": key_file,
            "timeout": timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if jumphost:
            jh_user, jh_host, jh_port = parse_jumphost(jumphost)
            jh_client = paramiko.SSHClient()
            jh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            jh_client.connect(
                jh_host, port=jh_port, username=jh_user,
                timeout=timeout, look_for_keys=True, allow_agent=True,
            )
            transport = jh_client.get_transport()
            assert transport is not None
            connect_kwargs["sock"] = transport.open_channel(
                "direct-tcpip", (host, 22), ("127.0.0.1", 0)
            )

        client.connect(host, **connect_kwargs)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdout_data = stdout.read().decode("utf-8", errors="replace")
        stderr_data = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout_data, stderr_data
    finally:
        client.close()
        if jh_client:
            try:
                jh_client.close()
            except Exception:
                pass


def get_uptime_via_ssh(host: str, key_file: str, username: str = "ubuntu") -> float | None:
    """Return system uptime in seconds via SSH, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                "-i",
                key_file,
                f"{username}@{host}",
                "cat /proc/uptime | cut -d' ' -f1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None
