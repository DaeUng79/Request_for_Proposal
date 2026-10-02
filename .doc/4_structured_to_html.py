from __future__ import annotations

import html
import json
import os
from pathlib import Path


INPUT_PATH = Path("output/json/structured.json")
OUTPUT_PATH = Path("output/structured_report.html")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def relative_asset_path(asset_path: str) -> str:
    return os.path.relpath(asset_path, OUTPUT_PATH.parent).replace(os.sep, "/")


def table_html_path(source: dict) -> str:
  return relative_asset_path(str(Path(source["image_path"]).with_suffix(".html")))


def render_navigation(sections: list[dict]) -> str:
    items = []
    for section in sections:
        indentation = " child" if section["level"] > 1 else ""
        items.append(
            f'<a class="nav-item{indentation}" href="#{esc(section["section_id"])}">'
            f'{esc(section["title"])}</a>'
        )
    return "".join(items)


def render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return '<p class="empty-evidence">직접 근거 없음</p>'

    items = []
    for source in evidence:
        page = source["page_number"] if source["page_number"] is not None else "미확인"
        label = f'{source["block_id"]} · 쪽 {page}'
        if "image_path" in source:
            items.append(
                f'<span class="evidence">{esc(label)} · {esc(source["table_id"])}</span>'
            )
        else:
            items.append(f'<span class="evidence">{esc(label)}</span>')
    return '<div class="evidence-list">' + "".join(items) + "</div>"


def render_table_gallery(evidence: list[dict]) -> str:
    tables = [source for source in evidence if "image_path" in source]
    if not tables:
        return ""

    figures = []
    for source in tables:
        html_path = table_html_path(source)
        page = source["page_number"] if source["page_number"] is not None else "미확인"
        figures.append(
            '<figure class="table-figure"><div class="table-file-bar">'
            '<span>{table_id} · 쪽 {page}</span>'
            '<a href="{path}" target="_blank">표 HTML 열기</a></div>'
            '<iframe src="{path}" title="{table_id} 표" loading="lazy"></iframe></figure>'.format(
                path=esc(html_path),
                table_id=esc(source["table_id"]),
                page=esc(page),
            )
        )
    return '<section class="table-gallery" aria-label="연결된 표 이미지">' + "".join(figures) + "</section>"


def render_section(section: dict) -> str:
    keywords = "".join(
        f'<span class="keyword">{esc(keyword)}</span>'
        for keyword in section["검색어"]
    )
    summary = esc(section["요약"]).replace("\n", "<br>")
    parent_note = "상위 항목" if section["level"] == 1 else "세부 항목"
    return f"""
    <article class="section-card level-{section['level']}" id="{esc(section['section_id'])}">
      <div class="section-heading">
        <div>
          <p class="section-kind">{parent_note}</p>
          <h2>{esc(section['title'])}</h2>
        </div>
        <a class="anchor-link" href="#{esc(section['section_id'])}">링크</a>
      </div>
      <p class="summary">{summary}</p>
      {f'<div class="keywords">{keywords}</div>' if keywords else ''}
      {render_table_gallery(section['근거'])}
      <details class="evidence-panel">
        <summary>근거 {len(section['근거'])}건</summary>
        {render_evidence(section['근거'])}
      </details>
    </article>
    """


