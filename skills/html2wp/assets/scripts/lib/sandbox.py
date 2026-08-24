"""Contain builds from projects the operator did not write.

The original project is never a writable container mount. A disposable copy
is built instead. Dependency acquisition receives network access but no
project source and runs with lifecycle scripts disabled; dependency lifecycle
and the actual project build receive the source copy but no network.

There is deliberately no automatic host fallback. ``H2WP_NO_SANDBOX=1`` is
the explicit, dangerous escape hatch for a project the operator trusts.
"""

import atexit
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

__all__ = [
    "NODE_IMAGE",
    "available",
    "copy_build_output",
    "prepare_workspace",
    "promote_dependencies",
    "reason_unavailable",
    "run_in_sandbox",
    "unsafe_override",
    "validate_dependency_metadata",
    "warn_unsandboxed",
]

# Official multi-platform node:22-bookworm-slim index, resolved 2026-08-24.
NODE_IMAGE = os.environ.get(
    "H2WP_BUILD_IMAGE",
    "node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436",
)

MEMORY = os.environ.get("H2WP_BUILD_MEMORY", "4g")
CPUS = os.environ.get("H2WP_BUILD_CPUS", "4")
PIDS = os.environ.get("H2WP_BUILD_PIDS", "1024")
OUTPUT_MAX_FILES = int(os.environ.get("H2WP_OUTPUT_MAX_FILES", "100000"))
OUTPUT_MAX_BYTES = int(os.environ.get("H2WP_OUTPUT_MAX_BYTES", str(2 * 1024**3)))

_workspace_roots = []


def unsafe_override():
    return os.environ.get("H2WP_NO_SANDBOX") == "1"


def reason_unavailable():
    """Why Docker cannot be used, independent of the unsafe override."""
    if not shutil.which("docker"):
        return "docker is not installed"
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return "docker is installed but not responding"
    if result.returncode != 0:
        return "docker is installed but not running"
    return None


def available():
    return reason_unavailable() is None


# What must not be copied into the build sandbox.
#
# The sandbox exists so an untrusted `npm run build` cannot reach the machine
# it runs on — but it was handed the whole project first, minus node_modules
# and .git. So the build script it was containing could read `.env`, `.ssh/`
# or `.aws/` out of its own working copy and write the contents into `dist/`,
# which is then uploaded and published. Isolating the process and then giving
# it the secrets is not isolation.
#
# This is the twin of SECRET_NAMES/SECRET_DIRS in secret-filter.mjs, in the
# other language. They are checked against each other by
# test-build-sandbox.sh; a name added there belongs here too.
_SECRET_DIRS = {
    ".ssh", ".aws", ".gcloud", ".azure", ".docker", ".gnupg", ".kube",
    ".terraform", "node_modules", ".git", ".hg", ".svn", ".astro",
    "_original", ".vscode", ".idea",
}

