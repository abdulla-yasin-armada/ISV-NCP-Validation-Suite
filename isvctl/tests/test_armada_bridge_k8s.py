"""Tests for Armada Bridge K8s node provisioning helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROVISION_DIR = Path(__file__).resolve().parents[1] / "configs/providers/armada-bridge/scripts/k8s"
sys.path.insert(0, str(_PROVISION_DIR))

from provision_nodes import parse_node_ids, resolve_node_type  # noqa: E402


class TestResolveNodeType:
    def test_import_defaults_to_bare_metal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_TYPE", raising=False)
        assert resolve_node_type(discovery_flow=False) == "bareMetal"

    def test_discovery_defaults_to_vm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_TYPE", raising=False)
        assert resolve_node_type(discovery_flow=True) == "vm"

    def test_explicit_bare_metal_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_TYPE", "bareMetal")
        assert resolve_node_type(discovery_flow=True) == "bareMetal"

    def test_explicit_vm_override_on_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_TYPE", "vm")
        assert resolve_node_type(discovery_flow=False) == "vm"


class TestParseNodeIds:
    def test_cli_and_env_merge_deduplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_IDS", "aaa,bbb")
        assert parse_node_ids(cli_node_ids=["bbb", "ccc"]) == ["aaa", "bbb", "ccc"]

    def test_empty_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_IDS", raising=False)
        assert parse_node_ids(cli_node_ids=[]) == []

    def test_whitespace_and_empty_segments_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_IDS", " aaa , , bbb ")
        assert parse_node_ids(cli_node_ids=[]) == ["aaa", "bbb"]
