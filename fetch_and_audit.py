#!/usr/bin/env python3
"""
SRDPM 工时数据拉取 + 审核 - 通用版（支持任意月份）

用法：
  python fetch_and_audit.py 2026 7        # 拉取7月数据并审核
  python fetch_and_audit.py 2026 6 7      # 拉取6月+7月数据并审核
  python fetch_and_audit.py --fetch-only 2026 7  # 只拉取不审核

数据存档目录结构：
  srdpm_archive/
    2026-06/
      raw_data.json         # 每日明细原始数据
      audit_report.json     # 审核发现
      audit_report.md       # 审核Markdown报告
    2026-07/
      ...
"""
import requests, json, time, re, os, sys, io, warnings, argparse, shutil, tempfile
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import calendar

warnings.filterwarnings('ignore')
# 使用 reconfigure 而非 TextIOWrapper 避免关闭底层buffer
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

# ========== 配置 ==========
USER = os.environ.get("SRDPM_USERNAME", "").strip()
PASS = os.environ.get("SRDPM_PASSWORD", "")
URL = "https://rd-mokadisplay.tcl.com/srdpm/#/work-list"
BASE_API = "https://rd-mokadisplay.tcl.com/srdpm-api/workload/approve"
PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = str(PROJECT_DIR)
ARCHIVE_DIR = os.path.join(OUT_DIR, "srdpm_archive")
CHIP_HISTORY_NAME = "chip_history.json"
VERIFY_TLS = os.environ.get("SRDPM_VERIFY_TLS", "true").strip().lower() not in {
    "0", "false", "no", "off"
}

# 此脚本的职责严格限定为抓取与审计。尽管 SRDPM 的读取接口位于
# ``workload/approve`` 路径下，也绝不能允许这里通过字符串拼接调用审批或驳回。
READ_ONLY_API_PATHS = frozenset({"userList", "list", "statistics", "detail"})


def get_month_range(year, month):
    """获取指定月份的日期范围"""
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return start, end


def month_label(year, month):
    """格式化月份标签，如 '2026-06'"""
    return f"{year}-{month:02d}"


def month_display(year, month):
    """中文显示，如 '2026年6月'"""
    return f"{year}年{month}月"


# ========== Step 1: 登录获取凭证 ==========
def login_srdpm(username=None, password=None):
    """创建只读抓取会话。

    未传入参数时保留命令行环境变量的既有行为；后台刷新器可仅在内存中
    从 Windows Credential Manager 传入凭据，避免把凭据写入环境变量。
    """
    username = USER if username is None else str(username).strip()
    password = PASS if password is None else str(password)
    if not username or not password:
        raise RuntimeError(
            "缺少 SRDPM 登录凭据。请先设置 SRDPM_USERNAME 和 SRDPM_PASSWORD 环境变量。"
        )

    print("=" * 60)
    print("[1] 登录 SRDPM 并获取凭证...")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=not VERIFY_TLS)
        page.set_viewport_size({"width": 1920, "height": 1080})

        page.goto(URL, timeout=60000, wait_until="networkidle")
        page.wait_for_timeout(2000)

        page.locator('input[name="username"]').fill(username)
        page.locator('input[name="password"]').fill(password)
        page.locator('button:has-text("登录")').click()
        page.wait_for_timeout(6000)

        captured_token = {}
        def on_request(request):
            if '/srdpm-api/' in request.url and 'accesstoken' not in captured_token:
                try:
                    h = request.headers
                    if 'accesstoken' in h:
                        captured_token['accesstoken'] = h['accesstoken']
                except:
                    pass

        page.on('request', on_request)

        try:
            page.locator('button:has-text("筛选")').click()
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"  筛选点击失败: {e}, 尝试等待...")
            page.wait_for_timeout(3000)

        cookies = page.context.cookies()
        cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])

        browser.close()

    accesstoken = captured_token.get('accesstoken', '')
    print(f"  accesstoken: {'已获取' if accesstoken else '未获取!!!'}")
    print(f"  cookies: {len(cookies)} 个")

    session = requests.Session()
    session.verify = VERIFY_TLS
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'accesstoken': accesstoken,
        'Cookie': cookie_str,
        'Referer': 'https://rd-mokadisplay.tcl.com/srdpm/'
    })
    return session


def api_call(session, path, payload):
    if path not in READ_ONLY_API_PATHS:
        raise ValueError(f"fetch_and_audit.py 只允许读取接口，拒绝调用: {path}")
    url = f"{BASE_API}/{path}"
    try:
        r = session.post(url, json=payload, timeout=30)
        response = r.json()
    except Exception as e:
        # 不把响应内容、cookie 或 token 写入错误信息；调用方必须停止本次刷新。
        raise RuntimeError(f"只读接口 {path} 调用失败") from e
    if not isinstance(response, dict) or response.get('code') != 200:
        raise RuntimeError(f"只读接口 {path} 返回失败状态")
    return response


