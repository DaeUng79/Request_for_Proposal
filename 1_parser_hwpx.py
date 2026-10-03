from __future__ import annotations

import json
import re
import zipfile
import argparse
import os
import sys
import hashlib
import mimetypes
from collections import Counter
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
HC = "http://www.hancom.co.kr/hwpml/2011/core"
PIC_TAG = f"{{{HP}}}pic"
IMG_TAG = f"{{{HC}}}img"
PAGE_NUMBER_PATTERN = re.compile(r"^\s*-?\s*(\d+)\s*-?\s*$")
PARSER_VERSION = "2.0.0"
SCHEMA_VERSION = 2


def text_content(node) -> str:
    """혼합 XML의 text/tail과 탭·줄바꿈을 보존한다."""
    if node.tag == f"{{{HP}}}tab":
        return "\t"
    if node.tag == f"{{{HP}}}lineBreak":
        return "\n"
    if node.tag in {f"{{{HP}}}tbl", PIC_TAG, f"{{{HP}}}subList"}:
        return ""
    return (node.text or "") + "".join(
        text_content(child) + (child.tail or "") for child in node
    )


def text_from_cell(cell) -> str:
    """
    셀 내부의 텍스트를 추출한다.
    줄바꿈도 최대한 보존한다.
    """

    paragraphs = []

    for p in cell.findall(f"./{{{HP}}}subList/{{{HP}}}p"):
        value = text_content(p).strip()

        if value:
            paragraphs.append(value)

    return "\n".join(paragraphs)


def find_binary_path(zip_names: set[str], binary_item_id: str | None) -> str | None:
    """binaryItemIDRef에 대응하는 HWPX BinData 경로를 찾는다."""

    if not binary_item_id:
        return None

    for name in sorted(zip_names):
        if not name.startswith("BinData/"):
            continue
        if Path(name).stem == binary_item_id:
            return name

    return None


def parse_image(
    pic,
    image_id: str,
    page_number: int | None,
    physical_page_number: int | None,
    page_number_source: str,
    source_path: str | None,
) -> dict[str, Any]:
    """HWPX 그림 하나를 JSON에서 참조할 수 있는 형태로 구조화한다."""

    image_node = pic.find(f".//{IMG_TAG}")
    binary_item_id = (
        image_node.get("binaryItemIDRef")
        if image_node is not None
        else None
    )

    def size_of(tag: str) -> dict[str, int] | None:
        node = pic.find(f"{{{HP}}}{tag}")
        if node is None:
            return None
        try:
            return {
                "width": int(node.get("width", "0")),
                "height": int(node.get("height", "0")),
            }
        except (TypeError, ValueError):
            return None

    shape_comment = pic.find(f"{{{HP}}}shapeComment")
    description = "".join(shape_comment.itertext()).strip() if shape_comment is not None else ""

    return {
        "image_id": image_id,
        "source_path": source_path,
        "binary_item_id": binary_item_id,
        "image_path": None,
        "page_number": page_number,
        "physical_page_number": physical_page_number,
        "page_number_source": page_number_source,
        "original_size": size_of("orgSz"),
        "display_size": size_of("curSz") or size_of("sz"),
        "description": description,
    }


def parse_border_fill_colors(header) -> dict[str, str]:
    """HWPX borderFill의 단색 채우기 정보를 셀 배경색으로 변환한다."""

    colors = {}
    for border_fill in header.iter(f"{{{HH}}}borderFill"):
        fill_id = border_fill.get("id")
        brush = border_fill.find(f"{{{HC}}}fillBrush")
        win_brush = brush.find(f"{{{HC}}}winBrush") if brush is not None else None
        color = win_brush.get("faceColor") if win_brush is not None else None
        if fill_id and color and color.lower() != "none":
            colors[fill_id] = color

    return colors


