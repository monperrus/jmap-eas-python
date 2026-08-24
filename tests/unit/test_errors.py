from __future__ import annotations

from pyactivesync.exceptions import StatusError

from jmap_eas.errors import BackendError, ForbiddenError, UnknownAccountError, map_eas_exception


def test_unknown_account_error_type_and_message():
    error = UnknownAccountError("bob")
    assert error.type == "accountNotFound"
    assert error.account_id == "bob"
    assert "bob" in str(error)


def test_forbidden_error_type():
    assert ForbiddenError.type == "forbidden"


def test_map_eas_exception_redacts_cause_from_message():
    cause = StatusError("Sync", "12", "ProtocolError")
    error = map_eas_exception(cause)
    assert isinstance(error, BackendError)
    assert error.cause is cause
    assert "Sync" not in str(error)
    assert "12" not in str(error)
    assert error.type == "serverFail"
