# Contributing

请先阅读 `README.md` 和 `SECURITY.md`。提交前运行：

```bash
bash scripts/setup.sh
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m unittest discover -s tests -v
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python /home/ww/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/cheap-worker
```

不要提交 `.env`、API key、运行日志、`runs/`、`.venv/` 或本地构建目录。涉及路径边界、凭据脱敏、补丁应用和 rollback 的改动必须增加失败路径测试。

Pull request 请说明行为变化、兼容性影响、实际检查、是否调用真实模型或硬件，以及未验证边界。不要把本地模拟 API 结果描述为真实供应商验收。
