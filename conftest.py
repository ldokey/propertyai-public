from __future__ import annotations

from contextlib import contextmanager
import sys

import pytest


@pytest.fixture(autouse=True)
def _legacy_test_global_writer_boundary(monkeypatch, request):
    if request.node.get_closest_marker("real_global_writer") is not None:
        return

    import propertyai_core.global_writer as global_writer

    @contextmanager
    def no_op_scope(*_args, **_kwargs):
        yield object()

    def no_op_assert():
        return {"status": "TEST_FENCE_OK"}

    original_scope = global_writer.mutation_scope
    original_assert = global_writer.assert_current_production_writer
    monkeypatch.setattr(global_writer, "mutation_scope", no_op_scope)
    monkeypatch.setattr(global_writer, "assert_current_production_writer", no_op_assert)

    # Collection imports many writer modules before fixtures run. Patch only the
    # exact imported function objects, not arbitrary names with similar spelling.
    for module in list(sys.modules.values()):
        if module is None:
            continue
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        if namespace.get("mutation_scope") is original_scope:
            monkeypatch.setattr(module, "mutation_scope", no_op_scope)
        if namespace.get("assert_current_production_writer") is original_assert:
            monkeypatch.setattr(module, "assert_current_production_writer", no_op_assert)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_global_writer: exercise the accepted Thin Client boundary without the legacy-test no-op lease",
    )
