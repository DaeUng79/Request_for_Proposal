from __future__ import annotations

import argparse
import base64
import html
import importlib
import importlib.util
import json
import mimetypes
import os
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("output/json/parsed.json")
DEFAULT_OUTPUT = Path("output/html/parsed_comparison.html")
PROJECT_ROOT = Path(__file__).resolve().parent


def use_project_environment() -> None:
    project_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not project_python.is_file():
        return
    if Path(os.path.abspath(sys.executable)) == Path(os.path.abspath(project_python)):
        return

    os.execv(
        str(project_python),
        [str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def find_source_document(requested: Path | None, document: dict[str, Any]) -> Path | None:
    if requested is not None:
        return requested if requested.is_file() else None

    expected_name = Path(document.get("filename", "")).name
    candidates = [
        Path("input") / expected_name,
        *sorted(Path("input").glob("*.hwpx")),
        *sorted(Path("input").glob("*.hwp")),
    ]
    return next((path for path in candidates if path.is_file()), None)


def image_data_uris(document: dict[str, Any], source_path: Path | None) -> dict[str, str]:
    if source_path is None:
        return {}

    images: dict[str, str] = {}
    try:
        with zipfile.ZipFile(source_path) as archive:
            names = set(archive.namelist())
            for image in document.get("images", []):
                source_name = image.get("source_path")
                if not source_name or source_name not in names:
                    binary_id = image.get("binary_item_id")
                    source_name = next(
                        (name for name in names if name.startswith("BinData/") and Path(name).stem == binary_id),
                        None,
                    )
                if not source_name:
                    continue
                mime_type = mimetypes.guess_type(source_name)[0] or "application/octet-stream"
                encoded = base64.b64encode(archive.read(source_name)).decode("ascii")
                images[image["image_id"]] = f"data:{mime_type};base64,{encoded}"
    except (OSError, zipfile.BadZipFile, KeyError):
        return {}
    return images


def render_table(table: dict[str, Any]) -> str:
    rows = []
    for row in table.get("cells", []):
        cells = []
        for cell in row:
            color = cell.get("background_color")
            color_value = str(color).strip() if color else ""
            css_color = color_value if color_value.startswith("#") else f"#{color_value}"
            style = (
                f' style="background-color: {esc(css_color)}" title="배경색 {esc(css_color)}"'
                if color_value
                else ""
            )
            text = esc(cell.get("text", "")).replace("\n", "<br>")
            cells.append(
                f'<td rowspan="{int(cell.get("row_span", 1))}" '
                f'colspan="{int(cell.get("col_span", 1))}"{style}>{text}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    table_id = esc(table.get("table_id", "표"))
    page = table.get("page_number")
    physical_page = table.get("physical_page_number")
    page_label = f"쪽 {esc(page)}" if page is not None else "쪽 번호 없음"
    if page is None and physical_page is not None:
        page_label += f" · 원본 {esc(physical_page)}쪽"
    return (
        f'<figure class="table-block"><figcaption>{table_id} · {page_label}</figcaption>'
        f'<div class="table-scroll"><table>{"".join(rows)}</table></div></figure>'
    )


def synchronize_page_numbers(
    blocks: list[dict[str, Any]],
    source_page_map: dict[int, list[int]],
) -> None:
    """렌더 엔진의 실제 쪽 매핑을 적용하고 추정 쪽번호의 누적 오차를 보정한다."""

    drift_anchor: int | None = None
    previous_source_number: int | None = None
    previous_actual_page: int | None = None

    for block in blocks:
        source_index = block.get("source_paragraph_index")
        if source_index is None:
            continue

        mapped_pages = source_page_map.get(int(source_index), [])
        if not mapped_pages:
            continue

        actual_page = min(mapped_pages)
        estimated_page = block.get("physical_page_number")
        block["physical_page_number"] = actual_page

        table = block.get("table")
        image = block.get("image")
        for nested in (table, image):
            if nested is not None:
                nested["physical_page_number"] = actual_page

        source_number = block.get("page_number")
        if (
            block.get("page_number_source") != "page_number_field"
            or not isinstance(source_number, int)
            or not isinstance(estimated_page, int)
        ):
            continue

        drift = actual_page - estimated_page
        if drift_anchor is None or (
            previous_actual_page is not None
            and actual_page > previous_actual_page
            and previous_source_number is not None
            and source_number < previous_source_number
        ):
            drift_anchor = drift

        corrected_number = source_number + drift - drift_anchor
        block["page_number"] = corrected_number
        if table is not None:
            table["page_number"] = corrected_number
        if image is not None:
            image["page_number"] = corrected_number

        previous_source_number = source_number
        previous_actual_page = actual_page


def render_block(block: dict[str, Any], images: dict[str, str]) -> str:
    block_type = block.get("type")
    block_id = esc(block.get("block_id", ""))
    if block_type == "table":
        return f'<div class="block" data-block-id="{block_id}">{render_table(block.get("table", {}))}</div>'
    if block_type == "image":
        image_id = block.get("image", {}).get("image_id", block.get("block_id"))
        source = images.get(image_id)
        if source:
            return (
                f'<figure class="image-block" data-block-id="{block_id}">'
                f'<img src="{source}" alt="{esc(block.get("text") or image_id)}" loading="lazy">'
                f'<figcaption>{block_id}</figcaption></figure>'
            )
        return f'<p class="missing-image" data-block-id="{block_id}">이미지를 불러올 수 없습니다. ({block_id})</p>'

    text = esc(block.get("text", "")).replace("\n", "<br>")
    if not text:
        return ""
    return f'<p class="paragraph-block" data-block-id="{block_id}">{text}</p>'


def render_pages(
    blocks: list[dict[str, Any]],
    images: dict[str, str],
    source_page_map: dict[int, list[int]],
) -> str:
    pages: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        source_index = block.get("source_paragraph_index")
        mapped_pages = source_page_map.get(int(source_index), []) if source_index is not None else []
        if mapped_pages:
            for page in mapped_pages:
                pages.setdefault(page, []).append(block)
            continue

        fallback_page = block.get("physical_page_number") or block.get("page_number")
        if fallback_page is not None:
            pages.setdefault(int(fallback_page), []).append(block)

    rendered = []
    for page, page_blocks in sorted(pages.items()):
        content = "".join(render_block(block, images) for block in page_blocks)
        rendered.append(
            f'<template class="parsed-page" data-page="{page}">{content}</template>'
        )
    return "".join(rendered)


def convert_pdf_to_svgs(pdf_path: Path, svg_dir: Path) -> int:
    try:
        pymupdf = importlib.import_module("pymupdf")
    except ImportError as error:
        raise RuntimeError("PDF를 SVG로 변환하려면 PyMuPDF가 필요합니다. `pip install PyMuPDF`로 설치하세요.") from error

    svg_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            svg = page.get_svg_image(text_as_path=False)
            (svg_dir / f"original_page_{page_index:03d}.svg").write_text(svg, encoding="utf-8")
        return len(pdf)


def convert_hwpx_to_svgs(
    hwpx_path: Path,
    svg_dir: Path,
) -> tuple[int, dict[int, list[int]]]:
    try:
        from pyhwpxlib.rhwp_bridge import RhwpEngine
    except ImportError as error:
        raise RuntimeError(
            "HWPX를 SVG로 직접 변환하려면 pyhwpxlib의 미리보기 엔진이 필요합니다. "
            "현재 프로젝트 가상환경에서 pyhwpxlib을 설치하거나 갱신하세요."
        ) from error

    svg_dir.mkdir(parents=True, exist_ok=True)
    document = RhwpEngine().load(str(hwpx_path))
    page_map: dict[int, list[int]] = {}
    try:
        existing_svgs = list(svg_dir.glob("original_page_*.svg"))
        reuse_svgs = (
            len(existing_svgs) == document.page_count
            and all(
                (svg_dir / f"original_page_{page:03d}.svg").is_file()
                and (svg_dir / f"original_page_{page:03d}.svg").stat().st_mtime_ns
                >= hwpx_path.stat().st_mtime_ns
                for page in range(1, document.page_count + 1)
            )
        )
        if not reuse_svgs:
            for old_svg in existing_svgs:
                old_svg.unlink()

        for page_index in range(document.page_count):
            page_number = page_index + 1
            tree = document.get_page_render_tree(page_index)

            def collect_paragraph_indices(node: dict[str, Any]) -> None:
                if node.get("type") == "Column":
                    for child in node.get("children", []):
                        paragraph_index = child.get("pi")
                        if paragraph_index is not None:
                            page_list = page_map.setdefault(int(paragraph_index), [])
                            if page_number not in page_list:
                                page_list.append(page_number)
                    return
                for child in node.get("children", []):
                    collect_paragraph_indices(child)

            collect_paragraph_indices(tree)
            if not reuse_svgs:
                svg = document.render_page_svg(page_index, embed_fonts=True)
                (svg_dir / f"original_page_{page_number:03d}.svg").write_text(
                    svg,
                    encoding="utf-8",
                )
    finally:
        document.close()

    return page_number, page_map


def build_html(
    document: dict[str, Any],
    source_path: Path | None,
    svg_page_count: int = 0,
    svg_dir: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
    source_page_map: dict[int, list[int]] | None = None,
) -> str:
    blocks = document.get("blocks", [])
    if source_page_map:
        synchronize_page_numbers(blocks, source_page_map)
    counts = Counter(block.get("type", "기타") for block in blocks)
    images = image_data_uris(document, source_path)
    pages_html = render_pages(blocks, images, source_page_map or {})
    parsed_page_numbers = {
        int(block.get("physical_page_number") or block.get("page_number"))
        for block in blocks
        if block.get("physical_page_number") or block.get("page_number")
    }
    page_count = max([svg_page_count, *parsed_page_numbers], default=0)
    page_options = "".join(
        f'<option value="{page}">쪽 {page}</option>'
        for page in range(1, page_count + 1)
    )
    svg_paths = {}
    svg_sources = {}
    if svg_dir is not None and svg_page_count:
        for page in range(1, svg_page_count + 1):
            svg_file = svg_dir / f"original_page_{page:03d}.svg"
            if not svg_file.is_file():
                continue
            svg_paths[str(page)] = os.path.relpath(
                svg_file.resolve(), output_path.parent.resolve()
            ).replace(os.sep, "/")
            svg_sources[str(page)] = (
                "data:image/svg+xml;base64,"
                + base64.b64encode(svg_file.read_bytes()).decode("ascii")
            )
    svg_paths_json = json.dumps(svg_paths, ensure_ascii=False)
    svg_sources_json = json.dumps(svg_sources, ensure_ascii=False)
    title = Path(document.get("filename", "파싱 결과")).stem
    source_name = source_path.name if source_path else document.get("filename", "원본 HWPX")
    source_link = ""
    if source_path is not None:
        source_href = os.path.relpath(source_path.resolve(), output_path.parent.resolve()).replace(os.sep, "/")
        source_link = f'<a class="source-link" href="{esc(source_href)}" download>원본 문서 다운로드</a>'

    return f'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} · 원문 비교</title>
  <style>
    :root {{ color-scheme: light; --ink:#172a35; --muted:#647780; --line:#d8e1e4; --teal:#087e78; --paper:#f3f7f7; }}
    * {{ box-sizing:border-box; }}
    [hidden] {{ display:none !important; }}
    body {{ margin:0; color:var(--ink); background:var(--paper); font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif; }}
    .topbar {{ position:sticky; top:0; z-index:5; display:flex; align-items:center; justify-content:space-between; gap:20px; padding:14px 24px; color:#fff; background:#153a46; box-shadow:0 3px 14px #153a4622; }}
    .topbar h1 {{ margin:0; font-size:17px; font-weight:700; }}
    .topbar p {{ margin:4px 0 0; color:#c8d9dc; font-size:12px; }}
    .stats {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .stat {{ border:1px solid #ffffff38; border-radius:999px; padding:5px 10px; color:#e6f3f0; font-size:12px; white-space:nowrap; }}
    .compare {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; padding:16px; height:calc(100vh - 82px); min-height:540px; }}
    .panel {{ display:flex; flex-direction:column; min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:12px; background:#fff; box-shadow:0 4px 18px #193d4510; }}
    .panel-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:13px 16px; border-bottom:1px solid var(--line); background:#fbfdfd; }}
    .panel-head h2 {{ margin:0; font-size:14px; }}
    .panel-head small {{ display:block; margin-top:3px; color:var(--muted); font-size:11px; }}
    .page-controls {{ display:flex; align-items:center; gap:7px; }}
    .file-label,.source-link {{ display:inline-flex; align-items:center; justify-content:center; min-height:34px; padding:0 11px; border:1px solid #b8d4d2; border-radius:7px; color:#086b66; background:#f2faf8; font-size:12px; font-weight:700; text-decoration:none; cursor:pointer; white-space:nowrap; }}
    .file-label:hover,.source-link:hover {{ background:#e4f4f0; }}
    input[type=file] {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; clip-path:inset(50%); }}
    .page-button,.page-select {{ min-height:34px; padding:0 10px; border:1px solid #d4e0e1; border-radius:7px; color:#2b454e; background:#fff; font-size:12px; cursor:pointer; }}
    .page-select {{ max-width:105px; }}
    .original-svg-wrap {{ display:flex; flex:1; min-height:0; justify-content:center; overflow:auto; padding:14px; background:#e9eff0; }}
    .original-svg {{ display:block; width:auto; height:100%; max-width:100%; object-fit:contain; background:#fff; box-shadow:0 2px 12px #17353b30; }}
    .original-empty {{ display:flex; flex:1; flex-direction:column; justify-content:center; align-items:center; gap:13px; padding:32px; text-align:center; background:linear-gradient(145deg,#f8fbfb,#edf4f3); }}
    .original-empty .icon {{ display:grid; place-items:center; width:58px; height:58px; border-radius:16px; color:#087e78; background:#dff1ed; font-size:25px; }}
    .original-empty h3 {{ margin:0; font-size:17px; }}
    .original-empty p {{ max-width:420px; margin:0; color:var(--muted); font-size:13px; line-height:1.7; }}
    .original-empty .hint {{ padding:11px 14px; border:1px solid #dce8e6; border-radius:8px; background:#fff; }}
    .parsed-content {{ flex:1; overflow:auto; padding:20px clamp(16px,2vw,30px) 40px; scroll-behavior:smooth; }}
    .page-group {{ max-width:820px; min-height:100%; margin:0 auto; padding:24px clamp(16px,3vw,34px); border:1px solid #e2e9e8; border-radius:5px; background:#fff; box-shadow:0 3px 11px #183a4210; }}
    .page-group h2 {{ margin:0 0 16px; padding-bottom:8px; border-bottom:1px solid #e6edec; color:#829197; font-size:11px; font-weight:600; letter-spacing:.04em; }}
    .page-empty {{ color:var(--muted); padding:36px 12px; text-align:center; }}
    .paragraph-block {{ margin:8px 0; font-family:"Apple SD Gothic Neo","Noto Serif KR",serif; font-size:14px; line-height:1.85; white-space:normal; overflow-wrap:anywhere; }}
    .table-block {{ margin:20px 0; }}
    .table-block figcaption,.image-block figcaption {{ margin-bottom:8px; color:var(--muted); font-size:11px; }}
    .table-scroll {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    td {{ min-width:52px; padding:7px 8px; border:1px solid #65767b; text-align:left; vertical-align:middle; white-space:pre-wrap; line-height:1.55; }}
    .image-block {{ margin:20px 0; text-align:center; }}
    .image-block img {{ max-width:100%; height:auto; }}
    .missing-image {{ color:#9b5e27; font-size:12px; }}
    @media(max-width:850px) {{ .topbar {{ align-items:flex-start; flex-direction:column; }} .compare {{ height:auto; min-height:0; grid-template-columns:1fr; }} .panel {{ min-height:65vh; }} .stats {{ gap:5px; }} }}
  </style>
</head>
<body>
  <header class="topbar">
    <div><h1>원문 · 파싱 결과 페이지 비교</h1><p>{esc(source_name)} · 원본 문서를 SVG로 직접 변환해 실제 쪽 단위로 연결합니다</p></div>
    <div class="stats"><span class="stat">문단 {counts.get('paragraph', 0):,}</span><span class="stat">표 {counts.get('table', 0):,}</span><span class="stat">이미지 {counts.get('image', 0):,}</span></div>
  </header>
  <main class="compare">
    <section class="panel" aria-label="원본 문서">
            <div class="panel-head"><div><h2>원본 SVG</h2><small id="original-status">{f'{svg_page_count}쪽 SVG 변환 완료' if svg_page_count else '원본 문서 SVG 변환이 필요합니다'}</small></div>
                <div class="page-controls"><button class="page-button" id="prev-page" type="button" aria-label="이전 쪽">이전</button><select class="page-select" id="page-select" aria-label="비교할 쪽" {'' if page_count else 'disabled'}>{page_options}</select><button class="page-button" id="next-page" type="button" aria-label="다음 쪽">다음</button><a class="source-link" id="svg-path-link" href="#" target="_blank" rel="noopener" hidden>SVG 파일 경로</a>{source_link}</div>
      </div>
            <div id="original-empty" class="original-empty"><div class="icon">↔</div><h3>{'원본 SVG 페이지가 없습니다' if svg_page_count == 0 else '원본 SVG 미리보기'}</h3><p>원본 문서를 직접 페이지별 SVG로 렌더링합니다. 쪽을 선택하면 해당 페이지의 원문 SVG와 파서 결과를 나란히 볼 수 있습니다.</p><p class="hint">원본: <strong>{esc(source_name)}</strong></p></div>
            <div class="original-svg-wrap" id="original-svg-wrap" hidden><img class="original-svg" id="original-svg" alt="원본 SVG 쪽 미리보기"></div>
    </section>
    <section class="panel" aria-label="파싱된 HTML">
            <div class="panel-head"><div><h2>해당 쪽 파싱 HTML</h2><small>원문 페이지 기준으로 파싱 결과와 동기화</small></div><button class="file-label" id="zoom-button" type="button">글자 크게</button></div>
            <div class="parsed-content" id="parsed-content"><section class="page-group" id="parsed-page"><h2 id="parsed-page-heading">쪽 선택 대기 중</h2><div id="parsed-page-content"></div></section>{pages_html}</div>
    </section>
  </main>
  <script>
        const svgPaths = {svg_paths_json};
        const svgSources = {svg_sources_json};
        const selector = document.getElementById('page-select');
        const svg = document.getElementById('original-svg');
        const svgWrap = document.getElementById('original-svg-wrap');
        const svgPathLink = document.getElementById('svg-path-link');
    const empty = document.getElementById('original-empty');
    const status = document.getElementById('original-status');
        const parsedContent = document.getElementById('parsed-page-content');
        const parsedTemplates = new Map([...document.querySelectorAll('template.parsed-page')].map(template => [template.dataset.page, template]));
        function showPage(value) {{
            if (!value) return;
            const page = Number(value);
            const svgPath = svgPaths[String(page)];
            const svgSource = svgSources[String(page)];
            if (svgSource) {{
                svg.src = svgSource;
                svgPathLink.href = svgPath;
                svgPathLink.textContent = `SVG 경로: ${{svgPath}}`;
                svgPathLink.title = svgPath;
                svgPathLink.hidden = false;
                svg.hidden = false;
                svgWrap.hidden = false;
                empty.hidden = true;
                status.textContent = `쪽 ${{page}} · SVG 원본`;
            }} else {{
                svgPathLink.hidden = true;
                svg.removeAttribute('src');
                svg.hidden = true;
                svgWrap.hidden = true;
                empty.hidden = false;
                status.textContent = `쪽 ${{page}} · 원본 SVG 없음`;
            }}
            document.getElementById('parsed-page-heading').textContent = `쪽 ${{page}} 파싱 결과`;
            parsedContent.replaceChildren();
            const template = parsedTemplates.get(String(page));
            if (template) parsedContent.append(template.content.cloneNode(true));
            else parsedContent.innerHTML = '<p class="page-empty">이 쪽에서 추출된 문단·표·이미지가 없습니다.</p>';
            document.getElementById('prev-page').disabled = page <= 1;
            document.getElementById('next-page').disabled = page >= selector.options.length;
            history.replaceState(null, '', `#page-${{page}}`);
        }}
        selector.addEventListener('change', () => showPage(selector.value));
        document.getElementById('prev-page').addEventListener('click', () => {{ if (selector.selectedIndex > 0) {{ selector.selectedIndex--; showPage(selector.value); }} }});
        document.getElementById('next-page').addEventListener('click', () => {{ if (selector.selectedIndex < selector.options.length - 1) {{ selector.selectedIndex++; showPage(selector.value); }} }});
        if (selector.options.length) {{
            const requestedPage = Number(location.hash.match(/page-(\\d+)/)?.[1] || 1);
            selector.value = String(Math.min(Math.max(requestedPage, 1), selector.options.length));
            showPage(selector.value);
        }}
    let enlarged = false;
    document.getElementById('zoom-button').addEventListener('click', (event) => {{
      enlarged = !enlarged;
      document.getElementById('parsed-content').style.fontSize = enlarged ? '1.18em' : '';
      event.currentTarget.textContent = enlarged ? '기본 글자' : '글자 크게';
    }});
  </script>
</body>
</html>
'''


def main() -> None:
    use_project_environment()

    parser = argparse.ArgumentParser(description="parsed.json을 비교용 HTML로 렌더링")
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT, help="파싱 JSON 경로")
    parser.add_argument(
        "--source-document",
        "--source-hwp",
        "--source-hwpx",
        dest="requested_document",
        type=Path,
        help="원본 HWP 또는 HWPX 경로 (생략하면 input 폴더에서 탐색)",
    )
    parser.add_argument("--source-pdf", type=Path, help="직접 원본 문서 변환 대신 사용할 PDF 경로 (선택)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="생성할 HTML 경로")
    args = parser.parse_args()

    input_json = args.input_json if args.input_json.is_absolute() else PROJECT_ROOT / args.input_json
    output_path = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    requested_document = args.requested_document
    if requested_document is not None and not requested_document.is_absolute():
        requested_document = PROJECT_ROOT / requested_document
    requested_pdf = args.source_pdf
    if requested_pdf is not None and not requested_pdf.is_absolute():
        requested_pdf = PROJECT_ROOT / requested_pdf

    try:
        is_default_json = input_json == (PROJECT_ROOT / DEFAULT_INPUT).resolve()
        if not input_json.is_file() and not is_default_json:
            raise FileNotFoundError(f"파싱 JSON을 찾을 수 없습니다: {input_json}")

        parser_sources = [
            *sorted((PROJECT_ROOT / "input").glob("*.hwpx")),
            *sorted((PROJECT_ROOT / "input").glob("*.hwp")),
        ]
        parser_source = requested_document or next(iter(parser_sources), None)
        if requested_document is not None and not requested_document.is_file():
            raise FileNotFoundError(f"지정한 원본 문서를 찾을 수 없습니다: {requested_document}")

        regenerate_json = is_default_json and parser_source is not None
        if regenerate_json and input_json.is_file():
            try:
                existing_filename = json.loads(
                    input_json.read_text(encoding="utf-8")
                ).get("filename")
            except (json.JSONDecodeError, OSError):
                existing_filename = None
            regenerate_json = (
                existing_filename != parser_source.name
                or input_json.stat().st_mtime_ns < parser_source.stat().st_mtime_ns
            )

        if regenerate_json or not input_json.is_file():
            parser_script = PROJECT_ROOT / "1_parser_hwpx.py"
            if parser_source is None:
                raise FileNotFoundError(
                    f"파싱 JSON과 원본 HWP/HWPX를 찾을 수 없습니다: {input_json}\n"
                    "input 폴더에 원본 문서를 두고 다시 실행하세요."
                )

            result = subprocess.run(
                [
                    sys.executable,
                    str(parser_script),
                    str(parser_source),
                    "--output-json",
                    str(input_json),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    "원본 문서에서 파싱 JSON을 자동 생성하려 했지만 파서 실행이 실패했습니다.\n"
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            print(f"원본 문서에서 파싱 JSON을 생성했습니다: {parser_source.name}")

        document = json.loads(input_json.read_text(encoding="utf-8"))
        source_path = find_source_document(requested_document, document)
        if requested_document is not None and source_path is None:
            raise FileNotFoundError(f"지정한 원본 문서를 찾을 수 없습니다: {requested_document}")
        if requested_pdf is not None and not requested_pdf.is_file():
            raise FileNotFoundError(f"지정한 원본 PDF를 찾을 수 없습니다: {requested_pdf}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        asset_root = output_path.parent.parent if output_path.parent.name == "html" else output_path.parent
        svg_dir = asset_root / "svg"
        source_page_map: dict[int, list[int]] = {}
        if requested_pdf:
            svg_page_count = convert_pdf_to_svgs(requested_pdf, svg_dir)
        elif source_path:
            svg_page_count, source_page_map = convert_hwpx_to_svgs(source_path, svg_dir)
        else:
            svg_page_count = 0
            print("주의: 원본 HWP/HWPX를 찾지 못해 SVG 없이 HTML만 생성합니다.", file=sys.stderr)

        html_content = build_html(
            document,
            source_path,
            svg_page_count,
            svg_dir,
            output_path,
            source_page_map,
        )
        output_path.write_text(html_content, encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError, RuntimeError, OSError, ValueError) as error:
        parser.exit(1, f"오류: {error}\n")

    print(f"비교 HTML 생성 완료: {output_path}")
    print(f"원본 문서: {source_path if source_path else '찾지 못함'}")
    print(f"원본 SVG 페이지: {svg_page_count}쪽")
    if source_page_map:
        print(f"원문 문단-페이지 매핑: {len(source_page_map)}개")


if __name__ == "__main__":
    main()
