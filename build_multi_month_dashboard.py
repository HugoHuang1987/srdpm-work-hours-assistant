#!/usr/bin/env python3
"""
多月工时审批看板生成器

读取 srdpm_archive/ 下所有月份的审核数据，
生成一个带月份选择器的单HTML看板页面。

用法：
  python build_multi_month_dashboard.py
"""
import json, os, re, glob, sys, io
from collections import defaultdict
from pathlib import Path

from approval_model import (
    assign_primary_categories,
    attach_groups_to_categories,
    build_approval_groups,
    iter_unique_children,
    manual_pairs_from_categories,
    normalize_approve_ids,
    summarize_groups,
)

try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = str(PROJECT_DIR)
ARCHIVE_DIR = os.path.join(OUT_DIR, "srdpm_archive")
OUTPUT_HTML = os.path.join(OUT_DIR, "工时审批看板_多月.html")


def serialize_for_inline_script(value):
    """Serialize JSON without allowing data to break out of an inline script."""
    return (
        json.dumps(value, ensure_ascii=False, default=str)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def discover_months():
    """扫描存档目录，发现所有有审核数据的月份"""
    months = []
    if not os.path.exists(ARCHIVE_DIR):
        print(f"  存档目录不存在: {ARCHIVE_DIR}")
        return months

    for entry in sorted(os.listdir(ARCHIVE_DIR)):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", entry):
            continue
        month_dir = os.path.join(ARCHIVE_DIR, entry)
        audit_file = os.path.join(month_dir, "audit_report.json")
        if os.path.isdir(month_dir) and os.path.exists(audit_file):
            months.append(entry)
            print(f"  发现月份: {entry}")
    return months


def load_month_audit(month_label):
    """加载指定月份的审核数据"""
    month_dir = os.path.join(ARCHIVE_DIR, month_label)
    audit_file = os.path.join(month_dir, "audit_report.json")

    with open(audit_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Also load raw data for enhanced stats
    raw_file = os.path.join(month_dir, "raw_data.json")
    raw_data = None
    if os.path.exists(raw_file):
        with open(raw_file, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

    # Load MD for platform data
    md_file = os.path.join(month_dir, "audit_report.md")
    md_text = ""
    if os.path.exists(md_file):
        with open(md_file, 'r', encoding='utf-8') as f:
            md_text = f.read()

    return data, raw_data, md_text


def build_category_data(data, md_text, raw_data=None):
    """从审核JSON + MD构建看板分类数据，并尝试把 raw_data 中的真实审批状态带进来"""

    raw_children = iter_unique_children(raw_data)

    def determine_status(date, person, items=None, title=None):
        """根据 raw_data 判断该异常条目在 SRDPM 系统上的真实状态"""
        if not raw_data or not date or not person:
            return "pending"
        statuses = []
        for record in raw_children:
            if record["date"] != date or record["person"] != person:
                continue
            child = record["child"]
            if items and child.get("items") != items:
                continue
            if title and child.get("title") != title:
                continue
            statuses.append(child.get("status", "待审"))
        if not statuses:
            return "pending"
        if all(s == "通过" for s in statuses):
            return "approved"
        return "pending"

    # 平台数据必须直接使用结构化 JSON。旧实现反向解析 Markdown 表格，
    # 工作内容含换行时会整行丢失（2026-07 曾由 52 条误变成 31 条）。
    platform_data = []
    for person, entries in data.get("platform_summary", {}).items():
        for entry in entries:
            date = entry.get("date", "")
            project = entry.get("items", "")
            title = entry.get("title", "")
            content = entry.get("content", "").strip()
            try:
                hours = float(entry.get("work_hours", 0) or 0)
            except (TypeError, ValueError):
                hours = 0.0
            platform_data.append({
                "date": date,
                "person": person,
                "project": project,
                "title": title,
                "content": content,
                "hours": hours,
                "status": determine_status(date, person, project, title)
            })

    # Dedup
    seen = set()
    platform_dedup = []
    for item in platform_data:
        key = (
            item["date"], item["person"], item["project"], item["title"],
            item["content"], round(item["hours"], 2)
        )
        if key not in seen:
            seen.add(key)
            platform_dedup.append(item)

    # Build categories
    cats = {}

    # 一、漏报人员 - 纯信息展示，无审批状态，无审批按钮
    cats["one"] = {
        "title": "一、漏报人员",
        "desc": "无漏报人员" if not data.get("missed") else f"{len(data['missed'])}人漏报（截止 {data.get('missed_cutoff', '未知')}）",
        "items": [],
        "auto_approve": False,  # 不自动审批
        "no_approval": True     # 标记：无审批按钮（漏报没有东西可审批）
    }
    for person, dates in data.get("missed", {}).items():
        cats["one"]["items"].append({
            "person": person,
            "missed_count": len(dates),
            "missed_dates": ", ".join(dates),
            "detail": f"漏报{len(dates)}天：{', '.join(dates)}"
            # 不设置 status - 漏报没有审批状态
        })

    # 二、请假/出差 - 直接审批
    cats["two"] = {
        "title": "二、请假/出差/休假",
        "desc": f"{len(data.get('no_checkin_leave', []))}条记录 ⭐ 直接审批",
        "items": [],
        "auto_approve": True,     # 直接审批
        "suggest_approve": True
    }
    for entry in data.get("no_checkin_leave", []):
        for child in entry.get("children", []):
            cats["two"]["items"].append({
                "date": entry["date"],
                "person": entry["person"],
                "project": child.get("items", ""),
                "title": child.get("title", ""),
                "content": child.get("content", "").strip(),
                "hours": float(child.get("work_hours", 0)),
                "status": determine_status(entry["date"], entry["person"], child.get("items"), child.get("title"))
            })

    # 三、工时异常（含无打卡合并的申报远大于打卡） - 需人工审核，有审核按钮和SRDPM审批对接
    cats["three"] = {
        "title": "三、工时异常",
        "subtitle": "3.1 申报超过打卡(含无打卡) + 3.2 申报远低于打卡(<70%)",
        "items": [],
        "auto_approve": False,
        "manual_approve": True    # 标记：需人工审核，有审核按钮
    }

    # 从 raw_data 收集某人当天所有 approve_ids
    def get_person_day_approve_ids(raw_data, date, person):
        if not raw_data or not date or not person:
            return []
        return normalize_approve_ids(
            record["approve_id"]
            for record in raw_children
            if record["date"] == date and record["person"] == person
        )

    for entry in data.get("hours_over", []):
        is_no_checkin = entry.get("no_checkin", False)
        subtype = "超打卡(无打卡)" if is_no_checkin else "超打卡"
        approve_ids = get_person_day_approve_ids(raw_data, entry["date"], entry["person"])
        cats["three"]["items"].append({
            "date": entry["date"],
            "person": entry["person"],
            "subtype": subtype,
            "reported": entry["reported"],
            "checked": entry["checked"],
            "leave": entry.get("leave_hours", 0),
            "effective": entry["effective"],
            "ratio": "∞（无打卡）" if is_no_checkin else f"{entry['ratio']*100:.0f}%",
            "no_checkin": is_no_checkin,
            "detail": entry.get("detail", ""),
            "status": determine_status(entry["date"], entry["person"]),
            "approve_ids": ",".join(approve_ids)
        })

    for entry in data.get("hours_low", []):
        approve_ids = get_person_day_approve_ids(raw_data, entry["date"], entry["person"])
        cats["three"]["items"].append({
            "date": entry["date"],
            "person": entry["person"],
            "subtype": "低申报",
            "reported": entry["reported"],
            "checked": entry["checked"],
            "leave": entry.get("leave_hours", 0),
            "effective": entry["effective"],
            "ratio": f"{entry['ratio']*100:.0f}%",
            "detail": "",
            "status": determine_status(entry["date"], entry["person"]),
            "approve_ids": ",".join(approve_ids)
        })

    # 补充工作内容：从 raw_data 中拼接该人员当日全部填报明细
    def get_day_work_detail(raw_data, date, person):
        if not raw_data or not date or not person:
            return ""
        seen = set()
        parts = []
        for record in raw_children:
            if record["date"] != date or record["person"] != person:
                continue
            child = record["child"]
            approve_id = child.get("approve_id")
            items = child.get("items", "")
            title = child.get("title", "")
            content = child.get("content", "").strip()
            hours = child.get("work_hours", "")
            key = approve_id if approve_id else f"{items}|{title}|{content}|{hours}"
            if key in seen:
                continue
            seen.add(key)
            text = f"{items} {title} {content}".strip()
            if text:
                parts.append(f"{text} ({hours}h)")
        return "；".join(parts)

    for item in cats["three"]["items"]:
        if not item.get("detail"):
            item["detail"] = get_day_work_detail(raw_data, item["date"], item["person"])

    # 四、项目归属异常 - 需人工审核，有审核按钮
    cats["four"] = {
        "title": "四、项目归属异常",
        "subtitle": f"4.2 其他人员项目归属异常：{len(data.get('project_mismatch', []))}条",
        "items": [],
        "auto_approve": False,
        "manual_approve": True    # 标记：需人工审核，有审核按钮
    }
    for entry in data.get("project_mismatch", []):
        # 单条异常的 approve_id 是该子项自己的，但审批需要整个人的当天所有项一起批
        approve_ids = get_person_day_approve_ids(raw_data, entry["date"], entry["person"])
        cats["four"]["items"].append({
            "date": entry["date"],
            "person": entry["person"],
            "customer": entry.get("customer", ""),
            "items": entry.get("items", ""),
            "title": entry.get("title", ""),
            "content": entry.get("content", "").strip(),
            "hours": float(entry.get("work_hours", 0)),
            "allowed": ", ".join(entry.get("allowed_chips", [])),
            "reason": entry.get("reason", ""),
            "status": determine_status(entry["date"], entry["person"], entry.get("items"), entry.get("title")),
            "approve_ids": ",".join(approve_ids)
        })

    # 五、公共事务/平台类
    cats["five"] = {
        "title": "五、公共事务/平台类",
        "desc": f"{len(platform_dedup)}条（去重后，不判违规）",
        "items": platform_dedup,
        "auto_approve": True
    }

    # 七、其他待定（无法归入1-6类的条目）
    cats["seven"] = {
        "title": "七、其他待定",
        "desc": "无法归入以上1-6类的条目",
        "items": [],
        "auto_approve": False,
        "no_approval": True
    }

    # 六、正常申报（自动审核）
    # 收集所有不在异常列表中的申报条目：工时正常、项目归属正常、非平台类
    abnormal_days = set()  # (date, person) 组合，标记为工时异常或请假出差的日子
    for entry in data.get("hours_over", []):
        abnormal_days.add((entry["date"], entry["person"]))
    for entry in data.get("hours_low", []):
        abnormal_days.add((entry["date"], entry["person"]))
    for entry in data.get("no_checkin_leave", []):
        abnormal_days.add((entry["date"], entry["person"]))
    missed_dates = set()  # (date, person) 漏报的日子
    for person, dates in data.get("missed", {}).items():
        for d in dates:
            missed_dates.add((d, person))
    # 项目归属异常的特定条目 (date, person, items) 用来排除
    mismatch_items = set()
    for entry in data.get("project_mismatch", []):
        mismatch_items.add((entry["date"], entry["person"], entry.get("items", "")))

    normal_items = []
    if raw_data:
        for record in raw_children:
            day_date = record["date"]
            person = record["person"]
            child = record["child"]
            day_key = (day_date, person)
            # 排除：漏报、工时异常、请假出差的整日
            if day_key in missed_dates or day_key in abnormal_days:
                continue
            items_code = child.get("items", "")
            rec_type = child.get("type", "")
            is_platform = (rec_type == "纯平台类" or rec_type == "客户平台类"
                           or items_code.startswith("YF-CP") or items_code.startswith("YF-SW"))
            # 排除：平台类（已在五）、项目归属异常（已在四）
            if is_platform:
                continue
            if (day_date, person, items_code) in mismatch_items:
                continue
            # 排除：出差/请假标题
            title = child.get("title", "")
            content = child.get("content", "")
            if any(kw in (title + content) for kw in ["出差", "休假", "请假", "leave", "Leave"]):
                continue
            # 正常条目。审批仍按人员+日期整单执行，行仅用于展示明细。
            normal_items.append({
                "date": day_date,
                "person": person,
                "project": items_code,
                "title": title,
                "content": content.strip(),
                "hours": float(child.get("work_hours", 0)),
                "approve_id": record["approve_id"],
                "status": "approved" if child.get("status") == "通过" else "pending",
            })

    cats["six"] = {
        "title": "六、正常申报",
        "desc": f"{len(normal_items)}条 · 工时正常且项目归属正确 ⭐ 可自动审核",
        "items": normal_items,
        "auto_approve": True,
        "suggest_approve": True
    }

    return cats


def build_enhanced_stats(raw_data):
    """从原始数据构建增强统计信息"""
    if not raw_data:
        return None

    records = []
    for record in iter_unique_children(raw_data):
        date_str = record["date"]
        person = record["person"]
        child = record["child"]
        items = child.get("items", "")
        project_name = child.get("project_name", "-")
        rec_type = child.get("type", "")
        title = child.get("title", "")
        content = child.get("content", "")
        hours = float(child.get("work_hours", 0))
        customer = child.get("customer") or ""

        chip = ""
        if project_name and project_name != "-":
            m = re.search(r'\d(MT\d{3,4}[A-Z0-9]*|AM\d{3,4}[A-Z0-9]*)', str(project_name))
            if m:
                chip_raw = m.group(1)
                cm = re.match(r'(MT|AM)(\d{3,4})', chip_raw)
                if cm:
                    chip = cm.group(0)

        is_platform = (rec_type == "纯平台类") or items.startswith("YF-CP") or items.startswith("YF-SW")

        if is_platform:
            project_group = "平台/公共事务"
        elif customer and chip:
            project_group = f"{customer}/{chip}"
        elif customer:
            project_group = f"{customer}/其他"
        elif chip:
            project_group = f"未分类/{chip}"
        else:
            project_group = "未分类"

        records.append({
            "date": date_str, "person": person, "items": items,
            "project_name": project_name, "type": rec_type,
            "title": title, "content": content, "hours": hours,
            "customer": customer, "chip": chip, "is_platform": is_platform,
            "project_group": project_group,
        })

    # Stats
    total_hours = sum(r["hours"] for r in records)
    project_hours = sum(r["hours"] for r in records if not r["is_platform"])
    platform_hours = sum(r["hours"] for r in records if r["is_platform"])
    total_count = len(records)
    persons = sorted(set(r["person"] for r in records))

    # Person stats
    person_stats = defaultdict(lambda: {"total": 0, "project": 0, "platform": 0, "count": 0, "days": set()})
    for r in records:
        ps = person_stats[r["person"]]
        ps["total"] += r["hours"]
        ps["count"] += 1
        ps["days"].add(r["date"])
        if r["is_platform"]:
            ps["platform"] += r["hours"]
        else:
            ps["project"] += r["hours"]

    person_ranking = sorted(person_stats.items(), key=lambda x: -x[1]["total"])

    # Week stats
    def get_week(date_str):
        d_day = int(date_str.split("-")[2])
        if d_day <= 7: return "第1周"
        elif d_day <= 14: return "第2周"
        elif d_day <= 21: return "第3周"
        elif d_day <= 28: return "第4周"
        else: return "第5周"

    week_stats = defaultdict(lambda: {"hours": 0, "count": 0})
    for r in records:
        week_stats[get_week(r["date"])]["hours"] += r["hours"]
        week_stats[get_week(r["date"])]["count"] += 1

    # Group stats
    group_stats = defaultdict(lambda: {"hours": 0, "count": 0, "persons": set()})
    for r in records:
        g = r["project_group"]
        group_stats[g]["hours"] += r["hours"]
        group_stats[g]["count"] += 1
        group_stats[g]["persons"].add(r["person"])

    group_ranking = sorted(group_stats.items(), key=lambda x: -x[1]["hours"])

    # Person-group matrix
    person_group_stats = defaultdict(lambda: defaultdict(float))
    for r in records:
        person_group_stats[r["person"]][r["project_group"]] += r["hours"]

    return {
        "total_hours": round(total_hours, 1),
        "project_hours": round(project_hours, 1),
        "platform_hours": round(platform_hours, 1),
        "total_count": total_count,
        "person_count": len(persons),
        "group_count": len(group_stats),
        "person_ranking": [(p, {"total": round(s["total"], 1), "project": round(s["project"], 1),
                              "platform": round(s["platform"], 1), "count": s["count"],
                              "days": len(s["days"])}) for p, s in person_ranking],
        "week_stats": {k: {"hours": round(v["hours"], 1), "count": v["count"]} for k, v in sorted(week_stats.items())},
        "group_ranking": [(g, {"hours": round(s["hours"], 1), "count": s["count"],
                             "person_count": len(s["persons"])}) for g, s in group_ranking],
        "person_group_matrix": {p: {g: round(person_group_stats[p].get(g, 0), 1)
                                     for g in [gg for gg, _ in group_ranking]}
                                for p, _ in person_ranking},
    }


def main():
    print("扫描存档目录...")
    months = discover_months()
    if not months:
        print("  ⚠️ 未发现任何月份数据，请先运行 fetch_and_audit.py")
        return

    # Load all months
    all_month_data = {}
    for ml in months:
        audit_data, raw_data, md_text = load_month_audit(ml)
        cats = build_category_data(audit_data, md_text, raw_data)
        approval_groups = build_approval_groups(raw_data, manual_pairs_from_categories(cats))
        assign_primary_categories(cats, approval_groups)
        attach_groups_to_categories(cats, approval_groups)
        represented_group_keys = {
            item.get("approval_group_key")
            for key in ("two", "three", "four", "five", "six")
            for item in cats[key]["items"]
            if item.get("approval_group_key")
        }
        for group_key, group in approval_groups.items():
            if group_key not in represented_group_keys:
                cats["seven"]["items"].append({
                    "date": group["date"],
                    "person": group["person"],
                    "detail": f"未被1-6类覆盖的审批整单，共{group['item_count']}条明细",
                })
        approval_summary = summarize_groups(approval_groups)
        enhanced = build_enhanced_stats(raw_data)

        # Convert month label to display
        year, month = ml.split("-")
        display = f"{int(year)}年{int(month)}月"

        all_month_data[ml] = {
            "display": display,
            "cats": cats,
            "approval_groups": approval_groups,
            "approval_summary": approval_summary,
            "enhanced": enhanced,
            "team_members": audit_data.get("team_members", []),
            "fetch_time": audit_data.get("fetch_time", ""),
            "daily_summary": audit_data.get("daily_summary", []),
        }

    # Build HTML
    # Serialize all month data as JSON
    all_data_json = serialize_for_inline_script(all_month_data)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SRDPM 工时审批看板 — 多月版</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }

/* 月份选择器 */
.month-selector { background: #fff; padding: 12px 32px; border-bottom: 2px solid #1a73e8; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.month-selector .label { font-size: 14px; font-weight: 600; color: #1a73e8; margin-right: 8px; }
.month-btn { padding: 8px 18px; border-radius: 6px; border: 2px solid #d0e0fd; background: #e8f0fe; color: #1a73e8; cursor: pointer; font-size: 14px; font-weight: 600; transition: all 0.2s; }
.month-btn:hover { background: #d0e0fd; }
.month-btn.active { background: #1a73e8; color: #fff; border-color: #1a73e8; }
.month-btn .badge { display: inline-block; min-width: 20px; height: 20px; border-radius: 10px; text-align: center; line-height: 20px; font-size: 11px; background: #fff; color: #1a73e8; margin-left: 6px; }
.month-btn.active .badge { background: rgba(255,255,255,0.3); color: #fff; }

.header { background: linear-gradient(135deg, #1a73e8, #1557b0); color: #fff; padding: 20px 32px; }
.header h1 { font-size: 22px; margin-bottom: 6px; }
.header .meta { font-size: 13px; opacity: 0.85; }


.toolbar { padding: 12px 32px; background: #fff; border-bottom: 1px solid #e8e8e8; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.toolbar button { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 14px; font-weight: 500; transition: all 0.2s; }
.toolbar button:disabled { cursor: wait; opacity: .55; }
.btn-execute { background: #c62828; color: #fff; display: none; }
.btn-execute.show { display: inline-block; }
.btn-execute:hover:not(:disabled) { background: #9f1f1f; }
.btn-confirm { background: #e65100; color: #fff; display: none; }
.btn-confirm.show { display: inline-block; }
.btn-confirm:hover:not(:disabled) { background: #bf360c; }
.btn-info { background: #e8f0fe; color: #1a73e8; border: 1px solid #d0e0fd !important; }
.btn-info:hover { background: #d0e0fd; }
.service-status { font-size: 12px; color: #666; }
.service-status.ready { color: #1a7a1a; }
.service-status.offline { color: #b71c1c; }
.approval-feedback { display: none; margin: 10px 32px 0; padding: 10px 14px; border-radius: 7px; font-size: 13px; white-space: pre-wrap; }
.approval-feedback.show { display: block; }
.approval-feedback.info { color: #0d47a1; background: #e3f2fd; border: 1px solid #90caf9; }
.approval-feedback.success { color: #1b5e20; background: #e8f5e9; border: 1px solid #a5d6a7; }
.approval-feedback.warning { color: #e65100; background: #fff3e0; border: 1px solid #ffcc80; }
.approval-feedback.error { color: #b71c1c; background: #ffebee; border: 1px solid #ef9a9a; }
.approval-modal-backdrop { position: fixed; inset: 0; z-index: 1000; display: none; align-items: center; justify-content: center; padding: 24px; background: rgba(0,0,0,.52); }
.approval-modal-backdrop.show { display: flex; }
.approval-modal { width: min(1100px, 96vw); max-height: 90vh; display: flex; flex-direction: column; background: #fff; border-radius: 12px; box-shadow: 0 18px 60px rgba(0,0,0,.3); overflow: hidden; }
.approval-modal-header { padding: 18px 22px 12px; border-bottom: 1px solid #eee; }
.approval-modal-header h3 { color: #b71c1c; margin-bottom: 6px; }
.approval-modal-summary { font-size: 14px; color: #444; }
.approval-modal-table-wrap { overflow: auto; margin: 14px 22px; border: 1px solid #ddd; border-radius: 8px; }
.approval-modal-table { min-width: 900px; }
.approval-modal-table thead { top: 0; }
.approval-modal-warning { margin: 0 22px 12px; padding: 10px 12px; color: #b71c1c; background: #ffebee; border-radius: 6px; font-size: 13px; font-weight: 600; }
.approval-modal-actions { display: flex; justify-content: flex-end; gap: 12px; padding: 14px 22px 18px; border-top: 1px solid #eee; }
.approval-modal-actions button { padding: 9px 20px; border-radius: 6px; border: 0; cursor: pointer; font-size: 14px; }
.approval-modal-cancel { background: #eee; color: #333; }
.approval-modal-accept { background: #c62828; color: #fff; font-weight: 700; }
.credential-modal { width: min(500px, 94vw); }
.credential-fields { display: grid; gap: 12px; padding: 18px 22px; }
.credential-fields label { display: grid; gap: 5px; color: #444; font-size: 13px; font-weight: 600; }
.credential-fields input { width: 100%; padding: 9px 11px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
.credential-note { color: #666; font-size: 12px; line-height: 1.7; }
.credential-error { min-height: 20px; color: #b71c1c; font-size: 12px; }
body.approval-busy .month-btn,
body.approval-busy .cat-nav-item,
body.approval-busy .btn-approve,
body.approval-busy .bulk-actions button { pointer-events: none; opacity: .55; }
.status-badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.status-badge.approved { background: #e6f7e6; color: #1a7a1a; border: 1px solid #b7e4b7; }
.status-badge.pending { background: #fff3e0; color: #e65100; border: 1px solid #ffcc80; }
.status-badge.selected { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; }
.bulk-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; padding: 10px 12px; background: #f8f9fa; border-radius: 8px; border: 1px solid #e8e8e8; }
.bulk-actions button { padding: 6px 12px; border-radius: 5px; cursor: pointer; }
.bulk-note { font-size: 12px; color: #e65100; }

.category-nav { display: flex; gap: 10px; padding: 16px 32px; background: #fff; border-bottom: 2px solid #e8e8e8; overflow-x: auto; flex-wrap: wrap; align-items: stretch; }
.cat-nav-item { display: flex; flex-direction: column; align-items: flex-start; padding: 10px 16px; border-radius: 8px; border: 2px solid #e0e0e0; background: #fafafa; color: #444; cursor: pointer; font-size: 14px; font-weight: 600; transition: all 0.2s; min-width: 120px; }
.cat-nav-item:hover { border-color: #1a73e8; background: #f8f9ff; }
.cat-nav-item.active { border-color: #1a73e8; background: #e8f0fe; color: #1a73e8; }
.cat-nav-item .cat-status { font-size: 12px; font-weight: 500; margin-top: 4px; opacity: 0.85; }
.cat-nav-item.auto { border-color: #b7e4b7; background: #e6f7e6; color: #1a7a1a; }
.cat-nav-item.auto.active { background: #c8e6c9; border-color: #1a7a1a; }
.cat-nav-item.manual { border-color: #ffcc80; background: #fff3e0; color: #e65100; }
.cat-nav-item.manual.active { background: #ffe0b2; border-color: #e65100; }
.cat-nav-item.complete { border-color: #b7e4b7; background: #e6f7e6; color: #1a7a1a; }
.cat-nav-item.complete.active { background: #c8e6c9; border-color: #1a7a1a; }

.content { padding: 20px 32px; max-width: 1400px; }
.panel { display: none; }
.panel.active { display: block; }
.panel-desc { font-size: 14px; color: #666; margin-bottom: 14px; padding: 10px 16px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #1a73e8; }

.table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #e8e8e8; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead { background: #fafafa; position: sticky; top: 0; z-index: 1; }
th { padding: 10px 12px; text-align: left; font-weight: 600; color: #444; border-bottom: 2px solid #e0e0e0; white-space: nowrap; }
td { padding: 8px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
tr:hover { background: #f8f9ff; }
td.nowrap { white-space: nowrap; }
td.content-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }

.btn-approve { padding: 4px 12px; border-radius: 4px; border: 1px solid #e65100; background: #fff3e0; color: #e65100; cursor: pointer; font-size: 12px; font-weight: 600; transition: all 0.2s; }
.btn-approve:hover { background: #e65100; color: #fff; }
.btn-approve.done { background: #e3f2fd; color: #1565c0; border-color: #1565c0; }
.btn-approve.done:hover { background: #1565c0; color: #fff; }

.empty-state { padding: 40px; text-align: center; color: #999; font-size: 15px; }
.empty-state .icon { font-size: 40px; margin-bottom: 10px; }

.instructions { margin-top: 20px; padding: 16px 20px; background: #fff; border-radius: 8px; border: 1px solid #e8e8e8; }
.instructions h3 { font-size: 15px; margin-bottom: 10px; color: #e65100; }
.instructions ol { padding-left: 20px; font-size: 13px; color: #555; line-height: 2; }

.filter-row { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
.filter-row input, .filter-row select { padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
.filter-row label { font-size: 13px; color: #666; font-weight: 500; }
.filter-count { font-size: 13px; color: #888; margin-left: auto; }

/* 增强统计区域 */
.stats-section { border-top: 4px solid #6c5ce7; margin-top: 30px; padding-top: 20px; }
.stats-header { background: linear-gradient(135deg, #6c5ce7, #5527c2); color: #fff; padding: 18px 32px; border-radius: 8px 8px 0 0; }
.stats-header h2 { font-size: 20px; margin-bottom: 4px; }
.stats-header .meta { font-size: 13px; opacity: 0.85; }
.stats-body { background: #f5f6fa; padding: 20px 32px; }

.stats-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 2px 8px rgba(0,0,0,.06); text-align: center; }
.stat-card .val { font-size: 24px; font-weight: 700; }
.stat-card .label { font-size: 13px; color: #636e72; margin-top: 4px; }

.chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
.chart-box { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 2px 12px rgba(0,0,0,.06); }
.chart-box h3 { font-size: 16px; margin-bottom: 16px; color: #1a1a2e; border-bottom: 2px solid #6c5ce7; padding-bottom: 8px; }

/* 分页控件 */
.pagination-bar { display: flex; align-items: center; gap: 12px; padding: 10px 0; font-size: 14px; flex-wrap: wrap; }
.pagination-bar .page-size-select { padding: 4px 8px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; background: #fff; cursor: pointer; }
.pagination-bar .page-nav { display: flex; align-items: center; gap: 6px; }
.pagination-bar .page-btn { padding: 4px 12px; border: 1px solid #ddd; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; color: #333; transition: all .2s; }
.pagination-bar .page-btn:hover:not(:disabled) { background: #6c5ce7; color: #fff; border-color: #6c5ce7; }
.pagination-bar .page-btn:disabled { opacity: .4; cursor: default; }
.pagination-bar .page-btn.active { background: #6c5ce7; color: #fff; border-color: #6c5ce7; font-weight: 600; }
.pagination-bar .page-info { color: #636e72; font-size: 13px; }

@media (max-width: 768px) {
    .header { padding: 14px 16px; }
    .month-selector { padding: 10px 16px; }
    .category-nav { padding: 10px 16px; }
    .cat-nav-item { padding: 8px 12px; font-size: 13px; min-width: 100px; }
    .content { padding: 12px 16px; }
    .toolbar { padding: 10px 16px; }
    .chart-row { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="month-selector" id="monthSelector">
    <span class="label">📅 选择月份：</span>
</div>

<div class="header" id="dashboardHeader">
    <h1>📋 SRDPM 工时审批看板</h1>
    <div class="meta" id="headerMeta">加载中...</div>
</div>

<div class="category-nav" id="categoryNav"></div>

<div class="toolbar">
    <button class="btn-execute" id="btnExecute" onclick="executeSelectedApprovals()">✅ 直接审批已选整单</button>
    <button class="btn-confirm" id="btnConfirm" onclick="confirmApprovals()">⬇️ 导出 JSON 备用</button>
    <button class="btn-info" onclick="selectAllAutoGroups()">⭐ 全选全部自动候选</button>
    <button class="btn-info" onclick="toggleInstructions()">📖 审批操作方法</button>
    <button class="btn-info" onclick="resetAll()">🔄 重置审批状态</button>
    <button class="btn-info" onclick="toggleStats()">📊 统计分析</button>
    <span id="approvalServiceStatus" class="service-status"></span>
    <span id="pendingCount" style="margin-left:12px;font-size:13px;color:#e65100;font-weight:600;"></span>
</div>

<div id="approvalFeedback" class="approval-feedback" role="status" aria-live="polite"></div>

<div id="approvalConfirmOverlay" class="approval-modal-backdrop" aria-hidden="true">
    <div class="approval-modal" role="dialog" aria-modal="true" aria-labelledby="approvalConfirmTitle">
        <div class="approval-modal-header">
            <h3 id="approvalConfirmTitle">确认真实审批清单</h3>
            <div id="approvalConfirmSummary" class="approval-modal-summary"></div>
        </div>
        <div class="approval-modal-table-wrap">
            <table class="approval-modal-table">
                <thead><tr><th>日期</th><th>人员</th><th>审核来源</th><th>项目/平台</th><th>总工时</th><th>影响范围</th></tr></thead>
                <tbody id="approvalConfirmRows"></tbody>
            </table>
        </div>
        <div class="approval-modal-warning">⚠️ SRDPM 审批不可撤回。请逐行核对后继续，并在随后出现的 Windows 安全确认中核对相同清单校验码；只有两处都确认才会启动真实审批。</div>
        <div class="approval-modal-actions">
            <button id="approvalConfirmCancel" class="approval-modal-cancel" type="button">取消，保留选择</button>
            <button id="approvalConfirmAccept" class="approval-modal-accept" type="button">确认并执行真实审批</button>
        </div>
    </div>
</div>

<div id="credentialSetupOverlay" class="approval-modal-backdrop" aria-hidden="true">
    <div class="approval-modal credential-modal" role="dialog" aria-modal="true" aria-labelledby="credentialSetupTitle">
        <div class="approval-modal-header">
            <h3 id="credentialSetupTitle">首次配置 SRDPM 登录信息</h3>
            <div class="approval-modal-summary">只需在此电脑配置一次，之后可直接从页面审批。</div>
        </div>
        <div class="credential-fields">
            <label>SRDPM 用户名
                <input id="credentialUsername" type="text" maxlength="256" autocomplete="username" spellcheck="false">
            </label>
            <label>SRDPM 密码
                <input id="credentialPassword" type="password" maxlength="4096" autocomplete="current-password">
            </label>
            <div class="credential-note">凭据仅发送给 127.0.0.1 本机服务，并保存到当前 Windows 用户的凭据管理器；不会写入 HTML、JSON、日志或代码。</div>
            <div id="credentialSetupError" class="credential-error" role="alert"></div>
        </div>
        <div class="approval-modal-actions">
            <button id="credentialSetupCancel" class="approval-modal-cancel" type="button">取消</button>
            <button id="credentialSetupSave" class="approval-modal-accept" type="button">安全保存并继续</button>
        </div>
    </div>
</div>

<div class="content" id="contentArea"></div>

<div class="content" id="instructionsPanel" style="display:none;">
    <div class="instructions">
        <h3>📖 安全审批操作步骤</h3>
        <ol>
            <li>先逐个处理“三、工时异常”和“四、项目归属异常”；按钮选择的是<b>人员+日期整单</b>。</li>
            <li>点击工具栏“全选全部自动候选”；含异常的整单会自动跳过，各分类中的重复展示不会重复计数。</li>
            <li>点击“直接审批已选整单”；页面会自动连接后台本机服务。首次使用时在 UI 内配置一次登录信息。</li>
            <li>逐行核对人员、日期、审核来源、项目、工时和影响范围后确认一次；本机服务会完成校验、真实审批和结果回读。</li>
            <li>只有 SRDPM 回读明确为“通过”的整单才会在页面标记为已审批；失败或状态未知的整单会保留选择，且不会自动重试。</li>
            <li>“导出 JSON 备用”仍可用于离线核对，导出本身<b>不会修改 SRDPM</b>。</li>
        </ol>
        <p style="margin-top:10px;font-size:12px;color:#888;">⚠️ SRDPM审批不可撤回。后台服务由 Windows 自动启动；登录凭据只保存在当前用户的 Windows 凭据管理器中。</p>
    </div>
</div>

<!-- 统计分析区域 -->
<div class="stats-section" id="statsSection" style="display:none;">
    <div class="stats-header">
        <h2>📊 工时统计分析</h2>
        <div class="meta" id="statsMeta"></div>
    </div>
    <div class="stats-body" id="statsBody"></div>
</div>

<script>
// ===== 数据 =====
const ALL_DATA = __ALL_DATA_PLACEHOLDER__;
const MONTHS = Object.keys(ALL_DATA).sort().reverse(); // 最新的月在前

function readLocalServiceConfig() {
    const element = document.getElementById("srdpm-local-service-config");
    if (!element || location.protocol !== "http:" || location.hostname !== "127.0.0.1") return null;
    try {
        const config = JSON.parse(element.textContent || "{}");
        if (config.api_base !== "/api/v1" || config.csrf_header !== "X-SRDPM-CSRF" ||
            typeof config.csrf_token !== "string" || config.csrf_token.length < 32) return null;
        return config;
    } catch (error) {
        return null;
    }
}

const LOCAL_SERVICE = readLocalServiceConfig();
const LOCAL_SERVICE_ORIGIN = "http://127.0.0.1:8765";

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[ch]);
}

let currentMonth = MONTHS[0]; // 默认选最新月
let CAT_DATA = {};
let APPROVAL_GROUPS = {};
let currentCatKey = "one";

// localStorage 只保存“已选择”；已审批只来自归档或本次服务端回读结果。
// 服务端每次仍从归档重建白名单并实时复检，不信任浏览器状态。
let approvalState = {};
const STORAGE_PREFIX = "srdpm_approval_v2_";
let approvalExecutionActive = false;

function getStorageKey(month) { return STORAGE_PREFIX + month; }

function loadState(month) {
    try {
        const saved = localStorage.getItem(getStorageKey(month));
        if (saved) approvalState = JSON.parse(saved);
        else approvalState = {};
    } catch(e) { approvalState = {}; }
}

function saveState() {
    localStorage.setItem(getStorageKey(currentMonth), JSON.stringify(approvalState));
}

function getGroupStatus(groupKey) {
    const group = APPROVAL_GROUPS[groupKey];
    if (!group) return "info";
    if (group.status === "approved") return "approved";
    return approvalState[groupKey] === "selected" ? "selected" : "pending";
}

function getStatus(catKey, idx) {
    const item = CAT_DATA[catKey].items[idx];
    if (catKey === "one" || catKey === "seven") return "info";
    return item?.approval_group_key ? getGroupStatus(item.approval_group_key) : "info";
}

function setGroupSelected(groupKey, selected) {
    if (approvalExecutionActive || !APPROVAL_GROUPS[groupKey] || getGroupStatus(groupKey) === "approved") return;
    if (selected) approvalState[groupKey] = "selected";
    else delete approvalState[groupKey];
    saveState();
}

function sanitizeState() {
    let changed = false;
    for (const key of Object.keys(approvalState)) {
        if (!APPROVAL_GROUPS[key] || approvalState[key] !== "selected" || getGroupStatus(key) === "approved") {
            delete approvalState[key];
            changed = true;
        }
    }
    if (changed) saveState();
}

function groupKeysForCategory(catKey, primaryOnly = true) {
    return [...new Set((CAT_DATA[catKey]?.items || [])
        .map(item => item.approval_group_key)
        .filter(groupKey => groupKey && (!primaryOnly || APPROVAL_GROUPS[groupKey]?.primary_category === catKey)))];
}

function deriveCounts(groupKeys = Object.keys(APPROVAL_GROUPS)) {
    const counts = {
        manual: {pending: 0, selected: 0, approved: 0, items: 0},
        auto: {pending: 0, selected: 0, approved: 0, items: 0}
    };
    for (const groupKey of new Set(groupKeys)) {
        const group = APPROVAL_GROUPS[groupKey];
        if (!group) continue;
        const mode = group.review_mode === "manual" ? "manual" : "auto";
        const status = getGroupStatus(groupKey);
        if (status in counts[mode]) counts[mode][status]++;
        counts[mode].items += (group.approve_ids || []).length;
    }
    return counts;
}

// ===== 月份切换 =====
const AUTO_CATS = ["two", "five", "six"];
const MANUAL_APPROVE_CATS = ["three", "four"];
const APPROVE_CATS = ["two", "three", "four", "five", "six"];
const NO_APPROVE_CATS = ["one", "seven"];        // 无审批按钮的分类（漏报、其他待定）
const ALL_CATS = ["one", "two", "three", "four", "five", "six", "seven"];

function init() {
    updateLocalServiceStatus();
    renderMonthSelector();
    switchMonth(currentMonth);
    if (importTransferredApprovalSelection()) {
        setTimeout(() => executeSelectedApprovals(), 0);
    }
}

function updateLocalServiceStatus() {
    const element = document.getElementById("approvalServiceStatus");
    if (LOCAL_SERVICE) {
        element.textContent = "本机审批服务：已连接";
        element.className = "service-status ready";
    } else {
        element.textContent = "直接审批：点击按钮将自动连接后台服务";
        element.className = "service-status ready";
    }
}

function buildLocalServiceTransferUrl(plan) {
    const params = new URLSearchParams();
    params.set("approval_selection", JSON.stringify({
        version: 1,
        month: plan.month,
        group_keys: plan.groups.map(group => group.group_key)
    }));
    return `${LOCAL_SERVICE_ORIGIN}/#${params.toString()}`;
}

function importTransferredApprovalSelection() {
    if (!LOCAL_SERVICE || !location.hash) return false;
    const params = new URLSearchParams(location.hash.slice(1));
    const encoded = params.get("approval_selection");
    if (!encoded) return false;
    history.replaceState(null, "", `${location.pathname}${location.search}`);
    try {
        if (encoded.length > 16_384) throw new Error("转交的审批选择超过安全上限");
        const payload = JSON.parse(encoded);
        const keys = Object.keys(payload || {}).sort();
        const groupKeys = payload?.group_keys;
        if (keys.join(",") !== "group_keys,month,version" || payload.version !== 1 ||
            !Object.prototype.hasOwnProperty.call(ALL_DATA, payload.month) ||
            !Array.isArray(groupKeys) || groupKeys.length < 1 || groupKeys.length > 200 ||
            new Set(groupKeys).size !== groupKeys.length ||
            groupKeys.some(key => typeof key !== "string" || !/^grp_[0-9a-f]{20}$/.test(key))) {
            throw new Error("转交的审批选择格式不合法");
        }
        if (payload.month !== currentMonth) switchMonth(payload.month);
        if (groupKeys.some(groupKey => !APPROVAL_GROUPS[groupKey])) {
            throw new Error("转交的整单已不在当前看板中");
        }
        approvalState = {};
        for (const groupKey of groupKeys) {
            if (getGroupStatus(groupKey) !== "approved") approvalState[groupKey] = "selected";
        }
        saveState();
        renderCategoryNav();
        switchTab(currentCatKey);
        return Object.keys(approvalState).length > 0;
    } catch (error) {
        setApprovalFeedback("error", `无法导入所选整单：${error.message || "格式错误"}`);
        return false;
    }
}

function renderMonthSelector() {
    const sel = document.getElementById("monthSelector");
    let html = '<span class="label">📅 选择月份：</span>';
    for (const ml of MONTHS) {
        const md = ALL_DATA[ml].display;
        const summary = ALL_DATA[ml].approval_summary || {};
        const manualGroups = summary.manual_pending_groups || 0;
        const autoGroups = summary.auto_pending_groups || 0;
        const isActive = ml === currentMonth;
        html += `<button class="month-btn ${isActive ? 'active' : ''}" data-month="${ml}" onclick="switchMonth('${ml}')">
            ${escapeHtml(md)}<span class="badge">${manualGroups}人工</span>${autoGroups > 0 ? `<span class="badge" style="background:#1a7a1a;color:#fff;">${autoGroups}自动</span>` : ''}
        </button>`;
    }
    sel.innerHTML = html;
}

function switchMonth(ml) {
    if (approvalExecutionActive && ml !== currentMonth) return;
    currentMonth = ml;
    CAT_DATA = ALL_DATA[ml].cats;
    APPROVAL_GROUPS = ALL_DATA[ml].approval_groups || {};
    loadState(ml);
    sanitizeState();
    resetAllPageState();
    currentCatKey = "one";

    // Update month selector
    document.querySelectorAll(".month-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.month === ml);
    });

    // Update header
    const display = ALL_DATA[ml].display;
    const fetchTime = ALL_DATA[ml].fetch_time || "";
    document.getElementById("headerMeta").textContent =
        `当前查看：${display} · 数据拉取：${fetchTime ? fetchTime.substring(0, 10) : '未知'} · 审批数据存档不变`;

    renderCategoryNav();
    switchTab("one");
    updatePendingCount();
    updateStats(ml);
}

function renderCategoryNav() {
    const nav = document.getElementById("categoryNav");
    let html = "";
    for (const key of ALL_CATS) {
        const cat = CAT_DATA[key];
        const rowCount = cat.items.length;
        let cls = "auto";
        let statusText;
        if (NO_APPROVE_CATS.includes(key)) {
            cls = rowCount > 0 ? "manual" : "complete";
            statusText = `信息展示 · ${rowCount}条`;
        } else {
            const groupKeys = groupKeysForCategory(key);
            const counts = deriveCounts(groupKeys);
            if (groupKeys.length === 0 && rowCount > 0) {
                cls = "auto";
                statusText = `明细视图 · ${rowCount}条 · 审批随所属整单`;
                html += `<button class="cat-nav-item ${cls}" data-cat="${key}" onclick="switchTab('${key}')">
                    <span>${escapeHtml(cat.title)}</span>
                    <span class="cat-status">${statusText}</span>
                </button>`;
                continue;
            }
            const manualTotal = counts.manual.pending + counts.manual.selected + counts.manual.approved;
            const autoTotal = counts.auto.pending + counts.auto.selected + counts.auto.approved;
            const selected = counts.manual.selected + counts.auto.selected;
            const pending = counts.manual.pending + counts.auto.pending;
            const approved = counts.manual.approved + counts.auto.approved;
            cls = pending === 0 && selected === 0 ? "complete" : (manualTotal > 0 ? "manual" : "auto");
            const modeText = manualTotal > 0 && autoTotal > 0
                ? `人工${manualTotal}单/自动${autoTotal}单`
                : (manualTotal > 0 ? `人工${manualTotal}单` : `自动${autoTotal}单`);
            statusText = `${modeText} · 待处理${pending} · 已选${selected} · 已审批${approved} · ${rowCount}条明细`;
        }

        html += `<button class="cat-nav-item ${cls}" data-cat="${key}" onclick="switchTab('${key}')">
            <span>${escapeHtml(cat.title)}</span>
            <span class="cat-status">${statusText}</span>
        </button>`;
    }
    nav.innerHTML = html;
}

function switchTab(key) {
    currentCatKey = key;
    document.querySelectorAll(".cat-nav-item").forEach(t => t.classList.remove("active"));
    const activeTab = document.querySelector(`.cat-nav-item[data-cat="${key}"]`);
    if (activeTab) activeTab.classList.add("active");
    renderPanel(key);
    updatePendingCount();
}


// ===== 分页状态 =====
const PAGE_SIZES = [10, 20, 50, 100];
// 只对大数据量分类（six=正常申报）启用分页，其他分类数据量小无需分页
const PAGINATED_CATS = ["six"];
let pageState = {};  // { catKey: { pageSize: 20, currentPage: 0, filteredIndices: [0,1,2,...] } }

function initPageState(key, items) {
    const indices = items.map((_, i) => i);
    if (!pageState[key]) {
        pageState[key] = { pageSize: 20, currentPage: 0, filteredIndices: indices };
    }
    // 已存在时保持当前分页状态（切换标签页/审核按钮时不应重置）
}

// 切换月份时重置所有分页状态
function resetAllPageState() {
    pageState = {};
}

function getFilteredIndices(key) {
    return pageState[key]?.filteredIndices || CAT_DATA[key].items.map((_, i) => i);
}

function renderBulkActions(key) {
    if (!APPROVE_CATS.includes(key)) return "";
    const allViewGroupKeys = groupKeysForCategory(key, false);
    const viewCounts = deriveCounts(allViewGroupKeys);
    const hasAutoPending = viewCounts.auto.pending > 0;
    const selectedCount = AUTO_CATS.includes(key)
        ? viewCounts.auto.selected
        : viewCounts.manual.selected;
    const routedManualCount = allViewGroupKeys.filter(groupKey => {
        const group = APPROVAL_GROUPS[groupKey];
        return group?.review_mode === "manual" && group.primary_category !== key;
    }).length;
    let html = '<div class="bulk-actions">';
    if (hasAutoPending) {
        html += `<button class="btn-info" onclick="selectCategoryGroups('${key}', 'auto', true)">⭐ 全选本类自动候选（${viewCounts.auto.pending}单）</button>`;
    }
    if (selectedCount > 0) {
        html += `<button class="btn-info" onclick="clearCategorySelection('${key}')">取消本类选择（${selectedCount}单）</button>`;
    }
    if (routedManualCount > 0 && AUTO_CATS.includes(key)) {
        html += `<span class="bulk-note">其中 ${routedManualCount} 个人日含异常，操作已归入异常分类，本类不重复计数。</span>`;
    } else if (MANUAL_APPROVE_CATS.includes(key)) {
        html += '<span class="bulk-note">人工异常必须逐个人员日期整单确认，此处不提供批量全选。</span>';
    }
    html += '</div>';
    return html;
}

function selectCategoryGroups(catKey, reviewMode, selected) {
    if (approvalExecutionActive) return;
    for (const groupKey of groupKeysForCategory(catKey, false)) {
        const group = APPROVAL_GROUPS[groupKey];
        if (!group || group.review_mode !== reviewMode || getGroupStatus(groupKey) === "approved") continue;
        if (selected) approvalState[groupKey] = "selected";
        else delete approvalState[groupKey];
    }
    saveState();
    renderCategoryNav();
    switchTab(catKey);
}

function selectAllAutoGroups() {
    if (approvalExecutionActive) return;
    for (const [groupKey, group] of Object.entries(APPROVAL_GROUPS)) {
        if (group.review_mode === "auto" && getGroupStatus(groupKey) !== "approved") {
            approvalState[groupKey] = "selected";
        }
    }
    saveState();
    renderCategoryNav();
    switchTab(currentCatKey);
}

function clearCategorySelection(catKey) {
    if (approvalExecutionActive) return;
    for (const groupKey of groupKeysForCategory(catKey, false)) {
        const group = APPROVAL_GROUPS[groupKey];
        if (AUTO_CATS.includes(catKey) && group?.review_mode !== "auto") continue;
        delete approvalState[groupKey];
    }
    saveState();
    renderCategoryNav();
    switchTab(catKey);
}

function renderPanel(key) {
    const cat = CAT_DATA[key];
    const area = document.getElementById("contentArea");
    const instructions = document.getElementById("instructionsPanel");
    instructions.style.display = "none";

    let html = `<div class="panel active" id="panel_${key}">`;
    html += `<div class="panel-desc">${escapeHtml(cat.desc || cat.subtitle || "")}</div>`;

    if (cat.items.length === 0) {
        const emoji = cat.auto_approve ? "✅" : "📭";
        html += `<div class="empty-state"><div class="icon">${emoji}</div>${escapeHtml(cat.desc || "无数据")}</div>`;
    } else {
        // 初始化分页状态
        initPageState(key, cat.items);
        html += renderBulkActions(key);
        html += renderTable(key, cat);
    }
    html += "</div>";
    area.innerHTML = html;
}

function renderTable(key, cat) {
    const items = cat.items;
    if (items.length === 0) return "";

    let cols, headers;
    if (key === "one") {
        cols = ["person", "missed_count", "missed_dates"];
        headers = ["人员", "漏报天数", "漏报日期"];
    } else if (key === "two") {
        cols = ["date", "person", "project", "title", "content", "hours", "status", "action"];
        headers = ["日期", "人员", "项目", "标题", "工作内容", "工时", "审批状态", "操作"];
    } else if (key === "three") {
        cols = ["date", "person", "subtype", "reported", "checked", "leave", "effective", "ratio", "detail", "status", "action"];
        headers = ["日期", "人员", "类型", "申报(h)", "打卡(h)", "休假(h)", "有效申报(h)", "比例", "工作内容", "审核状态", "操作"];
    } else if (key === "four") {
        cols = ["date", "person", "customer", "items", "title", "content", "hours", "allowed", "reason", "status", "action"];
        headers = ["日期", "人员", "客户", "项目代码", "标题", "工作内容", "工时", "允许机芯", "问题", "审核状态", "操作"];
    } else if (key === "five") {
        cols = ["date", "person", "project", "title", "content", "hours", "status", "action"];
        headers = ["日期", "人员", "项目", "标题", "工作内容", "工时", "审批状态", "操作"];
    } else if (key === "six") {
        cols = ["date", "person", "project", "title", "content", "hours", "status", "action"];
        headers = ["日期", "人员", "项目", "标题", "工作内容", "工时", "审核状态", "操作"];
    } else {
        cols = ["date", "person", "detail", "status"];
        headers = ["日期", "人员", "详情", "审批状态"];
    }

    let filterHtml = '';
    if (key === "three") {
        const persons = [...new Set(items.map(i => i.person))].sort();
        filterHtml = `<div class="filter-row">
            <label>类型：</label>
            <select id="filter_subtype" onchange="refilterThree()">
                <option value="all">全部</option>
                <option value="超打卡">超打卡</option>
                <option value="超打卡(无打卡)">超打卡(无打卡)</option>
                <option value="低申报">低申报</option>
            </select>
            <label>人员：</label>
            <select id="filter_person3" onchange="refilterThree()">
                <option value="all">全部</option>
                ${persons.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('')}
            </select>
            <span class="filter-count" id="filterCount3"></span>
        </div>`;
    }
    if (key === "five") {
        const persons5 = [...new Set(items.map(i => i.person))].sort();
        filterHtml = `<div class="filter-row">
            <label>人员：</label>
            <select id="filter_person5" onchange="refilterFive()">
                <option value="all">全部</option>
                ${persons5.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('')}
            </select>
            <input type="text" id="search5" placeholder="搜索内容..." oninput="refilterFive()" style="width:200px;">
            <span class="filter-count" id="filterCount5"></span>
        </div>`;
    }
    if (key === "six") {
        const persons6 = [...new Set(items.map(i => i.person))].sort();
        filterHtml = `<div class="filter-row">
            <label>人员：</label>
            <select id="filter_person6" onchange="refilterSix()">
                <option value="all">全部</option>
                ${persons6.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('')}
            </select>
            <input type="text" id="search6" placeholder="搜索内容..." oninput="refilterSix()" style="width:200px;">
            <span class="filter-count" id="filterCount6"></span>
        </div>`;
    }

    // 决定渲染哪些行：分页分类只渲染当前页，其他分类渲染全部
    const isPaginated = PAGINATED_CATS.includes(key);
    let indicesToRender;
    if (isPaginated) {
        const ps = pageState[key];
        const start = ps.currentPage * ps.pageSize;
        const end = Math.min(start + ps.pageSize, ps.filteredIndices.length);
        indicesToRender = ps.filteredIndices.slice(start, end);
    } else {
        indicesToRender = items.map((_, i) => i);
    }

    let tableHtml = filterHtml;

    // 分页分类：在表格上方加分页控件
    if (isPaginated) {
        tableHtml += renderPaginationBar(key);
    }

    tableHtml += '<div class="table-wrap"><table><thead><tr>';
    for (const h of headers) tableHtml += `<th>${h}</th>`;
    tableHtml += "</tr></thead><tbody>";

    for (const i of indicesToRender) {
        const item = items[i];
        const status = getStatus(key, i);
        tableHtml += `<tr id="row_${key}_${i}">`;

        for (const col of cols) {
            if (col === "status") {
                const group = APPROVAL_GROUPS[item.approval_group_key] || {};
                const isManualGroup = group.review_mode === "manual";
                const primaryCategory = group.primary_category || key;
                const isPrimaryView = primaryCategory === key;
                const primaryTitle = (CAT_DATA[primaryCategory]?.title || "主分类").replace(/^.+?、/, "");
                const cls = status === "approved" ? "approved" : (status === "selected" ? "selected" : "pending");
                let text;
                if (status === "approved") {
                    text = "SRDPM已审批";
                } else if (!isPrimaryView) {
                    text = status === "selected" ? `已在${primaryTitle}选择` : `随${primaryTitle}整单处理`;
                } else if (status === "selected") {
                    text = isManualGroup ? "人工已标记" : "已选自动审批";
                } else {
                    text = isManualGroup ? "待人工审核" : "可自动审批";
                }
                tableHtml += `<td class="nowrap"><span class="status-badge ${cls}">${escapeHtml(text)}</span></td>`;
            } else if (col === "action") {
                const group = APPROVAL_GROUPS[item.approval_group_key] || {};
                const isManualGroup = group.review_mode === "manual";
                const primaryCategory = group.primary_category || key;
                const isPrimaryView = primaryCategory === key;
                const primaryTitle = (CAT_DATA[primaryCategory]?.title || "主分类").replace(/^.+?、/, "");
                if (status === "approved") {
                    tableHtml += `<td class="nowrap"><span style="color:#1a7a1a;font-size:13px;">✓ 服务器已通过</span></td>`;
                } else if (status === "selected") {
                    const undoText = isManualGroup ? "撤销整单标记" : "取消整单选择";
                    tableHtml += `<td class="nowrap"><button class="btn-approve done" onclick="toggleApproval('${key}', ${i})">${undoText}</button></td>`;
                } else if (!isPrimaryView && isManualGroup) {
                    tableHtml += `<td class="nowrap"><button class="btn-info" onclick="switchTab('${primaryCategory}')">前往${escapeHtml(primaryTitle)}</button></td>`;
                } else if (status === "info") {
                    tableHtml += '<td class="nowrap">无审批批次</td>';
                } else {
                    const btnText = isManualGroup ? "标记整单通过" : "⭐ 选择整单";
                    tableHtml += `<td class="nowrap"><button class="btn-approve" onclick="toggleApproval('${key}', ${i})">${btnText}</button></td>`;
                }
            } else if (col === "detail" || col === "content" || col === "missed_dates") {
                const rawVal = String(item[col] ?? "");
                const displayVal = rawVal.length > 80 ? rawVal.substring(0, 80) + "..." : rawVal;
                tableHtml += `<td class="content-cell" title="${escapeHtml(rawVal)}">${escapeHtml(displayVal)}</td>`;
            } else if (col === "hours" || col === "reported" || col === "checked" || col === "leave" || col === "effective") {
                const val = item[col];
                const formatted = typeof val === 'number' ? val.toFixed(2) : val;
                tableHtml += `<td class="nowrap">${escapeHtml(formatted)}</td>`;
            } else if (col === "ratio") {
                const r = item[col];
                const isNoCheckin = item.no_checkin;
                const color = isNoCheckin ? 'color:#d32f2f;font-weight:700;' : (typeof r === 'string' && parseFloat(r) > 100 ? 'color:#d32f2f;font-weight:600;' : (typeof r === 'string' && parseFloat(r) < 70 ? 'color:#e65100;font-weight:600;' : ''));
                tableHtml += `<td class="nowrap" style="${color}">${escapeHtml(r)}</td>`;
            } else if (col === "missed_count") {
                tableHtml += `<td class="nowrap" style="font-weight:600;color:#e65100;">${escapeHtml(item[col])}</td>`;
            } else {
                const rawVal = String(item[col] ?? "");
                const displayVal = rawVal.length > 40 ? rawVal.substring(0, 40) + "..." : rawVal;
                tableHtml += `<td>${escapeHtml(displayVal)}</td>`;
            }
        }
        tableHtml += "</tr>";
    }
    tableHtml += "</tbody></table></div>";

    // 分页分类：在表格下方也加分页控件
    if (isPaginated) {
        tableHtml += renderPaginationBar(key);
    }

    return tableHtml;
}

// ===== 分页控件 =====
function renderPaginationBar(key) {
    const ps = pageState[key];
    if (!ps) return "";
    const totalFiltered = ps.filteredIndices.length;
    const totalPages = Math.ceil(totalFiltered / ps.pageSize);
    if (totalPages <= 1) {
        // 只有1页时显示简单统计
        return `<div class="pagination-bar"><span class="page-info">共 ${totalFiltered} 条</span></div>`;
    }
    const curPage = ps.currentPage + 1; // 1-based for display

    // 页码按钮：最多显示7个页码
    let pageButtons = '';
    const maxShow = 7;
    let startP = Math.max(1, curPage - 3);
    let endP = Math.min(totalPages, startP + maxShow - 1);
    if (endP - startP < maxShow - 1) startP = Math.max(1, endP - maxShow + 1);

    if (startP > 1) pageButtons += `<button class="page-btn" onclick="gotoPage('${key}', 0)">1</button>`;
    if (startP > 2) pageButtons += `<span class="page-info">...</span>`;
    for (let p = startP; p <= endP; p++) {
        const active = p === curPage ? 'active' : '';
        pageButtons += `<button class="page-btn ${active}" onclick="gotoPage('${key}', ${p - 1})">${p}</button>`;
    }
    if (endP < totalPages - 1) pageButtons += `<span class="page-info">...</span>`;
    if (endP < totalPages) pageButtons += `<button class="page-btn" onclick="gotoPage('${key}', ${totalPages - 1})">${totalPages}</button>`;

    return `<div class="pagination-bar">
        <label>每页：</label>
        <select class="page-size-select" onchange="changePageSize('${key}', this.value)">
            ${PAGE_SIZES.map(s => `<option value="${s}"${s === ps.pageSize ? ' selected' : ''}>${s} 条</option>`).join('')}
        </select>
        <span class="page-info">共 ${totalFiltered} 条，${totalPages} 页</span>
        <div class="page-nav">
            <button class="page-btn" onclick="gotoPage('${key}', 0)" ${curPage === 1 ? 'disabled' : ''}>«</button>
            <button class="page-btn" onclick="gotoPage('${key}', ${ps.currentPage - 1})" ${curPage === 1 ? 'disabled' : ''}>‹</button>
            ${pageButtons}
            <button class="page-btn" onclick="gotoPage('${key}', ${ps.currentPage + 1})" ${curPage === totalPages ? 'disabled' : ''}>›</button>
            <button class="page-btn" onclick="gotoPage('${key}', ${totalPages - 1})" ${curPage === totalPages ? 'disabled' : ''}>»</button>
        </div>
    </div>`;
}

function gotoPage(key, page) {
    const ps = pageState[key];
    if (!ps) return;
    const totalPages = Math.ceil(ps.filteredIndices.length / ps.pageSize);
    if (page < 0 || page >= totalPages) return;
    ps.currentPage = page;
    // 重新渲染当前分类的面板
    renderPanel(key);
}

function changePageSize(key, size) {
    const ps = pageState[key];
    if (!ps) return;
    ps.pageSize = parseInt(size);
    ps.currentPage = 0;  // 切换页大小时回到第一页
    renderPanel(key);
}

// ===== 审批交互 =====
function toggleApproval(catKey, idx) {
    if (approvalExecutionActive) return;
    const item = CAT_DATA[catKey]?.items[idx];
    const groupKey = item?.approval_group_key;
    if (!groupKey) return;
    const current = getStatus(catKey, idx);
    if (current === "approved") return;
    setGroupSelected(groupKey, current !== "selected");
    renderCategoryNav();
    switchTab(catKey);
}

function updatePendingCount() {
    const groups = Object.values(APPROVAL_GROUPS);
    const manualOpen = groups.filter(g => g.review_mode === "manual" && getGroupStatus(g.group_key) !== "approved");
    const autoOpen = groups.filter(g => g.review_mode === "auto" && getGroupStatus(g.group_key) !== "approved");
    const manualSelected = manualOpen.filter(g => getGroupStatus(g.group_key) === "selected").length;
    const autoSelected = autoOpen.filter(g => getGroupStatus(g.group_key) === "selected").length;
    const manualItems = manualOpen.reduce((sum, g) => sum + (g.approve_ids || []).length, 0);
    const autoItems = autoOpen.reduce((sum, g) => sum + (g.approve_ids || []).length, 0);
    document.getElementById("pendingCount").textContent =
        `需人工审核：${manualOpen.length}个人日/${manualItems}条明细（已标记${manualSelected}） · ` +
        `可自动审批：${autoOpen.length}个人日/${autoItems}条明细（已选${autoSelected}）`;

    const selectedGroups = groups.filter(g => getGroupStatus(g.group_key) === "selected");
    const selectedItems = selectedGroups.reduce((sum, g) => sum + (g.approve_ids || []).length, 0);
    const exportButton = document.getElementById("btnConfirm");
    const executeButton = document.getElementById("btnExecute");
    if (selectedGroups.length > 0) {
        exportButton.classList.add("show");
        executeButton.classList.add("show");
        exportButton.textContent = `⬇️ 导出 JSON 备用（${selectedGroups.length}个人日/${selectedItems}条明细）`;
        executeButton.textContent = `✅ 直接审批已选整单（${selectedGroups.length}个人日/${selectedItems}条明细）`;
    } else {
        exportButton.classList.remove("show");
        executeButton.classList.remove("show");
    }
    exportButton.disabled = approvalExecutionActive;
    executeButton.disabled = approvalExecutionActive;
}

function buildSelectedApprovalPlan() {
    const groups = Object.values(APPROVAL_GROUPS)
        .filter(group => getGroupStatus(group.group_key) === "selected")
        .sort((a, b) => `${a.date}|${a.person}`.localeCompare(`${b.date}|${b.person}`, "zh-CN"));
    if (groups.length === 0) return null;

    const seenIds = new Set();
    let duplicateId = null;
    for (const group of groups) {
        for (const approveId of (group.approve_ids || [])) {
            if (seenIds.has(approveId)) duplicateId = approveId;
            seenIds.add(approveId);
        }
    }
    if (duplicateId) {
        alert("审批计划生成失败：不同人员日期整单中出现重复审批ID。为避免误批，已停止操作。");
        return null;
    }

    const manualGroupCount = groups.filter(group => group.review_mode === "manual").length;
    const autoGroupCount = groups.length - manualGroupCount;
    const plan = {
        schema_version: 1,
        month: currentMonth,
        generated_at: new Date().toISOString(),
        source_fetch_time: ALL_DATA[currentMonth].fetch_time || "",
        summary: {
            group_count: groups.length,
            item_count: seenIds.size,
            manual_group_count: manualGroupCount,
            auto_group_count: autoGroupCount
        },
        groups: groups.map(group => ({
            group_key: group.group_key,
            date: group.date,
            person: group.person,
            user_id: group.user_id || null,
            review_mode: group.review_mode,
            item_count: group.item_count,
            approve_ids: [...group.approve_ids]
        }))
    };
    window.__lastApprovalPlan = plan;
    return plan;
}

function exportApprovalPlan() {
    const plan = buildSelectedApprovalPlan();
    if (!plan) return;
    const blob = new Blob([JSON.stringify(plan, null, 2)], {type: "application/json;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `srdpm-approval-plan-${currentMonth}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    alert(`已导出审批清单：${plan.summary.group_count}个人日、${plan.summary.item_count}条唯一明细。\n\n导出没有修改 SRDPM，可用于离线核对。`);
}

function confirmApprovals() {
    exportApprovalPlan();
}

function setApprovalFeedback(kind, message) {
    const element = document.getElementById("approvalFeedback");
    element.className = `approval-feedback show ${kind}`;
    element.textContent = message;
}

function showPreparedApprovalConfirmation(prepared) {
    const overlay = document.getElementById("approvalConfirmOverlay");
    const summary = prepared.summary;
    const groups = Array.isArray(prepared.groups) ? prepared.groups : [];
    const manualCount = groups.filter(group => group.review_mode === "manual").length;
    const autoCount = groups.length - manualCount;
    document.getElementById("approvalConfirmSummary").textContent =
        `${summary.month} · ${summary.group_count}个人日 · ${summary.id_count}个待审ID` +
        `（人工确认${manualCount}单，自动候选${autoCount}单） · ` +
        `清单校验码 ${String(summary.sha256 || "").slice(0, 12).toUpperCase()}`;

    const body = document.getElementById("approvalConfirmRows");
    body.replaceChildren();
    for (const group of groups) {
        const row = document.createElement("tr");
        const source = `${group.review_mode === "manual" ? "人工异常" : "自动候选"} · ${group.review_summary || "未分类"}`;
        const projectValues = Array.isArray(group.projects) ? group.projects : [];
        const projectCount = Number(group.project_count || projectValues.length);
        const projects = projectValues.length
            ? `${projectValues.join("、")}${projectCount > projectValues.length ? `等（共${projectCount}项）` : ""}`
            : "未标注项目";
        const values = [
            group.date || "",
            group.person || "",
            source,
            projects,
            `${Number(group.work_hours || 0).toFixed(2)}h`,
            `${group.item_count || group.id_count || 0}条明细 / ${group.id_count || 0}个待审ID`
        ];
        for (const value of values) {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.appendChild(cell);
        }
        body.appendChild(row);
    }

    overlay.classList.add("show");
    overlay.setAttribute("aria-hidden", "false");
    const accept = document.getElementById("approvalConfirmAccept");
    const cancel = document.getElementById("approvalConfirmCancel");
    accept.focus();
    return new Promise(resolve => {
        const finish = result => {
            overlay.classList.remove("show");
            overlay.setAttribute("aria-hidden", "true");
            accept.removeEventListener("click", acceptHandler);
            cancel.removeEventListener("click", cancelHandler);
            overlay.removeEventListener("click", backdropHandler);
            document.removeEventListener("keydown", keyHandler);
            resolve(result);
        };
        const acceptHandler = () => finish(true);
        const cancelHandler = () => finish(false);
        const backdropHandler = event => {
            if (event.target === overlay) finish(false);
        };
        const keyHandler = event => {
            if (event.key === "Escape") finish(false);
        };
        accept.addEventListener("click", acceptHandler);
        cancel.addEventListener("click", cancelHandler);
        overlay.addEventListener("click", backdropHandler);
        document.addEventListener("keydown", keyHandler);
    });
}

function showCredentialSetup() {
    const overlay = document.getElementById("credentialSetupOverlay");
    const usernameInput = document.getElementById("credentialUsername");
    const passwordInput = document.getElementById("credentialPassword");
    const errorElement = document.getElementById("credentialSetupError");
    const saveButton = document.getElementById("credentialSetupSave");
    const cancelButton = document.getElementById("credentialSetupCancel");
    usernameInput.value = "";
    passwordInput.value = "";
    errorElement.textContent = "";
    overlay.classList.add("show");
    overlay.setAttribute("aria-hidden", "false");
    usernameInput.focus();

    return new Promise(resolve => {
        const finish = result => {
            overlay.classList.remove("show");
            overlay.setAttribute("aria-hidden", "true");
            saveButton.removeEventListener("click", saveHandler);
            cancelButton.removeEventListener("click", cancelHandler);
            overlay.removeEventListener("click", backdropHandler);
            document.removeEventListener("keydown", keyHandler);
            usernameInput.value = "";
            passwordInput.value = "";
            errorElement.textContent = "";
            resolve(result);
        };
        const saveHandler = () => {
            const username = usernameInput.value.trim();
            const password = passwordInput.value;
            if (!username || !password) {
                errorElement.textContent = "用户名和密码都不能为空。";
                return;
            }
            if (/\\r|\\n|\\0/.test(username) || /\\r|\\n|\\0/.test(password)) {
                errorElement.textContent = "登录信息包含不允许的控制字符。";
                return;
            }
            finish({username, password});
        };
        const cancelHandler = () => finish(null);
        const backdropHandler = event => {
            if (event.target === overlay) finish(null);
        };
        const keyHandler = event => {
            if (event.key === "Escape") finish(null);
            if (event.key === "Enter" && event.target === passwordInput) saveHandler();
        };
        saveButton.addEventListener("click", saveHandler);
        cancelButton.addEventListener("click", cancelHandler);
        overlay.addEventListener("click", backdropHandler);
        document.addEventListener("keydown", keyHandler);
    });
}

async function ensureCredentialsConfigured() {
    const statusData = await requestLocalApprovalApi("/credentials/status");
    if (statusData.credentials?.configured === true) return true;
    const credentials = await showCredentialSetup();
    if (!credentials) return false;
    try {
        const saved = await requestLocalApprovalApi("/credentials/configure", {
            method: "POST",
            body: {username: credentials.username, password: credentials.password}
        });
        if (saved.credentials?.configured !== true) {
            throw new Error("Windows 凭据管理器未确认保存成功");
        }
        return true;
    } finally {
        credentials.password = "";
    }
}

function setApprovalBusy(active) {
    approvalExecutionActive = active;
    document.body.classList.toggle("approval-busy", active);
    document.querySelectorAll(".toolbar button").forEach(button => {
        button.disabled = active;
    });
    updatePendingCount();
}

async function requestLocalApprovalApi(path, {method = "GET", body = null} = {}) {
    if (!LOCAL_SERVICE) throw new Error("本机审批服务未连接");
    const headers = {[LOCAL_SERVICE.csrf_header]: LOCAL_SERVICE.csrf_token};
    if (body !== null) headers["Content-Type"] = "application/json";
    let response;
    try {
        response = await fetch(`${LOCAL_SERVICE.api_base}${path}`, {
            method,
            headers,
            body: body === null ? undefined : JSON.stringify(body),
            credentials: "same-origin",
            cache: "no-store",
            redirect: "error"
        });
    } catch (error) {
        error.responseReceived = false;
        throw error;
    }
    let data = null;
    try {
        data = await response.json();
    } catch (error) {
        const invalidResponse = new Error("本机审批服务返回了无法识别的结果");
        invalidResponse.responseReceived = true;
        throw invalidResponse;
    }
    if (!response.ok || !data?.ok) {
        const apiError = new Error(data?.error?.message || `本机审批服务请求失败（HTTP ${response.status}）`);
        apiError.code = data?.error?.code || "api_error";
        apiError.responseReceived = true;
        throw apiError;
    }
    return data;
}

function delay(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function pollApprovalJob(jobId) {
    const deadline = Date.now() + 20 * 60 * 1000;
    while (Date.now() < deadline) {
        await delay(900);
        const data = await requestLocalApprovalApi(`/approval/jobs/${encodeURIComponent(jobId)}`);
        const job = data.job;
        if (!job || job.job_id !== jobId) throw new Error("本机审批服务返回了不匹配的任务");
        if (job.status === "succeeded" || job.status === "failed") return job;
        setApprovalFeedback("info", job.message || "本机服务正在校验并审批，请勿关闭页面或重复点击……");
    }
    const error = new Error("等待审批结果超时；任务可能仍在执行，请勿重复审批，并到 SRDPM 人工核对");
    error.jobMayBeRunning = true;
    throw error;
}

function applyApprovalJobResult(job, executionMonth) {
    if (currentMonth !== executionMonth) {
        setApprovalFeedback("error", "审批已返回，但页面月份已改变。请重新打开本月看板核对，勿重复审批。");
        return;
    }
    const rows = Array.isArray(job.groups) ? job.groups : [];
    const verifiedRows = rows.filter(row => row.state === "verified_approved");
    for (const row of verifiedRows) {
        if (!APPROVAL_GROUPS[row.group_key]) continue;
        APPROVAL_GROUPS[row.group_key].status = "approved";
        delete approvalState[row.group_key];
    }
    saveState();
    renderCategoryNav();
    switchTab(currentCatKey);

    const unknownRows = rows.filter(row => row.state === "unknown");
    const notAttemptedRows = rows.filter(row => row.state === "not_attempted");
    const details = unknownRows.slice(0, 5).map(row => `${row.date} ${row.person}`).join("、");
    if (job.outcome === "succeeded") {
        setApprovalFeedback("success", `审批完成：${verifiedRows.length}个人日已由 SRDPM 回读确认通过。\n本页已更新；下次重新打开前请重新拉取本月数据，以同步本地归档。`);
    } else if (job.outcome === "partial_success") {
        setApprovalFeedback("warning", `部分完成：${verifiedRows.length}个人日已确认通过；${unknownRows.length}个人日状态未知；${notAttemptedRows.length}个人日未尝试。${details ? `\n状态未知：${details}${unknownRows.length > 5 ? "……" : ""}` : ""}\n请人工核对未成功整单，勿直接重复点击；重新打开前请先同步本月归档。`);
    } else if (job.outcome === "state_unknown") {
        setApprovalFeedback("error", `审批结果存在未知状态。${details ? `请在 SRDPM 核对：${details}${unknownRows.length > 5 ? "……" : ""}` : "请到 SRDPM 人工核对。"}\n勿直接重复点击。`);
    } else {
        setApprovalFeedback("warning", `审批未执行：${job.message || "提交前校验未通过"}。已保留原选择。`);
    }
}

async function executeSelectedApprovals() {
    if (approvalExecutionActive) return;
    const plan = buildSelectedApprovalPlan();
    if (!plan) return;
    if (!LOCAL_SERVICE) {
        setApprovalFeedback("info", "正在自动连接后台审批服务并转交当前选择……");
        location.assign(buildLocalServiceTransferUrl(plan));
        return;
    }

    const executionMonth = currentMonth;
    const groupKeys = plan.groups.map(group => group.group_key);
    let executeRequestStarted = false;
    let jobStarted = false;
    setApprovalBusy(true);
    setApprovalFeedback("info", "正在检查本机审批服务和登录配置……");
    try {
        if (!await ensureCredentialsConfigured()) {
            setApprovalFeedback("info", "已取消登录配置，本次没有发送真实审批请求，原选择保留。");
            return;
        }
        setApprovalFeedback("info", "正在由本机服务从当前归档重建并校验所选整单……");
        const prepareData = await requestLocalApprovalApi("/approval/prepare", {
            method: "POST",
            body: {month: executionMonth, group_keys: groupKeys}
        });
        const prepared = prepareData.prepared;
        const summary = prepared?.summary;
        if (!prepared?.ticket || !summary || summary.month !== executionMonth ||
            summary.group_count !== plan.summary.group_count || summary.id_count !== plan.summary.item_count) {
            throw new Error("页面数据与当前本地归档不一致，已停止审批；请重新生成并打开看板");
        }
        const preparedGroups = Array.isArray(prepared.groups) ? prepared.groups : [];
        const preparedKeys = new Set(preparedGroups.map(group => group.group_key));
        if (preparedGroups.length !== groupKeys.length ||
            groupKeys.some(groupKey => !preparedKeys.has(groupKey))) {
            throw new Error("本机服务返回的核对清单与所选整单不一致，已停止审批");
        }
        const confirmed = await showPreparedApprovalConfirmation(prepared);
        if (!confirmed) {
            setApprovalFeedback("info", "已取消，本次没有发送真实审批请求，原选择保留。");
            return;
        }

        setApprovalFeedback("info", "请在随后出现的 Windows 安全确认中核对相同清单校验码；请勿重复点击……");
        executeRequestStarted = true;
        const executeData = await requestLocalApprovalApi("/approval/execute", {
            method: "POST",
            body: {ticket: prepared.ticket}
        });
        const jobId = executeData.job?.job_id;
        if (!jobId) throw new Error("本机审批服务没有返回任务编号");
        jobStarted = true;
        const job = await pollApprovalJob(jobId);
        applyApprovalJobResult(job, executionMonth);
    } catch (error) {
        const outcomeMayBeUnknown = jobStarted ||
            (executeRequestStarted && error.responseReceived !== true);
        if (outcomeMayBeUnknown || error.jobMayBeRunning) {
            setApprovalFeedback("error", `${error.message || "无法取得审批结果"}\n真实任务可能已经开始，请勿重复点击；请先到 SRDPM 人工核对。`);
        } else {
            setApprovalFeedback("error", `审批已停止：${error.message || "未知错误"}。原选择保留。`);
        }
    } finally {
        setApprovalBusy(false);
    }
}

function resetAll() {
    if (approvalExecutionActive) return;
    if (confirm("确定要清空当前月份的本地选择吗？这不会修改SRDPM状态。")) {
        localStorage.removeItem(getStorageKey(currentMonth));
        approvalState = {};
        renderCategoryNav();
        switchTab(currentCatKey);
    }
}

function toggleInstructions() {
    document.getElementById("instructionsPanel").style.display =
        document.getElementById("instructionsPanel").style.display === "none" ? "block" : "none";
}

function toggleStats() {
    const section = document.getElementById("statsSection");
    const willShow = section.style.display === "none";
    section.style.display = willShow ? "block" : "none";
    if (willShow) updateStats(currentMonth);
}

function refilterThree() {
    const subtype = document.getElementById("filter_subtype")?.value || "all";
    const person = document.getElementById("filter_person3")?.value || "all";
    const rows = document.querySelectorAll("#panel_three tbody tr");
    let count = 0;
    rows.forEach(row => {
        const cells = row.querySelectorAll("td");
        if (cells.length < 3) return;
        const rowSubtype = cells[2]?.textContent || "";
        const rowPerson = cells[1]?.textContent || "";
        const match = (subtype === "all" || rowSubtype.includes(subtype)) && (person === "all" || rowPerson.includes(person));
        row.style.display = match ? "" : "none";
        if (match) count++;
    });
    const el = document.getElementById("filterCount3");
    if (el) el.textContent = `显示 ${count} 条`;
}

function refilterFive() {
    const person = document.getElementById("filter_person5")?.value || "all";
    const search = (document.getElementById("search5")?.value || "").toLowerCase();
    const rows = document.querySelectorAll("#panel_five tbody tr");
    let count = 0;
    rows.forEach(row => {
        const cells = row.querySelectorAll("td");
        if (cells.length < 3) return;
        const rowPerson = cells[1]?.textContent || "";
        const rowText = row.textContent.toLowerCase();
        const match = (person === "all" || rowPerson.includes(person)) && (!search || rowText.includes(search));
        row.style.display = match ? "" : "none";
        if (match) count++;
    });
    const el = document.getElementById("filterCount5");
    if (el) el.textContent = `显示 ${count} 条`;
}

function refilterSix() {
    const cat = CAT_DATA["six"];
    const person = document.getElementById("filter_person6")?.value || "all";
    const search = (document.getElementById("search6")?.value || "").toLowerCase();
    // 数据过滤：计算匹配的索引列表
    const filtered = [];
    for (let i = 0; i < cat.items.length; i++) {
        const item = cat.items[i];
        const match = (person === "all" || item.person.includes(person)) && (!search || (item.title + item.content + item.project + item.date + item.person).toLowerCase().includes(search));
        if (match) filtered.push(i);
    }
    // 更新分页状态的过滤索引
    pageState["six"].filteredIndices = filtered;
    pageState["six"].currentPage = 0;  // 过滤后回到第一页
    // 更新计数显示
    const el = document.getElementById("filterCount6");
    if (el) el.textContent = `显示 ${filtered.length} 条`;
    // 重新渲染面板
    renderPanel("six");
}

// ===== 统计分析 =====
let charts = [];

function updateStats(ml) {
    const enhanced = ALL_DATA[ml].enhanced;
    const display = ALL_DATA[ml].display;
    const section = document.getElementById("statsSection");

    if (!enhanced) {
        section.style.display = "none";
        return;
    }

    // Destroy old charts
    charts.forEach(c => c.destroy());
    charts = [];

    document.getElementById("statsMeta").textContent =
        `${display} · ${enhanced.total_count}条明细 · ${enhanced.person_count}人 · 总工时${enhanced.total_hours}h`;

    const body = document.getElementById("statsBody");
    let html = `<div class="stats-cards">
        <div class="stat-card"><div class="val" style="color:#6c5ce7;">${enhanced.total_hours}h</div><div class="label">总工时</div></div>
        <div class="stat-card"><div class="val" style="color:#00b894;">${enhanced.project_hours}h</div><div class="label">项目工时</div></div>
        <div class="stat-card"><div class="val" style="color:#00cec9;">${enhanced.platform_hours}h</div><div class="label">平台工时</div></div>
        <div class="stat-card"><div class="val" style="color:#e17055;">${enhanced.total_count}</div><div class="label">条目数</div></div>
        <div class="stat-card"><div class="val" style="color:#fd79a8;">${enhanced.person_count}</div><div class="label">涉及人数</div></div>
        <div class="stat-card"><div class="val" style="color:#0984e3;">${enhanced.group_count}</div><div class="label">项目群数</div></div>
    </div>`;

    html += `<div class="chart-row">
        <div class="chart-box"><h3>📅 各周工时趋势</h3><div style="height:260px;position:relative;"><canvas id="chartWeek"></canvas></div></div>
        <div class="chart-box"><h3>👥 人员工时排行</h3><div style="height:300px;position:relative;"><canvas id="chartPerson"></canvas></div></div>
    </div>`;

    html += `<div class="chart-row">
        <div class="chart-box"><h3>🔧 项目/平台占比</h3><div style="height:300px;position:relative;"><canvas id="chartStack"></canvas></div></div>
        <div class="chart-box"><h3>📦 项目群工时分析</h3><div style="height:400px;position:relative;"><canvas id="chartGroup"></canvas></div></div>
    </div>`;

    // Person-group heatmap table
    const allGroups = enhanced.group_ranking.map(g => g[0]);
    const personNames = enhanced.person_ranking.map(p => p[0]);
    const matrix = enhanced.person_group_matrix;

    html += `<div class="chart-box" style="margin-bottom:20px;">
        <h3>🔥 按人 × 项目群 工时矩阵</h3>
        <div style="font-size:12px;color:#636e72;margin-bottom:10px;">颜色越深工时越多。</div>
        <div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">
        <thead><tr style="background:#f8f9fa;">
            <th style="padding:8px;text-align:left;border-bottom:2px solid #e0e0e0;position:sticky;left:0;background:#f8f9fa;z-index:1;">人员</th>`;

    for (const g of allGroups) {
        html += `<th style="padding:8px;text-align:right;border-bottom:2px solid #e0e0e0;white-space:nowrap;" title="${escapeHtml(g)}">${escapeHtml(g)}</th>`;
    }
    html += `<th style="padding:8px;text-align:right;border-bottom:2px solid #e0e0e0;">合计</th></tr></thead><tbody>`;

    // Find max value for color gradient
    let maxVal = 0;
    for (const p of personNames) {
        for (const g of allGroups) {
            const v = matrix[p]?.[g] || 0;
            if (v > maxVal) maxVal = v;
        }
    }

    for (let pi = 0; pi < personNames.length; pi++) {
        const pname = personNames[pi];
        const bg = pi % 2 === 0 ? "#f8f9ff" : "#fff";
        html += `<tr style="background:${bg};">`;
        html += `<td style="padding:8px;border-bottom:1px solid #f0f0f0;font-weight:600;position:sticky;left:0;background:${bg};z-index:1;">${escapeHtml(pname)}</td>`;
        let rowTotal = 0;
        for (const g of allGroups) {
            const v = matrix[pname]?.[g] || 0;
            rowTotal += v;
            if (v > 0) {
                const intensity = Math.min(v / maxVal, 1.0);
                const alpha = 0.15 + intensity * 0.75;
                const color = `rgba(108, 92, 231, ${Math.round(alpha * 100) / 100})`;
                const textColor = intensity > 0.5 ? "#fff" : "#333";
                html += `<td style="padding:8px;border-bottom:1px solid #f0f0f0;text-align:right;background:${color};color:${textColor};font-weight:${v > 10 ? 600 : 400};">${v}</td>`;
            } else {
                html += `<td style="padding:8px;border-bottom:1px solid #f0f0f0;text-align:right;color:#ccc;">-</td>`;
            }
        }
        html += `<td style="padding:8px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:700;">${Math.round(rowTotal * 10) / 10}</td>`;
        html += `</tr>`;
    }
    html += `</tbody></table></div></div>`;

    body.innerHTML = html;

    // Render charts
    if (typeof Chart === "undefined") {
        body.insertAdjacentHTML("afterbegin", '<div class="panel-desc">为保证本机审批页面不加载外部脚本，趋势图已停用；统计表格与审批功能不受影响。</div>');
        return;
    }
    const weekLabels = Object.keys(enhanced.week_stats);
    const weekHours = weekLabels.map(w => enhanced.week_stats[w].hours);

    charts.push(new Chart(document.getElementById('chartWeek'), {
        type: 'bar',
        data: { labels: weekLabels, datasets: [{ label: '总工时(h)', data: weekHours, backgroundColor: ['#6c5ce7','#a29bfe','#fd79a8','#00b894','#e17055'], borderRadius: 6 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
    }));

    const personLabels = enhanced.person_ranking.map(p => p[0]);
    const personTotals = enhanced.person_ranking.map(p => p[1].total);
    const personProjects = enhanced.person_ranking.map(p => p[1].project);
    const personPlatforms = enhanced.person_ranking.map(p => p[1].platform);

    charts.push(new Chart(document.getElementById('chartPerson'), {
        type: 'bar',
        data: { labels: personLabels, datasets: [{ label: '总工时(h)', data: personTotals, backgroundColor: '#6c5ce7', borderRadius: 4 }] },
        options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } } }
    }));

    charts.push(new Chart(document.getElementById('chartStack'), {
        type: 'bar',
        data: { labels: personLabels, datasets: [
            { label: '项目工时', data: personProjects, backgroundColor: '#6c5ce7', borderRadius: 4 },
            { label: '平台工时', data: personPlatforms, backgroundColor: '#00cec9', borderRadius: 4 }
        ] },
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true } }, plugins: { legend: { position: 'top' } } }
    }));

    const groupLabels = enhanced.group_ranking.map(g => g[0]);
    const groupHours = enhanced.group_ranking.map(g => g[1].hours);
    const groupColors = groupLabels.map(g => {
        if (g.includes('平台') || g.includes('公共')) return '#00cec9';
        if (g.includes('G/')) return '#6c5ce7';
        if (g.includes('P/')) return '#e17055';
        if (g.includes('H/')) return '#00b894';
        if (g.includes('M/')) return '#fd79a8';
        if (g.includes('B/')) return '#0984e3';
        if (g.includes('未分类')) return '#b2bec3';
        return '#636e72';
    });

    charts.push(new Chart(document.getElementById('chartGroup'), {
        type: 'bar',
        data: { labels: groupLabels, datasets: [{ label: '工时(h)', data: groupHours, backgroundColor: groupColors, borderRadius: 4 }] },
        options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } } }
    }));
}

// Start
document.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>"""

    # Inject data
    html = html.replace("__ALL_DATA_PLACEHOLDER__", all_data_json)

    # Write output
    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n✅ 多月看板已生成: {OUTPUT_HTML}")
    print(f"   可用月份: {', '.join(months)}")
    for ml in months:
        display = all_month_data[ml]["display"]
        summary = all_month_data[ml]["approval_summary"]
        platform_rows = len(all_month_data[ml]["cats"]["five"]["items"])
        missed_people = len(all_month_data[ml]["cats"]["one"]["items"])
        print(
            f"   {display}: 人工审核 {summary['manual_pending_groups']}个人日/"
            f"{summary['manual_pending_items']}条明细 | 自动候选 "
            f"{summary['auto_pending_groups']}个人日/{summary['auto_pending_items']}条明细 | "
            f"平台关注 {platform_rows}条 | 已审批整单 {summary['approved_groups']}"
            f"{' | ' + str(missed_people) + '人漏报' if missed_people else ''}"
        )



if __name__ == "__main__":
    main()
