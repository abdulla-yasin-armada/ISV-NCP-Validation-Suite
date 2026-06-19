"""Tests for Armada Bridge K8s node provisioning helpers (now in k8s/setup.py)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)


def _load_k8s_setup():
    """Import k8s.setup as a package module with required paths in sys.path."""
    if str(BRIDGE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(BRIDGE_SCRIPTS))
    return importlib.import_module("k8s.setup")


_setup = _load_k8s_setup()
_resolve_k8s_node_type = _setup._resolve_k8s_node_type
_parse_k8s_node_ids = _setup._parse_k8s_node_ids


class TestResolveNodeType:
    def test_import_defaults_to_bare_metal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_TYPE", raising=False)
        assert _resolve_k8s_node_type(discovery_flow=False) == "bareMetal"

    def test_discovery_defaults_to_vm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_TYPE", raising=False)
        assert _resolve_k8s_node_type(discovery_flow=True) == "vm"

    def test_explicit_bare_metal_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_TYPE", "bareMetal")
        assert _resolve_k8s_node_type(discovery_flow=True) == "bareMetal"

    def test_explicit_vm_override_on_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_TYPE", "vm")
        assert _resolve_k8s_node_type(discovery_flow=False) == "vm"


class TestParseNodeIds:
    def test_cli_and_env_merge_deduplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_IDS", "aaa,bbb")
        assert _parse_k8s_node_ids(cli_node_ids=["bbb", "ccc"]) == ["aaa", "bbb", "ccc"]

    def test_empty_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_K8S_NODE_IDS", raising=False)
        assert _parse_k8s_node_ids(cli_node_ids=[]) == []

    def test_whitespace_and_empty_segments_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_K8S_NODE_IDS", " aaa , , bbb ")
        assert _parse_k8s_node_ids(cli_node_ids=[]) == ["aaa", "bbb"]
