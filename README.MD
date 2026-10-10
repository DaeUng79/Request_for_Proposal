# HWPX/HWP 문서 파싱 및 저장

이 프로젝트는 한글 문서를 파싱해 문단·표·이미지·페이지 정보를 구조화된 JSON으로 만들고, MongoDB에 저장하는 합니다.

## 파일별 역할

| 파일                                                  | 역할                                                                                 |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------ |
| [step1_parser_hwpx.py](step1_parser_hwpx.py)           | HWPX/HWP 파싱, 블록·표·이미지·페이지 메타데이터 생성, JSON 출력 및 저장 모듈 호출 |
| [step2_hwpx_storage.py](step2_hwpx_storage.py)         | JSON에서 저장 레코드와 검색용 청크 생성, MongoDB/GridFS 저장                         |
| [step3_ai_requirements.py](step3_ai_requirements.py)   | 저장 문서의 요구사항 총괄표·목록표·세부내역 원문 추출과 버전별 저장                |
| [step4_ai_criteria.py](step4_ai_criteria.py)           | 추출 요구사항 및 요청서 평가기준표 또는 Markdown 기본 서식으로 JSON 기준표 생성·저장 |
| [streamlit_main.py](streamlit_main.py)                 | (파싱결과 확인용) 문서 업로드·등록, 저장 이력 및 상세 보기 UI                       |
| [streamlit_page_preview.py](streamlit_page_preview.py) | (파싱결과 확인용) )원본 한글 파일을 페이지별 SVG로 렌더링하는 별도 프로세스 작업자  |
| [requirements.txt](requirements.txt)                   | 앱·파서 실행에 필요한 Python 의존성                                                 |

## 1. 파싱 구조 — `step1_parser_hwpx.py`

### 처리 흐름

```text
HWPX 원본
  ├─ ZIP 내부 Contents/header.xml, Contents/section*.xml, content.hpf 분석
  ├─ 문단 텍스트·스타일, 표·셀·병합·배경, 그림 참조 추출
  ├─ BinData 이미지 바이트를 SHA256 파일명으로 저장
  ├─ 렌더 트리로 문단과 물리 페이지를 연결하고 인쇄 쪽번호 추출
  └─ 구조화 JSON 생성
```

HWPX는 ZIP 안의 XML을 직접 읽기 때문에 표 셀 좌표와 병합 정보, 셀 안의 문단·중첩 표·그림 관계를 보존합니다. 렌더 트리의 문단 위치를 XML 문단과 연결해 페이지 정보를 추가합니다.

### JSON의 주요 구조

- `document_id`, `source_sha256`: 원본 파일의 SHA256. 같은 원본을 식별하는 키입니다.
- `blocks`: 문서 순서대로 저장되는 기본 단위입니다. `paragraph`, `table`, `image` 타입을 사용하며 `block_id`, `order`, `section`, 원본 XML 위치, 부모 블록·셀 참조 등을 가집니다.
- `table.cells`: 행별 셀 배열입니다. 각 셀은 좌표(`row`, `col`), 병합 범위(`row_span`, `col_span`), 텍스트, 배경색, 내부 자식 블록 ID를 담습니다. 중첩 표와 셀 내부 그림은 별도 블록으로 연결되어 중복 추출을 피합니다.
- `assets`: 추출된 이미지의 SHA256, MIME 유형, 크기, 로컬 저장 경로 등 바이너리 메타데이터입니다.
- `pages`: 물리 페이지 번호와 인쇄 쪽번호를 분리해 기록합니다. 표지처럼 번호가 보이지 않는 페이지의 `page_number`는 `null`일 수 있습니다.
- `warnings`: 누락된 이미지, 페이지 매핑 실패 등 주의 사항입니다.
- `tables`, `images`: 호환 목적의 파서 결과 목록입니다. MongoDB 저장 단계에서 이 중복 목록은 메타데이터에 다시 넣지 않습니다.

`physical_page_number`는  페이지 번호가 비어 있어서 카운트 되는 전체 페이지 목록입니다. `page_number`/`page_numbers`는 문서에 표시된 인쇄 쪽번호입니다. 페이지 매핑은 렌더러가 제공하는 문단 단위 정보이므로 개별 표 셀의 정확한 쪽 위치까지 보장하지 않습니다.

### 로컬 산출물

