#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.28",
# ]
# ///
"""保持仓库内置的 trim-cli skill 与 npm registry 同步。

对比 ``skills/trim-cli/manifest.json`` 中的版本号与 npm 上
``@trimjs/trim-cli`` 的 ``latest`` dist-tag。使用 ``--update`` 时，
下载最新 tarball，按 registry 的 ``dist.integrity`` 摘要校验完整性，
然后用其中的 ``package/skill/`` 内容整体替换 ``skills/trim-cli/``。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NPM_PACKAGE = "@trimjs/trim-cli"
REGISTRY_URL = f"https://registry.npmjs.org/{NPM_PACKAGE}"
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "trim-cli"
MANIFEST_PATH = SKILL_DIR / "manifest.json"
USER_AGENT = "fnos-skills-updater (+https://github.com/AkimioJR/fnos-skills)"
# 连接超时 30 秒；读取超时放宽到 300 秒，容忍大 tarball 的慢速下载
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=300.0)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10
CHUNK_SIZE = 1024 * 1024


def setup_logging() -> None:
    """初始化根日志器的输出格式与级别。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def local_skill_version() -> str:
    """读取本地 manifest.json 中记录的 skill 版本号。"""
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        msg = f"本地 {MANIFEST_PATH} 中没有有效的 version 字段"
        raise RuntimeError(msg)
    return version


def fetch_registry_metadata(client: httpx.Client) -> dict[str, Any]:
    """从 npm registry 拉取包的完整元数据。"""
    response = client.get(REGISTRY_URL)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def registry_latest(meta: dict[str, Any]) -> str:
    """从 registry 元数据中解析 latest dist-tag。"""
    dist_tags = meta.get("dist-tags")
    if not isinstance(dist_tags, dict):
        raise TypeError("registry 元数据缺少 dist-tags")
    latest = dist_tags.get("latest")
    if not isinstance(latest, str) or not latest:
        raise RuntimeError("registry 元数据缺少 dist-tags.latest")
    return latest


def registry_release(meta: dict[str, Any], version: str) -> dict[str, Any]:
    """从 registry 元数据中取出指定版本的信息。"""
    versions = meta.get("versions")
    if not isinstance(versions, dict) or version not in versions:
        msg = f"registry 元数据中没有版本 {version}"
        raise RuntimeError(msg)
    release: dict[str, Any] = versions[version]
    return release


def version_key(version: str) -> tuple[tuple[int, str], ...]:
    """把版本号拆成可比较的元组，兼容 0.1.0 这类简单三段式。"""
    return tuple(
        (int("".join(ch for ch in part if ch.isdigit()) or "0"), part)
        for part in version.split(".")
    )


def write_github_output(outputs: dict[str, str]) -> None:
    """存在 GITHUB_OUTPUT 时把结果写进去，供后续 workflow 步骤使用。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())


def file_digest(path: Path, algorithm: str) -> bytes:
    """分块计算文件摘要，避免大文件一次性读入内存。"""
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.digest()


def verify_integrity(tarball: Path, integrity: str) -> None:
    """校验 tarball 摘要与 registry 声明的 dist.integrity 是否一致。"""
    algorithm, separator, encoded = integrity.partition("-")
    if not separator or algorithm not in ("sha512", "sha256", "sha1"):
        msg = f"不支持的完整性摘要格式: {integrity}"
        raise RuntimeError(msg)
    expected = base64.b64decode(encoded)
    actual = file_digest(tarball, algorithm)
    # compare_digest 防止时序侧信道；此处主要是保持比较语义严谨
    if not hmac.compare_digest(actual, expected):
        msg = f"tarball 摘要不匹配: {tarball.name} != {integrity}"
        raise RuntimeError(msg)


def download_tarball(client: httpx.Client, url: str, dest: Path) -> None:
    """流式下载 tarball 到指定路径。"""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def extract_tarball(tarball: Path, dest: Path) -> None:
    """安全解包 tar.gz：拒绝符号链接等非普通条目与路径穿越。"""
    dest_root = dest.resolve()
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            if not (member.isfile() or member.isdir()):
                msg = f"拒绝解包非普通文件条目: {member.name}"
                raise RuntimeError(msg)
            target = (dest / member.name).resolve()
            if target != dest_root and dest_root not in target.parents:
                msg = f"拒绝路径穿越的 tarball 条目: {member.name}"
                raise RuntimeError(msg)
            archive.extract(member, dest, filter="data")


def replace_skill_dir(source: Path) -> None:
    """用解包出的 skill 目录整体替换仓库内的 skills/trim-cli。"""
    if SKILL_DIR.exists():
        shutil.rmtree(SKILL_DIR)
    shutil.copytree(source, SKILL_DIR)


def run_check(client: httpx.Client) -> int:
    """--check 模式：只比较版本，不下载。"""
    local = local_skill_version()
    meta = fetch_registry_metadata(client)
    latest = registry_latest(meta)
    if version_key(latest) > version_key(local):
        logger.info("发现可用更新: %s -> %s", local, latest)
        write_github_output({"has_update": "true", "latest_version": latest})
        return EXIT_UPDATE_AVAILABLE
    logger.info("skill 已是最新: %s@%s (本地 %s)", NPM_PACKAGE, latest, local)
    write_github_output({"has_update": "false", "latest_version": latest})
    return EXIT_OK


def run_update(client: httpx.Client) -> bool:
    """--update 模式：下载并应用最新 skill，返回是否发生了更新。"""
    local = local_skill_version()
    meta = fetch_registry_metadata(client)
    latest = registry_latest(meta)
    if version_key(latest) <= version_key(local):
        logger.info("skill 已是最新: %s@%s (本地 %s)", NPM_PACKAGE, latest, local)
        write_github_output({"has_update": "false"})
        return False

    release = registry_release(meta, latest)
    dist = release.get("dist")
    if not isinstance(dist, dict):
        raise TypeError(f"registry 元数据中版本 {latest} 缺少 dist")
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    if not isinstance(tarball_url, str) or not isinstance(integrity, str):
        raise TypeError(f"版本 {latest} 缺少 tarball 或 integrity 摘要")

    logger.info("开始更新 skill: %s -> %s", local, latest)
    logger.info("下载 %s", tarball_url)
    with tempfile.TemporaryDirectory(prefix="trim-cli-skill-") as tmp:
        work = Path(tmp)
        tarball = work / "package.tgz"
        download_tarball(client, tarball_url, tarball)
        verify_integrity(tarball, integrity)
        logger.info("完整性校验通过: %s", integrity)
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
    logger.info("skill 目录已更新: %s", SKILL_DIR)
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数，--check 与 --update 二选一。"""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="仅比较版本，不下载")
    mode.add_argument(
        "--update", action="store_true", help="下载并应用最新的 skill"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口，返回进程退出码。"""
    args = parse_args(argv)
    setup_logging()
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as client:
            if args.update:
                run_update(client)
                return EXIT_OK
            return run_check(client)
    except (httpx.HTTPError, RuntimeError, OSError, ValueError, tarfile.TarError) as err:
        logger.error("执行失败: %s", err)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
