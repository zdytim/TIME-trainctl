#!/usr/bin/env python3
"""Non-root, user-prefix installer and safe rollback for time-cli-mcp."""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import sys
import tempfile
import uuid


VERSION = "0.1.0"
PAYLOAD = ("time_core.py", "time_consent.py", "timectl.py", "time_mcp.py", "time_mcp_confirm.py", "installer.py")


def assert_no_symlink_components(path):
    current = pathlib.Path(os.path.abspath(str(path))).anchor
    for part in pathlib.Path(os.path.abspath(str(path))).parts[1:]:
        current = os.path.join(current, part)
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            break
        if os.path.islink(current) or not (stat_is_dir(value.st_mode) or stat_is_file(value.st_mode)):
            raise SystemExit("refusing path with unsafe component: %s" % current)


def stat_is_dir(mode):
    return stat.S_ISDIR(mode)


def stat_is_file(mode):
    return stat.S_ISREG(mode)


def owned_regular(path, label):
    try:
        value = os.lstat(str(path))
    except FileNotFoundError:
        return False
    if os.path.islink(path) or not stat_is_file(value.st_mode):
        raise SystemExit("refusing %s: must be a regular non-symlink file" % label)
    if value.st_uid != os.getuid():
        raise SystemExit("refusing %s: not owned by current user" % label)
    return True


def owned_directory(path, label):
    try:
        value = os.lstat(str(path))
    except FileNotFoundError:
        return False
    if os.path.islink(path) or not stat_is_dir(value.st_mode):
        raise SystemExit("refusing %s: must be a regular non-symlink directory" % label)
    if value.st_uid != os.getuid():
        raise SystemExit("refusing %s: not owned by current user" % label)
    return True


def digest(path):
    value = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def user_prefix(value):
    prefix = pathlib.Path(os.path.abspath(os.path.expanduser(value or "~/.local")))
    home = pathlib.Path.home().resolve()
    if prefix != home and str(home) not in ("/", "") and not str(prefix).startswith(str(home) + os.sep):
        raise SystemExit("refusing prefix outside current user's home: %s" % prefix)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SystemExit("refusing root installation; use the ordinary user's account")
    assert_no_symlink_components(prefix)
    if prefix.exists():
        owned_directory(prefix, "installation prefix")
    return prefix


def write_file(path, content, mode=0o755):
    assert_no_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(str(path), mode)


def wrapper(python_file, action=None, manifest=None):
    extra = ""
    if action:
        extra += " " + action
    if manifest:
        extra += " --manifest " + shlex.quote(str(manifest))
    return "#!/bin/sh\nexec /usr/bin/env python3 %s%s \"$@\"\n" % (
        shlex.quote(str(python_file)), extra)


