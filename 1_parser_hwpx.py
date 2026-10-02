from __future__ import annotations

import json
import re
import zipfile
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
HC = "http://www.hancom.co.kr/hwpml/2011/core"
PAGE_NUMBER_TAG = f"{{{HP}}}pageNum"
PAGE_HIDING_TAG = f"{{{HP}}}pageHiding"
NEW_NUMBER_TAG = f"{{{HP}}}newNum"
PIC_TAG = f"{{{HP}}}pic"
IMG_TAG = f"{{{HC}}}img"
PAGE_NUMBER_PATTERN = re.compile(r"^\s*-?\s*(\d+)\s*-?\s*$")


def text_from_cell(cell) -> str:
    """
    셀 내부의 텍스트를 추출한다.
    줄바꿈도 최대한 보존한다.
    """

    paragraphs = []

    for p in cell.findall(f".//{{{HP}}}subList/{{{HP}}}p"):
        parts = []

        for node in p.iter():
            if node.tag == f"{{{HP}}}lineBreak":
                parts.append("\n")
            elif node.tag == f"{{{HP}}}t":
                parts.append(node.text or "")

        value = "".join(parts).strip()

        if value:
            paragraphs.append(value)

    return "\n".join(paragraphs)


def extract_page_number(paragraph) -> int | None:
    """문단에 명시된 쪽번호가 있으면 추출하고 없으면 None을 반환한다."""

    text = "".join(
        node.text or ""
        for node in paragraph.iter(f"{{{HP}}}t")
    )
    match = PAGE_NUMBER_PATTERN.match(text)

    return int(match.group(1)) if match else None


def first_line_vertical_position(paragraph) -> int | None:
    """문단의 첫 줄이 페이지에서 시작하는 세로 위치를 반환한다."""

    line = paragraph.find(f"{{{HP}}}linesegarray/{{{HP}}}lineseg")
    if line is None:
        return None

    try:
        return int(line.get("vertpos", ""))
    except ValueError:
        return None


def page_number_restart(paragraph) -> int | None:
    """문단에 있는 '새 쪽번호 시작' PAGE 필드의 값을 반환한다."""

    for node in paragraph.iter(NEW_NUMBER_TAG):
        if node.get("numType") != "PAGE":
            continue
        try:
            return int(node.get("num", ""))
        except (TypeError, ValueError):
            continue

    return None


def page_hiding_value(paragraph) -> bool | None:
    """문단에서 쪽번호 숨김 설정을 읽는다.

    pageHiding은 설정이 있는 문단의 페이지에 적용되므로, 설정이 없는
    문단에서는 None을 반환해 이전 상태를 유지할 수 있게 한다.
    """

    hiding_nodes = list(paragraph.iter(PAGE_HIDING_TAG))
    if not hiding_nodes:
        return None

    return hiding_nodes[-1].get("hidePageNum") == "1"


