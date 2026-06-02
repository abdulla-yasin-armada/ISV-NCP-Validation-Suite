"""Generic polling utility for Armada Bridge provider scripts."""
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any


def poll_until(
    check_fn: Callable[[], tuple[bool, Any, str]],
    *,
    label: str,
    interval: int,
    timeout: int,
) -> Any:
    """Poll check_fn at `interval`-second intervals until done or timeout.

    check_fn() must return a 3-tuple:
      (True,  result,  status_msg)  — condition met; poll_until returns result
      (False, None,    status_msg)  — not yet; status_msg is printed to stderr

    Separating result from status_msg prevents large objects (e.g. node dicts)
    from being dumped to stderr when the condition is met.

    Progress is printed to stderr on every iteration showing elapsed time.
    On timeout, TimeoutError is raised (caught by @handle_bridge_errors).

    Example usage:
        def check():
            node = client.get(path)
            status = node.get("allocateStatus", "")
            if status == "done":
                return True, node, f"allocateStatus='{status}'"
            return False, None, f"allocateStatus='{status}'"

        node = poll_until(check, label="launch_instance", interval=15, timeout=540)
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()

    while True:
        done, result, status_msg = check_fn()
        elapsed = int(time.monotonic() - start)

        if done:
            print(
                f"[{label}] done after ~{elapsed}s — {status_msg}",
                file=sys.stderr,
            )
            return result

        print(
            f"[{label}] polling — {status_msg} ({elapsed}s elapsed, timeout={timeout}s)",
            file=sys.stderr,
        )

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"[{label}] timed out after {timeout}s — last status: {status_msg}"
            )

        time.sleep(interval)
