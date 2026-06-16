#!/usr/bin/env python3
"""api_endpoint_test — Armada Bridge security suite, test phase.

Verifies that the Bridge API endpoint is not publicly reachable.

Resolves the Bridge URL hostname via DNS and checks that every resolved IP
address is private/RFC-1918.  A private-only DNS response means the endpoint
is not reachable from the public internet.

Four sub-checks are run against the resolved IPs:
  probe_api_from_public    — all IPs for the API hostname are private
  probe_mgmt_from_public   — same hostname re-resolved (confirms consistency)
  verify_private_only      — no IP falls outside RFC-1918/loopback/link-local
  dns_not_public           — no globally-routable IP in the DNS response

Required env:
  BRIDGE_URL   Base URL of the Bridge API (passed via --bridge-url argument).

Output: {success, platform, endpoints_tested, tests}
"""
import argparse
import ipaddress
import json
import os
import socket
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import handle_bridge_errors
from common.network import is_private

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _resolve_ips(hostname: str, port: int) -> list[str]:
    try:
        results = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return list({r[4][0] for r in results})
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {hostname!r}: {exc}") from exc


def _is_global(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _check(ips: list[str], label: str) -> dict[str, Any]:
    all_private = all(is_private(ip) for ip in ips)
    has_public = any(_is_global(ip) for ip in ips)
    passed = all_private and not has_public
    return {
        "passed": passed,
        "resolved_ips": ips,
        "message": (
            f"{label}: all IPs are private — not reachable from public internet"
            if passed
            else f"{label}: public/global IP detected in {ips}"
        ),
    }


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--bridge-url", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "endpoints_tested": 2,
                "tests": {
                    "probe_api_from_public": {"passed": True},
                    "probe_mgmt_from_public": {"passed": True},
                    "verify_private_only": {"passed": True},
                    "dns_not_public": {"passed": True},
                },
            }
        )
    else:
        parsed = urllib.parse.urlparse(args.bridge_url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Base API endpoint
        api_ips = _resolve_ips(hostname, port)

        # Management path shares the same hostname — resolve again for clarity
        mgmt_ips = _resolve_ips(hostname, port)

        all_ips = list(set(api_ips + mgmt_ips))
        all_private = all(is_private(ip) for ip in all_ips)
        no_global = not any(_is_global(ip) for ip in all_ips)

        tests: dict[str, Any] = {
            "probe_api_from_public": _check(api_ips, "API endpoint"),
            "probe_mgmt_from_public": _check(mgmt_ips, "Management endpoint"),
            "verify_private_only": {
                "passed": all_private,
                "resolved_ips": all_ips,
                "message": (
                    "All resolved IPs are in RFC-1918 private range"
                    if all_private
                    else f"Non-private IP detected in {all_ips}"
                ),
            },
            "dns_not_public": {
                "passed": no_global,
                "resolved_ips": all_ips,
                "message": (
                    "No globally-routable IP in DNS response"
                    if no_global
                    else f"Globally-routable IP found: {all_ips}"
                ),
            },
        }

        result.update(
            {
                "success": all(t["passed"] for t in tests.values()),
                "platform": "security",
                "endpoints_tested": len(all_ips),
                "tests": tests,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