def parse_table(
    tbl,
    table_id: str,
    border_fill_colors: dict[str, str],
    page_number: int | None,
    physical_page_number: int | None,
    page_number_source: str,
) -> dict[str, Any]:
    """
    HWPX <hp:tbl> 하나를 구조화한다.
    병합 셀의 rowSpan / colSpan을 유지한다.
    """

    rows = []

    for tr in tbl.findall(f"./{{{HP}}}tr"):

        row_cells = []

        for tc in tr.findall(f"./{{{HP}}}tc"):

            addr = tc.find(f"{{{HP}}}cellAddr")
            span = tc.find(f"{{{HP}}}cellSpan")
            size = tc.find(f"{{{HP}}}cellSz")

            row = int(addr.get("rowAddr", "0")) if addr is not None else 0
            col = int(addr.get("colAddr", "0")) if addr is not None else 0

            row_span = (
                int(span.get("rowSpan", "1"))
                if span is not None
                else 1
            )

            col_span = (
                int(span.get("colSpan", "1"))
                if span is not None
                else 1
            )

            width = (
                int(size.get("width", "0"))
                if size is not None
                else 0
            )

            height = (
                int(size.get("height", "0"))
                if size is not None
                else 0
            )

            cell_id = (
                f"{table_id}_r{row}_c{col}"
            )
            border_fill_id = tc.get("borderFillIDRef")

            row_cells.append(
                {
                    "cell_id": cell_id,
                    "row": row,
                    "col": col,
                    "row_span": row_span,
                    "col_span": col_span,
                    "width": width,
                    "height": height,
                    "border_fill_id": border_fill_id,
                    "background_color": border_fill_colors.get(border_fill_id),
                    "text": text_from_cell(tc),
                }
            )

        rows.append(row_cells)

    return {
        "table_id": table_id,
        "image_path": None,
        "render_status": "not_rendered",
        "page_number": page_number,
        "physical_page_number": physical_page_number,
        "page_number_source": page_number_source,
        "row_count": int(tbl.get("rowCnt", len(rows))),
        "col_count": int(tbl.get("colCnt", 0)),
        "cells": rows,
    }


def add_table_context(document: dict[str, Any], context_size: int = 3) -> None:
    """각 표에 같은 섹션의 앞뒤 문단을 LLM용 문맥으로 연결한다."""

    blocks = document["blocks"]

    for index, block in enumerate(blocks):
        if block["type"] != "table":
            continue

        before = [
            candidate["text"]
            for candidate in blocks[:index]
            if candidate["type"] == "paragraph"
            and candidate["section"] == block["section"]
            and candidate.get("parent_block_id") == block.get("parent_block_id")
            and candidate.get("parent_cell_id") == block.get("parent_cell_id")
        ][-context_size:]

        after = [
            candidate["text"]
            for candidate in blocks[index + 1:]
            if candidate["type"] == "paragraph"
            and candidate["section"] == block["section"]
            and candidate.get("parent_block_id") == block.get("parent_block_id")
            and candidate.get("parent_cell_id") == block.get("parent_cell_id")
        ][:context_size]

        block["context_before"] = before
        block["context_after"] = after
        block["image_path"] = block["table"]["image_path"]
        block["table"]["context_before"] = before
        block["table"]["context_after"] = after


def render_node_text(node: dict[str, Any]) -> str:
    """pyhwpxlib 렌더 트리 노드의 TextRun 텍스트를 순서대로 결합한다."""

    if node.get("type") == "TextRun":
        return str(node.get("text", ""))
    return "".join(render_node_text(child) for child in node.get("children", []))


def page_number_from_render_tree(page_tree: dict[str, Any]) -> int | None:
    """렌더 트리의 바닥글에서 실제 표시되는 쪽번호를 읽는다."""

    def visit(node: dict[str, Any]):
        if node.get("type") == "Footer":
            yield node
        for child in node.get("children", []):
            yield from visit(child)

    for footer in visit(page_tree):
        # 바닥글에 문서명 등이 함께 있어도 독립된 쪽번호 줄을 읽는다.
        for line in footer.get("children", []):
            match = PAGE_NUMBER_PATTERN.fullmatch(render_node_text(line).strip())
            if match:
                return int(match.group(1))
    return None


def section_files_in_order(names) -> list[str]:
    return sorted(
        (name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name)),
        key=lambda name: int(re.search(r"section(\d+)", name).group(1)),
    )


def rendered_section_index(document, page_index: int) -> int:
    """Python bridge에 아직 없는 getPageInfo WASM API를 좁게 감싼다."""
    engine = document._engine
    get_info = engine._exports.get("hwpdocument_getPageInfo")
    if get_info is None:
        raise RuntimeError("구역별 페이지 매핑을 지원하는 pyhwpxlib이 필요합니다.")
    ptr, length, _, failed = get_info(engine._store, document._handle, page_index)
    if failed:
        raise RuntimeError(f"{page_index + 1}쪽의 구역 정보를 읽을 수 없습니다.")
    try:
        return int(json.loads(engine._mem_read(ptr, length))["sectionIndex"])
    finally:
        document._wb_free(engine._store, ptr, length, 1)


