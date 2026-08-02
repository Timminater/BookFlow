"""Regression tests for browser security headers."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def main_module():
    """Import `bookflow.main` with background startup disabled."""
    with patch("bookflow.download.orchestrator.start"):
        import bookflow.main as main

        importlib.reload(main)
        return main


def _csp_directives(header: str) -> dict[str, set[str]]:
    directives: dict[str, set[str]] = {}
    for directive in header.split(";"):
        name, *values = directive.strip().split()
        if name:
            directives[name] = set(values)
    return directives


def test_csp_allows_foliate_blob_stylesheets_without_allowing_blob_scripts(main_module):
    response = main_module.app.test_client().get("/api/config")
    directives = _csp_directives(response.headers["Content-Security-Policy"])

    assert "blob:" in directives["style-src"]
    assert "blob:" not in directives["script-src"]
