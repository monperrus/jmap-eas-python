from __future__ import annotations

from jmap_eas.config import PolicyConfig
from jmap_eas.jmap import identity
from jmap_eas.jmap.dispatcher import Environment


def _env():
    return Environment(
        account_id="alice", database=None, sync=None, adapter=None, policy=PolicyConfig(),
        identity_email="alice@example.com",
    )


def test_get_returns_synthesized_identity_when_ids_is_none():
    result = identity.get(_env(), {})
    assert len(result["list"]) == 1
    assert result["list"][0]["email"] == "alice@example.com"
    assert result["list"][0]["mayDelete"] is False
    assert result["notFound"] == []


def test_get_finds_identity_by_id():
    result = identity.get(_env(), {"ids": [identity.IDENTITY_ID]})
    assert len(result["list"]) == 1
    assert result["notFound"] == []


def test_get_reports_unknown_ids_as_not_found():
    result = identity.get(_env(), {"ids": ["missing"]})
    assert result["list"] == []
    assert result["notFound"] == ["missing"]