def section_start_page_number(sec) -> int | None:
    """섹션의 secPr/startNum에 지정된 시작 쪽번호를 읽는다."""

    start_num = sec.find(f".//{{{HP}}}secPr/{{{HP}}}startNum")
    if start_num is None:
        return None

    try:
        return int(start_num.get("page", ""))
    except (TypeError, ValueError):
        return None


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
    physical_page_number: int,
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
    extension = Path(source_path).suffix if source_path else ""
    image_path = (
        f"output/images/{binary_item_id}{extension}"
        if binary_item_id
        else f"output/images/{image_id}"
    )

    return {
        "image_id": image_id,
        "source_path": source_path,
        "binary_item_id": binary_item_id,
        "image_path": image_path,
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
    physical_page_number: int,
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
        "image_path": f"output/tables/{table_id}.png",
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
        ][-context_size:]

        after = [
            candidate["text"]
            for candidate in blocks[index + 1:]
            if candidate["type"] == "paragraph"
            and candidate["section"] == block["section"]
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
        match = PAGE_NUMBER_PATTERN.fullmatch(render_node_text(footer).strip())
        if match:
            return int(match.group(1))
    return None


def parse_rendered_table(
    node: dict[str, Any],
    table_id: str,
    page_number: int | None,
    physical_page_number: int,
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
        "image_path": f"output/tables/{table_id}.png",
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


def parse_hwpx(path: str | Path) -> dict[str, Any]:

    path = Path(path)

    document = {
        "filename": path.name,
        "blocks": [],
        "tables": [],
        "images": [],
    }

    with zipfile.ZipFile(path, "r") as z:

        header = ET.fromstring(z.read("Contents/header.xml"))
        border_fill_colors = parse_border_fill_colors(header)
        zip_names = set(z.namelist())

        section_files = sorted(
            name
            for name in z.namelist()
            if name.startswith("Contents/section")
            and name.endswith(".xml")
        )

        block_index = 0
        table_index = 0
        image_index = 0
        source_paragraph_offset = 0
        physical_page_number = 0
        printed_page_number: int | None = None
        page_number_configured = False

        for section_file in section_files:

            xml_data = z.read(section_file)

            root = ET.fromstring(xml_data)
            sec = root if root.tag == f"{{{HS}}}sec" else root.find(f".//{{{HS}}}sec")

            if sec is None:
                continue

            direct_paragraphs = set(sec.findall(f"./{{{HP}}}p"))
            source_paragraph_indices = {
                paragraph: source_paragraph_offset + index
                for index, paragraph in enumerate(sec.findall(f"./{{{HP}}}p"))
            }
            paragraphs = []

            for paragraph in sec.iter(f"{{{HP}}}p"):
                has_table = any(
                    run.findall(f"{{{HP}}}tbl")
                    for run in paragraph.findall(f"./{{{HP}}}run")
                )

                if paragraph in direct_paragraphs or has_table:
                    paragraphs.append(paragraph)

            section_start_number = section_start_page_number(sec)
            previous_vertical_position: int | None = None
            page_hidden = False

            # HWPX의 본문 흐름에 존재하는 top-level paragraph
            for paragraph_index, p in enumerate(paragraphs):
                current_vertical_position = first_line_vertical_position(p)
                automatic_page_break = (
                    current_vertical_position is not None
                    and previous_vertical_position is not None
                    and current_vertical_position < previous_vertical_position
                )
                starts_new_page = (
                    paragraph_index == 0
                    or p.get("pageBreak") == "1"
                    or automatic_page_break
                )

                if starts_new_page:
                    physical_page_number += 1
                    page_hidden = False

                    if printed_page_number is None:
                        printed_page_number = section_start_number
                        if printed_page_number is None:
                            printed_page_number = physical_page_number
                    elif paragraph_index != 0 or section_file != section_files[0]:
                        printed_page_number += 1

                if section_start_number is not None and paragraph_index == 0:
                    printed_page_number = section_start_number

                restart_number = page_number_restart(p)
                if restart_number is not None:
                    printed_page_number = restart_number

                if p.find(f".//{PAGE_NUMBER_TAG}") is not None:
                    page_number_configured = True

                hiding_value = page_hiding_value(p)
                if hiding_value is not None:
                    page_hidden = hiding_value

                if page_number_configured:
                    page_number = None if page_hidden else printed_page_number
                    page_number_source = (
                        "page_number_hidden"
                        if page_hidden
                        else "page_number_field"
                    )
                else:
                    # HWPX에 인쇄 쪽번호 필드가 없으면 실제 문서 쪽번호도
                    # 없는 것으로 둔다. 물리 페이지 위치는 별도 필드로 보존한다.
                    page_number = None
                    page_number_source = "page_number_unavailable"

                paragraph_parts = []

                for run in p.findall(f"./{{{HP}}}run"):

                    for child in run:

                        if child.tag == f"{{{HP}}}t":
                            paragraph_parts.append(child.text or "")

                        elif child.tag == f"{{{HP}}}lineBreak":
                            paragraph_parts.append("\n")

                        elif child.tag == f"{{{HP}}}tbl":

                            text = "".join(paragraph_parts).strip()
                            if text:
                                block_index += 1
                                document["blocks"].append(
                                    {
                                        "block_id": f"p_{block_index:04d}",
                                        "type": "paragraph",
                                        "section": section_file,
                                        "text": text,
                                        "source_paragraph_index": source_paragraph_indices.get(p),
                                        "page_number": page_number,
                                        "physical_page_number": physical_page_number,
                                        "page_number_source": page_number_source,
                                    }
                                )
                            paragraph_parts = []

                            table_index += 1

                            table_id = (
                                f"tbl_{table_index:04d}"
                            )

                            table = parse_table(
                                child,
                                table_id,
                                border_fill_colors,
                                page_number,
                                physical_page_number,
                                page_number_source,
                            )

                            block_index += 1

                            block = {
                                "block_id": table_id,
                                "type": "table",
                                "section": section_file,
                                "table_index": table_index,
                                "page_number": page_number,
                                "source_paragraph_index": source_paragraph_indices.get(p),
                                "physical_page_number": physical_page_number,
                                "page_number_source": page_number_source,
                                "table": table,
                            }

                            document["blocks"].append(block)
                            document["tables"].append(table)

                        elif child.tag == PIC_TAG:

                            if paragraph_parts:
                                text = "".join(paragraph_parts).strip()
                                if text:
                                    block_index += 1
                                    document["blocks"].append(
                                        {
                                            "block_id": f"p_{block_index:04d}",
                                            "type": "paragraph",
                                            "section": section_file,
                                            "text": text,
                                            "source_paragraph_index": source_paragraph_indices.get(p),
                                            "page_number": page_number,
                                            "physical_page_number": physical_page_number,
                                            "page_number_source": page_number_source,
                                        }
                                    )
                                paragraph_parts = []

                            image_index += 1
                            image_id = f"img_{image_index:04d}"
                            image_node = child.find(f".//{IMG_TAG}")
                            binary_item_id = (
                                image_node.get("binaryItemIDRef")
                                if image_node is not None
                                else None
                            )
                            source_path = find_binary_path(zip_names, binary_item_id)
                            image = parse_image(
                                child,
                                image_id,
                                page_number,
                                physical_page_number,
                                page_number_source,
                                source_path,
                            )

                            block_index += 1
                            block = {
                                "block_id": image_id,
                                "type": "image",
                                "section": section_file,
                                "text": image["description"],
                                "image_index": image_index,
                                "page_number": page_number,
                                "source_paragraph_index": source_paragraph_indices.get(p),
                                "physical_page_number": physical_page_number,
                                "page_number_source": page_number_source,
                                "image_path": image["image_path"],
                                "image": image,
                            }
                            document["blocks"].append(block)
                            document["images"].append(image)

                text = "".join(paragraph_parts).strip()
                if text:
                    block_index += 1
                    document["blocks"].append(
                        {
                            "block_id": f"p_{block_index:04d}",
                            "type": "paragraph",
                            "section": section_file,
                            "text": text,
                            "source_paragraph_index": source_paragraph_indices.get(p),
                            "page_number": page_number,
                            "physical_page_number": physical_page_number,
                            "page_number_source": page_number_source,
                        }
                    )

                previous_vertical_position = current_vertical_position

            source_paragraph_offset += len(source_paragraph_indices)

    add_table_context(document)

    return document


if __name__ == "__main__":

    base_dir = Path(__file__).resolve().parent

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

    if input_path.suffix.lower() == ".hwp":
        result = parse_hwp(input_path)
    elif input_path.suffix.lower() == ".hwpx":
        result = parse_hwpx(input_path)
    else:
        parser.error(f"지원하지 않는 파일 형식입니다: {input_path.suffix}")

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
