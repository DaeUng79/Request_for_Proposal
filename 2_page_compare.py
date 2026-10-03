from __future__ import annotations

import argparse
import html
import json
import os
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_JSON = PROJECT_ROOT / "output/json/parsed.json"
DEFAULT_SVG_DIR = PROJECT_ROOT / "output/svg"
DEFAULT_OUTPUT = PROJECT_ROOT / "output/html/page_comparison.html"
SVG_PATTERN = re.compile(r"^original_page_(\d+)\.svg$")
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def block_pages(block: dict[str, Any]) -> list[int]:
    """JSON 블록의 물리 쪽 목록을 우선 사용하고, 없으면 단일 쪽 필드를 읽는다."""

    value = block.get("physical_page_numbers")
    if value is None:
        value = block.get("physical_page_number")
    if value is None:
        value = block.get("page_number")
    values = value if isinstance(value, list) else [value]
    return sorted({page for page in values if isinstance(page, int) and page > 0})


def render_table(block: dict[str, Any], block_lookup=None) -> str:
    table = block.get("table", block)
    table_id = esc(table.get("table_id", block.get("block_id", "표")))
    page_number = block.get("page_number")
    caption = f"{table_id} · 인쇄 쪽 {esc(page_number)}" if page_number is not None else table_id
    rows: list[str] = []

    for row in table.get("cells", []):
        cells: list[str] = []
        for cell in row:
            color = str(cell.get("background_color") or "").strip()
            style = f' style="background-color:{esc(color)}"' if COLOR_PATTERN.fullmatch(color) else ""
            rowspan = max(1, int(cell.get("row_span", 1)))
            colspan = max(1, int(cell.get("col_span", 1)))
            text = esc(cell.get("text", "")).replace("\n", "<br>")
            if block_lookup is not None and cell.get("child_block_ids"):
                text = "".join(render_block(block_lookup[child_id], block_lookup)
                               for child_id in cell["child_block_ids"] if child_id in block_lookup)
            cells.append(
                f'<td rowspan="{rowspan}" colspan="{colspan}"{style}>{text}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        f'<figure class="table-block"><figcaption>{caption}</figcaption>'
        f'<div class="table-scroll"><table>{"".join(rows)}</table></div></figure>'
    )


def render_block(block: dict[str, Any], block_lookup=None) -> str:
    block_type = block.get("type")
    if block_type == "paragraph":
        text = esc(block.get("text", "")).replace("\n", "<br>")
        return f'<p class="paragraph-block">{text}</p>' if text else ""
    if block_type == "table":
        return render_table(block, block_lookup)
    if block_type == "image":
        description = block.get("text") or block.get("image", {}).get("description") or "이미지"
        return f'<p class="image-block">{esc(description)}</p>'
    return ""


