#!/usr/bin/env python3
"""Error handling decorator for Armada Bridge provider scripts."""
from __future__ import annotations

import functools
import json
import sys
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., int])


def handle_bridge_errors(func: F) -> F:
    """Wrap main() to catch errors and emit structured JSON to stdout.

    On failure:
      - Prints the full traceback to stderr (for debugging / log files).
      - Prints a clean JSON to stdout with just {success, error, error_type}
        so isvctl can surface a readable message to the user without noise.

    - NotImplementedError → {"success": false, "error": "Not implemented: ..."}
    - Any other exception → {"success": false, "error": <message>, "error_type": <class>}
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> int:
        try:
            return func(*args, **kwargs)
        except NotImplementedError as exc:
            print(f"\n[ERROR] {exc}", file=sys.stderr)
            print(json.dumps({"success": False, "error": f"Not implemented: {exc}"}))
            return 1
        except (RuntimeError, ValueError) as exc:
            # Known user-facing errors (missing env vars, API rejections, bad config).
            # The message itself is the full diagnosis — no traceback needed.
            print(f"\n[ERROR] {exc}", file=sys.stderr)
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 1
        except Exception as exc:
            # Unexpected error (bug) — print full traceback to stderr for debugging.
            print("\n[ERROR] Unexpected failure:", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 1

    return wrapper  # type: ignore[return-value]
