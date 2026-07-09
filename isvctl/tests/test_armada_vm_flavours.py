"""Unit tests for armada-bridge VM flavour resolution and allocate payload."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BRIDGE_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "configs" / "providers" / "armada-bridge" / "scripts"
)
if str(BRIDGE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SCRIPTS))

from common import vm as vm_mod

_FLAVOUR_A = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "gpu.small",
    "available": 2,
    "gpuCount": 1,
    "gpuType": "A100",
}
_FLAVOUR_B = {
    "id": "22222222-2222-2222-2222-222222222222",
    "name": "gpu.large",
    "available": 0,
    "gpuCount": 1,
}
_FLAVOUR_2GPU = {
    "id": "33333333-3333-3333-3333-333333333333",
    "name": "gpu.2x",
    "available": 5,
    "gpuCount": 2,
}


class TestPickGpuFlavour:
    def test_picks_available_gpu_flavour(self):
        picked = vm_mod.pick_gpu_flavour([_FLAVOUR_B, _FLAVOUR_A])
        assert picked is _FLAVOUR_A

    def test_matches_by_name(self):
        picked = vm_mod.pick_gpu_flavour(
            [_FLAVOUR_A, _FLAVOUR_B], explicit_name="gpu.large",
        )
        assert picked is None

    def test_explicit_uuid(self):
        picked = vm_mod.pick_gpu_flavour(
            [_FLAVOUR_A, _FLAVOUR_B],
            explicit_id="11111111-1111-1111-1111-111111111111",
        )
        assert picked is _FLAVOUR_A

    def test_requires_min_available(self):
        picked = vm_mod.pick_gpu_flavour([_FLAVOUR_A], min_available=3)
        assert picked is None
        picked = vm_mod.pick_gpu_flavour([_FLAVOUR_A], min_available=2)
        assert picked is _FLAVOUR_A

    def test_exact_gpu_count(self):
        picked = vm_mod.pick_gpu_flavour(
            [_FLAVOUR_2GPU, _FLAVOUR_A], exact_gpu_count=1,
        )
        assert picked is _FLAVOUR_A


class TestResolveVmFlavorTemplateId:
    def test_env_template_id_wins_in_strict_mode(self):
        client = MagicMock()
        env = {"BRIDGE_VM_FLAVOR_TEMPLATE_ID": "aaaa-bbbb", "BRIDGE_VM_FLAVOR_STRICT": "1"}
        with patch.dict("os.environ", env, clear=False):
            flavour_id, label = vm_mod.resolve_vm_flavor_template_id(client, "tenant-1", "")
        assert flavour_id == "aaaa-bbbb"
        client.get.assert_not_called()

    def test_auto_discover_before_env(self):
        client = MagicMock()
        env = {"BRIDGE_VM_FLAVOR_TEMPLATE_ID": "aaaa-bbbb"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(vm_mod, "list_vm_flavours", return_value=[_FLAVOUR_B, _FLAVOUR_A]):
                flavour_id, label = vm_mod.resolve_vm_flavor_template_id(client, "tenant-1", "")
        assert flavour_id == _FLAVOUR_A["id"]
        assert label == "gpu.small"

    def test_env_template_id_fallback_when_auto_misses(self):
        client = MagicMock()
        env = {"BRIDGE_VM_FLAVOR_TEMPLATE_ID": "aaaa-bbbb"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(vm_mod, "list_vm_flavours", return_value=[_FLAVOUR_2GPU]):
                flavour_id, label = vm_mod.resolve_vm_flavor_template_id(client, "tenant-1", "")
        assert flavour_id == "aaaa-bbbb"

    def test_auto_discover_requires_capacity_for_vm_count(self):
        client = MagicMock()
        low = {**_FLAVOUR_A, "available": 1}
        with patch.object(vm_mod, "list_vm_flavours", return_value=[low]):
            with pytest.raises(RuntimeError, match="no env fallback configured"):
                vm_mod.resolve_vm_flavor_template_id(client, "tenant-1", "", vm_count=2)

    def test_name_hint_fallback(self):
        client = MagicMock()
        with patch.object(vm_mod, "list_vm_flavours", return_value=[_FLAVOUR_A]):
            flavour_id, label = vm_mod.resolve_vm_flavor_template_id(
                client, "tenant-1", "gpu.small",
            )
        assert flavour_id == _FLAVOUR_A["id"]
        assert label == "gpu.small"

    def test_no_flavours_raises_without_fallback(self):
        client = MagicMock()
        with patch.object(vm_mod, "list_vm_flavours", return_value=[]):
            with pytest.raises(RuntimeError, match="No VM flavours returned"):
                vm_mod.resolve_vm_flavor_template_id(client, "tenant-1", "")


class TestAllocateVm:
    def test_posts_json_body(self):
        client = MagicMock()
        client.post.return_value = {"id": "vm-1"}
        with patch.dict("os.environ", {}, clear=False):
            vm_mod.allocate_vm(
                client,
                "tenant-1",
                name="test-vm",
                flavor_template_id="11111111-1111-1111-1111-111111111111",
                public_key="ssh-rsa AAAA",
            )
        client.post.assert_called_once()
        path, body = client.post.call_args[0][0], client.post.call_args[0][1]
        assert path == "/orchestrator/tenants/tenant-1/vms"
        assert body["name"] == "test-vm"
        assert body["flavorTemplateId"] == "11111111-1111-1111-1111-111111111111"
        assert body["vmSSHKey"] == "ssh-rsa AAAA"
        assert "flavor" not in body

    def test_requires_key_material(self):
        client = MagicMock()
        with pytest.raises(ValueError, match="public_key"):
            vm_mod.allocate_vm(
                client,
                "tenant-1",
                name="test-vm",
                flavor_template_id="11111111-1111-1111-1111-111111111111",
            )
