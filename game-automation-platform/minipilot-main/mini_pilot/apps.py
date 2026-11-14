"""Compatibility adapter for the unified Unicapture app catalog.

Package mappings are maintained only in ``unicapture.app_configs``. Executor
callers keep importing this module so existing integrations do not break.
"""

from __future__ import annotations

from unicapture.app_configs import (
    infer_app_key_from_text,
    iter_app_package_aliases,
    register_runtime_app,
    resolve_app_key,
    resolve_app_package,
)


# Backward-compatible read view built from the capturer catalog.
APP_PACKAGES: dict[str, str] = iter_app_package_aliases()


def get_package(app_name: str) -> str | None:
    """Return a package id from a friendly name, catalog key, or package id."""
    return resolve_app_package(app_name)


def get_capture_app_key(app_name: str) -> str | None:
    """Return the Unicapture catalog key for a supported name/package."""
    return resolve_app_key(app_name)


def infer_capture_app_key(text: str) -> str | None:
    """Infer an Unicapture app key from a natural-language goal."""
    return infer_app_key_from_text(text)


def register_custom_app(name: str, package: str) -> None:
    """Register a non-persistent app mapping in the unified runtime catalog."""
    register_runtime_app(name, package)
    clean_name = name.strip()
    clean_package = package.strip()
    if clean_name and clean_package:
        APP_PACKAGES[clean_name] = clean_package


def list_supported_apps() -> list[str]:
    """Return every supported friendly name and Unicapture catalog key."""
    return sorted(iter_app_package_aliases())
