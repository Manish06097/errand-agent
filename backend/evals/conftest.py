"""Eval-suite hooks: the report plugin, behind a check that langfuse stayed unloaded.

The offline guards come from backend/conftest.py, which also forces Langfuse off. With
it off nothing may import langfuse (spike F1, architecture 16.4), so a run that did fails
`make evals` and leaves the table as it was.
"""

from __future__ import annotations

import sys

import pytest

from evals.report import pytest_sessionfinish as write_report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if "langfuse" in sys.modules:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = "langfuse was imported during the eval run, with Langfuse off; report not written"
        if reporter is not None:
            reporter.write_line(message, red=True, bold=True)
        else:
            print(message, file=sys.stderr)
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    write_report(session, exitstatus)
