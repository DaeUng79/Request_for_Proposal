import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def is_header_row(row: list[dict]) -> bool:
    """짧은 항목명으로 구성된 첫 행을 표 헤더로 간주한다."""

    return all(len("".join(cell["text"].split())) <= 12 for cell in row)


def make_table_html(table: dict) -> str:

    cells = table["cells"]

    body = []

    for row_index, row in enumerate(cells):

        body.append("<tr>")

        header_row = row_index == 0 and is_header_row(row)

        for cell in row:
            classes = "body-cell" if cell["col"] > 0 and not header_row else ""
            background_color = cell.get("background_color")
            style = f' style="background-color: {background_color};"' if background_color else ""

            text = (
                cell["text"]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )

            body.append(
                f'''
                <td
                    data-cell-id="{cell["cell_id"]}"
                    class="{classes}"
                    rowspan="{cell["row_span"]}"
                    colspan="{cell["col_span"]}"
                    {style}
                >
                    {text}
                </td>
                '''
            )

        body.append("</tr>")

    return f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
    <meta charset="UTF-8">

    <style>

    body {{
        margin: 30px;
        background: white;
        font-family: Arial, "Apple SD Gothic Neo", sans-serif;
    }}

    .page-reference {{
        color: #59666b;
        font-size: 12px;
        margin: 0 0 10px;
    }}

    table {{
        border-collapse: collapse;
        width: max-content;
        max-width: 1200px;
    }}

    td {{
        border: 1px solid #333;
        padding: 8px 12px;
        min-width: 80px;
        text-align: center;
        vertical-align: middle;
        font-size: 14px;
        line-height: 1.5;
    }}

    td.body-cell {{
        text-align: left;
    }}

    </style>

    </head>

    <body>

        <p class="page-reference">문서 내 쪽: {table.get("page_number", "미확인")} ({"쪽 나누기 기준" if table.get("page_number_source") == "page_break_based" else "문서 표기 쪽번호"})</p>
        <table id="{table["table_id"]}">
            {''.join(body)}
        </table>

    </body>
    </html>
    """


def write_table_html(
    table: dict,
    output_dir: str = "output/tables",
) -> Path:
    output = Path(output_dir)
    output.mkdir(
        parents=True,
        exist_ok=True
    )

    html_path = output / (
        f'{table["table_id"]}.html'
    )

    html_path.write_text(
        make_table_html(table),
        encoding="utf-8"
    )

    return html_path


def render_table_image(
    table: dict,
    output_dir: str = "output/tables"
):
    output = Path(output_dir)
    html_path = write_table_html(table, output_dir)

    png_path = output / (
        f'{table["table_id"]}.png'
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1600,
                "height": 1200
            }
        )

        page.goto(
            html_path.resolve().as_uri()
        )

        page.locator(
            "table"
        ).screenshot(
            path=str(png_path)
        )

        browser.close()

    return png_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="표 JSON을 개별 HTML 또는 PNG로 렌더링"
    )
    parser.add_argument(
        "input_json",
        nargs="?",
        type=Path,
        default=Path("output/json/parsed.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/tables"),
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="PNG를 만들지 않고 HTML만 생성",
    )
    args = parser.parse_args()

    document = json.loads(
        args.input_json.read_text(encoding="utf-8")
    )

    for table in document["tables"]:
        if args.html_only:
            write_table_html(table, args.output_dir)
        else:
            render_table_image(table, args.output_dir)

    print("HTML 생성 완료")
    print("생성 HTML 개수:", len(document["tables"]))