def read_rendered_pagination(
    path: Path, rendered_document=None,
) -> tuple[dict[int, list[int]], list[dict[str, Any]]]:
    """전역 문단 인덱스→실제 페이지 목록과 하단에 표시된 쪽번호를 읽는다."""
    try:
        from pyhwpxlib.rhwp_bridge import RhwpEngine
    except ImportError as error:
        raise RuntimeError("정확한 쪽번호를 읽으려면 pyhwpxlib이 필요합니다.") from error

    offsets = []
    offset = 0
    with zipfile.ZipFile(path) as archive:
        for name in section_files_in_order(archive.namelist()):
            offsets.append(offset)
            section = ET.fromstring(archive.read(name))
            if section.tag != f"{{{HS}}}sec":
                section = section.find(f".//{{{HS}}}sec")
            if section is not None:
                offset += len(section.findall(f"./{{{HP}}}p"))

    renderer = rendered_document or RhwpEngine().load(str(path))
    page_map: dict[int, list[int]] = {}
    pages = []
    try:
        for page_index in range(renderer.page_count):
            physical_page = page_index + 1
            tree = renderer.get_page_render_tree(page_index)
            section_index = rendered_section_index(renderer, page_index)
            section_offset = offsets[section_index]
            printed_number = page_number_from_render_tree(tree)
            pages.append({
                "physical_page_number": physical_page,
                "page_number": printed_number,
                "page_number_source": "rendered_footer" if printed_number is not None else "page_number_unavailable",
            })

            def collect(node):
                if node.get("type") in ("Header", "Footer", "PageBg"):
                    return
                paragraph_index = node.get("pi")
                if paragraph_index is not None:
                    index = section_offset + int(paragraph_index)
                    mapped = page_map.setdefault(index, [])
                    if physical_page not in mapped:
                        mapped.append(physical_page)
                    # 셀 내부의 pi는 별도 번호이므로 부모 표의 페이지를 사용한다.
                    return
                for child in node.get("children", []):
                    collect(child)

            collect(tree)
    finally:
        if rendered_document is None:
            renderer.close()
    return page_map, pages


def apply_rendered_pagination(
    document: dict[str, Any],
    page_map: dict[int, list[int]],
    pages: list[dict[str, Any]],
) -> None:
    """단일 번호는 시작 페이지, 여러 쪽에 걸친 요소는 페이지 목록도 저장한다."""
    page_lookup = {page["physical_page_number"]: page for page in pages}
    document["pages"] = pages
    document["pagination_source"] = "render_tree"
    for block in document["blocks"]:
        mapped = page_map.get(block.get("source_paragraph_index"), [])
        metadata = dict(page_lookup[mapped[0]]) if mapped else {
            "physical_page_number": None,
            "page_number": None,
            "page_number_source": "page_number_unavailable",
        }
        metadata["physical_page_numbers"] = list(mapped)
        metadata["page_numbers"] = [page_lookup[page]["page_number"] for page in mapped]
        block.update(metadata)
        for key in ("table", "image"):
            if key in block:
                block[key].update(metadata)


def parse_rendered_table(
    node: dict[str, Any],
    table_id: str,
    page_number: int | None,
    physical_page_number: int | None,
) -> dict[str, Any]:
    """pyhwpxlib이 제공하는 HWP 표 셀 구조를 파싱한다."""

    cells_by_row: dict[int, list[dict[str, Any]]] = {}
    max_col = -1
    for cell_node in node.get("children", []):
        if cell_node.get("type") != "Cell":
            continue

        row = int(cell_node.get("row", 0))
        col = int(cell_node.get("col", 0))
        max_col = max(max_col, col)
        cell_paragraphs = [
            render_node_text(line).strip()
            for line in cell_node.get("children", [])
            if line.get("type") == "TextLine" and render_node_text(line).strip()
        ]
        cells_by_row.setdefault(row, []).append(
            {
                "cell_id": f"{table_id}_r{row}_c{col}",
                "row": row,
                "col": col,
                "row_span": 1,
                "col_span": 1,
                "width": 0,
                "height": 0,
                "border_fill_id": None,
                "background_color": None,
                "text": "\n".join(cell_paragraphs),
            }
        )

    rows = [sorted(cells_by_row[row], key=lambda cell: cell["col"])
            for row in sorted(cells_by_row)]
    return {
        "table_id": table_id,
        "image_path": None,
        "render_status": "not_rendered",
        "page_number": page_number,
        "physical_page_number": physical_page_number,
        "page_number_source": "page_number_field" if page_number is not None else "page_number_unavailable",
        "row_count": int(node.get("rows", len(rows))),
        "col_count": int(node.get("cols", max_col + 1)),
        "cells": rows,
    }


