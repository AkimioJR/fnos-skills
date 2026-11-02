#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.28",
# ]
# ///
"""手动发布指定版本的 fnos-cli skill。

流程：
1. 调用 update_skill 逻辑下载并解包指定版本的 skill，更新 README.md；
2. 检查并暂存 skills/fnos-cli 与 README.md，提交 commit；
3. 打附注 tag：fnos-cli-v<VERSION>；
4. 打包发布资产（zip、manifest.json）；
5. 推送 commit 与 tag 到 origin（带重试）；
6. 调用 gh release create 创建 GitHub Release 并上传资产（带重试）；
7. 清理临时产物。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
from update_skill import (
    DEFAULT_REGISTRY,
    REQUEST_TIMEOUT,
    SUPPORTED_SKILLS,
    USER_AGENT,
    resolve_registry,
    run_update,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "fnos-cli"
MANIFEST_PATH = SKILL_DIR / "manifest.json"
DIST_DIR = REPO_ROOT / "dist"
NOTES_PATH = REPO_ROOT / "notes.md"


def setup_logging() -> None:
    """初始化日志器。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def run_cmd_with_retry(cmd: list[str], max_retries: int = 3, delay: float = 3.0) -> None:
    """运行外部命令，失败时进行重试（用于应对网络抖动）。"""
    for attempt in range(1, max_retries + 1):
        logger.info(f"执行命令 (尝试 {attempt}/{max_retries}): {' '.join(cmd)}")
        proc = subprocess.run(cmd, check=False, cwd=REPO_ROOT)
        if proc.returncode == 0:
            return
        logger.warning(f"命令失败 (退出码 {proc.returncode})，{delay} 秒后重试...")
        time.sleep(delay)
    msg = f"命令在 {max_retries} 次尝试后最终失败: {' '.join(cmd)}"
    raise RuntimeError(msg)


def package_assets(version: str, update_result: dict[str, Any]) -> list[Path]:
    """打包 Release 所需的资产文件。"""
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    manifest_dest = DIST_DIR / "manifest.json"
    shutil.copy2(MANIFEST_PATH, manifest_dest)

    # 制作 zip 包（将 skills/fnos-cli 作为顶级目录打包）
    zip_path = DIST_DIR / "fnos-cli-skill.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(SKILL_DIR):
            root_path = Path(root)
            for file in files:
                file_path = root_path / file
                arcname = file_path.relative_to(REPO_ROOT / "skills")
                zf.write(file_path, arcname)

    assets = [zip_path, manifest_dest]

    # 生成 Release Notes
    tarball_url = update_result.get("tarball_url", "")
    integrity = update_result.get("integrity", "")
    tarball_sha256 = update_result.get("tarball_sha256", "")

    notes_content = (
        f"## fnos-cli v{version}\n\n"
        "Synced from npm package [@fnnas-labs/fnos-cli]"
        "(https://www.npmjs.com/package/@fnnas-labs/fnos-cli).\n\n"
        f"- npm tarball: {tarball_url}\n"
        f"- registry integrity: {integrity}\n"
        f"- tarball sha256: {tarball_sha256}\n\n"
        "版本与完整性校验均以 npm registry 为准。\n"
    )
    NOTES_PATH.write_text(notes_content, encoding="utf-8")
    return assets


def publish_version(version: str, registry_base: str = DEFAULT_REGISTRY) -> None:
    """发布指定版本。"""
    config = SUPPORTED_SKILLS["fnos-cli"]
    logger.info(f"=== 开始发布 fnos-cli v{version} ===")

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    ) as client:
        update_result = run_update(
            client, config, registry_base, target_version=version
        )

    tag_name = f"fnos-cli-v{version}"

    # 打包资产
    assets = package_assets(version, update_result)

    # Git 暂存与提交
    subprocess.run(["git", "add", "skills/fnos-cli", "README.md"], check=True, cwd=REPO_ROOT)
    status_proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], check=False, cwd=REPO_ROOT
    )
    if status_proc.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"update(fnos-cli): version: v{version}"],
            check=True,
            cwd=REPO_ROOT,
        )
    else:
        logger.info("工作区无新增修改，继续标签与发布")

    # 创建附注 Tag（若已存在则跳过）
    tag_check = subprocess.run(
        ["git", "tag", "-l", tag_name],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if tag_name not in tag_check.stdout.split():
        subprocess.run(
            ["git", "tag", "-a", tag_name, "-m", f"fnos-cli v{version}"],
            check=True,
            cwd=REPO_ROOT,
        )
    else:
        logger.info(f"Tag {tag_name} 已存在，跳过打标签")

    # 推送 commit 和 tag（通过 -c remote.origin.fetch="" 避免沙箱写入 refs/remotes 导致错误）
    run_cmd_with_retry(["git", "-c", "remote.origin.fetch=", "push", "origin", "HEAD:main"])
    run_cmd_with_retry(["git", "-c", "remote.origin.fetch=", "push", "origin", tag_name])

    # 检查 Release 是否已存在
    release_check = subprocess.run(
        ["gh", "release", "view", tag_name],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if release_check.returncode == 0:
        logger.info(f"GitHub Release {tag_name} 已存在，更新草稿状态（若有）")
        subprocess.run(
            ["gh", "release", "edit", tag_name, "--draft=false"],
            check=False,
            cwd=REPO_ROOT,
        )
    else:
        # 创建 Release 并上传附件
        asset_args = [str(a) for a in assets]
        gh_cmd = [
            "gh",
            "release",
            "create",
            tag_name,
            *asset_args,
            "--title",
            f"fnos-cli v{version}",
            "--notes-file",
            str(NOTES_PATH),
        ]
        run_cmd_with_retry(gh_cmd)
        logger.info(f"GitHub Release {tag_name} 创建成功")

    # 清理
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    if NOTES_PATH.exists():
        NOTES_PATH.unlink()

    logger.info(f"=== fnos-cli v{version} 发布完成 ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="要发布的 fnos-cli 版本号，例如 0.2.0")
    parser.add_argument(
        "--registry",
        dest="registry",
        default=None,
        help="自定义 npm registry URL",
    )
    args = parser.parse_args(argv)
    setup_logging()
    registry_base = resolve_registry(args.registry)
    try:
        publish_version(args.version, registry_base=registry_base)
        print(json.dumps({"success": True, "version": args.version}))
        return 0
    except (
        httpx.HTTPError,
        subprocess.CalledProcessError,
        RuntimeError,
        OSError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as err:
        logger.error(f"发布 v{args.version} 失败: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
