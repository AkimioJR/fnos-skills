#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.28",
# ]
# ///
"""Keep the bundled trim-cli skill in sync with the npm registry.

Compares the version in ``skills/trim-cli/manifest.json`` with the
``latest`` dist-tag of ``@trimjs/trim-cli`` on npm. With ``--update``,
the newest tarball is downloaded, verified against the registry's
``dist.integrity`` digest, and its ``package/skill/`` contents replace
``skills/trim-cli/``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx

NPM_PACKAGE = "@trimjs/trim-cli"
REGISTRY_URL = f"https://registry.npmjs.org/{NPM_PACKAGE}"
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "trim-cli"
MANIFEST_PATH = SKILL_DIR / "manifest.json"
USER_AGENT = "fnos-skills-updater (+https://github.com/AkimioJR/fnos-skills)"
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=300.0)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10
CHUNK_SIZE = 1024 * 1024


def local_skill_version() -> str:
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        msg = f"no version found in {MANIFEST_PATH}"
        raise RuntimeError(msg)
    return version


def fetch_registry_metadata(client: httpx.Client) -> dict[str, Any]:
    response = client.get(REGISTRY_URL)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def registry_latest(meta: dict[str, Any]) -> str:
    dist_tags = meta.get("dist-tags")
    if not isinstance(dist_tags, dict):
        raise TypeError("registry metadata is missing dist-tags")
    latest = dist_tags.get("latest")
    if not isinstance(latest, str) or not latest:
        msg = "registry metadata is missing dist-tags.latest"
        raise RuntimeError(msg)
    return latest


def registry_release(meta: dict[str, Any], version: str) -> dict[str, Any]:
    versions = meta.get("versions")
    if not isinstance(versions, dict) or version not in versions:
        msg = f"registry metadata is missing version {version}"
        raise RuntimeError(msg)
    release: dict[str, Any] = versions[version]
    return release


def version_key(version: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (int("".join(ch for ch in part if ch.isdigit()) or "0"), part)
        for part in version.split(".")
    )


def write_github_output(outputs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())


def file_digest(path: Path, algorithm: str) -> bytes:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.digest()


def verify_integrity(tarball: Path, integrity: str) -> None:
    algorithm, separator, encoded = integrity.partition("-")
    if not separator or algorithm not in ("sha512", "sha256", "sha1"):
        msg = f"unsupported integrity digest: {integrity}"
        raise RuntimeError(msg)
    expected = base64.b64decode(encoded)
    actual = file_digest(tarball, algorithm)
    if not hmac.compare_digest(actual, expected):
        msg = f"tarball digest mismatch: {tarball.name} != {integrity}"
        raise RuntimeError(msg)


def download_tarball(client: httpx.Client, url: str, dest: Path) -> None:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def extract_tarball(tarball: Path, dest: Path) -> None:
    dest_root = dest.resolve()
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            if not (member.isfile() or member.isdir()):
                msg = f"refusing non-regular tarball entry: {member.name}"
                raise RuntimeError(msg)
            target = (dest / member.name).resolve()
            if target != dest_root and dest_root not in target.parents:
                msg = f"refusing path-traversal tarball entry: {member.name}"
                raise RuntimeError(msg)
            archive.extract(member, dest, filter="data")


def replace_skill_dir(source: Path) -> None:
    if SKILL_DIR.exists():
        shutil.rmtree(SKILL_DIR)
    shutil.copytree(source, SKILL_DIR)


def run_check(client: httpx.Client) -> int:
    local = local_skill_version()
    meta = fetch_registry_metadata(client)
    latest = registry_latest(meta)
    if version_key(latest) > version_key(local):
        print(f"update available: {local} -> {latest}")
        write_github_output({"has_update": "true", "latest_version": latest})
        return EXIT_UPDATE_AVAILABLE
    print(f"skill is up to date: {NPM_PACKAGE}@{latest} (local {local})")
    write_github_output({"has_update": "false", "latest_version": latest})
    return EXIT_OK


def run_update(client: httpx.Client) -> bool:
    local = local_skill_version()
    meta = fetch_registry_metadata(client)
    latest = registry_latest(meta)
    if version_key(latest) <= version_key(local):
        print(f"skill is up to date: {NPM_PACKAGE}@{latest} (local {local})")
        write_github_output({"has_update": "false"})
        return False

    release = registry_release(meta, latest)
    dist = release.get("dist")
    if not isinstance(dist, dict):
        raise TypeError(f"registry metadata for {latest} is missing dist")
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    if not isinstance(tarball_url, str) or not isinstance(integrity, str):
        raise TypeError(f"release {latest} is missing tarball or integrity digest")

    print(f"updating skill: {local} -> {latest}")
    print(f"downloading {tarball_url}")
    with tempfile.TemporaryDirectory(prefix="trim-cli-skill-") as tmp:
        work = Path(tmp)
        tarball = work / "package.tgz"
        download_tarball(client, tarball_url, tarball)
        verify_integrity(tarball, integrity)
        print("integrity verified:", integrity)
        extract_tarball(tarball, work / "extract")
        replace_skill_dir(work / "extract" / "package" / "skill")
        write_github_output(
            {
                "has_update": "true",
                "version": latest,
                "tarball_url": tarball_url,
                "integrity": integrity,
                "tarball_sha256": file_digest(tarball, "sha256").hex(),
            }
        )
    print(f"updated skill directory: {SKILL_DIR}")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="only compare versions")
    mode.add_argument(
        "--update", action="store_true", help="download and apply the newest skill"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as client:
            if args.update:
                run_update(client)
                return EXIT_OK
            return run_check(client)
    except (httpx.HTTPError, RuntimeError, OSError, ValueError, tarfile.TarError) as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
