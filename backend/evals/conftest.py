"""Eval-suite hooks: the report plugin. The offline guards come from backend/conftest.py."""

from evals.report import pytest_sessionfinish

__all__ = ["pytest_sessionfinish"]
