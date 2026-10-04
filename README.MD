# HWPX/HWP 문서 파싱 및 저장

이 프로젝트는 한글 문서를 파싱해 문단·표·이미지·페이지 정보를 구조화된 JSON으로 만들고, 필요하면 MongoDB에 저장하는 문서 등록·조회 앱입니다. 핵심은 **원본 문서 구조를 보존하는 파싱**과 **재실행·실패에 안전한 저장**입니다.

## 파일별 역할

| 파일 | 역할 |
| --- | --- |
| [step1_parser_hwpx.py](step1_parser_hwpx.py) | HWPX/HWP 파싱, 블록·표·이미지·페이지 메타데이터 생성, JSON 출력 및 저장 모듈 호출 |
| [step2_hwpx_storage.py](step2_hwpx_storage.py) | JSON에서 저장 레코드와 검색용 청크 생성, MongoDB/GridFS 저장 |
| [streamlit_main.py](streamlit_main.py) | 문서 업로드·등록, 저장 이력 및 상세 보기 UI |
| [streamlit_page_preview.py](streamlit_page_preview.py) | 원본 한글 파일을 페이지별 SVG로 렌더링하는 별도 프로세스 작업자 |
| [requirements.txt](requirements.txt) | 앱·파서 실행에 필요한 Python 의존성 |

## 1. 파싱 구조 — `step1_parser_hwpx.py`

### 처리 흐름

```text
HWPX 원본
  ├─ ZIP 내부 Contents/header.xml, Contents/section*.xml, content.hpf 분석
  ├─ 문단 텍스트·스타일, 표·셀·병합·배경, 그림 참조 추출
  ├─ BinData 이미지 바이트를 SHA256 파일명으로 저장
  ├─ 렌더 트리로 문단과 물리 페이지를 연결하고 인쇄 쪽번호 추출
  └─ 구조화 JSON 생성

HWP 원본
  └─ pyhwpxlib 렌더 트리에서 페이지별 문단·표·그림 정보 추출
```

HWPX는 ZIP 안의 XML을 직접 읽기 때문에 표 셀 좌표와 병합 정보, 셀 안의 문단·중첩 표·그림 관계를 보존합니다. 렌더 트리의 문단 위치를 XML 문단과 연결해 페이지 정보를 추가합니다. HWP는 pyhwpxlib 렌더 트리 기반이며 HWPX와 같은 수준의 병합·음영·이미지 원본 추출을 제공하지 않습니다.

### JSON의 주요 구조

- `document_id`, `source_sha256`: 원본 파일의 SHA256. 같은 원본을 식별하는 키입니다.
- `blocks`: 문서 순서대로 저장되는 기본 단위입니다. `paragraph`, `table`, `image` 타입을 사용하며 `block_id`, `order`, `section`, 원본 XML 위치, 부모 블록·셀 참조 등을 가집니다.
- `table.cells`: 행별 셀 배열입니다. 각 셀은 좌표(`row`, `col`), 병합 범위(`row_span`, `col_span`), 텍스트, 배경색, 내부 자식 블록 ID를 담습니다. 중첩 표와 셀 내부 그림은 별도 블록으로 연결되어 중복 추출을 피합니다.
- `assets`: 추출된 이미지의 SHA256, MIME 유형, 크기, 로컬 저장 경로 등 바이너리 메타데이터입니다.
- `pages`: 물리 페이지 번호와 인쇄 쪽번호를 분리해 기록합니다. 표지처럼 번호가 보이지 않는 페이지의 `page_number`는 `null`일 수 있습니다.
- `warnings`: 누락된 이미지, 페이지 매핑 실패 등 주의 사항입니다.
- `tables`, `images`: 호환 목적의 파서 결과 목록입니다. MongoDB 저장 단계에서 이 중복 목록은 메타데이터에 다시 넣지 않습니다.

블록의 `physical_page_number`는 시작 페이지이며 `physical_page_numbers`는 여러 쪽에 걸치는 블록의 전체 페이지 목록입니다. `page_number`/`page_numbers`는 문서에 표시된 인쇄 쪽번호입니다. 페이지 매핑은 렌더러가 제공하는 문단 단위 정보이므로 개별 표 셀의 정확한 쪽 위치까지 보장하지 않습니다.

### 로컬 산출물

```text
output/
├─ json/<원본 SHA256>.json              # Streamlit 업로드 파싱 결과
├─ uploads/<원본 SHA256>/<파일명>       # Streamlit이 보관하는 원본
├─ images/<원본 SHA256>/<이미지 SHA256>.<확장자>  # HWPX에서 추출한 그림
└─ page_previews/<원본 SHA256>/         # 페이지별 SVG 미리보기 캐시
```

