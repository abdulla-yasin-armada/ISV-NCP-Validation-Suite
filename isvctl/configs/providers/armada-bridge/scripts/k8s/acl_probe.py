#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""acl_probe — K8sApiNetworkAclCheck live probe script.

SSHes into the pre-provisioned Tenant B node (BM or VM, kept alive by
setup.py during the test phase) and probes Tenant A's K8s API endpoint.
Used as the commands.unauthorized_probe for K8sApiNetworkAclCheck.

Exit semantics (as expected by K8sApiNetworkAclCheck):
  non-zero  API blocked (timeout / connection refused) — isolation enforced → PASS
  0         API reachable — isolation NOT enforced → FAIL
  127       Configuration error (missing password/key or bad args)

Auth modes:
  BM (default) — password auth via BRIDGE_BM_SSH_PASS env var.
  VM           — key auth via --key-file <path to private key>.

Optional env:
  BRIDGE_BM_SSH_PASS  SSH password for BM probe node (required when --key-file not given).
  BRIDGE_JUMPHOST     Optional jumphost in user@host[:port] format.

Arguments:
  --bm-ip         IP of the Tenant B probe node (BM management IP or VM public IP).
  --bm-user       SSH user (default: ubuntu).
  --api-endpoint  Tenant A K8s API URL, e.g. https://10.1.2.3:6443.
  --key-file      Path to SSH private key (VM auth). Mutually exclusive with password auth.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.ssh_utils import ssh_run_key, ssh_run_password

_CURL_PROBE_TIMEOUT_S = 10
_SSH_TIMEOUT_S = 30


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-tenant K8s API ACL probe")
    parser.add_argument("--bm-ip", required=True, help="Tenant B probe node IP (BM mgmt IP or VM public IP)")
    parser.add_argument("--bm-user", default="ubuntu", help="SSH user")
    parser.add_argument("--api-endpoint", required=True, help="Tenant A K8s API URL")
    parser.add_argument("--key-file", default="", help="SSH private key path (VM mode). If omitted, password auth is used.")
    args = parser.parse_args()

    jumphost = os.environ.get("BRIDGE_JUMPHOST", "").strip()
    probe_url = args.api_endpoint.rstrip("/") + "/readyz"

    # curl exits non-zero on timeout / connection refused (isolation enforced → PASS).
    curl_cmd = (
        f"curl --max-time {_CURL_PROBE_TIMEOUT_S} -ks "
        f"-o /dev/null -w '%{{http_code}}' {probe_url}"
    )

    if args.key_file:
        key_path = str(Path(args.key_file).expanduser())
        rc, _stdout, _stderr = ssh_run_key(
            args.bm_ip,
            args.bm_user,
            key_path,
            curl_cmd,
            jumphost=jumphost,
            timeout=_SSH_TIMEOUT_S + _CURL_PROBE_TIMEOUT_S,
        )
    else:
        ssh_pass = os.environ.get("BRIDGE_BM_SSH_PASS", "").strip()
        if not ssh_pass:
            print("acl_probe: BRIDGE_BM_SSH_PASS is not set (required for BM probe node)", file=sys.stderr)
            return 127
        rc, _stdout, _stderr = ssh_run_password(
            args.bm_ip,
            args.bm_user,
            ssh_pass,
            curl_cmd,
            jumphost=jumphost,
            timeout=_SSH_TIMEOUT_S + _CURL_PROBE_TIMEOUT_S,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