def dedupe_parent_records(records):
    """按人员父记录合并数据，并在边界处按 approve_id 去重子项。

    SRDPM 的多个 status 查询可能返回重叠集合。这里保留第一次出现的
    父记录顺序，同时把重复子项合并，避免后续审计与审批清单重复计数。
    """
    merged = {}
    order = []
    global_child_locations = {}

    for parent in records:
        parent_key = str(
            parent.get("id")
            or parent.get("uid")
            or parent.get("user_id")
            or parent.get("cn_name")
            or len(order)
        )
        if parent_key not in merged:
            base = dict(parent)
            base["children"] = []
            base["_child_keys"] = {}
            merged[parent_key] = base
            order.append(parent_key)

        target = merged[parent_key]
        child_keys = target["_child_keys"]
        for child in parent.get("children", []):
            approve_id = str(child.get("approve_id") or "").strip()
            if approve_id:
                child_key = ("approve_id", approve_id)
            else:
                child_key = (
                    "fallback",
                    str(child.get("items") or child.get("items_name") or ""),
                    str(child.get("title") or ""),
                    str(child.get("content") or "").strip(),
                    str(child.get("work_hours") or ""),
                )

            # approve_id 在同一天全局唯一。即使重叠状态查询给父记录分配了
            # 不同 id，也不能把同一审批明细再次放入另一个父记录。
            if approve_id and child_key in global_child_locations:
                existing_parent_key, existing_index = global_child_locations[child_key]
                existing = merged[existing_parent_key]["children"][existing_index]
                if child.get("status") == "通过" and existing.get("status") != "通过":
                    merged[existing_parent_key]["children"][existing_index] = dict(child)
                continue

            if child_key not in child_keys:
                child_keys[child_key] = len(target["children"])
                target["children"].append(dict(child))
                if approve_id:
                    global_child_locations[child_key] = (
                        parent_key,
                        child_keys[child_key],
                    )
                continue

            # 若重叠查询返回了不同状态，优先保留明确的“通过”状态。
            existing = target["children"][child_keys[child_key]]
            if child.get("status") == "通过" and existing.get("status") != "通过":
                target["children"][child_keys[child_key]] = dict(child)

    result = []
    for key in order:
        parent = merged[key]
        parent.pop("_child_keys", None)
        result.append(parent)
    return result


# ========== Step 2-4: 拉取月份数据 ==========
def fetch_month(session, year, month):
    """拉取指定月份的全部数据"""
    ml = month_label(year, month)
    md = month_display(year, month)
    start_date, end_date = get_month_range(year, month)

    print(f"\n{'=' * 60}")
    print(f"拉取 {md} 数据 ({ml})")
    print(f"{'=' * 60}")

    # 获取用户列表
    print("[2] 获取用户列表...")
    last_day_str = f"{year}-{month}-{calendar.monthrange(year, month)[1]}"
    user_resp = api_call(session, 'userList', {"time": last_day_str, "group_id": None})
    user_data = user_resp.get('data')
    if not isinstance(user_data, list) or not user_data:
        raise RuntimeError("只读接口 userList 返回空或结构异常")
    try:
        all_users = [{'uid': u['uid'], 'cn_name': u['cn_name']} for u in user_data]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("只读接口 userList 人员结构异常") from exc
    print(f"  获取到 {len(all_users)} 人")

    # 循环获取每日数据
    print("[3] 循环获取每日数据...")
    daily_data = {}
    current = start_date
    total_days = 0
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        time_param = f"{current.year}-{current.month}-{current.day}"

        list_data = []
        for status in ['0', '1', '2', '3']:
            page_num = 1
            while True:
                payload = {
                    "page": page_num,
                    "page_size": 50,
                    "type": None,
                    "status": status,
                    "time": time_param,
                    "user_id": None,
                    "group_id": None
                }
                resp = api_call(session, 'list', payload)
                response_data = resp.get('data')
                if not isinstance(response_data, dict):
                    raise RuntimeError(f"只读接口 list 返回结构异常（{date_str}）")
                data_list = response_data.get('data', [])
                if not isinstance(data_list, list):
                    raise RuntimeError(f"只读接口 list 明细结构异常（{date_str}）")
                if data_list:
                    list_data.extend(data_list)
                total = response_data.get('total', 0)
                per_page = response_data.get('per_page', 50)
                if not isinstance(total, int) or not isinstance(per_page, int) or per_page <= 0:
                    raise RuntimeError(f"只读接口 list 分页结构异常（{date_str}）")
                if page_num * per_page >= total or not data_list:
                    break
                page_num += 1

        received_count = len(list_data)
        list_data = dedupe_parent_records(list_data)

        stats_resp = api_call(session, 'statistics', {"time": time_param})
        detail_resp = api_call(session, 'detail', {"time": time_param})
        statistics_data = stats_resp.get('data')
        detail_data = detail_resp.get('data')
        if not isinstance(statistics_data, dict) or not isinstance(detail_data, dict):
            raise RuntimeError(f"只读统计或明细接口返回结构异常（{date_str}）")

        daily_data[date_str] = {
            'list': list_data,
            'statistics': statistics_data,
            'detail': detail_data
        }
        total_days += 1
        unique_children = sum(len(parent.get("children", [])) for parent in list_data)
        print(
            f"  {date_str}: 父记录={len(list_data)}条，子项={unique_children}条"
            f"（合并前父记录={received_count}条）"
        )
        current += timedelta(days=1)

    print(f"  共获取 {total_days} 天数据")

    # 保存原始数据
    month_dir = os.path.join(ARCHIVE_DIR, ml)
    os.makedirs(month_dir, exist_ok=True)
    raw_file = os.path.join(month_dir, "raw_data.json")

    print(f"[4] 保存原始数据到 {raw_file}...")
    with open(raw_file, 'w', encoding='utf-8') as f:
        json.dump({
            'users': all_users,
            'daily_data': daily_data,
            'fetch_time': datetime.now().isoformat(),
            'date_range': f"{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}"
        }, f, ensure_ascii=False, indent=2)
    print(f"  已保存 ({os.path.getsize(raw_file):,} bytes)")

    # 同时在工作目录保留一份兼容旧脚本的位置
    compat_file = os.path.join(OUT_DIR, f"srdpm_daily_data_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.json")
    shutil.copy2(raw_file, compat_file)
    print(f"  兼容副本: {compat_file}")

    return {
        'month_dir': month_dir,
        'raw_file': raw_file,
        'all_users': all_users,
        'daily_data': daily_data,
        'total_days': total_days,
        'start_date': start_date,
        'end_date': end_date,
    }