CLI 기본 JSON 경로는 `output/json/parsed.json`이며 `--output-json`으로 바꿀 수 있습니다. 이미지 출력은 `--asset-dir` 아래 `images/<원본 SHA256>/`에 저장됩니다.

## 2. 저장 구조 — `step2_hwpx_storage.py`

### 저장 흐름

1. 파싱 결과의 정렬된 JSON을 해시해 `revision`을 계산합니다. 같은 결과의 재실행은 같은 revision을 사용합니다.
2. 원본(선택 사항)과 추출 이미지를 SHA256 기준으로 GridFS `hwpx_files`에 저장합니다. 저장 전 파일 내용의 해시를 다시 검사합니다.
3. 블록·자산·청크를 각각 최대 500건 단위의 upsert로 저장하고, revision별 저장 건수를 확인합니다.
4. 모든 저장 및 건수 검증이 성공한 뒤에만 `Request`의 `active_revision`을 새 revision으로 바꿉니다.

따라서 저장 도중 실패해 보조 컬렉션에 일부 레코드가 남더라도 `Request.active_revision`을 통해 공개되지 않습니다. 조회 시에는 반드시 문서의 활성 revision을 함께 조건으로 사용합니다. 과거 revision의 자동 정리는 하지 않습니다.

### MongoDB 컬렉션

| 컬렉션 | 저장 내용 |
| --- | --- |
| `Request` | 문서 메타데이터와 활성 revision, 페이지·경고·건수, 전체 `blocks` 및 `assets` |
| `hwpx_blocks` | 문단·표·그림 블록별 레코드. 문서 순서 조회 인덱스 포함 |
| `hwpx_assets` | 이미지 자산 메타데이터 및 GridFS 참조 |
| `hwpx_chunks` | 검색용 텍스트 청크와 원본 블록/셀 참조. 임베딩은 생성하지 않음 |
| `hwpx_files.files`, `hwpx_files.chunks` | GridFS 바이너리. 이미지와 선택적으로 저장한 원본 파일 |

블록·자산·청크 레코드의 `_id`는 `{document_id}:{revision}:{block_id|asset_id|chunk_id}` 형태입니다. `Request._id`는 원본 `document_id`입니다. `Request`에는 전체 문서를 바로 열 수 있도록 블록과 자산이 함께 있고, 보조 컬렉션에는 개별 조회·검색을 위한 정규화된 사본이 있습니다.

### 검색용 청크

`make_chunks()`는 문단과 표의 각 행을 검색 단위로 만듭니다. 셀 내부 문단은 표 행에 포함되므로 별도 중복 청크를 만들지 않습니다. 긴 텍스트는 줄바꿈이나 공백 경계를 우선해 최대 2,000자 조각으로 나누며, `source_refs`에 블록 ID·셀 ID·문자 범위를 기록합니다. 임베딩, OCR, 외부 AI API 호출은 수행하지 않습니다.

단일 MongoDB BSON 레코드가 15 MiB 이상이면 DB 연결·쓰기 전에 중단합니다. 원본 파일은 기본적으로 GridFS에 저장하지 않으며, 파서 CLI의 `--store-source` 선택 시에만 함께 저장합니다. 이미지 바이너리는 추출 자산이 있으면 저장합니다.

## 3. Streamlit 앱 구조 — `streamlit_main.py`

- **등록:** HWPX 업로드 → 확장자·크기·파일 형식 검사 → 원본 해시별 `output/uploads/` 보관 → 파서 자식 프로세스 실행 → JSON을 `output/json/`에 저장 → MongoDB 저장.
- **저장 재시도:** 파싱이 끝난 뒤 DB 저장에 실패하면 파싱 결과를 세션 상태에 보관하고, 재시도 또는 JSON 다운로드를 제공합니다.
- **이력:** `Request`에서 저장 문서를 최근 순으로 조회하고 문서를 열거나 삭제할 수 있습니다.
- **상세 보기:** 페이지별 문서 보기, 표 보기, 이미지 보기, JSON 다운로드를 제공합니다. 본문 문자열은 HTML escape 처리 후 렌더링합니다.
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

파서 CLI는 다음과 같이 실행합니다.

```sh
python step1_parser_hwpx.py input/example.hwpx --no-mongo
```

`--no-mongo`를 생략하면 JSON 생성 후 MongoDB에도 저장합니다. 주요 옵션은 `--output-json`, `--asset-dir`, `--database`, `--collection`, `--env-file`, `--no-pagination`, `--strict-pagination` 및 `--no-strict-pagination`입니다. 파싱 전용 사용은 `--no-mongo`를 지정합니다.