def build_html(document: dict) -> str:
    sections = document["목차기반구조"]
    keywords = "".join(
        f'<span class="keyword">{esc(keyword)}</span>'
        for keyword in document["연관단어"]
    )
    section_html = "".join(render_section(section) for section in sections)

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(document['문서제목'])}</title>
  <style>
    :root {{
      --ink: #15232b;
      --muted: #5b6d75;
      --line: #d4dfe1;
      --paper: #f8faf8;
      --panel: #ffffff;
      --accent: #0a6b67;
      --accent-soft: #e4f1ed;
      --nav: #173b46;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--paper);
      font-family: "Noto Sans KR", "Apple SD Gothic Neo", sans-serif;
      line-height: 1.7;
    }}
    .page {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); min-height: 100vh; }}
    aside {{ background: var(--nav); color: #eef7f5; padding: 28px 20px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
    .brand {{ font-size: 13px; letter-spacing: .08em; margin: 0 0 20px; color: #9bd2c4; }}
    .aside-title {{ font-size: 18px; line-height: 1.45; margin: 0 0 22px; }}
    nav {{ display: grid; gap: 4px; }}
    .nav-item {{ color: #e7f0ef; text-decoration: none; font-size: 14px; padding: 7px 8px; border-left: 2px solid transparent; }}
    .nav-item:hover {{ background: #27515b; border-left-color: #79c7b3; }}
    .nav-item.child {{ margin-left: 12px; color: #c3d9d6; font-size: 13px; }}
    main {{ max-width: 1120px; padding: 54px clamp(24px, 5vw, 80px) 80px; }}
    header {{ padding-bottom: 36px; border-bottom: 1px solid var(--line); }}
    .eyebrow, .section-kind {{ color: var(--accent); font-weight: 700; font-size: 13px; letter-spacing: .06em; margin: 0 0 7px; }}
    h1 {{ font-family: "Noto Serif KR", "Nanum Myeongjo", serif; font-size: clamp(28px, 4vw, 46px); line-height: 1.25; margin: 0; letter-spacing: 0; }}
    .source {{ color: var(--muted); margin: 16px 0 0; font-size: 14px; word-break: break-all; }}
    .keywords {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 18px; }}
    .keyword {{ background: var(--accent-soft); color: #17554f; border: 1px solid #c4dfd8; border-radius: 4px; padding: 2px 9px; font-size: 13px; }}
    .content {{ padding-top: 34px; }}
    .section-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 26px; margin: 16px 0; scroll-margin-top: 18px; }}
    .section-card.level-1 {{ background: #eff5f4; border-color: #bacfcb; margin-top: 36px; }}
    .section-heading {{ display: flex; justify-content: space-between; gap: 14px; align-items: start; }}
    h2 {{ margin: 0; font-family: "Noto Serif KR", "Nanum Myeongjo", serif; font-size: 23px; line-height: 1.35; letter-spacing: 0; }}
    .anchor-link {{ color: var(--accent); font-size: 13px; text-decoration: none; padding-top: 4px; }}
    .summary {{ white-space: normal; margin: 20px 0 0; font-size: 15px; }}
    .evidence-panel {{ margin-top: 20px; border-top: 1px solid var(--line); padding-top: 13px; }}
    summary {{ cursor: pointer; color: var(--muted); font-size: 14px; font-weight: 700; }}
    .evidence-list {{ display: flex; flex-wrap: wrap; gap: 9px; margin-top: 14px; }}
    .evidence {{ border: 1px solid var(--line); color: #40545b; border-radius: 4px; padding: 4px 8px; font-size: 12px; display: inline-flex; align-items: center; gap: 7px; }}
    .table-gallery {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 18px; margin-top: 22px; }}
    .table-figure {{ margin: 0; border: 1px solid var(--line); background: #fff; }}
    .table-file-bar {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 9px 12px; color: var(--muted); background: #f3f7f6; border-bottom: 1px solid var(--line); font-size: 12px; }}
    .table-file-bar a {{ color: var(--accent); font-weight: 700; text-decoration: none; white-space: nowrap; }}
    .table-file-bar a:hover {{ text-decoration: underline; }}
    .table-figure iframe {{ display: block; width: 100%; height: 680px; border: 0; background: #fff; }}
    .empty-evidence {{ color: var(--muted); font-size: 13px; margin: 12px 0 0; }}
    @media (max-width: 800px) {{
      .page {{ display: block; }}
      aside {{ position: relative; height: auto; max-height: 260px; }}
      main {{ padding: 34px 20px 56px; }}
      .section-card {{ padding: 20px; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <aside>
      <p class="brand">STRUCTURED PROPOSAL</p>
      <h2 class="aside-title">{esc(document['문서제목'])}</h2>
      <nav aria-label="목차">{render_navigation(sections)}</nav>
    </aside>
    <main>
      <header>
        <p class="eyebrow">{esc(document['기관명'])}</p>
        <h1>{esc(document['문서제목'])}</h1>
        <p class="source">원문: {esc(document['문서출처'])}</p>
        <div class="keywords">{keywords}</div>
      </header>
      <section class="content" aria-label="목차 기반 정리">{section_html}</section>
    </main>
  </div>
</body>
</html>
"""


def main() -> None:
    document = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    OUTPUT_PATH.write_text(build_html(document), encoding="utf-8")
    print(f"HTML 생성 완료: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()