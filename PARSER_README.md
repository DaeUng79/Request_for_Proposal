# HWPX 파싱과 MongoDB 저장

## 실행

```sh
.venv/bin/python -m pip install -r requirements-parser.txt
.venv/bin/python 1_parser_hwpx.py
.venv/bin/python 1_parser_hwpx.py input/example.hwpx
```

입력 생략 시 `input` 폴더의 첫 HWPX(없으면 HWP)를 선택합니다. 기본 실행은 JSON 생성, 이미지 추출, MongoDB 저장까지 수행합니다. 실패하면 종료 코드 1을 반환하며 이미 생성된 로컬 JSON은 보존합니다.

```sh
# 로컬 파싱만 수행
.venv/bin/python 1_parser_hwpx.py --no-mongo
# 원본 HWP/HWPX 파일까지 함께 업로드할 때만 지정
.venv/bin/python 1_parser_hwpx.py --store-source
# DB 이름 지정
.venv/bin/python 1_parser_hwpx.py --database Request_for_Proposal
# 렌더링 없이 XML 추출만 수행
.venv/bin/python 1_parser_hwpx.py --no-pagination --no-mongo
# 렌더링 실패 시 전체 작업 중단
.venv/bin/python 1_parser_hwpx.py --strict-pagination
```

`--collection`으로 기본 `Request` 컬렉션 이름을 바꿀 수 있습니다. `--output-json`, `--asset-dir`, `--env-file`로 출력과 설정 경로를 지정할 수 있습니다. JSON 기본 경로는 `output/json/parsed.json`, 이미지는 `output/images/<원본 SHA256>/<이미지 SHA256>.<확장자>`입니다. `--asset-dir`은 `images` 폴더의 상위 경로입니다.

## 접속 설정

프로젝트 `.env`의 기존 `client_db=MongoClient("mongodb://...")` 형식을 지원합니다. 문자열 인자 하나만 AST로 읽으며 `eval`로 실행하지 않습니다. 다음 형식도 지원합니다.

```dotenv
MONGODB_URI=mongodb://USER:PASSWORD@HOST:27017/DATABASE
MONGODB_DATABASE=Request_for_Proposal
```

연결값 우선순위는 `MONGODB_URI`, `MONGO_URI`, `client_db`입니다. 같은 키는 프로세스 환경 변수가 `.env`보다 우선합니다. DB 이름은 `--database`, `MONGODB_DATABASE`, URI의 데이터베이스, `Request_for_Proposal` 순서로 결정합니다. `.env`는 수정하지 않습니다. 계정은 해당 DB에서 컬렉션·인덱스 생성 및 읽기·쓰기 권한이 필요합니다.

## 저장 구조

| 컬렉션                                      | 내용                                                                   |
| ------------------------------------------- | ---------------------------------------------------------------------- |
| `Request` | 추출 blocks·assets 전체, 원본 해시, 파일명, 버전, 활성 revision, 건수, 페이지, 경고 |
| `hwpx_blocks`                             | 문단·표·이미지와 순서·부모·셀·XML 출처                            |
| `hwpx_assets`                             | 이미지 해시, MIME 타입, 로컬 위치, GridFS ID                           |
| `hwpx_chunks`                             | 원본 참조가 있는 검색용 텍스트; 아직 임베딩 없음                       |
| `hwpx_files.files`, `hwpx_files.chunks` | 이미지 바이너리와 선택적으로 업로드한 원본(GridFS)                                |

JSON의 `tables`, `images`는 기존 도구와의 호환용 목록입니다. MongoDB에는 이 목록을 다시 저장하지 않습니다. `Request.blocks`에 문서 전체 구조를 보관하고 `hwpx_blocks`에 블록 단위 검색용 구조를 함께 저장합니다. 셀의 `child_block_ids`는 문단·중첩 표·이미지의 순서를 나타냅니다. 셀의 `text`는 직속 문단 텍스트이며 중첩 표 내용은 자식 블록에서 조회합니다.

원본 SHA256이 `document_id`입니다. 추출 JSON의 해시가 `revision`이며 같은 결과의 재실행은 같은 `_id`에 upsert합니다. 다른 결과는 새 revision으로 보존합니다. 저장 건수 확인을 마친 후 `Request.active_revision`을 바꿉니다. 실패한 업로드의 일부 레코드는 남을 수 있지만 활성 revision으로 공개되지 않습니다. 모든 조회는 활성 revision을 조건에 넣어야 합니다. 자동 삭제나 기존 문서 정리는 하지 않습니다.

```python
# db는 위 설정으로 연결한 PyMongo Database
metadata = db.Request.find_one({"_id": document_id})
query = {"document_id": document_id, "revision": metadata["active_revision"]}
blocks = list(db.hwpx_blocks.find(query).sort("order", 1))
chunks = list(db.hwpx_chunks.find(query))

from gridfs import GridFS
fs = GridFS(db, collection="hwpx_files")
if metadata.get("source_gridfs_id"):
    original_bytes = fs.get(metadata["source_gridfs_id"]).read()
asset = db.hwpx_assets.find_one(query)
if asset:
    image_bytes = fs.get(asset["gridfs_id"]).read()
```