_SECRET_NAME_RE = re.compile(
    r"""^(
        \.env(rc|\..*)?          |
        \.npmrc                  |
        \.yarnrc(\.yml)?         |
        \.pypirc                 |
        \.netrc                  |
        \.htpasswd               |
        id_(rsa|dsa|ecdsa|ed25519) |
        credentials(\.json|\.ya?ml)? |
        service-account.*\.json  |
        .*\.(pem|key|p12|pfx|keystore|jks) |
        secrets?\.(json|ya?ml|toml) |
        terraform\.tfvars        |
        .*\.sqlite3?             |
        .*\.sql                  |
        .*\.(zip|tar|tgz|gz|bz2|xz|7z|rar) |
        .*\.(bak|backup|dump)
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

#: Names withheld from the last prepare_workspace(), for the caller to report.
withheld = []


def _ignore_source(_directory, names):
    dropped = {
        name for name in names
        if name in _SECRET_DIRS or _SECRET_NAME_RE.match(name)
    }
    # A build that needed one of these is a build that will fail visibly, with
    # the name in hand — better than one that quietly published it.
    withheld.extend(sorted(dropped - {"node_modules", ".git"}))
    return dropped


def prepare_workspace(project):
    """Return ``(work, deps)`` disposable directories for one build."""
    project = Path(project)
    if project.is_symlink() or not project.is_dir():
        raise ValueError(f"project root is not a real directory: {project}")
    root = Path(tempfile.mkdtemp(prefix="h2wp-build-"))
    work, deps = root / "work", root / "deps"
    shutil.copytree(project, work, symlinks=True, ignore=_ignore_source)
    deps.mkdir()
    copied = False
    for name in ("package.json", "package-lock.json", "npm-shrinkwrap.json"):
        source = project / name
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, deps / name)
            copied = True
    if not copied or not (deps / "package.json").is_file():
        shutil.rmtree(root, ignore_errors=True)
        raise ValueError("package.json is missing or is a symlink")
    _workspace_roots.append(root)
    return work, deps


@atexit.register
def _cleanup_workspaces():
    for root in _workspace_roots:
        shutil.rmtree(root, ignore_errors=True)


def _dependency_spec_allowed(spec):
    if not isinstance(spec, str) or not spec:
        return False
    lowered = spec.lower()
    forbidden = (
        "http:", "https:", "git:", "git+", "github:", "gitlab:",
        "bitbucket:", "file:", "link:", "/", "./", "../",
        # "~/" and not "~". A bare tilde is npm's ordinary tilde range —
        # "~5.4.0" means 5.4.x and appears in a large share of real
        # package.json files. Forbidding the character refused those projects
        # outright with UNSAFE_DEPENDENCY_SOURCE, which is a worse outcome
        # than the home-relative path it was written to stop.
        "~/",
    )
    return not lowered.startswith(forbidden)


def validate_dependency_metadata(project):
    """Reject dependency metadata that can steer npm at arbitrary hosts."""
    project = Path(project)
    try:
        package = json.loads((project / "package.json").read_text())
    except (OSError, ValueError) as err:
        return f"package.json is not readable JSON ({err})"
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        values = package.get(field, {})
        if not isinstance(values, dict):
            return f"package.json {field} must be an object"
        for name, spec in values.items():
            if not isinstance(name, str) or not _dependency_spec_allowed(spec):
                return f"{field}.{name} uses a non-registry dependency: {spec!r}"

    for lock_name in ("package-lock.json", "npm-shrinkwrap.json"):
        lock_path = project / lock_name
        if not lock_path.exists():
            continue
        try:
            lock = json.loads(lock_path.read_text())
        except (OSError, ValueError) as err:
            return f"{lock_name} is not readable JSON ({err})"
        entries = list((lock.get("packages") or {}).values())
        entries += list((lock.get("dependencies") or {}).values())
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            resolved = entry.get("resolved")
            if resolved is None:
                continue
            if not isinstance(resolved, str) or not resolved.startswith("https://registry.npmjs.org/"):
                return f"{lock_name} resolves outside registry.npmjs.org: {resolved!r}"
    return None


def promote_dependencies(deps, work):
    source = Path(deps) / "node_modules"
    if not source.is_dir() or source.is_symlink():
        raise ValueError("npm install produced no safe node_modules directory")
    target = Path(work) / "node_modules"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)


def run_in_sandbox(command, project, timeout, label, *, network=False):
    """Run a shell command in a disposable build directory."""
    uid, gid = os.getuid(), os.getgid()
    name = f"h2wp-build-{secrets.token_hex(6)}"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--user", f"{uid}:{gid}",
        "-v", f"{Path(project).resolve()}:/work:rw",
        "-w", "/work",
        "--tmpfs", "/home/build:rw,size=512m,uid=%d,gid=%d" % (uid, gid),
        "--tmpfs", "/tmp:rw,size=1g",
        "-e", "HOME=/home/build",
        "-e", "npm_config_cache=/home/build/.npm",
        "-e", "CI=1",
        "--network", "bridge" if network else "none",
        "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL",
        "--memory", MEMORY,
        "--cpus", CPUS,
        "--pids-limit", PIDS,
        NODE_IMAGE,
        "sh", "-c", command,
    ]
    net = "registry metadata phase" if network else "network disabled"
    print(f"  ({label} in disposable container; {net})")
    try:
        return subprocess.run(argv, timeout=timeout)
    except BaseException:
        _force_remove(name)
        raise


def _force_remove(name):
    for verb in ("kill", "rm"):
        try:
            argv = ["docker", verb, name] if verb == "kill" else ["docker", verb, "-f", name]
            subprocess.run(argv, capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass


def copy_build_output(source, target):
    """Bounded copy of regular files only; never dereference build symlinks."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"build output is not a real directory: {source}")
    files = total = 0
    target.mkdir(parents=True, exist_ok=False)

    def visit(src_dir, dst_dir):
        nonlocal files, total
        for entry in os.scandir(src_dir):
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            destination = dst_dir / entry.name
            if stat.S_ISLNK(mode):
                raise ValueError(f"build output contains a symlink: {entry.path}")
            if stat.S_ISDIR(mode):
                destination.mkdir(mode=0o755)
                visit(Path(entry.path), destination)
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"build output contains a special file: {entry.path}")
            files += 1
            total += info.st_size
            if files > OUTPUT_MAX_FILES or total > OUTPUT_MAX_BYTES:
                raise ValueError(
                    f"build output exceeds safety limit ({files} files, {total} bytes)"
                )
            with open(entry.path, "rb") as source_file, open(destination, "xb") as dest_file:
                shutil.copyfileobj(source_file, dest_file, length=1024 * 1024)
            os.chmod(destination, 0o755 if mode & stat.S_IXUSR else 0o644)

    try:
        visit(source, target)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"files": files, "bytes": total}


def warn_unsandboxed(why):
    print(
        f"\n  !  Building WITHOUT a sandbox ({why}).\n"
        "  !  This is an explicit advanced override. Project scripts run as you,\n"
        "  !  with access to your home, credentials, environment and network.\n"
        "  !  Unset H2WP_NO_SANDBOX and use Docker for untrusted projects.\n",
        file=sys.stderr,
    )
