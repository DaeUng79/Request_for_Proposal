"""제안요청서 등록 및 조회: .venv/bin/python -m streamlit run streamlit_main.py"""
from __future__ import annotations

import copy
import hashlib
import html
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from bson import json_util
from gridfs import GridFS
from pymongo import MongoClient

from step2_hwpx_storage import MongoStorageError, mongo_settings, save_to_mongodb
    
ROOT = Path(__file__).resolve().parent
DATABASE = "Request_for_Proposal"
COLLECTION = "Request"
MAX_UPLOAD = 50 * 1024 * 1024
PAGE_SIZE = 20
KST = ZoneInfo("Asia/Seoul")


class AppError(RuntimeError):
    """화면에 표시해도 접속 정보를 노출하지 않는 오류."""


def worker_python():
    for candidate in (ROOT / ".venv/bin/python", ROOT / ".venv/Scripts/python.exe"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def load_page_previews(document):
    """보관된 원본으로 페이지별 화면을 만들고 파일 해시별로 재사용한다."""
    digest = document.get("source_sha256") or document.get("document_id") or document.get("_id")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None
    signature = hashlib.sha256()
    for name in ("streamlit_page_preview.py", "step1_parser_hwpx.py", "page_number_corrections.json",
                 "requirements.txt"):
        path = ROOT / name
        if path.is_file():
            signature.update(path.read_bytes())
    cache_dir = ROOT / "output/page_previews" / digest
    cached = cache_dir / (signature.hexdigest() + ".json")
    if cached.is_file():
        try:
            result = json.loads(cached.read_text(encoding="utf-8"))
            if result.get("source_sha256") == digest and result.get("svgs"):
                return result
        except (OSError, ValueError):
            pass

    source = None
    folder = ROOT / "output/uploads" / digest
    for candidate in sorted(folder.glob("*")):
        if (candidate.is_file() and candidate.suffix.lower() in {".hwpx", ".hwp"}
                and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest):
            source = candidate
            break
    if source is None and document.get("source_gridfs_id"):
        data = load_image(document["source_gridfs_id"])
        if hashlib.sha256(data).hexdigest() != digest:
            raise AppError("보관된 원본 파일과 문서의 해시가 다릅니다.")
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / clean_filename(document.get("filename", "document.hwpx"))
        source.write_bytes(data)
    if source is None:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_dir, prefix="render-") as temporary:
        output = Path(temporary) / "pages.json"
        try:
            completed = subprocess.run(
                [worker_python(), str(ROOT / "streamlit_page_preview.py"), str(source),
                 "--output", str(output)], cwd=ROOT, capture_output=True, timeout=300, check=False)
            if completed.returncode:
                raise AppError("페이지별 화면을 생성하지 못했습니다. 렌더링 환경을 확인해 주세요.")
            result = json.loads(output.read_text(encoding="utf-8"))
            if result.get("source_sha256") != digest or not result.get("svgs"):
                raise AppError("페이지별 화면과 원본 문서가 일치하지 않습니다.")
            output.replace(cached)
            return result
        except subprocess.TimeoutExpired:
            raise AppError("페이지별 화면 생성이 5분을 초과했습니다.") from None
        except (OSError, ValueError):
            raise AppError("페이지별 화면을 읽지 못했습니다. 다시 시도해 주세요.") from None


def parse_in_worker(source: Path):
    """렌더러를 새 프로세스에서 로드해 Streamlit의 import 캐시와 분리한다."""
    project_python = ROOT / ".venv" / "bin" / "python"
    if not project_python.is_file():
        project_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(project_python) if project_python.is_file() else sys.executable
    with tempfile.TemporaryDirectory(prefix="hwpx-parse-") as temporary:
        output = Path(temporary) / "parsed.json"
        command = [python, str(ROOT / "step1_parser_hwpx.py"), str(source),
                   "--no-mongo", "--strict-pagination", "--output-json", str(output),
                   "--asset-dir", str(ROOT / "output")]
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                                       timeout=300, check=False)
        except subprocess.TimeoutExpired:
            raise AppError("파일 파싱이 5분을 초과했습니다. 문서를 나누어 다시 등록해 주세요.") from None
        except OSError:
            raise AppError("파서 실행 환경을 시작하지 못했습니다. 프로젝트 가상환경을 확인해 주세요.") from None
        if completed.returncode != 0:
            # 자식 프로세스 출력에는 경로 등이 포함될 수 있으므로 화면에 그대로 노출하지 않는다.
            raise AppError("파일 파싱에 실패했습니다. 프로젝트 가상환경에 "
                           "requirements.txt의 의존성을 설치했는지 확인하고, "
                           "한글에서 문서가 정상적으로 열리는지 확인해 주세요.")
        try:
            return json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AppError("파싱 결과를 읽지 못했습니다. 파일을 다시 등록해 주세요.") from None


