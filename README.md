# fnos-skills

飞牛 OS（fnOS）技能包仓库。收录经过整理的 Agent Skill，并通过 GitHub Actions 自动跟踪上游 npm 包的版本更新。

当前收录：

| Skill | 版本 | 上游 | 说明 |
| --- | --- | --- | --- |
| [trim-cli](skills/trim-cli/) | 0.1.0 | [@trimjs/trim-cli](https://www.npmjs.com/package/@trimjs/trim-cli) | TRIM NAS / fnOS 的命令行客户端技能，覆盖登录认证、文件、相册、百度网盘、应用中心、Docker、存储、用户管理等 |

## 仓库结构

```
fnos-skills/
├── skills/
│   └── trim-cli/          # trim-cli skill（来自 npm 包内的 skill/ 目录）
│       ├── SKILL.md       # skill 入口（frontmatter: name / description）
│       ├── manifest.json  # 元数据，其中的 version 用于更新检测
│       ├── entries/       # 按任务类型的路由入口
│       ├── reference/     # 详细参考文档与工作流
│       ├── scripts/       # 跨平台启动器
│       └── bin/           # 各平台原生二进制
├── scripts/
│   └── update_skill.py    # 版本检测与更新脚本
└── .github/workflows/
    ├── lint-python.yml            # Python 质量门禁
    └── check-trim-cli-update.yml  # 自动更新流水线
```

## 安装 skill

### 通过 cc-switch（推荐）

1. 在 [cc-switch](https://github.com/farion1231/cc-switch) 的技能页添加本仓库（`AkimioJR/fnos-skills`）；
2. 刷新列表后选择 `trim-cli` 一键安装。

cc-switch 会递归扫描仓库中的 `SKILL.md` 并识别 skill，安装时取目录最后一段作为落盘名。

### 手动安装

将 `skills/trim-cli/` 整个目录复制到 Agent 的 skills 目录（如 Codex 为 `~/.codex/skills/`、Claude Code 为 `~/.claude/skills/`）即可，Agent 重启后按 `SKILL.md` 的 description 自动发现。

## 自动更新流水线

### 版本检测脚本

[scripts/update_skill.py](scripts/update_skill.py) 以 npm registry 为唯一版本与内容基准：

- `--check`：对比本地 `skills/trim-cli/manifest.json` 的 `version` 与 npm `dist-tags.latest`；诊断日志走 stderr，结果以一行 JSON 打到 stdout；有更新时退出码 `10`，已是最新退出码 `0`；
- `--update`：有更新时流式下载 npm tarball，按 registry 的 `dist.integrity`（sha512 + base64）校验完整性，校验失败立即报错；解包后将 `package/skill/` 全量替换 `skills/trim-cli/`，结果 JSON 中附带 `version` / `tarball_url` / `integrity` / `tarball_sha256`。

本地运行（依赖由脚本头部 PEP 723 内联声明，`uv run` 会自动解析）：

```bash
uv run scripts/update_skill.py --check
uv run scripts/update_skill.py --update
```

### GitHub Actions

| Workflow | 触发 | 行为 |
| --- | --- | --- |
| [lint-python.yml](.github/workflows/lint-python.yml) | `scripts/**/*.py` 变更（push / PR）、手动 | `ruff check` + `ty check` 质量门禁 |
| [check-trim-cli-update.yml](.github/workflows/check-trim-cli-update.yml) | 每日定时（UTC 22:30，北京时间约 06:30）、手动 | 检测 npm 新版本 → commit & push → 打 tag `trim-cli-vX.Y.Z` → 通过 [softprops/action-gh-release@v3](https://github.com/softprops/action-gh-release) 创建 Release |

Release Assets 包含：

- `trim-cli-skill.zip`：完整 skill 打包（与仓库内 `skills/trim-cli/` 内容一致）；
- `bin/` 下 6 个平台的原生二进制（darwin / linux / windows，x64 / arm64）；
- `manifest.json`。

Release 说明中记录 npm tarball 链接、registry `integrity` 值与 tarball sha256。版本与完整性校验均以 npm registry 为准；commit、tag 与 Release 的操作者身份为 `github-actions[bot]`，权限来自 workflow 内的 `permissions: contents: write`，无需额外配置 secret。