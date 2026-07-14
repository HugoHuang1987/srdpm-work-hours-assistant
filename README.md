# SRDPM 工时助手

这是一个面向 SRDPM 工时数据的本地辅助工具，用于按月份拉取数据、执行离线规则审计、梳理异常，并生成多月审核看板。

## 当前里程碑

第一阶段迁移与安全重构已完成：

- 人工异常、平台信息和正常申报使用互不混淆的统计口径；
- 原始明细按 `approve_id` 去重，页面审批以“当前选中的精确明细”为边界；
- 看板中可以可靠地选择、撤销和恢复待处理明细；
- 看板顶部可一键重新读取当前自然月数据、重跑审计并自动重载最新页面；
- 生成准确、可复核、ID 全局唯一的审批清单；
- 提供页面内“一次确认、直接审批”的本机服务，以及默认离线的命令行备用执行器；
- 所有规则和页面交互都有离线 `unittest` 与 Playwright 回归保护。

Windows 登录后，审批服务会在当前用户会话中静默后台启动并由 watchdog 保活。正常打开生成的 HTML、完成选择并点击“直接审批已选明细”即可：页面会自动进入 `127.0.0.1` 同源审批页，不需要手动运行脚本。首次使用时只在 UI 中输入一次账号密码；只读登录校验通过后，凭据保存到当前 Windows 用户的 Credential Manager。页面和 API 都只提交月份与不透明的精确选择标识；服务端从当前归档重新定位人员、日期和审批 ID。第四类项目归属异常一行只会审批该行匹配到的 ID，绝不会带上同日正常或平台项；无法唯一定位时会停用该行审批。网页逐行核对后，Windows 还会显示一次原生安全确认，两处清单校验码必须相同。随后执行器只复检、提交并回读被选中的 ID。顶部“重新读取当前月数据”只会调用固定的只读刷新器：不接收页面传入的月份、路径或凭据，绝不会提交审批。只有回读明确为“通过”的所选明细才会显示已审批。本项目的自动化测试全部使用假客户端，不会连接或修改 SRDPM。

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
        +--> local_approval_server.py（127.0.0.1，一次确认后真实审批）
        |
        +--> srdpm-approval-plan-YYYY-MM.json
                 |
                 v
             apply_approval_plan.py（离线/命令行备用）
```

- `fetch_and_audit.py`：登录 SRDPM、按月份拉取数据并运行审计。
- `project_mapping.json`：项目/机芯与人员的规则映射。
- `build_multi_month_dashboard.py`：从结构化归档生成多月看板。
- `srdpm_client.py`：延迟登录、只读校验及显式审批请求的最小客户端。
- `apply_approval_plan.py`：审批清单离线校验、安全确认、提交和回读。
- `approval_model.py`：去重、精确明细白名单与稳定选择标识。
- `local_approval_server.py`：同源本机桥接服务；审批只接受月份和精确选择标识，刷新只接受空请求并后台调用固定只读刷新器。
- `windows_credential_store.py`：当前用户 Windows Credential Manager 的最小安全封装。
- `manage_approval_autostart.ps1`：安装、检查或移除当前用户后台自启动；正常使用无需手工运行。
- `refresh_dashboard.py`：只读抓取当前月、审计并原子刷新看板；不包含审批功能。
- `manage_weekly_refresh.ps1`：管理每周一 10:00 的当前用户刷新任务；它不会自动审批。
- `启动工时审批看板.cmd`：仅保留为故障排查备用入口，不是日常操作步骤。
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

页面直批的凭据由 UI 首次配置到当前 Windows 用户的 Credential Manager，不会写入项目文件。命令行备用执行器仍可通过当前进程环境变量提供凭据：

```powershell
$env:SRDPM_USERNAME = '<本机已配置的用户名>'
$env:SRDPM_PASSWORD = '<本机已配置的密码>'
```

不要把真实值写入命令历史示例、源码、`.env`、JSON、Markdown、日志或测试夹具。历史凭据若曾写入文件，应先轮换再继续使用。

后台 UI 只返回“已配置/未配置”，不会把用户名或密码回显到页面状态。保存前会先做一次只读 SRDPM 登录校验；校验失败不会写入 Credential Manager。

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

直接打开 `工时审批看板_多月.html` 即可查看和选择。点击页面中的“直接审批已选明细”会自动连接后台服务并转交当前选择；不需要双击任何启动脚本。顶部“↻ 重新读取当前月数据”会读取 SRDPM 当前自然月、重跑审计并在成功后自动加载最新看板；它不会审批，且成功后会清除该月旧的本地选择，避免把旧快照的勾选带入新数据。如果页面已打开，重新生成后应强制刷新以加载新版本。

当前用户已安装每周一 10:00 的刷新任务：它只读取 SRDPM 当前月数据、重跑审计并更新页面，绝不会调用审批或驳回接口。页面手动刷新与该定时任务、真实审批相互排斥；刷新失败时旧归档和旧页面保持不变。首次运行前需先在 UI 中配置一次 Windows Credential Manager 凭据。若从 `file://` 直接打开旧静态页，刷新按钮会先安全地打开本机看板；请在该同源页面再点击一次，避免外部页面诱发带凭据刷新。

分类导航最前面新增“0、汇总信息”：它从原始 SRDPM 明细按唯一 ID 去重汇总，避免第三类整日异常与第四/六类明细重复累计。请假/出差/休假单列，公共事务/平台单列，其余第二至七类相关项目明细按机芯归并；支持人员和分类/机芯多选，矩阵及合计只统计当前筛选范围。分类导航 1-7 统一按待处理状态着色：有待处理项为橙色，无待处理项为绿色。第一类按漏报人员是否存在判断；第七类会收集原始数据中尚未被 1-6 类覆盖的待审明细，仅供人工跟进，不提供审批按钮。

## 辅助审核与候选审批

1. 在看板“三、工时异常”和“四、项目归属异常”中逐条审核并标记。第四类一行仅选择这条异常的 SRDPM 明细。
2. 第二、五、六类只是“可审批候选”，不会定时自动提交；需要时点击工具栏“全选全部可审批候选”，仍由你在页面确认。
3. 点击“直接审批已选明细”，在清单中逐行核对人员、日期、审核来源、项目/平台、工时和影响范围后确认一次。
4. 等待页面返回结果。成功、部分成功、状态未知和提交前拒绝会分开显示；状态未知时必须先去 SRDPM 人工核对，不能直接重试。

页面右侧的“导出 JSON 备用”不会修改 SRDPM，可继续用于离线核对：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json
```

如需验证登录可用性，只运行只读检查：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json --check-live
```

命令行真实执行仅作为备用；程序会要求输入与当前清单 SHA256 绑定的完整确认语：

```powershell
python apply_approval_plan.py .\srdpm-approval-plan-2026-07.json --execute --confirm-month 2026-07
```

不要把 `--execute` 放入任何定时任务，不要复用旧清单，也不要在本机审批服务正在执行时并行运行命令行真实审批。命令行备用执行器仍要求人员日期的实时待审 ID 全等；页面本机服务仅在从当前归档重建了精确白名单后，才允许所选 ID 作为实时待审集合的子集。

当前 2026-06/07 归档没有稳定的 `uid/user_id`，父记录 `id` 又是每天变化的审批记录 ID，因此执行器会按姓名查询并继续要求待审 ID 全等。现有团队名单没有同名人员；如果以后出现完全同名员工，应先停用页面直批并补充稳定人员标识，避免两人被归并为一个人员日整单。

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
