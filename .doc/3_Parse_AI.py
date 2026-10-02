from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel


INPUT_PATH = Path("output/json/parsed.json")
OUTPUT_PATH = Path("output/json/structured.json")
MODEL = "gpt-4o-mini"

ROMAN_HEADING = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.")
NUMBER_HEADING = re.compile(r"^\d+\.")


class DocumentMetadata(BaseModel):
    기관명: str
    문서제목: str
    연관단어: list[str]


class SectionSummary(BaseModel):
    요약: str
    검색어: list[str]


@dataclass
class TocItem:
    section_id: str
    title: str
    level: int
    toc_block_id: str
    parent_id: str | None


def normalize(text: str) -> str:
    return " ".join(text.split())


def heading_key(text: str) -> str:
    return re.sub(r"\s+", "", text)


def table_to_text(table: dict) -> str:
    """표를 LLM이 내용과 행 구성을 이해할 수 있는 텍스트로 바꾼다."""

    return "\n".join(
        " | ".join(cell["text"].replace("\n", " / ") for cell in row)
        for row in table["cells"]
    )


def block_to_text(block: dict) -> str:
    return block.get("text") or table_to_text(block["table"])


def toc_level(title: str) -> int:
    if ROMAN_HEADING.match(title):
        return 1
    if NUMBER_HEADING.match(title):
        return 2
    return 1


def extract_toc(blocks: list[dict]) -> tuple[list[TocItem], int]:
    """목차 이후 첫 번째 항목의 재등장 전까지를 문서 목차로 추출한다."""

    toc_start = next(
        (
            index
            for index, block in enumerate(blocks)
            if block["type"] == "paragraph" and normalize(block["text"]) == "목 차"
        ),
        None,
    )
    if toc_start is None:
        raise ValueError("parsed.json에서 '목차' 블록을 찾을 수 없습니다.")

    toc_blocks = []
    first_title = None
    content_start = None
    for index in range(toc_start + 1, len(blocks)):
        block = blocks[index]
        if block["type"] != "paragraph":
            continue
        title = normalize(block["text"])
        if not title:
            continue
        if first_title is None:
            first_title = title
        elif heading_key(title) == heading_key(first_title):
            content_start = index
            break
        toc_blocks.append(block)

    if not toc_blocks or content_start is None:
        raise ValueError("목차 종료 위치를 찾을 수 없습니다.")

    items = []
    ancestors: dict[int, str] = {}
    for block in toc_blocks:
        title = normalize(block["text"])
        level = toc_level(title)
        parent_id = ancestors.get(level - 1)
        section_id = f"toc_{len(items) + 1:03d}"
        items.append(TocItem(section_id, title, level, block["block_id"], parent_id))
        ancestors[level] = section_id
        for depth in list(ancestors):
            if depth > level:
                del ancestors[depth]

    return items, content_start


def split_content_by_toc(blocks: list[dict], toc_items: list[TocItem], content_start: int) -> dict[str, list[dict]]:
    """본문 제목의 재등장으로 각 블록을 가장 가까운 목차 항목에 연결한다."""

    item_by_title = {heading_key(item.title): item for item in toc_items}
    content = {item.section_id: [] for item in toc_items}
    active_item: TocItem | None = None

    for block in blocks[content_start:]:
        if block["type"] == "paragraph":
            matching_item = item_by_title.get(heading_key(normalize(block["text"])))
            if matching_item is not None:
                active_item = matching_item
                continue
        if active_item is not None:
            content[active_item.section_id].append(block)

    return content


def get_client() -> OpenAI:
    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    return OpenAI()


def extract_metadata(client: OpenAI, document: dict) -> DocumentMetadata:
    preview = [
        {"block_id": block["block_id"], "text": block_to_text(block)[:300]}
        for block in document["blocks"][:40]
    ]
    completion = client.beta.chat.completions.parse(
        model=MODEL,
        temperature=0,
        response_format=DocumentMetadata,
        messages=[
            {
                "role": "system",
                "content": "한국어 공공 문서에서 기관명, 문서 제목, 검색 키워드를 추출하세요. 확인할 수 없는 값은 '해당 없음'으로 작성하세요.",
            },
            {"role": "user", "content": json.dumps(preview, ensure_ascii=False)},
        ],
    )
    return completion.choices[0].message.parsed


def summarize_section(client: OpenAI, item: TocItem, blocks: list[dict]) -> SectionSummary:
    """목차 항목별 본문을 중복 없이 요약하고, 표는 표 ID로 참조한다."""

    sources = [
        {
            "block_id": block["block_id"],
            "type": block["type"],
            "text": block_to_text(block),
        }
        for block in blocks
    ]
    completion = client.beta.chat.completions.parse(
        model=MODEL,
        temperature=0,
        response_format=SectionSummary,
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 제안요청서 정리 전문가입니다. 제공된 목차 항목의 본문을 한 번만 통합해 "
                    "핵심 사실, 일정, 금액, 조건, 요구사항을 보존하여 정리하세요. 원문을 블록별로 반복하지 마세요. "
                    "표 내용은 핵심을 문장으로 쓰고 반드시 '[표: tbl_XXXX]' 형식으로 참조하세요. "
                    "본문이 없으면 요약에는 '해당 없음'을 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"목차 항목: {item.title}\n"
                    f"본문 블록: {json.dumps(sources, ensure_ascii=False)}"
                ),
            },
        ],
    )
    return completion.choices[0].message.parsed


def evidence_reference(block: dict) -> dict:
    """원문과 렌더링된 표 이미지로 돌아가기 위한 최소 근거를 남긴다."""

    reference = {"block_id": block["block_id"], "page_number": block["page_number"]}
    if block["type"] == "table":
        reference["table_id"] = block["table"]["table_id"]
        reference["image_path"] = block["image_path"]
    return reference


def build_structured_document(
    document: dict,
    metadata: DocumentMetadata,
    toc_items: list[TocItem],
    section_content: dict[str, list[dict]],
    summaries: dict[str, SectionSummary],
) -> dict:
    """목차 트리, 항목별 요약, 최소 근거 참조만 포함한 결과를 만든다."""

    sections = []
    for item in toc_items:
        summary = summaries[item.section_id]
        blocks = section_content[item.section_id]
        sections.append(
            {
                "section_id": item.section_id,
                "title": item.title,
                "level": item.level,
                "parent_id": item.parent_id,
                "toc_block_id": item.toc_block_id,
                "요약": summary.요약,
                "검색어": summary.검색어,
                "근거": [evidence_reference(block) for block in blocks],
            }
        )

    return {
        "기관명": metadata.기관명,
        "문서제목": metadata.문서제목,
        "연관단어": metadata.연관단어,
        "문서출처": document["filename"],
        "목차기반구조": sections,
        "원문데이터": "output/json/parsed.json",
    }


def main() -> None:
    document = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    toc_items, content_start = extract_toc(document["blocks"])
    section_content = split_content_by_toc(document["blocks"], toc_items, content_start)
    client = get_client()

    metadata = extract_metadata(client, document)
    summaries = {
        item.section_id: (
            summarize_section(client, item, section_content[item.section_id])
            if section_content[item.section_id]
            else SectionSummary(요약="하위 목차 참조", 검색어=[])
        )
        for item in toc_items
    }
    result = build_structured_document(document, metadata, toc_items, section_content, summaries)

    OUTPUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(f"목차 기반 구조화 완료: {OUTPUT_PATH}")
    print(f"목차 항목: {len(toc_items)}")


if __name__ == "__main__":
    main()
