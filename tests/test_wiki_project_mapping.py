import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import wiki_project_mapping as wiki_mapping


ATTACHMENT_FILENAME = "团队成员项目负荷_新拆分-20260720.xlsx"
OLDER_ATTACHMENT_FILENAME = "团队成员项目负荷_新拆分-20260714.xlsx"
TARGET_SHEET = "计算负荷用-2026-7月"


def _minimal_xlsx(sheets):
    """Build a small shared-string XLSX without relying on spreadsheet packages."""

    shared_strings = []
    shared_string_indexes = {}

    def shared_string_index(value):
        text = str(value)
        if text not in shared_string_indexes:
            shared_string_indexes[text] = len(shared_strings)
            shared_strings.append(text)
        return shared_string_indexes[text]

    worksheet_xml = []
    for _sheet_name, rows, merged_ranges in sheets:
        row_xml = []
        for row_number in sorted(rows):
            cells = []
            for column in sorted(rows[row_number]):
                value = rows[row_number][column]
                if value is None:
                    continue
                index = shared_string_index(value)
                cells.append(
                    f'<c r="{column}{row_number}" t="s"><v>{index}</v></c>'
                )
            row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        merge_xml = ""
        if merged_ranges:
            merge_cells = "".join(
                f'<mergeCell ref="{escape(cell_range)}"/>'
                for cell_range in merged_ranges
            )
            merge_xml = (
                f'<mergeCells count="{len(merged_ranges)}">'
                f"{merge_cells}</mergeCells>"
            )
        worksheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData>{merge_xml}'
            '</worksheet>'
        )

    content_type_overrides = "".join(
        '<Override '
        f'PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{content_type_overrides}"
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _rows, _merges) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{workbook_sheets}</sheets>'
        '</workbook>'
    )
    workbook_relationships = "".join(
        '<Relationship '
        f'Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    shared_strings_relationship_id = len(sheets) + 1
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{workbook_relationships}"
        '<Relationship '
        f'Id="rId{shared_strings_relationship_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        '</Relationships>'
    )
    string_items = "".join(
        f'<si><t xml:space="preserve">{escape(value)}</t></si>'
        for value in shared_strings
    )
    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
        f"{string_items}</sst>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings_xml)
        for index, xml in enumerate(worksheet_xml, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
    return output.getvalue()


def _mapping_sheet_rows(*, include_headers=True):
    rows = {
        1: {"A": "团队成员项目负荷（离线测试）"},
        3: {
            "A": "G",
            "B": "N/P/C/B项目",
            "D": "AM963",
            "I": "王明佳",
            "M": "田昭辉",
            "O": "邓赐祥",
        },
        # A/B/D are merged from row 3.  陈泽钦 must inherit the same project.
        4: {"O": "陈泽钦"},
        5: {
            "A": "预研",
            "B": "Flensberg",
            "D": "AM963D5\nAM966D5",
            "I": "王明佳\n李惠洁",
            "M": "田昭辉、陈泽钦",
            "O": "邓赐祥\n闫晓光",
        },
        6: {
            "A": "P",
            "B": "Avanta",
            "D": "MT9289",
            "I": "罗咏珊",
            "M": "邓梓炜",
        },
        7: {
            "A": "B",
            "B": "Hydrogen",
            "D": "MT9026",
            "M": "刘云培",
            "O": "邓梓炜",
        },
        8: {"A": "负荷说明"},
        # This is deliberately valid-looking; parsing must stop at 负荷说明.
        9: {
            "A": "G",
            "B": "页脚伪项目",
            "D": "AM999D5",
            "I": "应被忽略",
        },
        10: {"D": "新增", "I": "0.9"},
        11: {"D": "待定缺口", "I": "1.2"},
    }
    if include_headers:
        rows[2] = {
            "A": "客户",
            "B": "项目群",
            "D": "机芯",
            "I": "SPM",
            "M": "BSP",
            "O": "DIAG",
        }
    else:
        rows[2] = {
            "A": "客户",
            "B": "项目群",
            "D": "方案",
            "I": "负责人甲",
            "M": "负责人乙",
            "O": "负责人丙",
        }
    return rows


def _valid_xlsx():
    decoy_rows = {
        1: {
            "A": "客户",
            "B": "项目群",
            "D": "机芯",
            "I": "SPM",
            "M": "BSP",
            "O": "DIAG",
        },
        2: {
            "A": "旧月份",
            "B": "不应读取",
            "D": "MT9025",
            "I": "旧月人员",
        },
        3: {"A": "负荷说明"},
    }
    return _minimal_xlsx(
        [
            ("计算负荷用-2026-6月", decoy_rows, []),
            (TARGET_SHEET, _mapping_sheet_rows(), ["A3:A4", "B3:B4", "D3:D4"]),
        ]
    )


def _attachment(
    content,
    *,
    attachment_id="132360786",
    filename=ATTACHMENT_FILENAME,
    updated_at="2026-07-20T17:06:07.730+08:00",
    expected_size=None,
    download_path="/download/attachments/22824730/project-load.xlsx?version=1",
):
    return wiki_mapping.AttachmentMetadata(
        id=attachment_id,
        filename=filename,
        updated_at=updated_at,
        expected_size=len(content) if expected_size is None else expected_size,
        download_path=download_path,
    )


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(content))} if content else {}

    def json(self):
        if self._json_data is None:
            raise ValueError("not JSON")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class FakeWikiSession:
    def __init__(self, attachments, downloads):
        self.attachments = attachments
        self.downloads = downloads
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if "/rest/api/content/" in url:
            payload = {"results": self.attachments}
            return FakeResponse(
                json_data=payload,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        for suffix, response in self.downloads.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected offline URL: {url}")


def _attachment_api_item(
    *,
    attachment_id,
    filename,
    updated_at,
    size,
    download_path,
):
    return {
        "id": attachment_id,
        "title": filename,
        "version": {"when": updated_at},
        "extensions": {"fileSize": size},
        "_links": {"download": download_path},
    }


class ParseProjectMappingTests(unittest.TestCase):
    def test_selects_sheet_matching_attachment_month_and_parses_mapping(self):
        content = _valid_xlsx()

        mapping = wiki_mapping.parse_project_mapping_xlsx(
            content, _attachment(content)
        )

        people = mapping["person_projects"]
        self.assertNotIn("旧月人员", mapping["all_people"])
        self.assertNotIn("应被忽略", mapping["all_people"])
        self.assertNotIn("AM999D5", json.dumps(mapping, ensure_ascii=False))
        self.assertEqual(
            {3, 4, 5, 6, 7},
            {row["row"] for row in mapping["mapping"]},
        )

        self.assertIn("G/AM963", people["陈泽钦"])
        self.assertEqual(
            {"预研/AM963D5", "预研/AM966D5"},
            set(people["李惠洁"]),
        )
        self.assertTrue(
            {"预研/AM963D5", "预研/AM966D5"}.issubset(
                set(people["田昭辉"])
            )
        )
        self.assertTrue(
            {"预研/AM963D5", "预研/AM966D5"}.issubset(
                set(people["闫晓光"])
            )
        )

    def test_rejects_html_size_mismatch_and_missing_headers(self):
        html = b"<!doctype html><html><body>login</body></html>"
        with self.assertRaises(Exception):
            wiki_mapping.parse_project_mapping_xlsx(html, _attachment(html))

        valid = _valid_xlsx()
        with self.assertRaises(Exception):
            wiki_mapping.parse_project_mapping_xlsx(
                valid,
                _attachment(valid, expected_size=len(valid) + 1),
            )

        missing_headers = _minimal_xlsx(
            [(TARGET_SHEET, _mapping_sheet_rows(include_headers=False), [])]
        )
        with self.assertRaises(Exception):
            wiki_mapping.parse_project_mapping_xlsx(
                missing_headers, _attachment(missing_headers)
            )


class RefreshProjectMappingTests(unittest.TestCase):
    def test_download_uses_wiki_context_path_and_publishes_latest_mapping(self):
        content = _valid_xlsx()
        download_path = (
            "/download/attachments/22824730/project-load.xlsx?version=1"
        )
        attachment = _attachment_api_item(
            attachment_id="132360786",
            filename=ATTACHMENT_FILENAME,
            updated_at="2026-07-20T17:06:07.730+08:00",
            size=len(content),
            download_path=download_path,
        )
        session = FakeWikiSession(
            [attachment],
            {download_path: FakeResponse(content=content)},
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            target = Path(temporary_dir) / "project_mapping.json"
            target.write_text('{"old":true}', encoding="utf-8")

            result = wiki_mapping.refresh_project_mapping(
                target, session=session, token="offline-test-token"
            )

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(result.updated)
            self.assertEqual(ATTACHMENT_FILENAME, result.attachment_filename)
            self.assertIn("陈泽钦", saved["person_projects"])

        download_urls = [url for url in session.urls if "/attachments/" in url]
        self.assertEqual(1, len(download_urls))
        self.assertEqual(
            "https://idisplayvision.com/wiki" + download_path,
            download_urls[0],
        )

    def test_corrupt_newest_attachment_fails_without_falling_back(self):
        old_content = _valid_xlsx()
        corrupt_content = (
            b"<!doctype html><html><body>wrong download</body></html>"
            + b"x" * 1200
        )
        old_path = "/download/attachments/22824730/old.xlsx?version=1"
        newest_path = "/download/attachments/22824730/latest.xlsx?version=1"
        unrelated_path = "/download/attachments/22824730/unrelated.xlsx?version=1"
        attachments = [
            _attachment_api_item(
                attachment_id="120000001",
                filename=OLDER_ATTACHMENT_FILENAME,
                updated_at="2026-07-14T10:00:00+08:00",
                size=len(old_content),
                download_path=old_path,
            ),
            _attachment_api_item(
                attachment_id="132360786",
                filename=ATTACHMENT_FILENAME,
                updated_at="2026-07-20T17:06:07.730+08:00",
                size=len(corrupt_content),
                download_path=newest_path,
            ),
            _attachment_api_item(
                attachment_id="999999999",
                filename="无关附件-20260721.xlsx",
                updated_at="2026-07-21T09:00:00+08:00",
                size=len(old_content),
                download_path=unrelated_path,
            ),
        ]
        session = FakeWikiSession(
            attachments,
            {
                old_path: FakeResponse(content=old_content),
                newest_path: FakeResponse(content=corrupt_content),
                unrelated_path: FakeResponse(content=old_content),
            },
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            target = Path(temporary_dir) / "project_mapping.json"
            original = '{"sentinel":"keep-current-mapping"}'
            target.write_text(original, encoding="utf-8")

            with self.assertRaises(Exception):
                wiki_mapping.refresh_project_mapping(
                    target, session=session, token="offline-test-token"
                )

            self.assertEqual(original, target.read_text(encoding="utf-8"))

        download_urls = [url for url in session.urls if "/attachments/" in url]
        self.assertEqual(
            ["https://idisplayvision.com/wiki" + newest_path],
            download_urls,
        )


if __name__ == "__main__":
    unittest.main()
