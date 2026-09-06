"""Explicit, non-secret build/version metadata collection."""

from importlib import metadata
import platform

from jobscraper.version import APP_VERSION

_NOT_INSTALLED = "NOT_INSTALLED"


def _distribution_version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return _NOT_INSTALLED


def collect_build_metadata() -> dict[str, str]:
    """Return only the version facts explicitly required by the build contract."""
    return {
        "application_version": APP_VERSION,
        "python_version": platform.python_version(),
        "fastapi_version": _distribution_version("fastapi"),
        "playwright_version": _distribution_version("playwright"),
        "pyinstaller_version": _distribution_version("pyinstaller"),
        "tzdata_version": _distribution_version("tzdata"),
        "browser_revision": _NOT_INSTALLED,
    }
