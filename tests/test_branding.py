# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations


def test_branding_constants():
    from modelquad import branding

    assert branding.PRODUCT_NAME == "ModelQuad"
    assert branding.PRODUCT_SLUG == "modelquad"
    # The private predecessor's names are not part of the public product.
    assert not hasattr(branding, "LEGACY_PRODUCT_NAME")
    assert not hasattr(branding, "LEGACY_CLI_COMMAND")


def test_version_lookup_falls_back_to_source_version_without_dist_info(monkeypatch):
    """A copy with no dist-info must still report the real product version
    (``--version``, the MCP handshake), rather than 'n/a'."""

    from importlib import metadata as importlib_metadata

    import modelquad
    from modelquad.branding import installed_version

    def nothing_resolves(name):
        raise importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", nothing_resolves)
    assert installed_version("n/a") == modelquad.__version__
