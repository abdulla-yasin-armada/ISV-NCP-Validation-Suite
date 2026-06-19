# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tar archive creation utilities.

This module provides utilities for creating tar archives for deployment.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Default patterns to exclude from archives
DEFAULT_EXCLUDES: list[str] = [
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    ".git",
    ".terraform",
    "*.tfstate",
    "*.tfstate.backup",
]


class ArchiveError(Exception):
    """Exception raised when archive creation fails."""


def _darwin_tar_create_flags() -> list[str]:
    """Flags for macOS bsdtar to omit extended attributes Linux tar cannot apply."""
    if sys.platform != "darwin":
        return []
    return ["--no-xattrs", "--no-mac-metadata"]


def remote_extract_command(archive_name: str) -> str:
    """Shell command to extract a deploy archive on Linux without macOS xattr noise."""
    return (
        f'if tar --warning=no-unknown-keyword -xzf "{archive_name}" 2>/dev/null; then\n'
        f"  :\n"
        f"else\n"
        f'  tar -xzf "{archive_name}" 2> >(grep -v "Ignoring unknown extended header keyword" >&2)\n'
        f"fi"
    )


class TarArchive:
    """Create tar archives for deployment.

    Example:
        >>> archive = TarArchive()
        >>> archive.create(
        ...     output=Path("deploy.tar.gz"),
        ...     paths=["isvctl/", "isvtest/", "pyproject.toml"],
        ...     excludes=[".venv", "__pycache__", "*.pyc"],
        ... )
    """

    def __init__(self, working_dir: Path | None = None) -> None:
        """Initialize archive creator.

        Args:
            working_dir: Working directory for archive creation (default: cwd)
        """
        self.working_dir = working_dir or Path.cwd()

    def create(
        self,
        output: Path,
        paths: list[str],
        excludes: list[str] | None = None,
        compress: bool = True,
    ) -> Path:
        """Create a tar archive.

        Args:
            output: Output archive path
            paths: List of paths to include in archive
            excludes: Patterns to exclude (default: DEFAULT_EXCLUDES)
            compress: Use gzip compression (default: True)

        Returns:
            Path to created archive

        Raises:
            ArchiveError: If archive creation fails
        """
        if excludes is None:
            excludes = DEFAULT_EXCLUDES.copy()

        # Validate paths exist
        for path in paths:
            full_path = self.working_dir / path
            if not full_path.exists():
                raise ArchiveError(f"Path not found: {path}")

        # Build tar command
        cmd = ["tar", *_darwin_tar_create_flags()]

        # Add compression flag
        if compress:
            cmd.append("-czf")
        else:
            cmd.append("-cf")

        cmd.append(str(output))

        # Add exclude patterns
        for pattern in excludes:
            cmd.extend(["--exclude", pattern])

        # Add paths to archive
        cmd.extend(paths)

        logger.debug(f"Creating archive: {' '.join(cmd)}")

        # Set environment to prevent macOS extended attributes
        env = os.environ.copy()
        env["COPYFILE_DISABLE"] = "1"

        try:
            result = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                env=env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or f"tar exited with code {result.returncode}"
                raise ArchiveError(f"Archive creation failed: {error_msg}")

            if not output.exists():
                raise ArchiveError(f"Archive was not created: {output}")

            size = output.stat().st_size
            size_str = self._format_size(size)
            logger.info(f"Created archive: {output} ({size_str})")

            return output

        except FileNotFoundError:
            raise ArchiveError("tar command not found in PATH")

    def _format_size(self, size_bytes: int) -> str:
        """Format byte size to human-readable string.

        Args:
            size_bytes: Size in bytes

        Returns:
            Human-readable size string
        """
        size: float = size_bytes
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"