```text
output/
├─ json/<원본 SHA256>.json              # Streamlit 업로드 파싱 결과
├─ uploads/<원본 SHA256>/<파일명>       # Streamlit이 보관하는 원본
├─ images/<원본 SHA256>/<이미지 SHA256>.<확장자>  # HWPX에서 추출한 그림
└─ page_previews/<원본 SHA256>/         # 페이지별 SVG 미리보기 캐시
```

CLI 기본 JSON 경로는 `output/json/parsed.json`입니다.  이미지 출력은 `images/<원본 SHA256>/`에 저장됩니다.

## 2. 저장 구조 — `step2_hwpx_storage.py`

### 저장 흐름

1. 파싱 결과의 정렬된 JSON을 해시해 `revision`을 계산합니다. 같은 결과의 재실행은 같은 revision을 사용합니다.
2. 원본(선택 사항)과 추출 이미지를 SHA256 기준으로 GridFS `hwpx_files`에 저장합니다. 저장 전 파일 내용의 해시를 다시 검사합니다.
3. 블록·자산·청크를 각각 최대 500건 단위의 upsert로 저장하고, revision별 저장 건수를 확인합니다.
4. 모든 저장 및 건수 검증이 성공한 뒤에만 `Request`의 `active_revision`을 새 revision으로 바꿉니다.

따라서 저장 도중 실패해 보조 컬렉션에 일부 레코드가 남더라도 `Request.active_revision`을 통해 공개되지 않습니다. 조회 시에는 반드시 문서의 활성 revision을 함께 조건으로 사용합니다. 과거 revision의 자동 정리는 하지 않습니다.

### MongoDB 컬렉션

| 컬렉션                                      | 저장 내용                                                                         |
| ------------------------------------------- | --------------------------------------------------------------------------------- |
| `Request`                                 | 문서 메타데이터와 활성 revision, 페이지·경고·건수, 전체`blocks` 및 `assets` |
| `hwpx_blocks`                             | 문단·표·그림 블록별 레코드. 문서 순서 조회 인덱스 포함                          |
| `hwpx_assets`                             | 이미지 자산 메타데이터 및 GridFS 참조                                             |
| `hwpx_chunks`                             | 검색용 텍스트 청크와 원본 블록/셀 참조. 임베딩은 생성하지 않음                    |
| `RequestRequirements`                     | 원본 문서 ID·파싱 버전과 연결된 요구사항 추출 결과, 출처 블록, 모델·토큰 사용량 |
| `RequestCriteria`                          | 요구사항·참고 서식 버전에 연결된 정성적 평가기준표, 항목·배점, 모델·토큰 사용량 |
| `hwpx_files.files`, `hwpx_files.chunks` | GridFS 바이너리. 이미지와 선택적으로 저장한 원본 파일                             |

블록·자산·청크 레코드의 `_id`는 `{document_id}:{revision}:{block_id|asset_id|chunk_id}` 형태입니다. `Request._id`는 원본 `document_id`입니다. `Request`에는 전체 문서를 바로 열 수 있도록 블록과 자산이 함께 있고, 보조 컬렉션에는 개별 조회·검색을 위한 정규화된 사본이 있습니다.

### 검색용 청크

`make_chunks()`는 문단과 표의 각 행을 검색 단위로 만듭니다. 셀 내부 문단은 표 행에 포함되므로 별도 중복 청크를 만들지 않습니다. 긴 텍스트는 줄바꿈이나 공백 경계를 우선해 최대 2,000자 조각으로 나누며, `source_refs`에 블록 ID·셀 ID·문자 범위를 기록합니다. 임베딩, OCR, 외부 AI API 호출은 수행하지 않습니다.

단일 MongoDB BSON 레코드가 15 MiB 이상이면 DB 연결·쓰기 전에 중단합니다. 원본 파일은 기본적으로 GridFS에 저장하지 않으며, 파서 CLI의 `--store-source` 선택 시에만 함께 저장합니다. 이미지 바이너리는 추출 자산이 있으면 저장합니다.

## 3. Streamlit 앱 구조 — `streamlit_main.py`

