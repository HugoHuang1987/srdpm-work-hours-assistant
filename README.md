# SRDPM 工时助手

这是一个面向 SRDPM 工时数据的本地辅助工具，用于按月份拉取数据、执行离线规则审计、梳理异常，并生成多月审核看板。

## 当前里程碑

第一阶段迁移与安全重构已完成：

- 人工异常、平台信息和正常申报使用互不混淆的统计口径；
- 原始明细按 `approve_id` 去重，审批统一为“人员 + 日期”整单；
- 看板中可以可靠地选择、撤销和恢复待处理整单；
- 生成准确、可复核、ID 全局唯一的审批清单；
- 提供默认离线、执行前后双重校验的审批执行器；
- 所有规则和页面交互都有离线 `unittest` 与 Playwright 回归保护。

静态看板本身永远不执行真实审批，也不持有凭据。真实审批只能通过 `apply_approval_plan.py` 执行：默认仅离线校验；显式执行时要求月份与摘要确认、提交前实时状态全等校验、逐整单提交以及提交后“通过”状态回读。只有重新拉取归档确认服务器状态后，看板才显示“已审批”。本次迁移和测试没有执行任何真实审批。

## 核心流程

```text
fetch_and_audit.py
        |
        v
srdpm_archive/YYYY-MM/
  raw_data.json
  audit_report.json
  audit_report.md
        |
        v
build_multi_month_dashboard.py
        |
        v
工时审批看板_多月.html
        |
        v
srdpm-approval-plan-YYYY-MM.json
        |
        v
apply_approval_plan.py（默认离线；显式确认后才可执行）
```

- `fetch_and_audit.py`：登录 SRDPM、按月份拉取数据并运行审计。
- `project_mapping.json`：项目/机芯与人员的规则映射。
- `approval_model.py`：去重并生成互斥的人员日期审批整单。
- `build_multi_month_dashboard.py`：从结构化归档生成多月看板。
- `srdpm_client.py`：延迟登录、只读校验及显式审批请求的最小客户端。
- `apply_approval_plan.py`：审批清单离线校验、安全确认、提交和回读。
- `工时审批看板_多月.html`：本地生成物。请勿直接编辑，任何改动都应修改生成器后重新生成。

所有源码路径应相对于项目目录解析；运行不应依赖原 WorkBuddy 目录。

## 环境准备

建议使用 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

凭据只通过当前进程或用户环境变量提供：

```powershell
$env:SRDPM_USERNAME = '<本机已配置的用户名>'
$env:SRDPM_PASSWORD = '<本机已配置的密码>'
```

不要把真实值写入命令历史示例、源码、`.env`、JSON、Markdown、日志或测试夹具。历史凭据若曾写入文件，应先轮换再继续使用。

## 正确运行方式

脚本的位置参数是月份，不是年份；年份通过 `--year` 指定，默认值为当前年份。

拉取并审计单月：

```powershell
python fetch_and_audit.py 7
```

拉取并审计多月：

```powershell
python fetch_and_audit.py 6 7
```

显式指定年份：

```powershell
python fetch_and_audit.py --year 2026 7
```

只拉取、不生成审计报告：

```powershell
python fetch_and_audit.py --fetch-only --year 2026 7
```

生成或更新看板：

```powershell
python build_multi_month_dashboard.py
```

随后在本机浏览器打开 `工时审批看板_多月.html`。如果页面已打开，重新生成后应强制刷新以加载新版本。

## 辅助审核与自动审批

1. 在看板“三、工时异常”和“四、项目归属异常”中逐个人员日期整单审核并标记。
2. 点击工具栏“全选全部自动候选”；同日含任一异常的整单会自动跳过，同一整单在不同明细视图中不会重复计数。
3. 导出 `srdpm-approval-plan-YYYY-MM.json`。导出不会修改 SRDPM。
4. 先做纯离线校验：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json
```

5. 如需验证登录可用性，只运行只读检查：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json --check-live
```

6. 只有在核对当次清单并接受不可撤回风险后，才进入显式执行；程序会要求输入与当前清单 SHA256 绑定的完整确认语：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json --execute --confirm-month 2026-07
```

不要把 `--execute` 放入普通定时任务，也不要复用旧清单。任一人员日期的实时待审 ID 与计划不完全一致时，执行器会拒绝提交。

## 测试要求

项目已建立纯函数、现有归档、执行器假客户端和本地浏览器四层回归。之后每次修复都必须运行：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Playwright 回归也应由 `unittest` 测试套件驱动，加载本地生成页面并拦截全部网络请求。测试不得登录 SRDPM，也不得调用审批、驳回或任何其他生产接口。

最低回归范围包括：

- 人工异常数量不包含平台信息和正常申报；
- 含换行或特殊字符的工作内容不会导致记录丢失；
- 选择、撤销、确认、刷新、分页和重置后的 UI 与汇总一致；
- 数据重新排序后，本地选择仍绑定同一稳定业务 ID；
- 服务器未回读成功时绝不显示“已审批”。

## 安全与版本控制

以下内容只允许保存在本机，不得提交：

- `srdpm_archive/`、raw 数据、月度兼容快照和审核报告；
- 员工工时内容、内部 Excel、截图、抓包 HTML 和生成看板；
- Cookie、token、密码、凭据 JSON、审批日志和拒绝结果；
- 历史批量审批、审批 API 探测、驳回探测及类似危险脚本。

尤其不要把名称包含 `test` 的旧审批脚本当成自动化测试运行；其中部分脚本会直接修改生产数据，且审批不可撤回。