# ========== 审核逻辑（从 audit_june.py 移植） ==========
def load_project_mapping():
    """加载项目映射"""
    mapping_file = os.path.join(OUT_DIR, 'project_mapping.json')
    if not os.path.exists(mapping_file):
        print(f"  ⚠️ 项目映射文件不存在: {mapping_file}")
        return None
    with open(mapping_file, encoding='utf-8') as f:
        return json.load(f)


LEADERS_ANY = ['梁郁沛']
LEADERS_NO_G = ['何文亮']
LEADERS_ONLY_G = ['田昭辉']

LEAVE_TRAVEL_KEYWORDS = ['休假', '请假', '出差', 'leave', 'vacation', 'travel']


def normalize_customer(c):
    if not c:
        return ''
    c = str(c).strip()
    m = re.search(r'战略([A-Z])', c)
    if m:
        return m.group(1)
    m = re.search(r'([A-Z])', c)
    if m:
        return m.group(1)
    return c


def parse_num(s):
    if s is None:
        return 0.0
    s = str(s).strip().replace('--', '').replace('-', '')
    if not s:
        return 0.0
    try:
        return float(s)
    except:
        return 0.0


def parse_hours(total_hours):
    if not total_hours or not isinstance(total_hours, str):
        return 0.0, 0.0, False
    parts = total_hours.split('/')
    reported = parse_num(parts[0])
    if len(parts) > 1:
        checked = parse_num(parts[1])
        has_record = parts[1].strip() not in ['', '-', '--']
    else:
        checked = 0.0
        has_record = False
    return reported, checked, has_record


def is_leave_or_travel(child):
    title = child.get('title', '') or ''
    content = child.get('content', '') or ''
    text = (title + ' ' + content).lower()
    for kw in LEAVE_TRAVEL_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


def get_leave_travel_hours(children):
    total = 0.0
    for ch in children:
        if is_leave_or_travel(ch):
            total += parse_num(ch.get('work_hours', 0))
    return total


def is_platform_item(child):
    items = (child.get('items', '') or '').strip()
    ctype = (child.get('type', '') or '').strip()
    if ctype == '纯平台类':
        return True
    if items.startswith('YF-CP') or items.startswith('YF-SW'):
        return True
    if items in ['LD', 'EDID', 'Diag']:
        return True
    return False


def chip_normalize(code):
    code = code.upper().strip()
    m = re.match(r'([AMT]{1,2})\s*(\d+)([A-Z]*\d*)', code)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3) or ''


def _split_project_mapping_entry(value):
    """Return ``(customer, chip)`` without breaking chip aliases containing '/'."""
    text = str(value or '').strip()
    if not text:
        return '', ''
    # A chip such as MT9603/L contains a slash of its own.  If the value starts
    # with a recognizable chip prefix, the whole value is the chip-only form.
    if chip_normalize(text)[0]:
        return '', text
    if '/' in text:
        customer, chip = text.split('/', 1)
        return customer.strip(), chip.strip()
    return '', text


def _chip_codes_from_person_projects(person_projects):
    """提取当前 Wiki 映射中的机芯代码，不保留人员授权关系。"""
    return {
        _split_project_mapping_entry(value)[1].upper()
        for values in person_projects.values()
        for value in values
        if _split_project_mapping_entry(value)[1]
    }