### MongoDB 연결 설정

저장과 Streamlit 조회는 `step2_hwpx_storage.py`의 `mongo_settings()`를 통해 MongoDB URI를 가져옵니다. 우선순위는 `MONGODB_URI` → `MONGO_URI` → `client_db`이며, 같은 이름의 값이 있으면 프로세스 환경변수가 `.env` 파일 값보다 우선합니다.

- **로컬 실행:** 프로젝트 루트의 `.env` 파일에 `MONGODB_URI`를 설정할 수 있습니다. `python-dotenv`가 읽으므로 값의 따옴표를 사용할 수 있습니다.
- **웹 배포:** 호스팅 서비스의 앱 환경변수/Secrets에 `MONGODB_URI`를 등록합니다. 저장소에 `download.env` 같은 파일을 올리는 것만으로는 해당 값이 프로세스 환경변수로 자동 등록되지 않습니다. 배포 서비스에서 환경변수를 주입하거나 파일명을 `.env`로 만들어 앱 루트에 배치해야 합니다.
- `rootusername`, `rootpassword` 같은 별도 변수는 현재 코드가 URI에 자동으로 결합하지 않습니다. 인증정보를 포함한 전체 접속 URI를 `MONGODB_URI` 하나에 지정합니다.

URI 형식은 다음과 같습니다. 실제 사용자명·비밀번호·주소를 README나 Git 저장소에 기록하지 마세요.

```text
mongodb://<사용자명>:<URL-인코딩된-비밀번호>@<호스트>:<포트>/<데이터베이스>?authSource=<인증-DB>
```

클라우드 플랫폼의 MongoDB를 사용할 때는 앱에서 접근 가능한 주소를 지정해야 합니다. 플랫폼 내부 네트워크의 `mongo:27017` 같은 주소는 같은 내부 네트워크에 연결된 서비스에서만 작동할 수 있습니다. 외부에서 접속할 경우 플랫폼이 제공하는 외부 호스트와 포트를 사용하세요. 비밀번호에 `@`, `:`, `/`, `#`, `%` 등이 포함되면 URI 사용자정보 부분에서 URL 인코딩해야 합니다.

Streamlit은 `DATABASE = "Request_for_Proposal"`을 명시적으로 전달하므로, Streamlit 연결의 DB 이름은 `MONGODB_DATABASE`가 아니라 이 상수로 정해집니다. CLI에서는 `--database`가 우선이며, 지정하지 않으면 `MONGODB_DATABASE` 또는 URI의 DB 이름, 마지막으로 `Request_for_Proposal`을 사용합니다. 환경변수를 변경한 뒤에는 웹서비스를 재시작/재배포해야 적용됩니다.

### 접속이 되지 않을 때

1. 배포 서비스의 실행 로그에서 `ValueError`인지 `MongoStorageError`인지 확인합니다. 설정값 누락·잘못된 URI 형식은 MongoDB 연결 전에 실패하며, 네트워크·인증·권한 문제는 연결/저장 단계에서 발생합니다.
2. `MONGODB_URI`가 `mongodb://` 또는 `mongodb+srv://`로 시작하는지, 호스트·포트·사용자 인증정보가 맞는지 확인합니다. `mongo:27017`은 배포 네트워크 외부에서 접근하는 주소가 아닐 수 있습니다.
3. MongoDB 서비스의 가동 상태, 앱에서 DB로 나가는 네트워크 허용 여부, 대상 DB에서 컬렉션/인덱스를 만들고 읽고 쓸 권한이 있는지 확인합니다.
4. HTTP 503이 웹앱 주소 접속 시 나타나면 앱 호스팅/게이트웨이 문제일 수 있고, 저장 요청에서만 보이면 앱 로그의 MongoDB 저장 단계와 DB 서비스 로그를 함께 확인합니다. 상태 코드만으로 어느 서비스가 503을 반환했는지 단정할 수 없습니다.

접속 URI, 비밀번호, API 키는 소스 코드·README·공개 저장소에 기록하지 말고 배포 플랫폼의 비밀 환경변수로 관리하세요. 자격증명이 이미 Git이나 공개 채팅에 노출됐다면 즉시 교체하세요.

## 설정 주의

앱의 파일 배치는 이 README의 파일 목록을 기준으로 합니다. 로컬에서는 루트 `.env`, 배포에서는 플랫폼 환경변수 `MONGODB_URI`를 사용하고, 실제 값은 저장소에 커밋하지 마세요.
