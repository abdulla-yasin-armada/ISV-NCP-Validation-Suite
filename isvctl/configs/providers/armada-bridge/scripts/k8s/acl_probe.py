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

SSHes into the pre-provisioned Tenant B BM node (kept alive by
setup.py (_provision_acl_probe_bm) during the test phase) and probes Tenant A's
K8s API endpoint. Used as the commands.unauthorized_probe for K8sApiNetworkAclCheck.

Exit semantics (as expected by K8sApiNetworkAclCheck):
  non-zero  API blocked (timeout / connection refused) — isolation enforced → PASS
  0         API reachable — isolation NOT enforced → FAIL
  127       Configuration error (missing password or bad args)

Required env:
  BRIDGE_BM_SSH_PASS  SSH password for the Tenant B BM node.
  BRIDGE_JUMPHOST     Optional jumphost in user@host[:port] format.

Arguments:
  --bm-ip         Management IP of the Tenant B BM node.
  --bm-user       SSH user (default: ubuntu).
  --api-endpoint  Tenant A K8s API URL, e.g. https://10.1.2.3:6443.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.ssh_utils import ssh_run_password

_CURL_PROBE_TIMEOUT_S = 10
_SSH_TIMEOUT_S = 30


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-tenant K8s API ACL probe")
    parser.add_argument("--bm-ip", required=True, help="Tenant B BM management IP")
    parser.add_argument("--bm-user", default="ubuntu", help="SSH user")
    parser.add_argument("--api-endpoint", required=True, help="Tenant A K8s API URL")
    args = parser.parse_args()

    ssh_pass = os.environ.get("BRIDGE_BM_SSH_PASS", "").strip()
    if not ssh_pass:
        print("acl_probe: BRIDGE_BM_SSH_PASS is not set", file=sys.stderr)
        return 127

    jumphost = os.environ.get("BRIDGE_JUMPHOST", "").strip()
    probe_url = args.api_endpoint.rstrip("/") + "/readyz"

    # curl exits non-zero on timeout / connection refused (isolation enforced → PASS).
    curl_cmd = (
        f"curl --max-time {_CURL_PROBE_TIMEOUT_S} -ks "
        f"-o /dev/null -w '%{{http_code}}' {probe_url}"
    )

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