def load_and_update_chip_history(person_projects, current_chips=None):
    """合并当前 Wiki 机芯到只增不减的历史库，并返回全部历史代码。"""
    history_path = Path(OUT_DIR) / CHIP_HISTORY_NAME
    historical = set()
    if not history_path.is_file():
        raise ValueError(f'机芯历史库不存在，拒绝在可能遗忘旧机芯的情况下继续: {history_path}')
    try:
        payload = json.loads(history_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'机芯历史库无法读取: {history_path}') from exc
    if payload.get('schema_version') != 1 or not isinstance(payload.get('chips'), list):
        raise ValueError(f'机芯历史库格式不合法: {history_path}')
    if any(not isinstance(code, str) or not code.strip() for code in payload['chips']):
        raise ValueError(f'机芯历史库包含无效机芯: {history_path}')
    historical.update(code.strip().upper() for code in payload['chips'])

    if current_chips is None:
        current = _chip_codes_from_person_projects(person_projects)
    else:
        if not isinstance(current_chips, (list, tuple, set)) or any(
            not isinstance(code, str) or not code.strip() for code in current_chips
        ):
            raise ValueError('当前 Wiki 机芯列表格式不合法')
        current = {code.strip().upper() for code in current_chips}
    merged = historical | current
    normalized_payload = {
        'schema_version': 1,
        'purpose': '只增不减地记住曾在 Wiki 项目负荷映射中出现过的机芯；不代表当前人员仍有申报权限。',
        'chips': sorted(merged),
    }
    if merged != historical:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{CHIP_HISTORY_NAME}.', suffix='.tmp', dir=history_path.parent
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
                json.dump(normalized_payload, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, history_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    return sorted(merged)


def build_chip_norm(codes):
    result = {}
    for code in codes:
        pure = _split_project_mapping_entry(code)[1].upper()
        if not pure:
            continue
        pref, num, suf = chip_normalize(pure)
        if pref:
            result[pure] = (pref, num, suf)
    return result


def _explicit_d_revision(suffix):
    """仅提取明确写出的 D+数字版本；D4 与 D5 必须严格区分。"""
    # 完整项目码中的 DD6/DA6 是方案后缀，不是 D4/D5 机芯版本。
    match = re.match(r'D([45])', suffix or '')
    return match.group(1) if match else None


def select_chip_candidates_for_matching(chip_candidates):
    """明确写出的 D4/D5 优先，避免前面的模糊整机码掩盖真实机芯版本。"""
    explicit = [
        candidate
        for candidate in chip_candidates
        if _explicit_d_revision(candidate[3])
    ]
    return explicit or list(chip_candidates)


def extract_chip_candidates(project_name):
    if not project_name or project_name == '-':
        return []
    candidates = []
    for m in re.finditer(r'\d([AMT]{1,2}\d{2,4}[A-Z0-9]+)', str(project_name)):
        cand = m.group(1).upper()
        if len(cand) >= 6:
            candidates.append(cand)
    # Parenthetical chips can follow Chinese text without a whitespace boundary,
    # for example “新开MT9603L机芯”.  Capture that authoritative suffix too.
    for m in re.finditer(r'([AMT]{1,2}\d{2,4}[A-Z0-9]*)(?=机芯)', str(project_name), re.I):
        cand = m.group(1).upper()
        if len(cand) >= 5:
            candidates.append(cand)
    for m in re.finditer(r'(?:^|[\s(（])([AMT]{1,2}\d{2,4}[A-Z]?[A-Z0-9]*)(?:$|[\s)）]|[^A-Z0-9]|机芯)', str(project_name)):
        cand = m.group(1).upper()
        if len(cand) >= 5:
            if not any(cand in c and cand != c for c in candidates):
                candidates.append(cand)
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            pref, num, suf = chip_normalize(c)
            if pref:
                result.append((c, pref, num, suf))
    return result


def match_chip(candidate_tuple, allowed_chips, wiki_chips_norm):
    cand_code, cand_pref, cand_num, cand_suf = candidate_tuple
    for chip in allowed_chips:
        chip_pure = _split_project_mapping_entry(chip)[1]
        if chip_pure not in wiki_chips_norm:
            continue
        wiki_pref, wiki_num, wiki_suf = wiki_chips_norm[chip_pure]
        same_t_am_family = (
            {cand_pref, wiki_pref} == {'AM', 'T'} and
            cand_num == wiki_num
        )
        if cand_pref == wiki_pref or same_t_am_family:
            cand_revision = _explicit_d_revision(cand_suf)
            wiki_revision = _explicit_d_revision(wiki_suf)
            # An explicit project revision must match exactly.  A project code
            # without D4/D5 may retain the existing broad matching behavior.
            if cand_revision and cand_revision != wiki_revision:
                continue
            if (cand_num == wiki_num or wiki_num.startswith(cand_num) or
                    wiki_num.endswith(cand_num)):
                return chip_pure
    return None


def check_project_ownership(person, customer, project_name, chip_candidates, person_projects,
                            wiki_chips_norm, chip_history=None, chip_history_norm=None):
    chip_codes = [c[0] for c in chip_candidates]

    if person in LEADERS_ANY:
        return True, '组长任意报', [], [], chip_codes
    if person in LEADERS_ONLY_G:
        if customer == 'G':
            return True, '田昭辉报G项目', [], [], chip_codes
        if chip_candidates:
            for cand in chip_candidates:
                if match_chip(cand, ['AM966D5'], wiki_chips_norm):
                    return True, '田昭辉报AM966D5（视同G项目）', [], [], chip_codes
        return False, f'田昭辉只报G项目，但客户为{customer}', [], [], chip_codes
    if person in LEADERS_NO_G and customer == 'G':
        return False, '何文亮不报G项目', [], [], chip_codes

    if person in LEADERS_NO_G:
        return True, '组长（不报G规则已检查）', [], [], chip_codes

    if not project_name or project_name == '-':
        return True, '无项目名称，跳过', [], [], chip_codes

    if person not in person_projects:
        return False, f'{person} 不在项目负荷映射中', [], [], chip_codes

    allowed = person_projects[person]
    allowed_chips = [_split_project_mapping_entry(c)[1] for c in allowed]

    matching_candidates = select_chip_candidates_for_matching(chip_candidates)
    matched = []
    for cand in matching_candidates:
        m = match_chip(cand, allowed, wiki_chips_norm)
        if m:
            matched.append(m)

    if matched:
        return True, f'匹配到机芯: {", ".join(matched)}', matched, allowed_chips, chip_codes

    if not chip_candidates:
        allowed_customers = [
            _split_project_mapping_entry(c)[0]
            for c in allowed
            if _split_project_mapping_entry(c)[0]
        ]
        if customer in allowed_customers:
            return True, f'客户 {customer} 匹配', [], allowed_chips, chip_codes

    historical_matches = []
    if chip_history and chip_history_norm:
        for cand in matching_candidates:
            historical = match_chip(cand, chip_history, chip_history_norm)
            if historical and historical not in historical_matches:
                historical_matches.append(historical)
    if historical_matches:
        return False, (
            f'识别到历史机芯: {", ".join(historical_matches)}；'
            f'但不在当前允许范围（允许: {", ".join(allowed)}）'
        ), [], allowed_chips, chip_codes

    return False, f'项目归属待确认（允许: {", ".join(allowed)}）', [], allowed_chips, chip_codes


def run_audit(year, month, fetch_result):
    """对指定月份数据执行审核"""
    ml = month_label(year, month)
    md = month_display(year, month)

    print(f"\n{'=' * 60}")
    print(f"审核 {md} 数据")
    print(f"{'=' * 60}")

    mapping_data = load_project_mapping()
    if not mapping_data:
        print("  ⚠️ 无法加载项目映射，跳过审核")
        return None

    person_projects = mapping_data['person_projects']
    all_people = mapping_data['all_people']
    current_wiki_chips = mapping_data.get('all_chips')
    if current_wiki_chips is None:
        current_wiki_chips = sorted(_chip_codes_from_person_projects(person_projects))

    # 历史机芯只用于识别和解释，不参与当前人员授权判定。
    chip_history = load_and_update_chip_history(person_projects, current_wiki_chips)
    chip_history_norm = build_chip_norm(chip_history)

    # 构建 Wiki 机芯规范化映射
    wiki_chips_norm = build_chip_norm(current_wiki_chips)

    daily_data = fetch_result['daily_data']
    all_users = fetch_result['all_users']
    TEAM_MEMBERS = sorted([u['cn_name'] for u in all_users], key=lambda x: (len(x), x))
    print(f"  团队: {', '.join(TEAM_MEMBERS)}")

    all_dates = sorted(daily_data.keys())
    analysis = {}

    for date_str in all_dates:
        day_data = daily_data[date_str]
        analysis[date_str] = {}

        person_records = {}
        for rec in day_data['list']:
            name = rec['cn_name']
            if name not in person_records:
                person_records[name] = rec
            else:
                existing = person_records[name]
                existing_children = existing.get('children', [])
                new_children = rec.get('children', [])
                seen_keys = set()
                for ch in existing_children:
                    key = (ch.get('items', ''), ch.get('project_name', ''), ch.get('title', ''), ch.get('content', ''))
                    seen_keys.add(key)
                for ch in new_children:
                    key = (ch.get('items', ''), ch.get('project_name', ''), ch.get('title', ''), ch.get('content', ''))
                    if key not in seen_keys:
                        existing_children.append(ch)
                        seen_keys.add(key)
                existing['children'] = existing_children

        for name, rec in person_records.items():
            reported, checked, has_record = parse_hours(rec.get('total_hours'))
            analysis[date_str][name] = {
                'reported': reported,
                'checked': checked,
                'has_record': has_record,
                'attendance': rec.get('attendance'),
                'status': rec.get('status'),
                'leave_days': rec.get('leave_days', ''),
                'children': rec.get('children', [])
            }

        detail = day_data.get('detail', {})
        uncommitted = detail.get('uncommitted', '') if isinstance(detail, dict) else ''
        if uncommitted in ['无', 'None', None]:
            uncommitted = ''
        analysis[date_str]['__uncommitted__'] = [n.strip() for n in uncommitted.split(',') if n.strip()]

    # 漏报截止到拉取日期（不含当天）——只有拉取日期之前的工作日才判漏报
    fetch_time_str = fetch_result.get('raw_file', '')
    # 从 raw_data 的 fetch_time 字段获取拉取时间
    raw_data_obj = None
    raw_file_path = os.path.join(fetch_result['month_dir'], 'raw_data.json')
    if os.path.exists(raw_file_path):
        with open(raw_file_path, 'r', encoding='utf-8') as f:
            raw_data_obj = json.load(f)
    fetch_datetime = None
    if raw_data_obj and raw_data_obj.get('fetch_time'):
        try:
            fetch_datetime = datetime.fromisoformat(raw_data_obj['fetch_time'])
        except:
            pass
    # 如果无法获取拉取时间，用当前日期（保守）
    if not fetch_datetime:
        fetch_datetime = datetime.now()
    # 漏报截止日期：拉取日期前一天（不含当天）
    missed_cutoff = (fetch_datetime - timedelta(days=1)).strftime('%Y-%m-%d')
    print(f"  漏报截止日期：{missed_cutoff}（拉取时间 {fetch_datetime.strftime('%Y-%m-%d')}，不含当天）")

    # Generate findings
    findings = {
        'missed': defaultdict(list),
        'hours_over': [],
        'hours_low': [],
        'no_checkin_leave': [],       # 请假/出差 - 标记建议直接审批
        'project_mismatch': [],
        'platform_summary': defaultdict(list),
        'leader_issues': [],
        'daily_summary': []
    }

    for date_str in all_dates:
        day_info = analysis[date_str]
        uncommitted = day_info.get('__uncommitted__', [])
        # 漏报只统计截止日期之前的日期
        if date_str <= missed_cutoff:
            for person in uncommitted:
                if person in TEAM_MEMBERS:
                    findings['missed'][person].append(date_str)

        summary = {
            'date': date_str,
            'uncommitted': uncommitted,
            'over': [], 'low': [],
            'project_issues': [], 'platform_count': 0
        }

        for person in day_info:
            if person.startswith('__'):
                continue
            rec = day_info[person]
            reported = rec['reported']
            checked = rec['checked']
            has_record = rec['has_record']
            children = rec.get('children', [])

            leave_travel_hours = get_leave_travel_hours(children)

            # 无打卡记录的情况
            if not has_record and reported > 0:
                has_leave = any(is_leave_or_travel(ch) for ch in children)
                if has_leave:
                    # 请假/出差：标记为"建议直接审批"
                    findings['no_checkin_leave'].append({
                        'date': date_str, 'person': person,
                        'reported': reported, 'leave_hours': leave_travel_hours,
                        'suggest_auto_approve': True,  # 建议直接审批
                        'children': [{
                            'items': ch.get('items', ''),
                            'title': ch.get('title', ''),
                            'content': ch.get('content', ''),
                            'work_hours': ch.get('work_hours', ''),
                            'type': ch.get('type', ''),
                            'project_name': ch.get('project_name', ''),
                        } for ch in children if is_leave_or_travel(ch)]
                    })
                else:
                    # 无打卡且无合理理由 → 合并到工时异常（申报远大于打卡）
                    # 打卡=0，有效申报=申报-休假(此处休假=0)=申报，比例=无穷大
                    effective_reported = reported - leave_travel_hours
                    if effective_reported < 0:
                        effective_reported = 0
                    findings['hours_over'].append({
                        'date': date_str, 'person': person,
                        'reported': reported, 'checked': 0,
                        'leave_hours': leave_travel_hours,
                        'effective': effective_reported,
                        'ratio': float('inf'),
                        'no_checkin': True,  # 标记：无打卡
                        'detail': '; '.join([
                            f"{ch.get('items','')}:{ch.get('title','')}:{ch.get('content','')[:30]}"
                            for ch in children[:5]
                        ]),
                    })
                    summary['over'].append(f"{person}({effective_reported:.2f}/0,无打卡)")

            if has_record and checked > 0:
                effective_reported = reported - leave_travel_hours
                if effective_reported < 0:
                    effective_reported = 0
                ratio = effective_reported / checked if checked > 0 else 0

                if effective_reported > checked and ratio > 1.0:
                    findings['hours_over'].append({
                        'date': date_str, 'person': person,
                        'reported': reported, 'checked': checked,
                        'leave_hours': leave_travel_hours,
                        'effective': effective_reported,
                        'ratio': ratio
                    })
                    summary['over'].append(f"{person}({effective_reported:.2f}/{checked})")
                elif ratio < 0.7 and effective_reported > 0:
                    findings['hours_low'].append({
                        'date': date_str, 'person': person,
                        'reported': reported, 'checked': checked,
                        'leave_hours': leave_travel_hours,
                        'effective': effective_reported,
                        'ratio': ratio
                    })
                    summary['low'].append(f"{person}({effective_reported:.2f}/{checked})")

            seen_issues = set()
            for child in children:
                if is_leave_or_travel(child):
                    continue

                customer = child.get('customer', '') or child.get('customer_name', '')
                customer_norm = normalize_customer(customer)
                project_name = child.get('project_name', '') or ''
                items = child.get('items', '') or child.get('items_name', '') or ''
                work_hours = child.get('work_hours', '')
                title = child.get('title', '') or ''
                content = child.get('content', '') or ''
                ctype = child.get('type', '') or ''
                chip_candidates = extract_chip_candidates(project_name)

                if is_platform_item(child):
                    findings['platform_summary'][person].append({
                        'date': date_str, 'items': items,
                        'title': title, 'content': content,
                        'work_hours': work_hours, 'type': ctype,
                        'project_name': project_name, 'customer': customer,
                    })
                    summary['platform_count'] += 1
                    continue

                ok, reason, matched, allowed, chip_codes = check_project_ownership(
                    person, customer_norm, project_name, chip_candidates,
                    person_projects, wiki_chips_norm, chip_history, chip_history_norm)

                issue_key = (date_str, person, items, project_name, title)
                if issue_key in seen_issues:
                    continue
                seen_issues.add(issue_key)

                issue = {
                    'date': date_str, 'person': person,
                    'customer': customer, 'customer_norm': customer_norm,
                    'items': items, 'project_name': project_name,
                    'title': title, 'content': content,
                    'work_hours': work_hours, 'type': ctype,
                    'chip_candidates': chip_codes, 'matched_chip': matched,
                    'allowed_chips': allowed, 'reason': reason, 'ok': ok
                }

                if not ok:
                    if person in LEADERS_ONLY_G and customer_norm != 'G':
                        findings['leader_issues'].append(issue)
                        summary['project_issues'].append(f"{person}: {items} {reason}")
                    elif person in LEADERS_NO_G and customer_norm == 'G':
                        findings['leader_issues'].append(issue)
                        summary['project_issues'].append(f"{person}: {items} {reason}")
                    else:
                        findings['project_mismatch'].append(issue)
                        summary['project_issues'].append(f"{person}: {items} {reason}")

        findings['daily_summary'].append(summary)

    # 保存审核报告
    month_dir = fetch_result['month_dir']

    # JSON
    json_output = {
        'month': ml,
        'month_display': md,
        'fetch_time': datetime.now().isoformat(),
        'missed_cutoff': missed_cutoff,
        'team_members': TEAM_MEMBERS,
        'missed': {k: v for k, v in findings['missed'].items()},
        'hours_over': findings['hours_over'],
        'hours_low': findings['hours_low'],
        'no_checkin_leave': findings['no_checkin_leave'],
        'project_mismatch': findings['project_mismatch'],
        'leader_issues': findings['leader_issues'],
        'platform_summary': {k: v for k, v in findings['platform_summary'].items()},
        'daily_summary': findings['daily_summary'],
    }
    json_file = os.path.join(month_dir, "audit_report.json")
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    print(f"  JSON 已保存: {json_file}")

    # 兼容副本
    compat_json = os.path.join(OUT_DIR, f"审核报告_{md}.json")
    shutil.copy2(json_file, compat_json)

    # Markdown
    report_lines = []
    report_lines.append(f"# SRDPM 工时审核报告（{md}）v2")
    report_lines.append("")
    report_lines.append(f"- 审核时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report_lines.append(f"- 数据范围：{fetch_result['start_date'].strftime('%Y-%m-%d')} ~ {fetch_result['end_date'].strftime('%Y-%m-%d')}（共 {fetch_result['total_days']} 天）")
    report_lines.append(f"- 团队人数：{len(TEAM_MEMBERS)} 人")
    report_lines.append("")

    # 一、漏报
    report_lines.append("## 一、漏报人员")
    report_lines.append("")
    report_lines.append(f"（截止日期：{missed_cutoff}，不含数据拉取当天）")
    report_lines.append("")
    if findings['missed']:
        report_lines.append("| 人员 | 漏报天数 | 漏报日期 |")
        report_lines.append("|------|---------|---------|")
        for person in sorted(findings['missed'].keys()):
            dates = findings['missed'][person]
            report_lines.append(f"| {person} | {len(dates)} | {', '.join(dates)} |")
    else:
        report_lines.append("✅ 无漏报人员。")
    report_lines.append("")

    # 二、请假/出差（建议直接审批）
    report_lines.append("## 二、请假/出差/休假（⭐ 建议直接审批）")
    report_lines.append("")
    ncl = findings['no_checkin_leave']
    if ncl:
        leave_by_person = defaultdict(list)
        for item in ncl:
            leave_by_person[item['person']].append(item)
        for person in sorted(leave_by_person.keys()):
            entries = leave_by_person[person]
            total_days = len(set(e['date'] for e in entries))
            total_hours = sum(e['leave_hours'] for e in entries)
            report_lines.append(f"### {person}（{total_days}天，共 {total_hours}h）⭐ 建议直接审批")
            report_lines.append("")
            report_lines.append("| 日期 | 项目 | 工作内容 | 工时 | 类型 |")
            report_lines.append("|------|------|---------|------|------|")
            for entry in sorted(entries, key=lambda x: x['date']):
                for ch in entry['children']:
                    content_text = f"{ch['title']}：{ch['content']}" if ch['title'] and ch['content'] else (ch['title'] or ch['content'])
                    report_lines.append(f"| {entry['date']} | {ch['items']} | {content_text[:60]} | {ch['work_hours']} | {ch['type']} |")
            report_lines.append("")
    else:
        report_lines.append("✅ 无。")
    report_lines.append("")

    # 三、工时异常（含无打卡合并的申报远大于打卡）
    report_lines.append("## 三、工时异常（已扣除休假/出差小时）")
    report_lines.append("")
    ho = findings['hours_over']
    if ho:
        report_lines.append("### 3.1 申报超过打卡（含无打卡情况）")
        report_lines.append("")
        report_lines.append("| 日期 | 人员 | 申报 | 打卡 | 休假/出差 | 有效申报 | 比例 | 说明 |")
        report_lines.append("|------|------|------|------|----------|---------|------|------|")
        for item in sorted(ho, key=lambda x: (x['date'], x['person'])):
            leave_str = f"-{item['leave_hours']}h" if item['leave_hours'] > 0 else "-"
            ratio_str = "∞（无打卡）" if item.get('no_checkin') else f"{item['ratio']*100:.0f}%"
            note = "⚠️ 无打卡无合理理由" if item.get('no_checkin') else ""
            report_lines.append(f"| {item['date']} | {item['person']} | {item['reported']} | {item['checked']} | {leave_str} | {item['effective']:.2f} | {ratio_str} | {note} |")
    else:
        report_lines.append("### 3.1 申报超过打卡")
        report_lines.append("")
        report_lines.append("✅ 无。")
    report_lines.append("")

    hl = findings['hours_low']
    if hl:
        report_lines.append("### 3.2 申报远低于打卡（<70%）")
        report_lines.append("")
        report_lines.append("| 日期 | 人员 | 申报 | 打卡 | 休假/出差 | 有效申报 | 比例 |")
        report_lines.append("|------|------|------|------|----------|---------|------|")
        for item in sorted(hl, key=lambda x: (x['date'], x['person'])):
            leave_str = f"-{item['leave_hours']}h" if item['leave_hours'] > 0 else "-"
            ratio_pct = f"{item['ratio']*100:.0f}%"
            report_lines.append(f"| {item['date']} | {item['person']} | {item['reported']} | {item['checked']} | {leave_str} | {item['effective']:.2f} | {ratio_pct} |")
    else:
        report_lines.append("### 3.2 申报远低于打卡（<70%）")
        report_lines.append("")
        report_lines.append("✅ 无。")
    report_lines.append("")

    # 四、项目归属
    report_lines.append("## 四、项目归属异常（不含平台/公共类）")
    report_lines.append("")
    if findings['leader_issues']:
        report_lines.append("### 4.1 组长规则异常")
        report_lines.append("")
        report_lines.append("| 日期 | 人员 | 项目代码 | 客户 | 标题 | 内容 | 工时 | 问题 |")
        report_lines.append("|------|------|---------|------|------|------|------|------|")
        for issue in sorted(findings['leader_issues'], key=lambda x: (x['date'], x['person'])):
            report_lines.append(f"| {issue['date']} | {issue['person']} | {issue['items']} | {issue['customer']}({issue['customer_norm']}) | {issue['title'][:20]} | {issue['content'][:30]} | {issue['work_hours']} | {issue['reason']} |")
        report_lines.append("")
    else:
        report_lines.append("✅ 无组长规则异常。")
        report_lines.append("")

    if findings['project_mismatch']:
        report_lines.append("### 4.2 其他人员项目归属异常")
        report_lines.append("")
        report_lines.append("| 日期 | 人员 | 项目代码 | 客户 | 标题 | 内容 | 工时 | 允许机芯 | 问题 |")
        report_lines.append("|------|------|---------|------|------|------|------|---------|------|")
        for issue in sorted(findings['project_mismatch'], key=lambda x: (x['date'], x['person'])):
            report_lines.append(f"| {issue['date']} | {issue['person']} | {issue['items']} | {issue['customer']}({issue['customer_norm']}) | {issue['title'][:15]} | {issue['content'][:25]} | {issue['work_hours']} | {', '.join(issue['allowed_chips'][:4])} | {issue['reason']} |")
        report_lines.append("")
    else:
        report_lines.append("✅ 无其他人员项目归属异常。")
    report_lines.append("")

    # 五、平台
    report_lines.append("## 五、公共事务/平台类项目（需人工重点关注）")
    report_lines.append("")
    ps = findings['platform_summary']
    if ps:
        for person in sorted(ps.keys()):
            entries = ps[person]
            dates_set = sorted(set(e['date'] for e in entries))
            total_h = sum(parse_num(e['work_hours']) for e in entries)
            report_lines.append(f"### {person}（{len(dates_set)}天 / {len(entries)}条 / 共 {total_h:.1f}h）")
            report_lines.append("")
            report_lines.append(f"涉及日期：{', '.join(dates_set)}")
            report_lines.append("")
            report_lines.append("| 日期 | 项目 | 标题 | 工作内容 | 工时 |")
            report_lines.append("|------|------|------|---------|------|")
            for entry in sorted(entries, key=lambda x: x['date']):
                report_lines.append(f"| {entry['date']} | {entry['items']} | {entry['title'][:20]} | {entry['content'][:50]} | {entry['work_hours']} |")
            report_lines.append("")
    else:
        report_lines.append("无公共事务/平台类项目。")
    report_lines.append("")

    # 六、每日汇总
    report_lines.append("## 六、每日汇总")
    report_lines.append("")
    report_lines.append("| 日期 | 未报 | 超打卡 | 低申报 | 平台 | 项目问题 |")
    report_lines.append("|------|------|--------|--------|------|---------|")
    for s in findings['daily_summary']:
        # 只在汇总表中展示截止日期范围内的日期（未来日期不展示未报信息）
        date_str = s['date']
        if date_str > missed_cutoff:
            # 未来日期：标注为"数据拉取后"
            report_lines.append(f"| {s['date']} | —（拉取后） | {', '.join(s['over']) if s['over'] else '-'} | {', '.join(s['low']) if s['low'] else '-'} | {s['platform_count'] or '-'} | {', '.join([i.split(':')[0] for i in s['project_issues']]) if s['project_issues'] else '-'} |")
        else:
            uncom = ', '.join(s['uncommitted']) if s['uncommitted'] else '-'
            over = ', '.join(s['over']) if s['over'] else '-'
            low = ', '.join(s['low']) if s['low'] else '-'
            plat = str(s['platform_count']) if s['platform_count'] > 0 else '-'
            issues = ', '.join([i.split(':')[0] for i in s['project_issues']]) if s['project_issues'] else '-'
            report_lines.append(f"| {s['date']} | {uncom} | {over} | {low} | {plat} | {issues} |")
    report_lines.append("")

    # 七、规则说明
    report_lines.append("## 七、规则说明")
    report_lines.append("")
    report_lines.append('1. **漏报截止**：漏报只统计到数据拉取日期的前一天（不含拉取当天），未来日期不算漏报')
    report_lines.append('2. **出差=休假**：title/content含【出差】的条目视同请假，无打卡不判异常')
    report_lines.append("3. **工时计算**：申报超标比例 = (申报总时数 - 休假/出差时数) / 打卡时数；**无打卡=打卡0h，合并到超打卡**")
    report_lines.append("4. **平台识别**：type=纯平台类 或 items以YF-CP/YF-SW开头的视为平台/公共事务，不判项目归属异常")
    report_lines.append("5. **组长规则**：梁郁沛任意报；何文亮不报G；田昭辉只报G（可报AM966D5）；**平台类不限**")
    report_lines.append("6. **机芯匹配**：SRDPM项目名全称匹配Wiki简称（MT603↔MT9603等）")
    report_lines.append("7. **请假/出差**：甄别出后建议直接审批通过")
    report_lines.append("")

    md_file = os.path.join(month_dir, "audit_report.md")
    with open(md_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    print(f"  MD 已保存: {md_file}")

    compat_md = os.path.join(OUT_DIR, f"审核报告_{md}.md")
    shutil.copy2(md_file, compat_md)

    # Print summary
    print(f"\n审核概要 ({md}):")
    print(f"  漏报: {len(findings['missed'])} 人（截止 {missed_cutoff}）")
    print(f"  请假/出差（建议直接审批）: {len(findings['no_checkin_leave'])} 条")
    no_checkin_over = sum(1 for h in findings['hours_over'] if h.get('no_checkin'))
    has_checkin_over = len(findings['hours_over']) - no_checkin_over
    print(f"  工时异常-超打卡: {has_checkin_over} 条（含无打卡 {no_checkin_over} 条）")
    print(f"  工时异常-低申报: {len(findings['hours_low'])} 条")
    print(f"  项目归属异常: {len(findings['project_mismatch'])} 条")
    print(f"  组长违规: {len(findings['leader_issues'])} 条")
    print(f"  平台事务: {len(findings['platform_summary'])} 人")

    return json_output


def main():
    parser = argparse.ArgumentParser(description="SRDPM 工时数据拉取 + 审核")
    parser.add_argument("months", nargs="+", type=int, help="月份列表，如 6 7 表示6月和7月")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="年份，默认当前年份")
    parser.add_argument("--fetch-only", action="store_true", help="只拉取数据不审核")
    args = parser.parse_args()

    invalid_months = [month for month in args.months if month < 1 or month > 12]
    if invalid_months:
        parser.error(f"月份必须在 1-12 之间：{invalid_months}")

    # 登录
    session = login_srdpm()

    all_results = {}
    for month in args.months:
        result = fetch_month(session, args.year, month)
        if result:
            if not args.fetch_only:
                audit_json = run_audit(args.year, month, result)
                result['audit'] = audit_json
            all_results[month_label(args.year, month)] = result

    print(f"\n{'=' * 60}")
    print("全部完成！")
    print(f"{'=' * 60}")
    for ml, result in all_results.items():
        print(f"  {ml}: 数据已存档到 {result['month_dir']}")
        if result.get('audit'):
            print(f"    审核报告已生成")
    print(f"\n数据存档目录: {ARCHIVE_DIR}")
    print("下一步：运行 build_multi_month_dashboard.py 生成看板")


if __name__ == "__main__":
    main()
