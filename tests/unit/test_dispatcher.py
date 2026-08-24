from __future__ import annotations

import pytest

from jmap_eas.errors import ForbiddenError, InvalidArgumentsError
from jmap_eas.jmap.dispatcher import Dispatcher, resolve_pointer


def _env():
    return object()  # handlers below never touch env


# -- resolve_pointer ------------------------------------------------------------


def test_resolve_pointer_empty_path_returns_whole_value():
    assert resolve_pointer({"a": 1}, "") == {"a": 1}


def test_resolve_pointer_object_key():
    assert resolve_pointer({"a": {"b": 2}}, "/a/b") == 2


def test_resolve_pointer_array_index():
    assert resolve_pointer({"list": [10, 20, 30]}, "/list/1") == 20


def test_resolve_pointer_unescapes_tilde_and_slash():
    assert resolve_pointer({"a/b": {"c~d": 5}}, "/a~1b/c~0d") == 5


def test_resolve_pointer_wildcard_maps_over_array():
    value = {"list": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    assert resolve_pointer(value, "/list/*/id") == ["a", "b", "c"]


def test_resolve_pointer_wildcard_flattens_nested_arrays():
    value = {"list": [{"ids": ["a", "b"]}, {"ids": ["c"]}]}
    assert resolve_pointer(value, "/list/*/ids") == ["a", "b", "c"]


def test_resolve_pointer_missing_key_raises():
    with pytest.raises(ValueError):
        resolve_pointer({"a": 1}, "/b")


def test_resolve_pointer_index_out_of_range_raises():
    with pytest.raises(ValueError):
        resolve_pointer([1, 2], "/5")


def test_resolve_pointer_into_scalar_raises():
    with pytest.raises(ValueError):
        resolve_pointer(1, "/a")


# -- Dispatcher -----------------------------------------------------------------


def test_dispatcher_executes_calls_in_order_and_returns_call_id():
    order: list[str] = []

    def handler_a(env, args):
        order.append("a")
        return {"seen": args}

    def handler_b(env, args):
        order.append("b")
        return {"seen": args}

    dispatcher = Dispatcher({"A": handler_a, "B": handler_b})
    responses = dispatcher.execute(
        [("A", {"x": 1}, "c0"), ("B", {"y": 2}, "c1")], _env()
    )
    assert order == ["a", "b"]
    assert responses == [("A", {"seen": {"x": 1}}, "c0"), ("B", {"seen": {"y": 2}}, "c1")]


def test_dispatcher_unknown_method_returns_error_without_aborting_batch():
    def handler(env, args):
        return {"ok": True}

    dispatcher = Dispatcher({"Known/method": handler})
    responses = dispatcher.execute(
        [("Unknown/method", {}, "c0"), ("Known/method", {}, "c1")], _env()
    )
    assert responses[0] == ("error", {"type": "unknownMethod"}, "c0")
    assert responses[1] == ("Known/method", {"ok": True}, "c1")


def test_dispatcher_method_error_does_not_abort_batch():
    def failing(env, args):
        raise InvalidArgumentsError("bad")

    def ok(env, args):
        return {"ok": True}

    dispatcher = Dispatcher({"Fail": failing, "Ok": ok})
    responses = dispatcher.execute([("Fail", {}, "c0"), ("Ok", {}, "c1")], _env())
    assert responses[0] == ("error", {"type": "invalidArguments"}, "c0")
    assert responses[1] == ("Ok", {"ok": True}, "c1")


def test_dispatcher_resolves_result_reference():
    def get(env, args):
        return {"list": [{"id": "m1"}, {"id": "m2"}]}

    def query(env, args):
        return {"ids": args["ids"]}

    dispatcher = Dispatcher({"Get": get, "Query": query})
    responses = dispatcher.execute(
        [
            ("Get", {}, "c0"),
            ("Query", {"ids#": {"resultOf": "c0", "name": "Get", "path": "/list/*/id"}}, "c1"),
        ],
        _env(),
    )
    assert responses[1] == ("Query", {"ids": ["m1", "m2"]}, "c1")


def test_dispatcher_result_reference_to_wrong_method_name_is_invalid():
    def get(env, args):
        return {"list": []}

    def query(env, args):
        return {"ids": args.get("ids")}

    dispatcher = Dispatcher({"Get": get, "Query": query})
    responses = dispatcher.execute(
        [
            ("Get", {}, "c0"),
            ("Query", {"ids#": {"resultOf": "c0", "name": "WrongName", "path": "/list"}}, "c1"),
        ],
        _env(),
    )
    assert responses[1] == ("error", {"type": "invalidResultReference"}, "c1")


def test_dispatcher_result_reference_to_missing_call_id_is_invalid():
    def query(env, args):
        return {"ids": args.get("ids")}

    dispatcher = Dispatcher({"Query": query})
    responses = dispatcher.execute(
        [("Query", {"ids#": {"resultOf": "missing", "name": "Get", "path": "/list"}}, "c1")], _env()
    )
    assert responses[0] == ("error", {"type": "invalidResultReference"}, "c1")


def test_dispatcher_result_reference_to_errored_call_is_invalid():
    def failing(env, args):
        raise InvalidArgumentsError("bad")

    def query(env, args):
        return {"ids": args.get("ids")}

    dispatcher = Dispatcher({"Fail": failing, "Query": query})
    responses = dispatcher.execute(
        [
            ("Fail", {}, "c0"),
            ("Query", {"ids#": {"resultOf": "c0", "name": "Fail", "path": "/list"}}, "c1"),
        ],
        _env(),
    )
    assert responses[1] == ("error", {"type": "invalidResultReference"}, "c1")


def test_dispatcher_malformed_result_reference_is_invalid():
    def query(env, args):
        return {"ids": args.get("ids")}

    dispatcher = Dispatcher({"Query": query})
    responses = dispatcher.execute([("Query", {"ids#": "not-a-ref-object"}, "c1")], _env())
    assert responses[0] == ("error", {"type": "invalidResultReference"}, "c1")


def test_dispatcher_duplicate_call_ids_do_not_crash():
    def echo(env, args):
        return dict(args)

    dispatcher = Dispatcher({"Echo": echo})
    responses = dispatcher.execute(
        [("Echo", {"n": 1}, "dup"), ("Echo", {"n": 2}, "dup")], _env()
    )
    assert responses == [("Echo", {"n": 1}, "dup"), ("Echo", {"n": 2}, "dup")]


def test_dispatcher_propagates_forbidden_error_type():
    def forbidden(env, args):
        raise ForbiddenError("disabled")

    dispatcher = Dispatcher({"Denied": forbidden})
    responses = dispatcher.execute([("Denied", {}, "c0")], _env())
    assert responses == [("error", {"type": "forbidden"}, "c0")]
