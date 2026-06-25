from __future__ import annotations

import re
from pathlib import Path


def test_runtime_dependencies_pinned():
    """Verify all runtime dependencies use compatible-release (~=) or exact (==) pinning.
    
    This enforces the pinned-version policy for production safety in a trading system.
    Compatible-release (~=X.Y.Z) allows patch-level updates within X.Y.* but not X.(Y+1).
    """
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    
    in_dependencies = False
    found_deps = []
    unpinned = []
    
    for line in content.split('\n'):
        if 'dependencies = [' in line:
            in_dependencies = True
            continue
        if in_dependencies and ']' in line and not line.strip().startswith('#'):
            break
        if in_dependencies and line.strip() and not line.strip().startswith('#'):
            dep = line.strip().strip(',')
            found_deps.append(dep)
            if not ('~=' in dep or '==' in dep):
                unpinned.append(dep)
    
    assert found_deps, "No dependencies found in pyproject.toml"
    assert not unpinned, f"Unpinned dependencies found (must use ~= or ==): {unpinned}"


def test_dev_dependencies_pinned():
    """Verify all dev dependencies use compatible-release (~=) or exact (==) pinning."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    
    in_dev = False
    found_deps = []
    unpinned = []
    
    for line in content.split('\n'):
        if '[dependency-groups]' in line or 'dev = [' in line:
            in_dev = True
            continue
        if in_dev and ']' in line and not line.strip().startswith('#'):
            break
        if in_dev and line.strip() and not line.strip().startswith('#'):
            dep = line.strip().strip(',')
            found_deps.append(dep)
            if not ('~=' in dep or '==' in dep):
                unpinned.append(dep)
    
    assert found_deps, "No dev dependencies found in pyproject.toml"
    assert not unpinned, f"Unpinned dev dependencies found (must use ~= or ==): {unpinned}"


def test_test_dependencies_pinned():
    """Verify all test dependencies use compatible-release (~=) or exact (==) pinning."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    
    in_test = False
    found_deps = []
    unpinned = []
    
    for line in content.split('\n'):
        if 'test = [' in line:
            in_test = True
            continue
        if in_test and ']' in line and not line.strip().startswith('#'):
            break
        if in_test and line.strip() and not line.strip().startswith('#'):
            dep = line.strip().strip(',')
            found_deps.append(dep)
            if not ('~=' in dep or '==' in dep):
                unpinned.append(dep)
    
    assert found_deps, "No test dependencies found in pyproject.toml"
    assert not unpinned, f"Unpinned test dependencies found (must use ~= or ==): {unpinned}"


def test_pinned_versions_match_lock():
    """Verify that pinned versions are compatible with versions in uv.lock.
    
    This ensures local `uv sync` resolves to the same versions as Docker builds
    from requirements.lock (exported from uv.lock).
    """
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    requirements_lock = Path(__file__).parent.parent / "requirements.lock"
    
    if not requirements_lock.exists():
        return
    
    lock_content = requirements_lock.read_text()
    pyproject_content = pyproject.read_text()
    
    lock_versions = {}
    for line in lock_content.split('\n'):
        match = re.match(r'^([a-zA-Z0-9\-_.]+)==([\d.]+)', line)
        if match:
            pkg_name = match.group(1).lower()
            version = match.group(2)
            lock_versions[pkg_name] = version
    
    pyproject_deps = re.findall(r'"([a-zA-Z0-9\-_.]+)~=([\d.]+)"', pyproject_content)
    
    for pkg_name, pinned_version in pyproject_deps:
        normalized = pkg_name.lower()
        if normalized in lock_versions:
            lock_version = lock_versions[normalized]
            major_minor = '.'.join(pinned_version.split('.')[:2])
            lock_major_minor = '.'.join(lock_version.split('.')[:2])
            assert major_minor == lock_major_minor, (
                f"{pkg_name}~={pinned_version} incompatible with lock version {lock_version}"
            )


def test_lock_update_comment_present():
    """Verify that a comment notes the lock-update mechanism."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    
    assert 'uv run inv lock-update' in content, (
        "Comment about lock-update mechanism missing from pyproject.toml"
    )
