#!/usr/bin/env python3
"""Read the latest project-load attachment from the trusted Wiki page.

The module deliberately performs GET requests only.  The workbook is parsed in
memory and converted into the small, deterministic ``project_mapping.json``
used by the SRDPM audit.  Nothing from the attachment is extracted to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

import requests


WIKI_BASE_URL = "https://idisplayvision.com/wiki"
WIKI_PAGE_ID = "22824730"
WIKI_PAT_ENV = "IDISPLAYVISION_WIKI_PAT"
ATTACHMENT_API_URL = (
    f"{WIKI_BASE_URL}/rest/api/content/{WIKI_PAGE_ID}/child/attachment"
    "?limit=200&expand=version,extensions"
)
ATTACHMENT_NAME_PATTERN = re.compile(
    r"^团队成员项目负荷_新拆分-(\d{8})\.xlsx$"
)
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 1000
MAX_XML_BYTES = 20 * 1024 * 1024
MAX_SHEET_ROWS = 2000
MAX_SHEET_COLUMNS = 500
REQUEST_TIMEOUT = (10, 60)
RELEVANT_COLUMNS = {
    "customer": 1,
    "project": 2,
    "chip": 4,
    "spm": 9,
    "bsp": 13,
    "diag": 15,
}
EXPECTED_HEADERS = {
    1: "客户",
    2: "项目群",
    4: "机芯",
    9: "SPM",
    13: "BSP",
    15: "DIAG",
}
PERSON_PATTERN = re.compile(r"^[\u3400-\u9fff]{2,4}$")
CHIP_PATTERN = re.compile(
    r"^(?:AM|MT|T)\s*\d{2,4}[A-Z]?[A-Z0-9]*(?:/[A-Z0-9]+)?$",
    re.IGNORECASE,
)


class WikiMappingError(RuntimeError):
    """Base class for safe, fail-closed Wiki mapping failures."""


class WikiMappingCredentialError(WikiMappingError):
    """The configured Wiki PAT is unavailable."""


class WikiMappingDownloadError(WikiMappingError):
    """The attachment metadata or bytes could not be read safely."""


class WikiMappingValidationError(WikiMappingError):
    """The downloaded workbook does not satisfy the mapping contract."""


@dataclass(frozen=True)
class AttachmentMetadata:
    id: str
    filename: str
    updated_at: str
    expected_size: int
    download_path: str

    @property
    def attachment_date(self) -> datetime:
        match = ATTACHMENT_NAME_PATTERN.fullmatch(self.filename)
        if match is None:
            raise WikiMappingValidationError("Wiki 附件文件名不符合约定")
        try:
            return datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError as exc:
            raise WikiMappingValidationError("Wiki 附件日期无效") from exc


@dataclass(frozen=True)
class MappingRefreshResult:
    updated: bool
    attachment_id: str
    attachment_filename: str
    source_sha256: str


def _safe_response_content(response: Any, *, maximum: int) -> bytes:
    content = getattr(response, "content", None)
    if not isinstance(content, (bytes, bytearray)):
        raise WikiMappingDownloadError("Wiki 响应内容无效")
    content = bytes(content)
    if not content or len(content) > maximum:
        raise WikiMappingDownloadError("Wiki 响应大小异常")
    return content


def _request(
    session: Any,
    url: str,
    *,
    token: str,
    maximum: int,
) -> tuple[Any, bytes]:
    try:
        response = session.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except Exception as exc:
        raise WikiMappingDownloadError("Wiki 只读请求失败") from exc
    if getattr(response, "status_code", None) != 200:
        raise WikiMappingDownloadError("Wiki 只读请求未成功")
    return response, _safe_response_content(response, maximum=maximum)


def _parse_attachment_results(content: bytes) -> list[AttachmentMetadata]:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WikiMappingDownloadError("Wiki 附件清单不是有效 JSON") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise WikiMappingDownloadError("Wiki 附件清单结构异常")

    attachments: list[AttachmentMetadata] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        filename = unquote(str(item.get("title", ""))).strip()
        if ATTACHMENT_NAME_PATTERN.fullmatch(filename) is None:
            continue
        attachment_id = str(item.get("id", "")).strip()
        version = item.get("version")
        extensions = item.get("extensions")
        links = item.get("_links")
        updated_at = version.get("when") if isinstance(version, dict) else None
        expected_size = extensions.get("fileSize") if isinstance(extensions, dict) else None
        download_path = links.get("download") if isinstance(links, dict) else None
        if (
            not attachment_id.isdigit()
            or not isinstance(updated_at, str)
            or not updated_at.strip()
            or not isinstance(expected_size, int)
            or expected_size < 1024
            or expected_size > MAX_DOWNLOAD_BYTES
            or not isinstance(download_path, str)
            or not download_path.startswith("/download/attachments/")
        ):
            raise WikiMappingDownloadError("Wiki 项目负荷附件元数据异常")
        metadata = AttachmentMetadata(
            id=attachment_id,
            filename=filename,
            updated_at=updated_at.strip(),
            expected_size=expected_size,
            download_path=download_path,
        )
        # Validate the date before it participates in sorting.
        metadata.attachment_date
        attachments.append(metadata)

    if not attachments:
        raise WikiMappingDownloadError("Wiki 页面没有符合约定的项目负荷附件")
    return attachments


def _select_latest_attachment(attachments: Iterable[AttachmentMetadata]) -> AttachmentMetadata:
    return max(
        attachments,
        key=lambda item: (
            item.attachment_date,
            item.updated_at,
            int(item.id),
        ),
    )


def _attachment_download_url(attachment: AttachmentMetadata) -> str:
    # Confluence returns /download/... even though this installation lives below
    # /wiki.  urljoin() would drop /wiki and return the site's HTML home page.
    url = f"{WIKI_BASE_URL}/{attachment.download_path.lstrip('/')}"
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "idisplayvision.com"
        or not parsed.path.startswith("/wiki/download/attachments/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WikiMappingDownloadError("Wiki 附件下载地址不在允许范围")
    return url


def _read_zip_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise WikiMappingValidationError("Wiki 附件缺少必要的 XLSX 结构") from exc
    if info.file_size < 0 or info.file_size > MAX_XML_BYTES:
        raise WikiMappingValidationError("Wiki 附件 XML 大小异常")
    content = archive.read(info)
    if len(content) != info.file_size:
        raise WikiMappingValidationError("Wiki 附件 XML 读取不完整")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise WikiMappingValidationError("Wiki 附件包含不允许的 XML 声明")
    return content


def _parse_xml(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise WikiMappingValidationError("Wiki 附件 XML 无法解析") from exc


def _normalize_workbook_target(target: str) -> str:
    if not isinstance(target, str) or not target:
        raise WikiMappingValidationError("Wiki 工作表关系无效")
    if target.startswith("/"):
        normalized = str(PurePosixPath(target.lstrip("/")))
    else:
        normalized = str(PurePosixPath("xl") / PurePosixPath(target))
    if normalized.startswith("../") or "/../" in normalized or not normalized.startswith("xl/"):
        raise WikiMappingValidationError("Wiki 工作表路径越界")
    return normalized


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    content = _read_zip_entry(archive, "xl/sharedStrings.xml")
    root = _parse_xml(content)
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{MAIN_NS}}}t"))
        for item in root.findall(f"{{{MAIN_NS}}}si")
    ]


def _column_index(letters: str) -> int:
    result = 0
    for character in letters:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _cell_coordinate(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9]\d*)", reference or "")
    if match is None:
        raise WikiMappingValidationError("Wiki 工作表单元格坐标无效")
    column = _column_index(match.group(1))
    row = int(match.group(2))
    if row > MAX_SHEET_ROWS or column > MAX_SHEET_COLUMNS:
        raise WikiMappingValidationError("Wiki 工作表有效范围异常")
    return row, column


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            node.text or "" for node in cell.findall(f".//{{{MAIN_NS}}}t")
        )
    value_node = cell.find(f"{{{MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text
    if cell_type == "s":
        try:
            index = int(value)
            return shared[index]
        except (ValueError, IndexError) as exc:
            raise WikiMappingValidationError("Wiki 工作表共享文本索引无效") from exc
    return value


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _parse_sheet_cells(
    archive: zipfile.ZipFile, sheet_path: str, shared: list[str]
) -> tuple[dict[tuple[int, int], str], set[tuple[int, int]]]:
    root = _parse_xml(_read_zip_entry(archive, sheet_path))
    cells: dict[tuple[int, int], str] = {}
    for cell in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row/{{{MAIN_NS}}}c"):
        coordinate = _cell_coordinate(cell.get("r", ""))
        text = _clean_text(_cell_text(cell, shared))
        if text:
            cells[coordinate] = text
    original_coordinates = set(cells)

    for merge in root.findall(f".//{{{MAIN_NS}}}mergeCells/{{{MAIN_NS}}}mergeCell"):
        reference = merge.get("ref", "")
        parts = reference.split(":")
        if len(parts) == 1:
            start = end = _cell_coordinate(parts[0])
        elif len(parts) == 2:
            start, end = _cell_coordinate(parts[0]), _cell_coordinate(parts[1])
        else:
            raise WikiMappingValidationError("Wiki 工作表合并范围无效")
        start_row, start_col = start
        end_row, end_col = end
        if end_row < start_row or end_col < start_col:
            raise WikiMappingValidationError("Wiki 工作表合并范围无效")
        if (end_row - start_row + 1) * (end_col - start_col + 1) > 10000:
            raise WikiMappingValidationError("Wiki 工作表合并范围过大")
        source = cells.get(start, "")
        if not source:
            continue
        for row in range(start_row, end_row + 1):
            for column in range(start_col, end_col + 1):
                existing = cells.get((row, column), "")
                if existing and existing != source:
                    raise WikiMappingValidationError("Wiki 合并单元格包含冲突内容")
                cells[(row, column)] = source
    return cells, original_coordinates


def _find_sheet_path(
    archive: zipfile.ZipFile, expected_sheet_name: str
) -> str:
    workbook = _parse_xml(_read_zip_entry(archive, "xl/workbook.xml"))
    relationships = _parse_xml(
        _read_zip_entry(archive, "xl/_rels/workbook.xml.rels")
    )
    relationship_targets = {
        node.get("Id", ""): node.get("Target", "")
        for node in relationships
        if node.tag.endswith("Relationship")
    }
    matches = []
    for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet"):
        if sheet.get("name") != expected_sheet_name:
            continue
        relationship_id = sheet.get(f"{{{REL_NS}}}id", "")
        target = relationship_targets.get(relationship_id)
        if target is None:
            raise WikiMappingValidationError("Wiki 工作表关系缺失")
        matches.append(_normalize_workbook_target(target))
    if len(matches) != 1:
        raise WikiMappingValidationError("Wiki 附件没有唯一的当月项目负荷工作表")
    return matches[0]


def _split_people(value: str, *, row: int, role: str) -> list[str]:
    value = _clean_text(value)
    if not value:
        return []
    people = []
    for part in re.split(r"[\r\n、,，;；/]+", value):
        person = part.strip()
        if not person:
            continue
        if PERSON_PATTERN.fullmatch(person) is None:
            raise WikiMappingValidationError(
                f"Wiki 第 {row} 行 {role} 负责人格式无法安全识别"
            )
        if person not in people:
            people.append(person)
    return people


def _split_chips(value: str) -> list[str]:
    chips = []
    for part in _clean_text(value).split("\n"):
        chip = re.sub(r"\s+", "", part).upper()
        if chip and chip not in chips:
            chips.append(chip)
    return chips


def _is_recognizable_chip(chip: str) -> bool:
    return CHIP_PATTERN.fullmatch(chip) is not None


def _validate_archive(content: bytes, expected_size: int) -> zipfile.ZipFile:
    if len(content) != expected_size:
        raise WikiMappingValidationError("Wiki 附件大小与清单不一致")
    if len(content) < 1024 or len(content) > MAX_DOWNLOAD_BYTES or not content.startswith(b"PK"):
        raise WikiMappingValidationError("Wiki 附件不是有效的 XLSX 文件")
    stream = io.BytesIO(content)
    try:
        archive = zipfile.ZipFile(stream)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise WikiMappingValidationError("Wiki 附件不是有效的 XLSX 文件") from exc
    if len(infos) > MAX_ZIP_ENTRIES:
        archive.close()
        raise WikiMappingValidationError("Wiki 附件条目数量异常")
    if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise WikiMappingValidationError("Wiki 附件解压大小异常")
    entry_names = [info.filename for info in infos]
    if len(entry_names) != len(set(entry_names)) or any(
        name.startswith(("/", "\\"))
        or "\\" in name
        or ".." in PurePosixPath(name).parts
        for name in entry_names
    ):
        archive.close()
        raise WikiMappingValidationError("Wiki 附件内部路径异常")
    names = set(entry_names)
    required = {
        "[Content_Types].xml",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    }
    lowered_names = {name.lower() for name in names}
    forbidden_content = any(
        name == "xl/vbaproject.bin"
        or name.startswith("xl/activex/")
        or name.startswith("xl/embeddings/")
        for name in lowered_names
    )
    if not required.issubset(names) or forbidden_content:
        archive.close()
        raise WikiMappingValidationError("Wiki 附件缺少必要结构或包含不允许的内容")
    return archive


def parse_project_mapping_xlsx(
    content: bytes, attachment: AttachmentMetadata
) -> dict[str, Any]:
    """Convert one validated attachment into the current authorization mapping."""

    digest = hashlib.sha256(content).hexdigest()
    attachment_date = attachment.attachment_date
    expected_sheet_name = (
        f"计算负荷用-{attachment_date.year:04d}-{attachment_date.month}月"
    )
    archive = _validate_archive(content, attachment.expected_size)
    try:
        sheet_path = _find_sheet_path(archive, expected_sheet_name)
        shared = _shared_strings(archive)
        cells, original_coordinates = _parse_sheet_cells(archive, sheet_path, shared)
    finally:
        archive.close()

    header_rows = [
        row
        for row in range(1, 31)
        if all(_clean_text(cells.get((row, column))) == label for column, label in EXPECTED_HEADERS.items())
    ]
    if len(header_rows) != 1:
        raise WikiMappingValidationError("Wiki 当月工作表表头结构异常")
    header_row = header_rows[0]

    sentinel_rows = [
        row
        for row in range(header_row + 1, MAX_SHEET_ROWS + 1)
        if _clean_text(cells.get((row, 1))).startswith("负荷说明")
    ]
    if not sentinel_rows:
        raise WikiMappingValidationError("Wiki 当月工作表缺少业务区终止标志")
    end_row = min(sentinel_rows)
    if end_row <= header_row + 1:
        raise WikiMappingValidationError("Wiki 当月工作表业务区为空")

    mapping: list[dict[str, Any]] = []
    all_people: set[str] = set()
    person_projects: dict[str, list[str]] = {}
    all_chips: set[str] = set()

    for row in range(header_row + 1, end_row):
        values = {
            name: _clean_text(cells.get((row, column)))
            for name, column in RELEVANT_COLUMNS.items()
        }
        roles = {
            role: _split_people(values[role], row=row, role=role.upper())
            for role in ("spm", "bsp", "diag")
        }
        people_on_row = []
        for role in ("spm", "bsp", "diag"):
            for person in roles[role]:
                all_people.add(person)
                if person not in people_on_row:
                    people_on_row.append(person)

        chips = _split_chips(values["chip"])
        recognized_chips = [chip for chip in chips if _is_recognizable_chip(chip)]
        if people_on_row and chips and len(recognized_chips) != len(chips):
            raise WikiMappingValidationError(
                f"Wiki 第 {row} 行存在无法安全识别的负责人机芯"
            )
        all_chips.update(recognized_chips)

        # A merged continuation without any role is only a visual row.  Keep
        # unassigned projects whose source cells are real, and rows with roles.
        has_original_context = any(
            (row, RELEVANT_COLUMNS[name]) in original_coordinates
            for name in ("customer", "project", "chip")
        )
        if people_on_row or has_original_context:
            mapping.append(
                {
                    "row": row,
                    "customer": values["customer"],
                    "project": values["project"],
                    "chip": values["chip"],
                    "spm": "、".join(roles["spm"]),
                    "bsp": "、".join(roles["bsp"]),
                    "diag": "、".join(roles["diag"]),
                }
            )

        customer = values["customer"].strip()
        if "/" in customer:
            raise WikiMappingValidationError(
                f"Wiki 第 {row} 行客户名称包含不支持的分隔符"
            )
        for person in people_on_row:
            entries = person_projects.setdefault(person, [])
            for chip in recognized_chips:
                entry = f"{customer}/{chip}" if customer else chip
                if entry not in entries:
                    entries.append(entry)

    person_projects = {
        person: person_projects[person]
        for person in sorted(person_projects)
        if person_projects[person]
    }
    if len(mapping) < 5 or len(all_people) < 3 or len(person_projects) < 3 or len(all_chips) < 3:
        raise WikiMappingValidationError("Wiki 当月项目负荷有效数据量异常")

    return {
        "schema_version": 2,
        "source": {
            "kind": "confluence_attachment",
            "page_id": WIKI_PAGE_ID,
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "updated_at": attachment.updated_at,
            "sha256": digest,
            "sheet": expected_sheet_name,
        },
        "mapping": mapping,
        "person_projects": person_projects,
        "all_people": sorted(all_people),
        "all_chips": sorted(all_chips),
    }


def _write_mapping_if_changed(target_path: Path, mapping: dict[str, Any]) -> bool:
    serialized = (
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    try:
        existing = target_path.read_bytes() if target_path.is_file() else None
    except OSError as exc:
        raise WikiMappingValidationError("暂存项目映射无法读取") from exc
    if existing == serialized:
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, target_path)
    except OSError as exc:
        raise WikiMappingValidationError("暂存项目映射无法写入") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def refresh_project_mapping(
    target_path: Path | str,
    *,
    session: Any | None = None,
    token: str | None = None,
) -> MappingRefreshResult:
    """Check the trusted page and refresh ``target_path`` from its newest XLSX."""

    selected_token = token if token is not None else os.environ.get(WIKI_PAT_ENV, "")
    if not isinstance(selected_token, str) or not selected_token.strip():
        raise WikiMappingCredentialError("未配置 Wiki 访问凭据")
    selected_token = selected_token.strip()
    own_session = session is None
    selected_session = session or requests.Session()
    try:
        _response, listing_content = _request(
            selected_session,
            ATTACHMENT_API_URL,
            token=selected_token,
            maximum=2 * 1024 * 1024,
        )
        attachment = _select_latest_attachment(
            _parse_attachment_results(listing_content)
        )
        _download_response, workbook_content = _request(
            selected_session,
            _attachment_download_url(attachment),
            token=selected_token,
            maximum=MAX_DOWNLOAD_BYTES,
        )
        mapping = parse_project_mapping_xlsx(workbook_content, attachment)
        updated = _write_mapping_if_changed(Path(target_path).resolve(), mapping)
        return MappingRefreshResult(
            updated=updated,
            attachment_id=attachment.id,
            attachment_filename=attachment.filename,
            source_sha256=mapping["source"]["sha256"],
        )
    finally:
        if own_session:
            try:
                selected_session.close()
            except Exception:
                pass


__all__ = [
    "AttachmentMetadata",
    "MappingRefreshResult",
    "WikiMappingCredentialError",
    "WikiMappingDownloadError",
    "WikiMappingError",
    "WikiMappingValidationError",
    "parse_project_mapping_xlsx",
    "refresh_project_mapping",
]