- **등록:** HWPX 업로드 → 확장자·크기·파일 형식 검사 → 원본 해시별 `output/uploads/` 보관 → 파서 자식 프로세스 실행 → JSON을 `output/json/`에 저장 → MongoDB 저장.
- **저장 재시도:** 파싱이 끝난 뒤 DB 저장에 실패하면 파싱 결과를 세션 상태에 보관하고, 재시도 또는 JSON 다운로드를 제공합니다.
- **이력:** `Request`에서 저장 문서를 최근 순으로 조회하고 문서를 열거나 삭제할 수 있습니다.
- **상세 보기:** 페이지별 문서 보기, 표 보기, 이미지 보기, JSON 다운로드를 제공합니다. 본문 문자열은 HTML escape 처리 후 렌더링합니다.
- **문서별 탭:** `제안요청서`에서 전체 문서를, `제안 요구사항`에서 총괄표·목록표와 세부내역을, `정성적 평가기준`에서 생성된 평가기준표를 확인합니다. 각 생성 버튼을 눌렀을 때만 OpenAI API를 호출하며 저장 결과를 다시 열 때는 호출하지 않습니다.
- **원본 미리보기:** 원본 파일이 로컬 업로드 폴더나 GridFS에 있으면 페이지 미리보기를 생성·재사용합니다. 원본이 없으면 저장된 블록으로 대체 표시하되 페이지별 정확한 배치는 보장할 수 없습니다.

## 4. `streamlit_page_preview.py`의 역할

Streamlit 본체에서 한글 렌더러를 직접 계속 보유하지 않도록 별도 Python 프로세스에서 실행되는 작업자입니다. 파서의 페이지 매핑·인쇄 쪽번호 보정 로직을 이용해 원본을 열고, 각 물리 페이지를 SVG로 렌더링해 다음 구조의 JSON을 반환합니다.

- `source_sha256`: 입력 원본 검증용 해시
- `pages`: 물리 페이지와 인쇄 쪽번호 메타데이터
- `svgs`: 물리 페이지 번호를 키로 하는 SVG 문자열

렌더링된 SVG에 인쇄 쪽번호가 이미 포함된 경우 그 텍스트를 제거합니다. Streamlit이 페이지 바깥쪽에 별도 쪽번호 표시를 하기 때문입니다. 캐시 키에는 원본 해시와 작업자·파서·쪽번호 보정 파일 등의 해시가 반영되어 코드나 설정이 바뀌면 새 미리보기를 생성합니다.

## 실행 및 설정

현재 저장소의 의존성 파일을 설치한 뒤 앱을 실행합니다.

```sh
python -m pip install -r requirements.txt
streamlit run streamlit_main.py
```

### MongoDB 연결 설정

저장과 Streamlit 조회는 `step2_hwpx_storage.py`의 `mongo_settings()`를 통해 MongoDB URI를 가져옵니다. 우선순위는 `MONGODB_URI` → `MONGO_URI` → `client_db`이며, 같은 이름의 값이 있으면 프로세스 환경변수가 `.env` 파일 값보다 우선합니다.

- **로컬 실행:** 프로젝트 루트의 `.env` 파일에 `MONGODB_URI`를 설정할 수 있습니다. `python-dotenv`가 읽으므로 값의 따옴표를 사용할 수 있습니다.
- **웹 배포:**  인증정보를 포함한 전체 접속 URI를 `MONGODB_URI` 하나에 지정합니다.

URI 형식은 다음과 같습니다.

```text
mongodb://<사용자명>:<URL-인코딩된-비밀번호>@<호스트>:<포트>/<데이터베이스>?authSource=<인증-DB>
```

## 5. 요구사항 원문 추출 — `step3_ai_requirements.py`

프로젝트 `.env` 또는 환경변수에 `OPENAI_API_KEY`를 설정합니다. 환경변수가 우선하며, 키는 화면·로그·추출 결과에 저장하지 않습니다. 기본 모델은 `gpt-4o`입니다. (`gpt-4o-mini로는 충분한 결과도출 불가`)

1. DB `Request`의 현재 `active_revision`에 해당하는 파싱 JSON을 읽습니다.
2. 문서 전체의 문단·표를 비용을 줄인 JSON으로 나누어 보냅니다. 중복 셀 문단·스타일·이미지 바이너리는 제외하며, 긴 블록도 생략 없이 분할합니다. 배치 경계의 문맥은 겹쳐 제공합니다.
3. LLM은 [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)로 총괄표·목록표와 세부내역에 해당하는 **기존 블록 ID만** 반환합니다. 항목명·필드명을 고정하지 않습니다.
4. 프로그램이 해당 ID의 원본 블록과 하위 문단·표·이미지를 복사합니다. 셀 텍스트, 문단, 줄바꿈, 병합, 순서, 출처 페이지를 그대로 보존합니다. 목록표와 상세표의 표현이 달라도 맞추거나 요약하지 않습니다.
5. 출처·원문 동일성을 검증하고 `RequestRequirements`에 저장합니다. 원래 `Request`나 파싱 블록은 수정하지 않습니다. 결과는 문서 ID, 원본 revision, 추출기 버전, 모델별로 재사용하며 재파싱 후에는 이전 결과를 표시하지 않습니다. 문서를 삭제하면 연결된 요구사항·평가기준 결과도 삭제됩니다.

