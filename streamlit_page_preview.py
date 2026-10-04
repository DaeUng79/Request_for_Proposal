"""원본 문서의 각 페이지를 별도로 렌더링하는 Streamlit 미리보기 작업자."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def remove_rendered_page_label(svg: str, printed_number: int | None) -> str:
    """본문 밖의 쪽번호만 제거한다. 화면 바깥쪽에 교정된 쪽번호를 표시한다."""
    if printed_number is None:
        return svg
    import re
    root = ET.fromstring(svg)
    # 본문은 clip-path 그룹 안에 있으며 머리말/바닥글의 text는 루트에 있다.
    lines = {}
    for node in root.findall('{http://www.w3.org/2000/svg}text'):
        lines.setdefault(node.get('y'), []).append(node)
    for nodes in lines.values():
        text = ''.join(''.join(n.itertext()) for n in nodes).strip()
        match = re.fullmatch(r'\s*-?\s*(\d+)\s*-?\s*', text)
        if match and int(match.group(1)) == printed_number:
            for node in nodes:
                root.remove(node)
    return ET.tostring(root, encoding='unicode')


def build_page_previews(source: Path) -> dict:
    from pyhwpxlib.rhwp_bridge import RhwpEngine

    spec = importlib.util.spec_from_file_location('preview_parser', source_parser_path())
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    renderer = RhwpEngine().load(str(source))
    try:
        if source.suffix.lower() == '.hwpx':
            mapping, pages = parser.read_rendered_pagination(source, renderer)
            parser.correct_page_numbers(source, digest, mapping, pages)
        else:
            pages = [{'physical_page_number': i + 1,
                      **parser.page_number_metadata_from_render_tree(renderer.get_page_render_tree(i))}
                     for i in range(renderer.page_count)]
        svgs = {}
        for index, page in enumerate(pages):
            original = page.get('rendered_page_number', page['page_number'])
            svgs[str(index + 1)] = remove_rendered_page_label(renderer.render_page_svg(index), original)
        return {'source_sha256': digest, 'pages': pages, 'svgs': svgs}
    finally:
        renderer.close()


def source_parser_path():
    return Path(__file__).resolve().with_name('step1_parser_hwpx.py')


if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('source', type=Path)
    cli.add_argument('--output', required=True, type=Path)
    args = cli.parse_args()
    result = build_page_previews(args.source)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