def parse_hwp(path: str | Path) -> dict[str, Any]:
    """pyhwpxlib의 실제 렌더 트리에서 HWP 5.x 문단과 표를 추출한다."""

    try:
        from pyhwpxlib.rhwp_bridge import RhwpEngine
    except ImportError as error:
        raise RuntimeError(
            "HWP 파일을 파싱하려면 프로젝트 환경에 pyhwpxlib이 필요합니다."
        ) from error

    path = Path(path)
    engine = RhwpEngine().load(str(path))
    document = {"filename": path.name, "blocks": [], "tables": [], "images": []}
    block_index = 0
    table_index = 0
    image_index = 0

    try:
        for page_index in range(engine.page_count):
            physical_page_number = page_index + 1
            page_tree = engine.get_page_render_tree(page_index)
            page_number = page_number_from_render_tree(page_tree)
            columns: list[dict[str, Any]] = []

            def collect_columns(node: dict[str, Any]) -> None:
                if node.get("type") == "Column":
                    columns.append(node)
                    return
                for child in node.get("children", []):
                    collect_columns(child)

            collect_columns(page_tree)

            for column in columns:
                section = f"page_{physical_page_number}"

                paragraph_index: int | None = None
                paragraph_parts: list[str] = []
                paragraph_images: list[dict[str, Any]] = []

                def append_image() -> None:
                    nonlocal block_index, image_index
                    image_index += 1
                    image_id = f"img_{image_index:04d}"
                    image = {
                        "image_id": image_id,
                        "source_path": None,
                        "binary_item_id": None,
                        "image_path": None,
                        "page_number": page_number,
                        "physical_page_number": physical_page_number,
                        "page_number_source": "page_number_field" if page_number is not None else "page_number_unavailable",
                        "original_size": None,
                        "display_size": None,
                        "description": "HWP 원본 이미지 데이터는 렌더 미리보기에서만 표시됩니다.",
                    }
                    block_index += 1
                    document["blocks"].append(
                        {
                            "block_id": image_id,
                            "type": "image",
                            "section": section,
                            "text": image["description"],
                            "image_index": image_index,
                            "page_number": page_number,
                            "physical_page_number": physical_page_number,
                            "page_number_source": image["page_number_source"],
                            "image_path": None,
                            "image": image,
                        }
                    )
                    document["images"].append(image)

                def flush_paragraph() -> None:
                    nonlocal block_index, paragraph_index
                    text = "".join(paragraph_parts).strip()
                    if text:
                        block_index += 1
                        document["blocks"].append(
                            {
                                "block_id": f"p_{block_index:04d}",
                                "type": "paragraph",
                                "section": section,
                                "text": text,
                                "page_number": page_number,
                                "physical_page_number": physical_page_number,
                                "page_number_source": "page_number_field" if page_number is not None else "page_number_unavailable",
                            }
                        )
                    paragraph_parts.clear()
                    paragraph_index = None
                    for _ in paragraph_images:
                        append_image()
                    paragraph_images.clear()

                for node in column.get("children", []):
                    node_type = node.get("type")
                    if node_type == "TextLine":
                        current_index = node.get("pi")
                        if (paragraph_parts or paragraph_images) and current_index != paragraph_index:
                            flush_paragraph()
                        paragraph_index = current_index
                        paragraph_parts.append(render_node_text(node))
                        paragraph_images.extend(
                            child for child in node.get("children", [])
                            if child.get("type") == "Image"
                        )
                    elif node_type == "Table":
                        flush_paragraph()
                        table_index += 1
                        table_id = f"tbl_{table_index:04d}"
                        table = parse_rendered_table(
                            node, table_id, page_number, physical_page_number
                        )
                        block_index += 1
                        document["blocks"].append(
                            {
                                "block_id": table_id,
                                "type": "table",
                                "section": section,
                                "table_index": table_index,
                                "page_number": page_number,
                                "physical_page_number": physical_page_number,
                                "page_number_source": table["page_number_source"],
                                "table": table,
                            }
                        )
                        document["tables"].append(table)
                    elif node_type == "Image":
                        flush_paragraph()
                        append_image()

                flush_paragraph()

    finally:
        engine.close()

    add_table_context(document)
    return document