검색용 청크는 문단과 표의 행을 기준으로 구성하고 2,000자를 넘으면 분리합니다. 표 행에는 좌표와 병합 정보가 포함됩니다. `source_refs`의 문자 범위는 `text_source`에 지정한 원문/행 직렬화 문자열 기준입니다. 머리글이나 제목 의미를 임의로 확정하지 않습니다. 임베딩과 OCR은 자동 수행하지 않으며 외부 AI API를 호출하지 않습니다. 임베딩 모델을 선택한 후 `hwpx_chunks.text`와 문맥을 이용해 별도 생성할 수 있습니다.

## 지원 범위와 제한

### Streamlit 페이지별 문서 보기

문서 보기는 원본 파일을 `page_preview_worker.py`에서 페이지별 SVG로 렌더링합니다. 여러 쪽에 걸친 표는 각 페이지에 실제 배치된 부분만 표시하며 전체 표를 첫 페이지에 모으거나 매 페이지에 반복하지 않습니다. `표` 보기에는 기존 추출 표 전체를 유지합니다.

기존 DB 문서도 `output/uploads/<SHA256>`에 같은 원본이 있거나 원본이 GridFS에 저장되어 있으면 바로 적용됩니다. 페이지 화면은 `output/page_previews`에 캐시하며 조회 과정에서 DB를 변경하지 않습니다. 원본이 없으면 정확한 페이지 분할을 재구성할 수 없으므로 재등록 안내를 표시합니다. 화면의 페이지 경계는 렌더러 기준이므로 한글 프로그램의 실제 배치와 차이가 있을 수 있습니다.

### 확인된 인쇄 쪽번호 보정

`page_number_corrections.json`은 원본 SHA256별로 사용자가 확인한 인쇄 번호의 기준을 저장합니다. 기록관리시스템 문서에는 `physical_page_number: 2`, `page_number: 1`을 적용했습니다. CLI와 Streamlit 업로드 파싱 모두 같은 설정을 읽습니다. 파일 내용이 달라져 해시가 바뀌면 보정을 자동 적용하지 않습니다.

보정은 기준 페이지부터 다음 XML 번호 재시작 설정 전까지만 적용하며, 번호가 숨겨진 페이지의 `null`은 유지합니다. `page_number_source`는 `verified_number_correction`, 보정 전 값은 `rendered_page_number`로 남깁니다. 블록·표·이미지 및 페이지 목록에 같은 번호를 적용합니다.

이 설정은 **인쇄 번호 보정**이며 렌더러의 페이지 나눔을 고치는 기능은 아닙니다. 전체 쪽수와 물리 페이지 경계는 한글 원본과 다를 수 있습니다. 해당 문서는 `rendered_layout_not_verified` 경고를 남깁니다. 기존 DB 문서는 자동 변경되지 않으며 재등록해야 반영됩니다.

- HWPX 혼합 XML의 탭·줄바꿈·tail, 중첩 표와 셀 내부 이미지, 병합, 단색 배경을 보존합니다.
- 채우기 유형과 원본 borderFill XML을 보존합니다. 그라데이션·무늬를 화면에 정확히 재현하는 기능은 없습니다.
- 글자/문단 스타일 참조와 텍스트 run을 보존합니다. 스타일 정의의 완전한 해석은 구현하지 않았으며 원본을 업로드한 경우 GridFS에서 조회할 수 있습니다.
- `physical_page_number`는 표지를 포함한 실제 페이지 순서(1부터)입니다. `page_number`는 머리말·바닥글의 독립된 숫자 줄에서 읽은 인쇄 쪽번호이며, 표시된 번호가 없는 표지는 `null`을 유지합니다. 출처는 `page_number_source`의 `rendered_header` 또는 `rendered_footer`로 구분합니다.
- 페이지는 렌더 트리의 상위 문단 기준 매핑입니다. 여러 페이지의 표에서 개별 셀의 정확한 페이지는 확정하지 않습니다. 렌더러 실패 시 기본적으로 중단하며, `--no-strict-pagination` 사용 시에만 경고와 함께 XML 결과를 유지합니다. `--no-pagination` 사용 시 페이지 번호를 추출하지 않습니다.
- 표 PNG는 만들지 않으며 `image_path=null`, `render_status=not_rendered`로 표시합니다.
- HWP는 기존 렌더 트리 추출을 유지합니다. 병합·음영·이미지 바이너리 지원은 HWPX와 다르며 경고를 기록합니다.
- Request의 전체 추출 문서 또는 단일 BSON 레코드가 15 MiB 이상이면 DB 쓰기 전 중단합니다. 매우 큰 표의 자동 분할은 구현하지 않았습니다.
- 문서 크기/항목 수 제한을 넘는 ZIP은 거절합니다. 지원하지 않는 개체까지 완전한 시각 재현을 보장하지 않습니다.

## 검증

```sh
.venv/bin/python -m unittest discover -s tests -v
```

회귀 테스트는 텍스트 누락, 중첩 표 중복, 셀 내부 이미지와 manifest, 병합·배경색, 페이지 실패, 안정적인 ID, 설정 해석, 긴 청크 분리를 검증합니다.

현재 서버의 wire version 7과 호환되도록 PyMongo 4.13.2를 고정했습니다. PyMongo 4.14부터 MongoDB 4.0 지원이 제거되므로 최신 버전으로 임의 변경하면 접속이 실패할 수 있습니다. [공식 변경 기록](https://pymongo.readthedocs.io/en/stable/changelog.html#changes-in-version-4-14-0-2025-08-06)
