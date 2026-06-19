# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Pytest configuration for the isvctl test suite.

Patches the armada-bridge file logger so that importing modules that
pull in ``common.bridge_client`` (e.g. ``common.vpc``) during test
collection does not attempt to create log files in the user's home
directory.

Note: sys.path is NOT modified here. Each test file inserts its own
provider scripts path so that different providers' ``common`` packages
do not shadow each other.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

# Replace the file-based logger with a no-op mock so that importing
# ``common.bridge_client`` does not create log files on disk.
_mock_file_logger = MagicMock()
_mock_file_logger.get_file_logger.return_value = logging.getLogger("test.bridge_client")
patch.dict("sys.modules", {"common.file_logger": _mock_file_logger}).start()
