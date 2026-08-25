from __future__ import annotations

import json
import logging

from jmap_eas.observability import JsonFormatter, Metrics, configure_logging, get_logger


def test_json_formatter_produces_valid_json_with_expected_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="jmap_eas.test", level=logging.INFO, pathname=__file__, lineno=1, msg="hello %s",
        args=("world",), exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "jmap_eas.test"
    assert payload["message"] == "hello world"
    assert "time" in payload


def test_json_formatter_includes_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="jmap_eas.test", level=logging.WARNING, pathname=__file__, lineno=1, msg="sync failed",
        args=(), exc_info=None,
    )
    record.fields = {"account_id": "alice"}  # type: ignore[attr-defined]
    payload = json.loads(formatter.format(record))
    assert payload["account_id"] == "alice"


def test_get_logger_is_namespaced():
    logger = get_logger("app")
    assert logger.name == "jmap_eas.app"


def test_configure_logging_is_idempotent():
    configure_logging()
    logger = logging.getLogger("jmap_eas")
    handler_count = len(logger.handlers)
    configure_logging()
    assert len(logger.handlers) == handler_count


def test_metrics_increment_and_snapshot():
    metrics = Metrics()
    metrics.increment("requests_total")
    metrics.increment("requests_total")
    metrics.increment("errors_total")
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 2
    assert snapshot["errors_total"] == 1
    assert snapshot["sync_failures_total"] == 0


def test_metrics_snapshot_is_a_copy():
    metrics = Metrics()
    snapshot = metrics.snapshot()
    metrics.increment("requests_total")
    assert snapshot["requests_total"] == 0
