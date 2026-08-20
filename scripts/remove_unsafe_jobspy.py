#!/usr/bin/env python3
"""Remove the retired vulnerable JobSpy graph from CareerKit's owned venv."""
from __future__ import annotations

from importlib import metadata
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
import subprocess
import sys
from collections.abc import Callable


def _accepts_secure_markdownify(requires: list[str], version: Version) -> bool:
    """Whether installed JobSpy metadata accepts this secure converter."""
    for raw in requires:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            return False
        if canonicalize_name(requirement.name) != "markdownify":
            continue
        if requirement.marker and not requirement.marker.evaluate():
            continue
        if version in requirement.specifier:
            return True
    return False


def unsafe_retired_packages() -> list[str]:
    """Installed retired packages that setup should remove from its own venv."""
    try:
        markdownify = Version(metadata.version("markdownify"))
        markdownify_unsafe = markdownify < Version("0.14.1")
    except metadata.PackageNotFoundError:
        markdownify = None
        markdownify_unsafe = False
    except InvalidVersion:
        markdownify = None
        markdownify_unsafe = True

    try:
        jobspy = metadata.distribution("python-jobspy")
    except metadata.PackageNotFoundError:
        return ["markdownify"] if markdownify_unsafe else []

    if markdownify is None:
        packages = ["python-jobspy"]
        if markdownify_unsafe:
            packages.append("markdownify")
        return packages
    if (markdownify_unsafe or
            not _accepts_secure_markdownify(jobspy.requires or [], markdownify)):
        return ["python-jobspy", "markdownify"]
    return []


def has_unsafe_jobspy_pair() -> bool:
    """Compatibility wrapper for callers that need a yes/no migration check."""
    return bool(unsafe_retired_packages())


def cleanup(
    *,
    runner: Callable | None = None,
    emit: Callable[[str], None] = print,
) -> bool:
    """Uninstall the unsafe pair; return whether a migration was performed."""
    packages = unsafe_retired_packages()
    if not packages:
        return False
    emit(f"  removing unsupported vulnerable package(s): {', '.join(packages)} ...")
    invoke = runner or subprocess.run
    invoke(
        [sys.executable, "-m", "pip", "uninstall", "--quiet", "--yes",
         *packages],
        check=True,
    )
    emit(
        "  ! JobSpy currently has no supported secure installation. If the "
        "jobspy feed is active in profile/employers.yaml, set active: false "
        "before the next pull."
    )
    return True


if __name__ == "__main__":
    cleanup()