화면 사용: 저장 이력에서 문서 열기 → **제안 요구사항** → **제안 요구사항 추출**. 이후 `총괄표`와 `세부내역` 하위 탭에서 확인하거나 별도의 JSON으로 다운로드합니다. **다시 추출** 버튼은 API를 새로 호출합니다.

CLI에서도 실행할 수 있습니다. 파일명은 일부 일치와 한글 NFC/NFD를 지원하며, 여러 문서가 검색되면 `--document-id`를 사용합니다.

```sh
# 추출·검증 후 원본 문서에 연결해 저장
.venv/bin/python step3_ai_requirements.py --filename '특허 빅데이터' --save --output output/requirements.json

# 특정 문서 재추출 (--force 사용 시 새 API 비용 발생)
.venv/bin/python step3_ai_requirements.py --document-id '<문서 ID>' --save --force
```

`--save`를 생략하면 DB에 쓰지 않습니다. 저장된 결과가 없으면 API 추출은 수행합니다. JSON에는 `summary`, `details` 각각의 `block_ids`와 원문 `blocks`, `document_id`, `source_revision`, 원문 해시, 추출 시각, `usage`, `warnings`가 들어 있습니다.

원문 보존은 **기존 파싱 JSON 기준**입니다. 기존 파서의 오탈자·수식 문자열도 유지하며 이미지 속 문자는 새로 OCR하지 않습니다. 총괄표/세부내역이 없거나 목록의 요구사항 번호가 세부내역에서 확인되지 않으면 `needs_review`로 표시합니다. 출처가 맞지 않는 AI 응답이나 실패한 배치가 있으면 부분 결과를 저장하지 않습니다. 원문 동일성 검증은 자동으로 수행하지만, 새로운 문서 양식에서 모든 요구사항을 선택했는지는 사용자 확인이 필요할 수 있습니다.

## 6. 정성적 평가기준표 생성 — `step4_ai_criteria.py`

제안요청서 상세 보기의 **정성적 평가기준** 탭에서 생성합니다. 먼저 **제안 요구사항** 탭에서 요구사항을 추출·저장해야 합니다. GPT-4o가 총괄표와 세부내역을 기준표 형식의 JSON으로 작성하며, 화면에서는 제안 요구사항과 같은 문서 스타일로 렌더링합니다.

1. 현재 제안요청서에서 정성적 평가기준표를 찾으면 그 표의 평가부문·평가항목·배점을 우선 사용합니다. 없으면 [doc/ai_criteria.md](doc/ai_criteria.md)의 평가기준·평가요소 예시를 기본 서식으로 삼습니다.
2. JSON에는 `evaluation_division`, `evaluation_item`, `evaluation_criteria`, `evaluation_elements`, `score`를 포함합니다. 기존 요청서의 고정 항목·배점은 바꾸지 않고, 평가기준과 요소는 해당 제안요구사항에 맞춰 작성합니다.
3. 누락·중복 항목, 빈 평가기준, 총 배점을 검증합니다. 결과는 `RequestCriteria`에 원본 문서 revision, 요구사항 결과 ID, 사용 기준표 버전, 모델 버전을 연결해 저장합니다.
4. 요구사항 또는 기준표 버전이 바뀌면 이전 결과를 재사용하지 않습니다. JSON을 다운로드할 수 있고 화면에서는 문서형 표로 확인할 수 있습니다.

CLI에서는 다음과 같이 실행할 수 있습니다.

```sh
.venv/bin/python step4_ai_criteria.py --document-id '<문서 ID>' --save --output output/criteria.json
```

`--save`를 생략하면 결과를 저장하지 않습니다. `--force`는 저장 결과가 있어도 GPT-4o를 다시 호출합니다.

관련 회귀 검증:

```sh
.venv/bin/python -m unittest test_ai_requirements test_ai_criteria test_requirements_ui tests.test_document_view -v
```