def parse_hwpx(
    path: str | Path, *, output_dir: str | Path | None = None,
    paginate: bool = True, strict_pagination: bool = False,
) -> dict[str, Any]:
    """원본 계층을 유지하면서 문단, 표, 셀 내부 그림을 한 번씩 추출한다."""
    path = Path(path)
    document_id = hashlib.sha256(path.read_bytes()).hexdigest()
    asset_dir = Path(output_dir or Path(__file__).resolve().parent / "output") / "images" / document_id
    document = {
        "document_id": document_id, "source_sha256": document_id,
        "filename": path.name, "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "blocks": [], "tables": [], "images": [], "assets": [],
        "warnings": [], "pages": [], "pagination_source": "unavailable",
    }
    counts: Counter = Counter()
    assets = {}

    def warn(code, **details):
        document["warnings"].append({"code": code, **details})

    with zipfile.ZipFile(path) as archive:
        # 압축 해제 폭증을 파싱 전에 제한한다. 이미지도 직접 읽고 지정 경로에만 쓴다.
        members = archive.infolist()
        if len(members) > 10000 or sum(i.file_size for i in members) > 512 * 1024 * 1024:
            raise ValueError("HWPX 압축 해제 크기 또는 항목 수 제한을 초과했습니다.")
        zip_names = set(archive.namelist())
        colors = {}
        fills = {}
        if "Contents/header.xml" in zip_names:
            header = ET.fromstring(archive.read("Contents/header.xml"))
            colors = parse_border_fill_colors(header)
            for fill in header.iter(f"{{{HH}}}borderFill"):
                brush = fill.find(f"{{{HC}}}fillBrush")
                fills[fill.get("id")] = {
                    "fill_types": [n.tag.rsplit("}", 1)[-1] for n in brush] if brush is not None else [],
                    "source_xml": ET.tostring(fill, encoding="unicode"),
                }
        else:
            warn("missing_header")
        manifest = {}
        if "Contents/content.hpf" in zip_names:
            package = ET.fromstring(archive.read("Contents/content.hpf"))
            for item in package.iter():
                if item.tag.rsplit("}", 1)[-1] == "item":
                    href = item.get("href", "").removeprefix("./")
                    manifest[item.get("id")] = (href, item.get("media-type"))

        section_files = section_files_in_order(zip_names)
        if not section_files:
            raise ValueError("HWPX 구역 XML을 찾을 수 없습니다.")
        paragraph_offset = 0
        for section_file in section_files:
            root = ET.fromstring(archive.read(section_file))
            sec = root if root.tag == f"{{{HS}}}sec" else root.find(f".//{{{HS}}}sec")
            if sec is None:
                warn("missing_section_root", section=section_file)
                continue
            locations = {}

            def locate(node, location):
                locations[node] = location
                siblings = Counter()
                for child in node:
                    local = child.tag.rsplit("}", 1)[-1]
                    siblings[local] += 1
                    locate(child, f"{location}/{local}[{siblings[local]}]")

            locate(sec, "/sec[1]")

            def base(kind, node, source_index, parent_block, parent_cell):
                counts[kind] += 1
                prefix = {"paragraph": "p", "table": "tbl", "image": "img"}[kind]
                return {
                    "block_id": f"{prefix}_{counts[kind]:04d}", "type": kind,
                    "order": len(document["blocks"]), "section": section_file,
                    "source_paragraph_index": source_index,
                    "source_xml_path": locations[node],
                    "parent_block_id": parent_block, "parent_cell_id": parent_cell,
                    "page_number": None, "physical_page_number": None,
                    "page_number_source": "page_number_unavailable",
                    "physical_page_numbers": [], "page_numbers": [],
                    "page_mapping_level": "paragraph",
                }

            def parse_paragraph(p, source_index, parent_block=None, parent_cell=None):
                created = []
                parts = []
                runs = []

                def flush():
                    value = "".join(parts).strip()
                    if value:
                        block = base("paragraph", p, source_index, parent_block, parent_cell)
                        block.update(text=value, runs=list(runs),
                                     paragraph_style_id=p.get("styleIDRef"),
                                     paragraph_properties_id=p.get("paraPrIDRef"))
                        document["blocks"].append(block)
                        created.append(block["block_id"])
                    parts.clear()
                    runs.clear()

                def visit(node, char_style=None):
                    if node.tag == f"{{{HP}}}run":
                        char_style = node.get("charPrIDRef")
                    if node.tag in {f"{{{HP}}}t", f"{{{HP}}}tab", f"{{{HP}}}lineBreak"}:
                        value = text_content(node)
                        parts.append(value)
                        runs.append({"text": value, "char_properties_id": char_style})
                        return
                    if node.tag == f"{{{HP}}}tbl":
                        flush()
                        block = base("table", node, source_index, parent_block, parent_cell)
                        table = parse_table(node, block["block_id"], colors, None, None, "page_number_unavailable")
                        block.update(table=table, table_index=counts["table"])
                        document["blocks"].append(block)
                        document["tables"].append(table)
                        created.append(block["block_id"])
                        for tr, row in zip(node.findall(f"./{{{HP}}}tr"), table["cells"]):
                            for tc, cell in zip(tr.findall(f"./{{{HP}}}tc"), row):
                                cell["size_unit"] = "hwpunit"
                                cell["fill"] = fills.get(cell["border_fill_id"])
                                cell["child_block_ids"] = []
                                for cp in tc.findall(f"./{{{HP}}}subList/{{{HP}}}p"):
                                    cell["child_block_ids"].extend(parse_paragraph(
                                        cp, source_index, block["block_id"], cell["cell_id"]
                                    ))
                        return
                    if node.tag == PIC_TAG:
                        flush()
                        block = base("image", node, source_index, parent_block, parent_cell)
                        img = node.find(f".//{IMG_TAG}")
                        binary_id = img.get("binaryItemIDRef") if img is not None else None
                        source, mime = manifest.get(binary_id, (None, None))
                        if source not in zip_names:
                            source = find_binary_path(zip_names, binary_id)
                        picture = parse_image(node, block["block_id"], None, None,
                                              "page_number_unavailable", source)
                        picture.update(asset_id=None, extraction_status="missing_binary", size_unit="hwpunit")
                        if source:
                            data = archive.read(source)
                            digest = hashlib.sha256(data).hexdigest()
                            suffix = Path(source).suffix.lower()
                            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                                suffix = ".bin"
                            asset_dir.mkdir(parents=True, exist_ok=True)
                            target = asset_dir / f"{digest}{suffix}"
                            target.write_bytes(data)
                            picture.update(asset_id=digest, image_path=str(target.resolve()), extraction_status="extracted")
                            assets[digest] = {
                                "asset_id": digest, "sha256": digest, "kind": "image",
                                "mime_type": mime or mimetypes.guess_type(source)[0] or "application/octet-stream",
                                "byte_size": len(data), "local_path": str(target.resolve()),
                            }
                        else:
                            warn("missing_image_binary", block_id=block["block_id"])
                        block.update(image=picture, image_index=counts["image"],
                                     image_path=picture["image_path"], text=picture["description"])
                        document["blocks"].append(block)
                        document["images"].append(picture)
                        created.append(block["block_id"])
                        return
                    if node.tag == f"{{{HP}}}subList":
                        flush()
                        for cp in node.findall(f"./{{{HP}}}p"):
                            created.extend(parse_paragraph(cp, source_index, parent_block, parent_cell))
                        return
                    # 머리말·꼬리말은 본문 검색에 중복 삽입하지 않는다.
                    if node.tag in {f"{{{HP}}}header", f"{{{HP}}}footer"}:
                        return
                    for child in node:
                        visit(child, char_style)

                for run in p.findall(f"./{{{HP}}}run"):
                    visit(run)
                flush()
                return created

            direct = sec.findall(f"./{{{HP}}}p")
            for index, paragraph in enumerate(direct):
                parse_paragraph(paragraph, paragraph_offset + index)
            paragraph_offset += len(direct)

    document["assets"] = list(assets.values())
    if paginate:
        try:
            page_map, pages = read_rendered_pagination(path)
            apply_rendered_pagination(document, page_map, pages)
        except Exception as error:
            if strict_pagination:
                raise
            warn("pagination_failed", error_type=type(error).__name__)
    else:
        warn("pagination_disabled")
    add_table_context(document)
    return document

