#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.28",
# ]
# ///
"""保持仓库内置的 skill 与 npm registry 同步。

支持 trim-cli 与 fnos-cli 技能包，对比 ``skills/<skill>/manifest.json`` 中的版本号与
npm 上对应包的 dist-tags 或指定版本。使用 ``--update`` 时，下载对应 tarball，
按 registry 的 ``dist.integrity`` 摘要校验完整性，然后用其中的 ``package/skill/``
内容整体替换 ``skills/<skill>/``。

约定：诊断信息走 stderr（logging），结果以一行 JSON 打印到 stdout，
由调用方（本地或 CI 中的 bash/jq）解析，脚本不直接写 CI 特殊文件。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
README_PATH = REPO_ROOT / "README.md"
USER_AGENT = "fnos-skills-updater (+https://github.com/AkimioJR/fnos-skills)"
DEFAULT_REGISTRY = "https://registry.npmjs.org"

# 连接超时 30 秒；读取超时放宽到 300 秒，容忍大 tarball 的慢速下载
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=300.0)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class SkillConfig:
    name: str
    package: str
    dir_name: str


SUPPORTED_SKILLS: dict[str, SkillConfig] = {
    "fnos-cli": SkillConfig(
        name="fnos-cli",
        package="@fnnas-labs/fnos-cli",
        dir_name="fnos-cli",
    ),
    "trim-cli": SkillConfig(
        name="trim-cli",
        package="@trimjs/trim-cli",
        dir_name="trim-cli",
    ),
}


def setup_logging() -> None:
    """初始化日志器：输出到 stderr，与 stdout 上的结果 JSON 分离。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def resolve_registry(custom_registry: str | None = None) -> str:
    """获取有效 registry 地址，优先级：参数 > 环境变量 > 默认地址。"""
    if custom_registry:
        return custom_registry.rstrip("/")
    env_registry = os.environ.get("NPM_REGISTRY")
    if env_registry:
        return env_registry.rstrip("/")
    return DEFAULT_REGISTRY


def local_skill_version(skill_dir: Path) -> str | None:
    """读取本地 manifest.json 中记录的 skill 版本号。不存在则返回 None。"""
    manifest_path = skill_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = manifest.get("version")
        if isinstance(version, str) and version:
            return version
    except (json.JSONDecodeError, OSError) as err:
        logger.warning(f"读取本地 manifest.json 失败: {err}")
    return None


def fetch_registry_metadata(
    client: httpx.Client, package: str, registry_base: str
) -> dict[str, Any]:
    """从 npm registry 拉取包的完整元数据。"""
    url = f"{registry_base}/{package}"
    response = client.get(url, follow_redirects=True)
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
    """把版本号拆成可比较的元组，兼容 0.1.0 这类三段式。"""
    return tuple(
        (int("".join(ch for ch in part if ch.isdigit()) or "0"), part)
        for part in version.split(".")
    )


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
    if not hmac.compare_digest(actual, expected):
        msg = f"tarball 摘要不匹配: {tarball.name} != {integrity}"
        raise RuntimeError(msg)


def download_tarball(client: httpx.Client, url: str, dest: Path) -> None:
    """流式下载 tarball 到指定路径。"""
    with client.stream("GET", url, follow_redirects=True) as response:
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


def replace_skill_dir(source: Path, target_dir: Path) -> None:
    """用解包出的 skill 目录整体替换仓库内的目标目录。"""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source, target_dir)


def sync_readme_version(skill_name: str, version: str) -> None:
    """把 README 收录表格中指定 skill 行的版本号同步为新版本。

    README 版本列为纯展示信息，更新失败只降级为警告，不阻塞更新流程。
    """
    if not README_PATH.exists():
        logger.warning("README 文件不存在，跳过版本同步")
        return
    text = README_PATH.read_text(encoding="utf-8")
    updated, count = re.subn(
        rf"(\| \[{re.escape(skill_name)}\]\(skills/{re.escape(skill_name)}/\) \| )[^|]+( \|)",
        rf"\g<1>{version}\g<2>",
        text,
    )
    if count == 0:
        logger.warning(f"README 中未找到 {skill_name} 收录行，跳过版本同步")
        return
    README_PATH.write_text(updated, encoding="utf-8")
    logger.info(f"README 中 {skill_name} 版本号已同步为 {version}")


def run_check(
    client: httpx.Client,
    config: SkillConfig,
    registry_base: str,
    target_version: str | None = None,
) -> dict[str, Any]:
    """--check 模式：只比较版本，不下载，返回结果字典。"""
    skill_dir = SKILLS_DIR / config.dir_name
    local = local_skill_version(skill_dir)
    meta = fetch_registry_metadata(client, config.package, registry_base)

    if target_version:
        resolved_version = target_version
        # 确认目标版本在元数据中存在
        registry_release(meta, resolved_version)
    else:
        resolved_version = registry_latest(meta)

    if local is None:
        has_update = True
        logger.info(f"本地未发现 {config.name}，可安装版本: {resolved_version}")
    elif target_version:
        has_update = local != resolved_version
        if has_update:
            logger.info(
                f"发现指定版本更新: {config.name} 本地 {local} -> 目标 {resolved_version}"
            )
        else:
            logger.info(f"skill 版本已是指定版本: {config.name}@{resolved_version}")
    else:
        has_update = version_key(resolved_version) > version_key(local)
        if has_update:
            logger.info(
                f"发现可用更新: {config.name} {local} -> 最新 {resolved_version}"
            )
        else:
            logger.info(
                f"skill 已是最新: {config.package}@{resolved_version} (本地 {local})"
            )

    return {
        "has_update": has_update,
        "skill": config.name,
        "package": config.package,
        "local_version": local,
        "target_version": resolved_version,
        "latest_version": registry_latest(meta),
    }


