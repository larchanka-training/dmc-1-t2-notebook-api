"""Tests for the canonical feature-demo notebook identity (``demo_id``).

Locks the contract value of ``DEMO_NAMESPACE`` and the determinism of the
per-user ``demo_id`` derivation. Both are shared with the frontend (UI #67);
a silent change here would orphan every existing feature-demo notebook.
"""

from uuid import UUID

from app.modules.notebooks.demo import DEMO_NAMESPACE, demo_id


def test_demo_id_is_deterministic_for_same_owner() -> None:
    owner = UUID("3b9c1d2e-4f50-4a61-8b72-9c8d7e6f5a40")
    assert demo_id(owner) == demo_id(owner)


def test_demo_id_differs_between_owners() -> None:
    owner_a = UUID("11111111-1111-1111-1111-111111111111")
    owner_b = UUID("22222222-2222-2222-2222-222222222222")
    assert demo_id(owner_a) != demo_id(owner_b)


def test_demo_namespace_is_the_frozen_contract_constant() -> None:
    """``DEMO_NAMESPACE`` is the FE/BE contract value — must not drift."""
    assert DEMO_NAMESPACE == UUID("7f3a2b14-9c8d-4e6f-b1a2-c3d4e5f60718")


def test_demo_id_known_vectors() -> None:
    """Regression guard for the namespace + uuid5 derivation.

    These vectors pin the exact output. If ``DEMO_NAMESPACE`` or the
    derivation changes, every persisted feature-demo notebook becomes
    unreachable — so this test must fail loudly on any such change.
    """
    assert demo_id(UUID("00000000-0000-0000-0000-000000000001")) == UUID(
        "bf6f2f5d-9d1e-5e9d-a71d-e8247b073860"
    )
    assert demo_id(UUID("00000000-0000-0000-0000-000000000002")) == UUID(
        "eb1fa42b-2da0-591d-b18a-c3d2d815374c"
    )