def install(args):
    source = pathlib.Path(args.source).resolve()
    prefix = user_prefix(args.prefix)
    for name in PAYLOAD:
        if not owned_regular(source / name, "artifact source %s" % name):
            raise SystemExit("artifact is missing %s" % name)
    bin_dir = prefix / "bin"
    share_dir = prefix / "share" / "time-cli-mcp"
    manifest_path = share_dir / "manifest.json"
    version_dir = prefix / ("lib/time-cli-mcp-%s" % VERSION)
    targets = [bin_dir / "timectl", bin_dir / "time-mcp", bin_dir / "time-mcp-confirm",
               bin_dir / "time-cli-mcp-uninstall", bin_dir / "time-cli-mcp-rollback",
               version_dir, manifest_path]
    for target in targets:
        if target.exists() or target.is_symlink():
            raise SystemExit("refusing to overwrite existing path: %s" % target)
    for directory in (bin_dir, version_dir.parent, share_dir.parent, share_dir):
        if directory.exists():
            owned_directory(directory, "managed directory")
    assert_no_symlink_components(bin_dir)
    assert_no_symlink_components(version_dir.parent)
    assert_no_symlink_components(share_dir)
    staging = prefix / ("lib/.time-cli-mcp-install-%s" % uuid.uuid4().hex)
    created_wrappers = []
    installed_version = False
    try:
        staging.mkdir(parents=True, mode=0o755)
        for name in PAYLOAD:
            destination = staging / name
            shutil.copy2(str(source / name), str(destination))
            os.chmod(str(destination), 0o755 if name.endswith(".py") else 0o644)
        final_lib = version_dir.parent
        final_lib.mkdir(parents=True, exist_ok=True)
        os.rename(str(staging), str(version_dir))
        installed_version = True
        write_file(bin_dir / "timectl", wrapper(version_dir / "timectl.py"))
        created_wrappers.append(bin_dir / "timectl")
        write_file(bin_dir / "time-mcp", wrapper(version_dir / "time_mcp.py"))
        created_wrappers.append(bin_dir / "time-mcp")
        write_file(bin_dir / "time-mcp-confirm", wrapper(version_dir / "time_mcp_confirm.py"))
        created_wrappers.append(bin_dir / "time-mcp-confirm")
        write_file(bin_dir / "time-cli-mcp-uninstall",
                   wrapper(version_dir / "installer.py", "uninstall", manifest_path))
        created_wrappers.append(bin_dir / "time-cli-mcp-uninstall")
        write_file(bin_dir / "time-cli-mcp-rollback",
                   wrapper(version_dir / "installer.py", "rollback", manifest_path))
        created_wrappers.append(bin_dir / "time-cli-mcp-rollback")
        files = [bin_dir / "timectl", bin_dir / "time-mcp", bin_dir / "time-mcp-confirm",
                 bin_dir / "time-cli-mcp-uninstall", bin_dir / "time-cli-mcp-rollback"]
        files.extend(version_dir / name for name in PAYLOAD)
        manifest = {
            "artifact": "time-cli-mcp",
            "version": VERSION,
            "prefix": str(prefix),
            "version_dir": str(version_dir),
            "manifest": str(manifest_path),
            "files": [{"path": str(path), "sha256": digest(path)} for path in files],
            "server_files_changed": False,
            "installed_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }
        share_dir.mkdir(parents=True, mode=0o700)
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        os.chmod(str(manifest_path), 0o600)
    except Exception:
        for path in created_wrappers:
            try:
                path.unlink()
            except OSError:
                pass
        for name in PAYLOAD:
            candidate = version_dir / name
            if installed_version and (candidate.exists() or candidate.is_symlink()):
                owned_regular(candidate, "partial managed file")
                candidate.unlink()
        for directory in ((version_dir, staging) if installed_version else (staging,)):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    print("installed timectl and time-mcp under %s" % prefix)


def load_manifest(path):
    path = pathlib.Path(os.path.abspath(os.path.expanduser(path)))
    assert_no_symlink_components(path)
    owned_regular(path, "manifest")
    with path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("artifact") != "time-cli-mcp" or not manifest.get("files"):
        raise SystemExit("invalid time-cli-mcp manifest")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit("invalid time-cli-mcp release version")
    if not isinstance(manifest.get("prefix"), str):
        raise SystemExit("manifest prefix is missing")
    prefix = user_prefix(manifest["prefix"])
    expected_manifest = prefix / "share" / "time-cli-mcp" / "manifest.json"
    if path != expected_manifest:
        raise SystemExit("manifest is outside the fixed managed release path")
    version_dir = prefix / ("lib/time-cli-mcp-%s" % version)
    if pathlib.Path(manifest.get("version_dir", "")) != version_dir:
        raise SystemExit("manifest version directory is not the fixed managed release path")
    expected = {
        prefix / "bin" / "timectl",
        prefix / "bin" / "time-mcp",
        prefix / "bin" / "time-mcp-confirm",
        prefix / "bin" / "time-cli-mcp-uninstall",
        prefix / "bin" / "time-cli-mcp-rollback",
    }
    expected.update(version_dir / name for name in PAYLOAD)
    actual = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise SystemExit("invalid manifest file record")
        item_path = pathlib.Path(item["path"])
        if item_path not in expected:
            raise SystemExit("manifest contains an unmanaged path: %s" % item_path)
        if item_path in actual:
            raise SystemExit("manifest contains a duplicate path")
        actual.add(item_path)
    if actual != expected:
        raise SystemExit("manifest does not enumerate exactly the managed release files")
    return path, manifest


def remove_installed(args):
    manifest_path, manifest = load_manifest(args.manifest)
    mismatches = []
    for item in manifest["files"]:
        path = pathlib.Path(item["path"])
        if not owned_regular(path, "managed release file") or digest(path) != item["sha256"]:
            mismatches.append(str(path))
    if mismatches:
        raise SystemExit("refusing rollback; installed file changed: %s" % ", ".join(mismatches))
    for item in reversed(manifest["files"]):
        pathlib.Path(item["path"]).unlink()
    version_dir = pathlib.Path(manifest["version_dir"])
    try:
        version_dir.rmdir()
    except OSError:
        pass
    owned_regular(manifest_path, "manifest")
    manifest_path.unlink()
    try:
        manifest_path.parent.rmdir()
    except OSError:
        pass
    print("removed time-cli-mcp user files; server versions and system files were unchanged")


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action")
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--source", default=str(pathlib.Path(__file__).parent))
    install_parser.add_argument("--prefix", default="~/.local")
    for name in ("uninstall", "rollback"):
        child = sub.add_parser(name)
        child.add_argument("--manifest", default="~/.local/share/time-cli-mcp/manifest.json")
    args = parser.parse_args(argv)
    if args.action == "install":
        install(args)
        return 0
    if args.action in ("uninstall", "rollback"):
        remove_installed(args)
        return 0
    parser.error("choose install, uninstall, or rollback")


if __name__ == "__main__":
    raise SystemExit(main())
