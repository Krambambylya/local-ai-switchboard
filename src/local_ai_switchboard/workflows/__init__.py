"""Packaged workflow manifests. The sample Klein entry is disabled on purpose."""

from pathlib import Path


def packaged_workflows_dir() -> Path:
    """Return the directory of manifests shipped with the package."""

    return Path(__file__).resolve().parent