def run_update(
    client: httpx.Client,
    config: SkillConfig,
    registry_base: str,
    target_version: str | None = None,
) -> dict[str, Any]:
    """--update 模式：下载并应用指定或最新 skill，返回结果字典。"""
    skill_dir = SKILLS_DIR / config.dir_name
    local = local_skill_version(skill_dir)
    meta = fetch_registry_metadata(client, config.package, registry_base)

    if target_version:
        resolved_version = target_version
    else:
        resolved_version = registry_latest(meta)

    # 如果未指定特定版本且已是最新，则无需更新
    if (
        not target_version
        and local is not None
        and version_key(resolved_version) <= version_key(local)
    ):
        logger.info(
            f"skill 已是最新: {config.package}@{resolved_version} (本地 {local})"
        )
        return {
            "has_update": False,
            "skill": config.name,
            "package": config.package,
            "local_version": local,
            "version": resolved_version,
            "latest_version": registry_latest(meta),
        }

    # 如果指定了版本且本地版本已经等于该版本，也无需更新
    if target_version and local == target_version:
        logger.info(f"skill 已是目标版本: {config.name}@{resolved_version}")
        return {
            "has_update": False,
            "skill": config.name,
            "package": config.package,
            "local_version": local,
            "version": resolved_version,
            "latest_version": registry_latest(meta),
        }

    release = registry_release(meta, resolved_version)
    dist = release.get("dist")
    if not isinstance(dist, dict):
        raise TypeError(f"registry 元数据中版本 {resolved_version} 缺少 dist")
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    if not isinstance(tarball_url, str) or not isinstance(integrity, str):
        raise TypeError(f"版本 {resolved_version} 缺少 tarball 或 integrity 摘要")

    logger.info(f"开始更新 {config.name} skill: {local} -> {resolved_version}")
    logger.info(f"下载 {tarball_url}")
    with tempfile.TemporaryDirectory(prefix=f"{config.name}-skill-") as tmp:
        work = Path(tmp)
        tarball = work / "package.tgz"
        download_tarball(client, tarball_url, tarball)
        verify_integrity(tarball, integrity)
        logger.info(f"完整性校验通过: {integrity}")
        extract_tarball(tarball, work / "extract")
        skill_src = work / "extract" / "package" / "skill"
        if not skill_src.exists():
            msg = f"tarball 中缺少 package/skill 目录: {tarball_url}"
            raise RuntimeError(msg)
        replace_skill_dir(skill_src, skill_dir)
        sync_readme_version(config.name, resolved_version)
        tarball_sha256 = file_digest(tarball, "sha256").hex()

    logger.info(f"skill 目录已更新: {skill_dir}")
    return {
        "has_update": True,
        "skill": config.name,
        "package": config.package,
        "local_version": local,
        "version": resolved_version,
        "tarball_url": tarball_url,
        "integrity": integrity,
        "tarball_sha256": tarball_sha256,
        "latest_version": registry_latest(meta),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill",
        choices=list(SUPPORTED_SKILLS.keys()),
        default="fnos-cli",
        help="目标 skill 名称（默认 fnos-cli）",
    )
    parser.add_argument(
        "--version",
        dest="version",
        default=None,
        help="目标版本号（例如 0.6.0），缺省时使用 registry latest",
    )
    parser.add_argument(
        "--registry",
        dest="registry",
        default=None,
        help="自定义 npm registry URL（或通过 NPM_REGISTRY 环境变量）",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="仅比较版本，不下载")
    mode.add_argument(
        "--update", action="store_true", help="下载并应用指定或最新的 skill"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口：成功时把结果 JSON 打印到 stdout 并返回退出码。"""
    args = parse_args(argv)
    setup_logging()
    config = SUPPORTED_SKILLS[args.skill]
    registry_base = resolve_registry(args.registry)
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        ) as client:
            if args.update:
                result = run_update(
                    client, config, registry_base, target_version=args.version
                )
                exit_code = EXIT_OK
            else:
                result = run_check(
                    client, config, registry_base, target_version=args.version
                )
                exit_code = EXIT_UPDATE_AVAILABLE if result["has_update"] else EXIT_OK
    except (
        httpx.HTTPError,
        RuntimeError,
        OSError,
        ValueError,
        tarfile.TarError,
    ) as err:
        logger.error(f"执行失败: {err}")
        return EXIT_ERROR
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