if __name__ == "__main__":

    base_dir = Path(__file__).resolve().parent
    project_python = base_dir / ".venv/bin/python"
    if project_python.is_file() and os.path.abspath(sys.executable) != str(project_python):
        os.execv(str(project_python), [str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]])

    parser = argparse.ArgumentParser(
        description="HWP/HWPX 문서를 파싱"
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        type=Path,
        help="파싱할 HWP 또는 HWPX 파일 경로. 생략하면 input 폴더에서 자동 선택합니다.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="파싱 결과 JSON 경로",
    )
    parser.add_argument("--env-file", type=Path, default=base_dir / ".env", help="MongoDB 설정 파일")
    parser.add_argument("--database", help="MongoDB 데이터베이스명 (설정/URI보다 우선)")
    parser.add_argument("--collection", default="Request", help="추출 문서 컬렉션명 (기본: Request)")
    parser.add_argument("--no-mongo", action="store_true", help="MongoDB 저장 없이 로컬 결과만 생성")
    parser.add_argument("--store-source", action="store_true", help="원본 HWP/HWPX 파일도 GridFS에 저장")
    parser.add_argument("--no-pagination", action="store_true", help="HWPX 페이지 렌더링 생략")
    parser.add_argument("--strict-pagination", action="store_true", help="페이지 매핑 실패 시 중단")
    parser.add_argument("--asset-dir", type=Path, default=base_dir / "output", help="이미지 출력 상위 폴더")
    args = parser.parse_args()

    if args.input_path is None:
        candidates = sorted((base_dir / "input").glob("*.hwpx"))
        candidates.extend(sorted((base_dir / "input").glob("*.hwp")))
        if not candidates:
            parser.error(f"input 폴더에서 HWP 또는 HWPX 파일을 찾을 수 없습니다: {base_dir / 'input'}")
        input_path = candidates[0]
    else:
        input_path = args.input_path
        if not input_path.is_absolute() and not input_path.is_file():
            input_path = base_dir / input_path

    if not input_path.is_file():
        parser.error(f"입력 파일을 찾을 수 없습니다: {input_path}")

    try:
        if input_path.suffix.lower() == ".hwp":
            result = parse_hwp(input_path)
            digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
            result.update(document_id=digest, source_sha256=digest,
                          parser_version=PARSER_VERSION, schema_version=SCHEMA_VERSION,
                          assets=[], warnings=[{"code": "hwp_render_only",
                          "detail": "HWP는 병합·음영·이미지 바이너리 추출을 지원하지 않습니다."}])
            for index, block in enumerate(result["blocks"]):
                block["order"] = index
                block["physical_page_numbers"] = [block["physical_page_number"]]
                block["page_numbers"] = [block["page_number"]]
        elif input_path.suffix.lower() == ".hwpx":
            result = parse_hwpx(input_path, output_dir=args.asset_dir,
                                paginate=not args.no_pagination,
                                strict_pagination=args.strict_pagination)
        else:
            parser.error(f"지원하지 않는 파일 형식입니다: {input_path.suffix}")
    except Exception as error:
        parser.exit(1, f"파싱 실패 ({type(error).__name__}). 입력 파일과 렌더링 환경을 확인하세요.\n")

    output_json = args.output_json or base_dir / "output/json/parsed.json"
    if not output_json.is_absolute():
        output_json = base_dir / output_json

    output_json.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    output_json.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print("파싱 완료")
    print("입력 파일:", input_path)
    print("결과 파일:", output_json)
    print(
        "표 개수:",
        len(result["tables"])
    )
    print("블록 개수:", len(result["blocks"]), "이미지 개수:", len(result["images"]))
    print("파싱 경고:", len(result.get("warnings", [])))
    if not args.no_mongo:
        from hwpx_storage import MongoStorageError, save_to_mongodb
        try:
            saved = save_to_mongodb(result, input_path, env_path=args.env_file, database=args.database,
                                    store_source=args.store_source, collection_name=args.collection)
        except MongoStorageError as error:
            parser.exit(1, f"{error}\n로컬 JSON은 보존되었습니다.\n")
        except Exception as error:
            # 접속 값이나 드라이버 예외 원문을 출력하지 않는다.
            parser.exit(1, f"MongoDB 저장 실패 ({type(error).__name__}). 로컬 JSON은 보존되었습니다.\n"
                          ".env의 URI, 네트워크 연결, 데이터베이스 쓰기 권한을 확인하세요.\n")
        print("MongoDB 저장 완료:", json.dumps(saved, ensure_ascii=False))
