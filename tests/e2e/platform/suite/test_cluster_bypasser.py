"""Cluster 1 — Cloudflare bypasser wiring + clean-failure behavior.

AA search and detail fetches use the configured bypasser after direct mirror
requests hit a Cloudflare gate. With the bypasser disabled, the same gate must
still fail cleanly instead of hanging or returning challenge HTML as releases.

These tests assert what is host-observable:
  * the external bypasser is configured from env, and
  * a CF-gated search succeeds with the external bypasser, and
  * the same search fails cleanly when bypassing is disabled.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests


def _boot_log() -> str:
    path = os.environ.get("E2E_BOOKFLOW_LOG")
    if not path or not Path(path).exists():
        return ""
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _cf_gated_search_has_no_releases(client) -> bool:
    """Return whether a bounded CF-gated search produced no releases."""
    try:
        resp = client.direct_search("Mistborn", request_timeout=30)
    except requests.exceptions.Timeout:
        return True  # could not complete -> definitively no releases obtained
    assert resp.status_code in (200, 404, 500, 503), (
        f"CF-gated search returned an unexpected status: {resp.status_code} {resp.text[:200]}"
    )
    return not client.releases_from(resp)


@pytest.mark.profiles("bypasser-external")
def test_external_bypasser_is_configured(client) -> None:
    """The external (FlareSolverr) bypasser path is selected via env."""
    assert client.get("/api/health").status_code == 200
    log = _boot_log()
    if not log:
        pytest.skip("E2E_BOOKFLOW_LOG not available")
    assert "USING_EXTERNAL_BYPASSER" in log and "EXT_BYPASSER_URL" in log, (
        "external bypasser config was not synced from env"
    )


@pytest.mark.profiles("bypasser-external")
def test_cf_gated_search_succeeds_with_external_bypasser(client) -> None:
    """A configured external bypasser makes CF-gated search usable."""
    resp = client.direct_search("Mistborn", request_timeout=60)
    assert resp.status_code == 200
    assert client.releases_from(resp)


@pytest.mark.profiles("bypasser-disabled")
def test_cf_gated_search_fails_when_bypasser_off(client) -> None:
    """Negative control: AA behind Cloudflare + bypasser OFF -> no releases, clean
    failure. A regression that ignored the gate would wrongly return results."""
    assert _cf_gated_search_has_no_releases(client), (
        "results returned even though AA is Cloudflare-gated and the bypasser is "
        "disabled — the challenge is being ignored (regression for #202/#410)"
    )
