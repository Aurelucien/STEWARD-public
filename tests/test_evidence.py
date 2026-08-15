import pytest

from local_steward.evidence import canonical_json, digest
from local_steward.errors import EvidenceError


def test_canonical_digest_ignores_mapping_order() -> None:
    assert digest({"payload": {"b": 2, "a": 1}}) == digest({"payload": {"a": 1, "b": 2}})


def test_nan_is_rejected() -> None:
    with pytest.raises(EvidenceError):
        canonical_json({"number": float("nan")})