@contextmanager
def database_connection():
    try:
        # 수동으로 직접 URI 입력
        uri = "mongodb://ysgpt2024:ysgpt2024@svc.sel4.cloudtype.app:31286/" #외부에서 접속방법
        # uri = "mongodb://ysgpt2024:ysgpt2024@mongo:27017/" #Cloudtype에 등록된 서비스끼지 접속방법 

        mongo:27017
        
        with MongoClient(uri, serverSelectionTimeoutMS=7000, connectTimeoutMS=7000,
                         socketTimeoutMS=30000, appname="rfp-streamlit") as client:
            yield client[DATABASE]
    except Exception as error:
        raise AppError(f"데이터베이스에 접근하지 못했습니다 ({type(error).__name__}). "
                       "서버 연결을 확인한 뒤 다시 시도해 주세요.") from None


def history_query(search=""):
    query = {"$or": [{"status": "ready"}, {"status": {"$exists": False}}]}
    if search.strip():
        forms = {unicodedata.normalize(form, search.strip()) for form in ("NFC", "NFD")}
        query = {"$and": [query, {"$or": [
            {"filename": {"$regex": re.escape(term), "$options": "i"}} for term in sorted(forms)
        ]}]}
    return query


def load_history(search="", page=1):
    with database_connection() as db:
        query = history_query(search)
        total = db[COLLECTION].count_documents(query)
        page = max(1, min(page, max(1, math.ceil(total / PAGE_SIZE))))
        projection = {"filename": 1, "stored_at": 1, "counts": 1,
                      "status": 1, "active_revision": 1}
        rows = list(db[COLLECTION].find(query, projection)
                    .sort([("stored_at", -1), ("_id", 1)])
                    .skip((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
        return rows, total, page


PAGE_FIELDS = {"physical_page_number", "physical_page_numbers", "page_number",
               "page_numbers", "page_number_source"}


def recover_page_metadata(document, previous_blocks):
    """동일 본문의 이전 버전에서 페이지 정보만 복원한다. DB는 변경하지 않는다."""
    def content(value, parent=None):
        if isinstance(value, dict):
            return {k: content(v, k) for k, v in value.items()
                    if k not in PAGE_FIELDS | {"_id", "revision", "document_id"}
                    and not (parent == "table" and k in {"context_before", "context_after"})}
        if isinstance(value, list):
            return [content(v) for v in value]
        return value

    current = document.get("blocks", [])
    previous = {b["block_id"]: b for b in previous_blocks}
    if not current or len(current) != len(previous):
        return None
    if any(b["block_id"] not in previous or content(b) != content(previous[b["block_id"]])
           for b in current):
        return None
    recovered = copy.deepcopy(document)
    pages = {}
    for block in recovered["blocks"]:
        old = previous[block["block_id"]]
        metadata = {k: copy.deepcopy(old[k]) for k in PAGE_FIELDS if k in old}
        block.update(metadata)
        for key in ("table", "image"):
            if key in block:
                block[key].update(metadata)
        physical = old.get("physical_page_numbers") or [old.get("physical_page_number")]
        printed = old.get("page_numbers") or [old.get("page_number")]
        for i, number in enumerate(physical):
            if isinstance(number, int) and number > 0:
                pages[number] = {"physical_page_number": number,
                                 "page_number": printed[i] if i < len(printed) else None}
    if not pages:
        return None
    recovered["pages"] = [pages[p] for p in sorted(pages)]
    recovered["pagination_source"] = "matching_revision"
    recovered["warnings"] = [w for w in recovered.get("warnings", [])
                              if w.get("code") not in {"pagination_failed", "pagination_disabled"}]
    return recovered


def load_document(document_id):
    with database_connection() as db:
        document = db[COLLECTION].find_one({"_id": document_id})
        if document and "blocks" not in document and document.get("active_revision"):
            document["blocks"] = list(db.hwpx_blocks.find({
                "document_id": document_id, "revision": document["active_revision"]
            }).sort("order", 1))
        if document and document.get("blocks") and not any(
            b.get("physical_page_numbers") or b.get("physical_page_number")
            for b in document["blocks"]
        ):
            revisions = db.hwpx_blocks.distinct("revision", {
                "document_id": document_id, "physical_page_number": {"$gt": 0},
                "revision": {"$ne": document.get("active_revision")},
            })
            for revision in sorted(revisions):
                previous = list(db.hwpx_blocks.find({
                    "document_id": document_id, "revision": revision,
                }))
                recovered = recover_page_metadata(document, previous)
                if recovered:
                    return recovered
        return document


def delete_document(document_id):
    """선택 문서와 모든 버전의 보조 데이터를 삭제한다. 공유 파일은 보존한다."""
    with database_connection() as db:
        document = db[COLLECTION].find_one({"_id": document_id})
        if not document:
            return
        assets = list(document.get("assets", [])) + list(
            db.hwpx_assets.find({"document_id": document_id}))
        file_ids = {a.get("gridfs_id") or a.get("sha256") for a in assets}
        file_ids.add(document.get("source_gridfs_id"))
        file_ids.discard(None)
        for name in ("blocks", "assets", "chunks"):
            db[f"hwpx_{name}"].delete_many({"document_id": document_id})
        db[COLLECTION].delete_one({"_id": document_id})
        fs = GridFS(db, collection="hwpx_files")
        for file_id in file_ids:
            referenced = db[COLLECTION].find_one({"$or": [
                {"source_gridfs_id": file_id}, {"assets.gridfs_id": file_id},
                {"assets.sha256": file_id}]}, {"_id": 1})
            asset_reference = db.hwpx_assets.find_one({"$or": [
                {"gridfs_id": file_id}, {"sha256": file_id}]}, {"_id": 1})
            if not referenced and not asset_reference and fs.exists(file_id):
                fs.delete(file_id)


def load_image(asset_id):
    with database_connection() as db:
        file = GridFS(db, collection="hwpx_files").get(asset_id)
        if file.length > MAX_UPLOAD:
            raise AppError("이미지가 미리보기 용량 제한을 초과했습니다.")
        return file.read()


def clean_filename(filename):
    name = unicodedata.normalize("NFC", filename.replace("\\", "/").rsplit("/", 1)[-1])
    name = "".join(c for c in name if c.isprintable()).strip()
    if not name or Path(name).suffix.lower() not in {".hwpx", ".hwp"}:
        raise AppError("HWPX 또는 HWP 파일을 선택해 주세요.")
    if len(name.encode("utf-8")) > 240:
        name = name[:60] + Path(name).suffix.lower()
    return name


def parse_upload(data: bytes, filename: str):
    """업로드를 문서 해시별로 보관하고 기존 파서로 처리한다. DB 쓰기는 별도 수행."""
    if not data or len(data) > MAX_UPLOAD:
        raise AppError("비어 있지 않은 50 MB 이하의 파일을 선택해 주세요.")
    name = clean_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix == ".hwpx" and not data.startswith(b"PK"):
        raise AppError("올바른 HWPX 파일이 아닙니다. 파일 형식을 확인해 주세요.")
    if suffix == ".hwp" and not data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise AppError("지원하는 HWP 5.x 파일이 아닙니다. HWPX로 저장해 등록해 주세요.")
    digest = hashlib.sha256(data).hexdigest()
    folder = ROOT / "output" / "uploads" / digest
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / name
    source.write_bytes(data)
    result = parse_in_worker(source)
    output = ROOT / "output" / "json" / f"{digest}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result, source


def json_bytes(document):
    return json_util.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def display_name(name):
    return unicodedata.normalize("NFC", str(name or "이름 없는 문서"))


def display_time(value):
    if not isinstance(value, datetime):
        return "저장 시각 없음"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime("%Y.%m.%d %H:%M")


def page_label(block):
    pages = block.get("physical_page_numbers") or []
    if not pages and block.get("physical_page_number") is not None:
        pages = [block["physical_page_number"]]
    return " · ".join(f"{p}쪽" for p in pages) or "페이지 미확인"


def table_html(table):
    """좌표와 rowspan/colspan을 유지하고 모든 원문 문자열을 이스케이프한다."""
    rendered = []
    for row in table.get("cells", []):
        cells = []
        for cell in sorted(row, key=lambda c: c.get("col", 0)):
            color = str(cell.get("background_color") or "")
            style = f"background:{color};" if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color) else ""
            rs, cs = max(1, int(cell.get("row_span", 1))), max(1, int(cell.get("col_span", 1)))
            text = html.escape(cell.get("text", "")).replace("\n", "<br>")
            cells.append(f'<td rowspan="{rs}" colspan="{cs}" style="{style}">{text}</td>')
        rendered.append("<tr>" + "".join(cells) + "</tr>")
    return '<div class="rfp-table"><table>' + "".join(rendered) + "</table></div>"


def build_document_html(document, selected_page=None, image_loader=None, page_previews=None):
    """페이지별 렌더링을 우선 표시하며 원본이 없을 때만 저장 블록을 사용한다."""
    import base64

    blocks = document.get("blocks", [])
    lookup = {b["block_id"]: b for b in blocks}
    assets = {a.get("asset_id"): a for a in document.get("assets", [])}
    image_cache = {}

    def pages_of(block):
        pages = block.get("physical_page_numbers") or [block.get("physical_page_number")]
        return sorted({p for p in pages if isinstance(p, int) and p > 0})

    printed_pages = {}
    for block in blocks:
        physical = block.get("physical_page_numbers") or [block.get("physical_page_number")]
        printed = block.get("page_numbers") or [block.get("page_number")]
        for index, physical_page in enumerate(physical):
            if physical_page is not None and index < len(printed):
                printed_pages.setdefault(physical_page, printed[index])
    metadata_pages = page_previews["pages"] if page_previews else document.get("pages", [])
    for metadata in metadata_pages:
        if "page_number" in metadata:
            printed_pages[metadata.get("physical_page_number")] = metadata["page_number"]

    def printed_label(physical_page):
        number = printed_pages.get(physical_page)
        return f"{html.escape(str(number))}쪽" if number is not None else ""

    def render(block, ancestors=frozenset()):
        block_id = block.get("block_id")
        if block_id in ancestors:
            return '<p class="notice">중첩 요소의 참조를 확인할 수 없습니다.</p>'
        ancestors = ancestors | {block_id}
        kind = block.get("type")
        if kind == "paragraph":
            text = html.escape(block.get("text", ""))
            return f'<p class="paragraph">{text}</p>'
        if kind == "image":
            picture = block.get("image", {})
            asset_id = picture.get("asset_id")
            if asset_id not in image_cache:
                url = None
                if asset_id and image_loader:
                    try:
                        from PIL import Image
                        data = image_loader(assets.get(asset_id, {}).get("gridfs_id") or asset_id)
                        with Image.open(io.BytesIO(data)) as image:
                            image.thumbnail((1800, 2400))
                            buffer = io.BytesIO()
                            image.convert("RGBA").save(buffer, format="PNG")
                        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
                    except Exception:
                        pass
                image_cache[asset_id] = url
            url = image_cache[asset_id]
            description = html.escape(picture.get("description") or "문서 이미지")
            if not url:
                return f'<figure class="notice">{description} · 이미지 미리보기를 불러오지 못했습니다.</figure>'
            return f'<figure><img src="{url}" alt="{description}"></figure>'
        if kind != "table":
            return ""
        table = block.get("table", {})
        rows = []
        widths = {}
        for row in table.get("cells", []):
            cells = []
            for cell in sorted(row, key=lambda c: c.get("col", 0)):
                rs, cs = max(1, int(cell.get("row_span", 1))), max(1, int(cell.get("col_span", 1)))
                col = int(cell.get("col", 0))
                width = max(0, int(cell.get("width", 0))) / cs
                for column in range(col, min(col + cs, 256)):
                    widths[column] = max(widths.get(column, 0), width)
                color = str(cell.get("background_color") or "")
                style = f"background-color:{color};" if re.fullmatch(r"#[a-fA-F0-9]{3,8}", color) else ""
                children = [lookup[c] for c in cell.get("child_block_ids", []) if c in lookup]
                content = "".join(render(child, ancestors) for child in children)
                if not children:
                    content = '<p class="paragraph">' + html.escape(cell.get("text", "")) + '</p>'
                cells.append(f'<td rowspan="{rs}" colspan="{cs}" style="{style}">{content}</td>')
            rows.append("<tr>" + "".join(cells) + "</tr>")
        column_count = min(256, max(int(table.get("col_count", 0)), max(widths, default=-1) + 1))
        total_width = sum(widths.get(c, 0) for c in range(column_count))
        columns = ""
        if total_width and all(widths.get(c, 0) > 0 for c in range(column_count)):
            columns = '<colgroup>' + ''.join(
                f'<col style="width:{100 * widths[c] / total_width:.3f}%">' for c in range(column_count)
            ) + '</colgroup>'
        return '<div class="table-wrap"><table>' + columns + ''.join(rows) + '</table></div>'

    top_level = [b for b in blocks if not b.get("parent_block_id")]
    groups = {}
    for block in top_level:
        pages = pages_of(block)
        groups.setdefault(pages[0] if pages else 0, []).append(block)
    page_numbers = sorted(set(groups) | {p for block in top_level for p in pages_of(block)} | {
        p["physical_page_number"] for p in document.get("pages", [])
        if isinstance(p.get("physical_page_number"), int)
    })
    if page_previews:
        page_numbers = [p["physical_page_number"] for p in metadata_pages]
    if selected_page is not None:
        page_numbers = [selected_page]
    title = html.escape(display_name(document.get("filename")))
    papers = []
    for page in page_numbers:
        svg = page_previews.get("svgs", {}).get(str(page)) if page_previews else None
        if svg:
            encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            content = f'<img class="page-preview" loading="lazy" decoding="async" src="data:image/svg+xml;base64,{encoded}" alt="물리 {page}페이지 문서 내용">'
        else:
            page_blocks = groups.get(page, [])
            # 세부 페이지 정보가 없는 표를 첫 페이지에 몰아 놓지 않는다.
            content = ''.join(render(b) for b in page_blocks if len(pages_of(b)) <= 1)
            if any(page in pages_of(b) and len(pages_of(b)) > 1 for b in top_level):
                content += '<p class="notice">페이지별 내용을 표시하려면 원본 파일을 다시 등록해 주세요.</p>'
            if not content:
                content = '<p class="notice">이 페이지의 추출 내용이 없습니다.</p>'
        label = printed_label(page)
        badge = f"<span>{label}</span>" if label else ""
        heading = f"{title} · {label}" if label else title
        footer = f"<footer>— {label} —</footer>" if label else ""
        separator_label = f"{label} 시작" if label else "페이지 구분"
        paper_class = "paper rendered-paper" if svg else "paper"
        papers.append(f'<div class="page-divider" role="separator" aria-label="{separator_label}">{badge}</div><article class="{paper_class}"><header>{heading}</header><main>{content}</main>{footer}</article>')
    return '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>''' + title + '''</title>
<style>
* {box-sizing:border-box;} body {margin:0;background:#e9edf2;color:#20262e;}
.reader {padding:24px 16px;}
.page-divider {display:flex;align-items:center;gap:16px;width:100%;max-width:210mm;margin:8px auto 20px;
 color:#334155;font:600 13px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;}
.page-divider::before,.page-divider::after {content:"";height:1px;flex:1;background:#a9b9ce;}
.page-divider span {padding:5px 16px;border:1px solid #cbd5e1;border-radius:20px;background:#fff;white-space:nowrap;}
.paper {width:210mm;min-height:297mm;margin:0 auto 24px;padding:16mm 20mm 14mm;background:white;
 box-shadow:0 5px 24px #24375419;display:flex;flex-direction:column;}
header {font:10px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;color:#788493;border-bottom:1px solid #dfe4eb;padding-bottom:10px;margin-bottom:24px;overflow-wrap:anywhere;}
main {flex:1;font-family:"AppleMyungjo","Batang","Noto Serif KR",serif;font-size:11pt;line-height:1.85;}
.paragraph {margin:0 0 9px;white-space:pre-wrap;tab-size:4;overflow-wrap:anywhere;}
footer {text-align:center;padding-top:28px;color:#64748b;font:12px/1.5 sans-serif;}
table {width:100%;border-collapse:collapse;table-layout:fixed;font-size:10pt;line-height:1.65;margin:12px 0;}
td {border:1px solid #687583;padding:7px 9px;vertical-align:middle;overflow-wrap:anywhere;}
td .paragraph {margin:2px 0;} td table {margin:4px 0;} td td {padding:5px;}
figure {margin:16px 0;text-align:center;} img {max-width:100%;height:auto;vertical-align:middle;}
.rendered-paper {padding:0;min-height:0;}
.rendered-paper header {margin:12px 20px 0;}
.rendered-paper footer {padding:0 0 12px;}
.page-preview {display:block;width:100%;height:auto;}
.notice {font:11px/1.7 -apple-system,sans-serif;color:#64748b;background:#f5f7fa;border-left:3px solid #a9b9ce;padding:8px 12px;margin:12px 0;}
@media(max-width:850px) {.reader {padding:12px 6px;}.paper {width:100%;min-height:900px;padding:24px 20px;}main {font-size:10.5pt;}td {padding:5px;}}
@media print {.page-divider {display:none;}@page {size:A4;margin:15mm;}body,.reader {background:white;padding:0;}.paper {width:100%;min-height:0;padding:0;box-shadow:none;margin:0;break-after:page;} .paper:last-child {break-after:auto;}tr,figure {break-inside:avoid;}}
</style></head><body><div class="reader">''' + ''.join(papers) + '</div></body></html>'


def document_view(document):
    try:
        previews = load_page_previews(document)
    except AppError as error:
        st.warning(str(error))
        previews = None
    markup = build_document_html(document, image_loader=load_image, page_previews=previews)
    st.iframe(markup, height=1000)


def render_document(document):
    if not document:
        st.info("문서를 찾을 수 없습니다. 저장 목록을 새로고침해 주세요.")
        return

    # 파일명과 다운로드 버튼을 나란히 배치하기 위해 컬럼 분할
    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.subheader(display_name(document.get("filename")))
    with col_btn:
        st.download_button(
            "추출 결과 JSON 다운로드",
            json_bytes(document),
            file_name=Path(display_name(document.get("filename"))).stem + ".json",
            mime="application/json",
            key="download_document",
        )

    st.caption(f"저장일 {display_time(document.get('stored_at'))} · 본문 위치는 실제 파일 페이지 기준입니다.")
    blocks = document.get("blocks", [])
    tables = [b for b in blocks if b.get("type") == "table"]
    images = [b for b in blocks if b.get("type") == "image"]
    cols = st.columns(4)
    for col, label, value in zip(cols, ["페이지", "블록", "표", "이미지"],
                                 [len(document.get("pages", [])) or "—", len(blocks), len(tables), len(images)]):
        col.metric(label, value)

    if not blocks:
        st.info("이 문서는 현재 파서의 본문 구조가 없습니다. JSON 다운로드에서 저장 내용을 확인할 수 있습니다.")
        return
    view = st.radio("보기", ["문서 보기", "표", "이미지"], horizontal=True, key="detail_view", label_visibility="collapsed")
    if view == "문서 보기":
        document_view(document)
    elif view == "표":
        if not tables:
            st.info("추출된 표가 없습니다.")
            return
        index = st.selectbox("표 선택", range(len(tables)), format_func=lambda i: f"표 {i + 1} · {page_label(tables[i])}")
        block = tables[index]
        if block.get("parent_cell_id"):
            st.caption("상위 표 셀: " + block["parent_cell_id"])
        st.markdown(table_html(block["table"]), unsafe_allow_html=True)
        st.caption("병합과 단색 음영을 표시합니다. 중첩 표는 표 선택 목록에서 따로 확인할 수 있습니다.")
    else:
        if not images:
            st.info("추출된 이미지가 없습니다.")
            return
        index = st.selectbox("이미지 선택", range(len(images)), format_func=lambda i: f"이미지 {i + 1} · {page_label(images[i])}")
        picture = images[index].get("image", {})
        asset_id = picture.get("asset_id")
        assets = {a.get("asset_id"): a for a in document.get("assets", [])}
        asset = assets.get(asset_id, {})
        if not asset_id:
            st.info("원본 이미지가 저장되지 않았습니다.")
            return
        try:
            data = load_image(asset.get("gridfs_id") or asset_id)
        except AppError as error:
            st.error(str(error))
            return
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                st.image(image, caption=picture.get("description") or "추출 이미지", width="stretch")
        except Exception:
            st.info("이 이미지 형식은 미리보기를 지원하지 않습니다. 파일을 다운로드해 확인하세요.")
        extension = Path(picture.get("source_path") or "image.bin").suffix
        st.download_button("이미지 다운로드", data, file_name=f"{asset_id[:12]}{extension}",
                           mime=asset.get("mime_type") or "application/octet-stream")

def clear_upload_feedback():
    st.session_state.pop("pending_save", None)
    st.session_state.pop("saved_upload", None)


def register_view():
    # st.subheader("제안요청서 등록")
    with st.container(border=True):
        uploaded = st.file_uploader("파일 등록", type=["hwpx"], key="proposal_upload",
                                    on_change=clear_upload_feedback,
                                    help="파일당 최대 50 MB. 표와 이미지 보존을 위해 HWPX를 권장합니다.")
        # st.caption("HWPX 최대 50 MB · 같은 파일을 다시 등록하면 기존 문서를 갱신합니다.")
        clicked = st.button("파싱 및 저장", type="primary", disabled=uploaded is None, key="register")
    if clicked:
        # 이전 파일의 성공/실패 결과가 새 업로드에 표시되지 않도록 초기화한다.
        st.session_state.pop("pending_save", None)
        st.session_state.pop("saved_upload", None)
        with st.status("문서를 처리하고 있습니다…", expanded=True) as status:
            try:
                st.write("1. 문서 파싱 및 추출")
                document, source = parse_upload(uploaded.getvalue(), uploaded.name)
                st.session_state.pending_save = (document, str(source))
                st.write("2. 몽고DB 저장")
                saved = save_to_mongodb(document, source, env_path=ROOT / ".env",
                                        database=DATABASE, collection_name=COLLECTION)
                st.session_state.saved_upload = saved
                st.session_state.selected_document = saved["document_id"]
                st.session_state.pop("pending_save", None)
                status.update(label="등록을 완료했습니다", state="complete", expanded=False)
            except (AppError, MongoStorageError) as error:
                status.update(label="처리를 완료하지 못했습니다", state="error")
                st.error(str(error))
            except Exception as error:
                status.update(label="처리를 완료하지 못했습니다", state="error")
                st.error(f"처리 중 오류가 발생했습니다 ({type(error).__name__}). 파일과 저장 환경을 확인해 주세요.")
    if st.session_state.get("pending_save"):
        document, source = st.session_state.pending_save
        st.warning("파싱 결과를 보관 중입니다. 연결을 확인한 후 저장을 다시 시도할 수 있습니다.")
        st.download_button("파싱 결과 다운로드", json_bytes(document), file_name="parsed.json", mime="application/json")
        if st.button("DB 저장 재시도", key="retry_save"):
            try:
                with st.spinner("데이터베이스에 저장 중입니다…"):
                    saved = save_to_mongodb(document, Path(source), env_path=ROOT / ".env",
                                            database=DATABASE, collection_name=COLLECTION)
                st.session_state.saved_upload = saved
                st.session_state.selected_document = saved["document_id"]
                del st.session_state.pending_save
                st.rerun()
            except MongoStorageError as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"저장에 실패했습니다 ({type(error).__name__}).")
    if st.session_state.get("saved_upload"):
        saved = st.session_state.saved_upload
        counts = saved["counts"]
        st.success(f"저장 완료 · 블록 {counts['blocks']:,}개 · 이미지 {counts['assets']:,}개 · 검색용 청크 {counts['chunks']:,}개")
        if st.button("저장한 문서 열기", key="open_saved"):
            st.session_state.selected_document = saved["document_id"]
            st.rerun()


def history_view():
    """사이드바에서 문서를 검색·선택하고, 최초 접속 시 최신 문서를 선택한다."""
    st.subheader("저장 이력")
    if st.session_state.get("deleted_document_name"):
        st.success(f"문서를 삭제했습니다: {st.session_state.pop('deleted_document_name')}")

    page = int(st.session_state.get("history_page", 1))
    try:
        rows, total, page = load_history("", page)
    except AppError as error:
        st.error(str(error))
        return False
    st.session_state.history_page = page
    st.caption(f"총 {total:,}개 문서 · 최근 저장 순")
    if rows:
        if not st.session_state.get("selected_document"):
            st.session_state.selected_document = rows[0]["_id"]
        options = {row["_id"]: f"{display_name(row.get('filename'))} · {display_time(row.get('stored_at'))}" for row in rows}
        ids = list(options)
        selected = st.session_state.get("selected_document")
        choice = st.selectbox("열람할 문서", ids, index=ids.index(selected) if selected in ids else 0,
                              format_func=options.get, key=f"history_choice_{page}")
        if st.session_state.get("pending_delete_document") != choice:
            st.session_state.pop("pending_delete_document", None)
        actions = st.columns(2)
        if actions[0].button("문서 열기", type="primary", key="open_document"):
            st.session_state.selected_document = choice
        if actions[1].button("문서 삭제", key="delete_document"):
            st.session_state.pending_delete_document = choice
        if st.session_state.get("pending_delete_document") == choice:
            name = display_name(next(row.get("filename") for row in rows if row["_id"] == choice))
            st.warning(f"‘{name}’ 문서와 저장된 추출 데이터를 삭제할까요? 되돌릴 수 없습니다.")
            confirm, cancel = st.columns(2)
            if confirm.button("삭제 확인", type="primary", key="confirm_delete_document"):
                try:
                    delete_document(choice)
                except AppError as error:
                    st.error(str(error))
                else:
                    if st.session_state.get("selected_document") == choice:
                        st.session_state.pop("selected_document", None)
                    if st.session_state.get("saved_upload", {}).get("document_id") == choice:
                        st.session_state.pop("saved_upload", None)
                    st.session_state.pop("pending_delete_document", None)
                    st.session_state.deleted_document_name = name
                    st.rerun()
            if cancel.button("취소", key="cancel_delete_document"):
                st.session_state.pop("pending_delete_document", None)
                st.rerun()
    return True

def main():
    st.set_page_config(page_title="제안요청서 보관함", page_icon="📄", layout="wide")
    # st.markdown("""<style>
    # .block-container {max-width:1180px;padding-top:2.4rem;padding-bottom:4rem;}
    # h1 {letter-spacing:-.04em;} h2,h3 {letter-spacing:-.025em;}
    # [data-testid="stMetric"] {background:#f3f6fb;border:1px solid #e3e9f2;border-radius:12px;padding:16px;}
    # .rfp-table {overflow:auto;max-height:650px;border:1px solid #dbe3ef;border-radius:10px;}
    # .rfp-table table {border-collapse:collapse;width:100%;font-size:14px;}
    # .rfp-table td {border:1px solid #dbe3ef;padding:12px;vertical-align:top;white-space:pre-wrap;min-width:90px;}
    # </style>""", unsafe_allow_html=True)
    with st.sidebar:
        st.markdown("### 제안요청서 보관함")
        register_view()
        # st.divider()
        history_loaded = history_view()
    st.title("심사임당 & 몇점이GO 문서파싱 결과")
    if st.session_state.get("selected_document"):
        try:
            document = load_document(st.session_state.selected_document)
        except AppError as error:
            st.error(str(error))
            return
        render_document(document)
    elif history_loaded:
        st.info("제안요청서를 등록해주세요")
    else:
        st.warning("저장 자료를 불러오지 못했습니다. 사이드바에서 새로고침해 주세요.")


if __name__ == "__main__":
    main()