def build_html(
    document: dict[str, Any],
    svg_dir: Path,
    output_path: Path,
) -> tuple[str, int]:
    svg_files: dict[int, Path] = {}
    for path in svg_dir.iterdir():
        match = SVG_PATTERN.fullmatch(path.name)
        if match and path.is_file():
            svg_files[int(match.group(1))] = path

    if not svg_files:
        raise FileNotFoundError(f"SVG 페이지를 찾을 수 없습니다: {svg_dir}")

    pages = sorted(svg_files)
    block_lookup = {block["block_id"]: block for block in document.get("blocks", [])}
    blocks_by_page: dict[int, list[dict[str, Any]]] = {page: [] for page in pages}
    for block in document.get("blocks", []):
        if block.get("parent_block_id"):
            continue
        for page in block_pages(block):
            if page in blocks_by_page:
                blocks_by_page[page].append(block)

    svg_urls = {
        str(page): Path(os.path.relpath(svg_path.resolve(), output_path.parent.resolve())).as_posix()
        for page, svg_path in svg_files.items()
    }
    templates = []
    for page in pages:
        content = "".join(render_block(block, block_lookup) for block in blocks_by_page[page])
        if not content:
            content = '<p class="empty">이 페이지에 연결된 파싱 결과가 없습니다.</p>'
        templates.append(
            f'<template class="parsed-page" data-page="{page}">{content}</template>'
        )

    title = esc(Path(document.get("filename", "문서 비교")).stem)
    page_list_json = json.dumps(pages, ensure_ascii=False)
    svg_urls_json = json.dumps(svg_urls, ensure_ascii=False).replace("<", "\\u003c")
    page_total = len(pages)
    first_svg = esc(svg_urls[str(pages[0])])

    page_options = "".join(f'<option value="{page}">{page}쪽</option>' for page in pages)
    template_html = "".join(templates)
    content_html = f'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101923">
  <title>{title} · 페이지별 2분할 비교</title>
  <style>
    :root {{ color-scheme:dark; --bg:#101923; --panel:#182431; --line:#2b3b49; --text:#f2f6f8; --muted:#9eb0bd; --accent:#63d6c4; }}
    * {{ box-sizing:border-box; }}
    body {{ min-height:100vh; margin:0; color:var(--text); background:radial-gradient(ellipse at 50% -20%,#244052 0%,var(--bg) 58%); font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif; }}
    .app {{ width:min(1500px,100%); min-height:100vh; margin:auto; padding:20px clamp(12px,3vw,38px) 24px; display:flex; flex-direction:column; gap:14px; }}
    header {{ display:flex; justify-content:space-between; align-items:center; gap:14px; }}
    h1 {{ margin:0; font-size:clamp(17px,2vw,22px); }}
    .subtitle {{ margin:4px 0 0; color:var(--muted); font-size:12px; }}
    .counter {{ min-width:100px; padding:9px 13px; border:1px solid var(--line); border-radius:11px; color:var(--accent); background:#16222e; text-align:center; font-weight:700; font-variant-numeric:tabular-nums; }}
    .progress {{ height:4px; overflow:hidden; border-radius:99px; background:#ffffff16; }}
    .progress span {{ display:block; width:0; height:100%; border-radius:inherit; background:linear-gradient(90deg,#4bbba9,var(--accent)); transition:width .2s; }}
    .compare {{ flex:1; min-height:0; display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:14px; }}
    .panel {{ min-width:0; min-height:440px; display:flex; flex-direction:column; overflow:hidden; border:1px solid var(--line); border-radius:15px; background:var(--panel); box-shadow:0 18px 50px #0003; }}
    .panel-head {{ padding:12px 15px; border-bottom:1px solid var(--line); }}
    .panel-head h2 {{ margin:0; font-size:14px; }}
    .panel-head small {{ display:block; margin-top:4px; color:var(--muted); font-size:11px; }}
    .original-stage {{ flex:1; min-height:0; display:flex; align-items:center; justify-content:center; overflow:auto; padding:12px; background:#121d28; }}
    .original-stage img {{ display:block; width:auto; height:auto; max-width:100%; max-height:100%; object-fit:contain; background:white; box-shadow:0 10px 35px #0007; }}
    .parsed-stage {{ flex:1; min-height:0; overflow:auto; padding:12px clamp(10px,1.5vw,22px) 24px; color:#1c2a31; background:#f3f6f7; }}
    .paragraph-block {{ margin:8px 0; font-family:"Apple SD Gothic Neo","Noto Serif KR",serif; font-size:13px; line-height:1.75; overflow-wrap:anywhere; }}
    .table-block {{ margin:13px 0; }}
    figcaption {{ margin-bottom:6px; color:#647780; font-size:10px; }}
    .table-scroll {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:11px; }}
    td {{ min-width:44px; padding:6px 7px; border:1px solid #75858a; text-align:left; vertical-align:middle; white-space:pre-wrap; line-height:1.45; }}
    .image-block,.empty {{ padding:22px 10px; color:#65757c; text-align:center; font-size:12px; }}
    .toolbar {{ display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:8px; padding:11px; border:1px solid var(--line); border-radius:13px; background:#17232fe8; }}
    button,select {{ min-height:38px; padding:0 12px; border:1px solid var(--line); border-radius:9px; color:var(--text); background:#1b2a37; font-size:13px; cursor:pointer; }}
    button:hover:not(:disabled) {{ border-color:#63d6c488; color:var(--accent); }}
    button:disabled {{ opacity:.35; cursor:not-allowed; }}
    select {{ min-width:90px; }}
    .hint {{ margin:0; color:#8294a1; text-align:center; font-size:11px; }}
    @media(max-width:850px) {{ .compare {{ grid-template-columns:1fr; }} .panel {{ min-height:60vh; }} }}
  </style>
</head>
<body>
  <main class="app">
    <header><div><h1>원본 · parsed.json 페이지 비교</h1><p class="subtitle">{title}</p></div><div class="counter" id="counter">1 / {page_total}</div></header>
    <div class="progress" role="progressbar" aria-label="문서 진행률"><span id="progress-fill"></span></div>
    <section class="compare" aria-label="페이지별 2분할 비교">
      <article class="panel"><div class="panel-head"><h2>원본 SVG</h2><small id="original-label">원본 {pages[0]}쪽</small></div><div class="original-stage"><img id="original-image" src="{first_svg}" alt="원본 {pages[0]}쪽" draggable="false"></div></article>
      <article class="panel"><div class="panel-head"><h2>파싱 결과</h2><small id="parsed-label">원본 {pages[0]}쪽 문단·표</small></div><div class="parsed-stage" id="parsed-content"></div></article>
    </section>
    <nav class="toolbar" aria-label="페이지 이동"><button id="previous" type="button">← 이전</button><button id="first" type="button">처음</button><select id="page-select" aria-label="페이지 선택">{page_options}</select><button id="next" type="button">다음 →</button><button id="last" type="button">끝</button><button id="play" type="button" aria-pressed="false">▶ 자동 넘김</button></nav>
    <p class="hint">페이지를 이동하면 원본 SVG와 파싱 결과가 함께 바뀝니다 · 키보드 ← → 이동 · 스페이스 자동 넘김(3초 간격)</p>
    {template_html}
  </main>
  <script>
    (() => {{
      const pages = {page_list_json};
      const svgUrls = {svg_urls_json};
      const templates = new Map([...document.querySelectorAll('template.parsed-page')].map(item => [Number(item.dataset.page), item]));
      const select = document.getElementById('page-select');
      const image = document.getElementById('original-image');
      const parsed = document.getElementById('parsed-content');
      const previous = document.getElementById('previous');
      const next = document.getElementById('next');
      const play = document.getElementById('play');
      let index = 0;
      let timer = null;
      function showPage(page) {{
        const nextIndex = pages.indexOf(Number(page));
        index = nextIndex < 0 ? 0 : nextIndex;
        const current = pages[index];
        image.src = svgUrls[String(current)];
        image.alt = `원본 ${{current}}쪽`;
        document.getElementById('original-label').textContent = `원본 ${{current}}쪽`;
        document.getElementById('parsed-label').textContent = `원본 ${{current}}쪽 문단·표`;
        document.getElementById('counter').textContent = `${{index + 1}} / ${{pages.length}}`;
        document.getElementById('progress-fill').style.width = `${{(index + 1) / pages.length * 100}}%`;
        select.value = String(current);
        previous.disabled = index === 0;
        next.disabled = index === pages.length - 1;
        parsed.replaceChildren();
        const template = templates.get(current);
        if (template) parsed.append(template.content.cloneNode(true));
        history.replaceState(null, '', `#page-${{current}}`);
      }}
      function stop() {{
        if (timer !== null) window.clearInterval(timer);
        timer = null;
        play.textContent = '▶ 자동 넘김';
        play.setAttribute('aria-pressed', 'false');
      }}
      function togglePlay() {{
        if (timer !== null) return stop();
        if (index === pages.length - 1) showPage(pages[0]);
        play.textContent = '⏸ 자동 넘김 중';
        play.setAttribute('aria-pressed', 'true');
        timer = window.setInterval(() => {{
          if (index >= pages.length - 1) return stop();
          showPage(pages[index + 1]);
        }}, 3000);
      }}
      previous.addEventListener('click', () => showPage(pages[index - 1]));
      next.addEventListener('click', () => showPage(pages[index + 1]));
      document.getElementById('first').addEventListener('click', () => showPage(pages[0]));
      document.getElementById('last').addEventListener('click', () => showPage(pages[pages.length - 1]));
      select.addEventListener('change', () => showPage(select.value));
      play.addEventListener('click', togglePlay);
      document.addEventListener('keydown', event => {{
        if (event.target instanceof HTMLSelectElement) return;
        if (event.key === 'ArrowLeft' && index > 0) showPage(pages[index - 1]);
        else if (event.key === 'ArrowRight' && index < pages.length - 1) showPage(pages[index + 1]);
        else if (event.code === 'Space') {{ event.preventDefault(); togglePlay(); }}
      }});
      const requestedPage = Number(location.hash.match(/page-(\\d+)/)?.[1]);
      showPage(pages.includes(requestedPage) ? requestedPage : pages[0]);
    }})();
  </script>
</body>
</html>
'''
    return content_html, page_total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="parsed.json과 페이지별 SVG를 2분할 HTML로 생성합니다."
    )
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="파싱 JSON 경로")
    parser.add_argument("--svg-dir", type=Path, default=DEFAULT_SVG_DIR, help="SVG 페이지 폴더")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="생성할 HTML 경로")
    args = parser.parse_args()

    json_path = args.json if args.json.is_absolute() else PROJECT_ROOT / args.json
    svg_dir = args.svg_dir if args.svg_dir.is_absolute() else PROJECT_ROOT / args.svg_dir
    output_path = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output

    if not json_path.is_file():
        parser.error(f"파싱 JSON을 찾을 수 없습니다: {json_path}")
    if not svg_dir.is_dir():
        parser.error(f"SVG 폴더를 찾을 수 없습니다: {svg_dir}")

    try:
        document = json.loads(json_path.read_text(encoding="utf-8"))
        html_content, page_total = build_html(document, svg_dir, output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html_content, encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.exit(1, f"오류: {error}\n")

    print(f"페이지별 2분할 HTML 생성 완료: {output_path}")
    print(f"사용한 파싱 JSON: {json_path}")
    print(f"SVG 페이지: {page_total}쪽")


if __name__ == "__main__":
    main()
