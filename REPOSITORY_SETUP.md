# 创建共享 Git 仓库

本目录可以直接在本地初始化 Git；不能凭空向不存在的远程地址 push。推荐先检查敏感文件，再提交：

```bash
git init
git add .
git diff --cached --check
git status --short
git commit -m "Initial chat-led cheap worker framework"
```

然后在 GitHub/GitLab 等服务创建一个空仓库，并添加 remote：

```bash
git remote add origin <远程仓库 URL>
git branch -M main
git push -u origin main
```

也可以使用已认证的官方 CLI 创建远程仓库并推送，例如 GitHub CLI：

```bash
gh repo create <仓库名> --private --source . --remote origin --push
```

不要在未确认组织、可见性和 remote URL 前运行创建或 push 命令。若已有远程仓库，跳过创建步骤，直接 `git remote add` 和 `git push -u`。推送前检查 `.gitignore` 是否覆盖 `.env`、`.venv`、`runs/`、构建产物和本地凭据；公开共享前补充合适的 `LICENSE`，本仓库不会替用户选择法律授权。

后续更新：

```bash
git status
git add <reviewed-files>
git diff --cached --check
git commit -m "Describe one focused change"
git push
```
